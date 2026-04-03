"""Groq LLM analysis — picks the best coin from scanner results."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config
from src.utils.budget_tracker import log_llm_call, check_budget, BudgetExceededError


def analyze_with_groq(
    top10: list[dict],
    fear_greed: dict,
    news_text: str = "",
    pump_alerts: list[dict] | None = None,
    enrichment_text: str = "",
) -> dict | None:
    """
    Send top 10 scanner results + F&G + optional news to Groq.
    Prints the recommendation and returns the rec dict (or None on failure).
    """
    if not config.GROQ_API_KEY:
        print("  ERROR: GROQ_API_KEY not set in .env")
        return None

    # ── Budget check (hard stop if daily limit reached) ───────────────────
    try:
        check_budget("groq")
    except BudgetExceededError as e:
        print(f"  BUDGET EXCEEDED: {e}")
        return None

    try:
        from groq import Groq
    except ImportError:
        print("  ERROR: groq package not installed. Run: pip install groq")
        return None

    client = Groq(api_key=config.GROQ_API_KEY)

    fg_value = fear_greed.get("value", "?")
    fg_label = fear_greed.get("label", "Unknown")

    # Build coin list
    lines = []
    for i, r in enumerate(top10, 1):
        rsi_str = f"{r['rsi']:.1f}" if r.get("rsi") else "N/A"
        lines.append(
            f"{i}. {r['symbol']} ({r['name']}) | score={r['score']} | "
            f"price=${r['price']:,.4f} | 24h={r['change_24h']:+.1f}% | "
            f"7d={r['change_7d']:+.1f}% | RSI={rsi_str} | "
            f"MACD={r['macd']} | BB={r['bb_pos']} | trend={r['trend']}\n"
            f"   signals: {', '.join(r['reasons']) if r['reasons'] else 'none'}"
        )
    coins_text = "\n".join(lines)

    news_section       = f"\nRECENT NEWS:\n{news_text}\n" if news_text else ""
    enrichment_section = f"\nMARKET INTELLIGENCE:\n{enrichment_text}\n" if enrichment_text else ""

    pump_section = ""
    if pump_alerts:
        pump_lines = []
        for c in pump_alerts:
            sym  = c.get("symbol", "").upper()
            ch7d = c.get("price_change_percentage_7d_in_currency") or 0
            ch24 = c.get("price_change_percentage_24h") or 0
            vol  = c.get("total_volume") or 0
            mcap = c.get("market_cap") or 1
            pump_lines.append(
                f"  {sym} | 7d={ch7d:+.1f}% | 24h={ch24:+.1f}%"
                f" | vol/mcap={vol/mcap:.2f}x | price=${c.get('current_price', 0):,.6f}"
            )
        pump_section = (
            "\n\nPUMP ALERTS (>100% gain in 7d — could be real breakout OR pump & dump):\n"
            + "\n".join(pump_lines)
            + "\nFor each pump alert, briefly state if it looks like a genuine breakout or a P&D, and why."
        )

    prompt = f"""You are a crypto analyst. Here are the top 10 oversold/bullish coins right now:

{coins_text}

Fear & Greed Index: {fg_value}/100 ({fg_label}).{news_section}{enrichment_section}{pump_section}

Pick the ONE best coin to buy today (from the top 10 OR the pump alerts if a pump alert looks like a genuine breakout). If any coin dropped more than 50% in 7 days, assess whether it is a rug pull or a legitimate dip — explain why before deciding. Give: coin name, entry price, stop-loss, take-profit, timeframe, and reasoning. Be specific with numbers.

Return this JSON object:
{{
  "coin": "SYMBOL",
  "entry_price": <number>,
  "stop_loss": <number>,
  "take_profit": <number>,
  "timeframe": "e.g. 3-7 days",
  "reasoning": "specific reasoning with numbers",
  "pump_assessment": "brief assessment of pump alerts, or null if none"
}}"""

    print("\n  Sending to Groq (llama-3.3-70b)...")

    try:
        response = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a crypto analyst. You always respond with valid JSON only — "
                        "no markdown, no explanation outside the JSON object.\n\n"
                        "When evaluating coins, watch for rug pull indicators:\n"
                        "- 7d drop >80% (e.g. SIREN dropped 94% in 11 days)\n"
                        "- 24h volume > 50% of market cap (panic selling / exit liquidity)\n"
                        "- Memecoin on BNB/SOL chain with no real utility\n"
                        "- Recent ATH followed by an immediate crash\n"
                        "- RSI oversold does NOT mean buy if the project is dead\n\n"
                        "If you detect these patterns, label the coin LIKELY RUG PULL and do NOT "
                        "recommend it. An oversold rug pull is not a buying opportunity — it is a "
                        "falling knife. Never pick a coin purely because its RSI is low."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=config.LLM_MAX_TOKENS,
            temperature=config.LLM_TEMPERATURE,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        print(f"  ERROR calling Groq API: {e}")
        return None

    # ── Log the call ──────────────────────────────────────────────────────
    try:
        usage = response.usage
        log_llm_call(
            model="groq",
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            endpoint="scan_analysis",
        )
    except Exception:
        pass

    content = response.choices[0].message.content.strip()

    try:
        start = content.find("{")
        end = content.rfind("}") + 1
        rec = json.loads(content[start:end]) if start >= 0 else {}
    except Exception:
        print(f"\n  Groq raw response:\n{content}")
        return None

    def _fmt(val):
        return f"${val:,.4f}" if isinstance(val, (int, float)) else str(val)

    print("\n" + "=" * 60)
    print("  GROQ LLM RECOMMENDATION")
    print("=" * 60)
    print(f"\n  BEST BUY:     {rec.get('coin', '?')}")
    print(f"  Entry Price:  {_fmt(rec.get('entry_price'))}")
    print(f"  Stop Loss:    {_fmt(rec.get('stop_loss'))}")
    print(f"  Take Profit:  {_fmt(rec.get('take_profit'))}")
    print(f"  Timeframe:    {rec.get('timeframe', '?')}")
    print(f"\n  Reasoning:\n  {rec.get('reasoning', '?')}")
    if rec.get("pump_assessment"):
        print(f"\n  Pump Assessment:\n  {rec['pump_assessment']}")
    print("=" * 60)

    return rec
