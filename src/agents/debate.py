"""Multi-agent Bull/Bear Debate + Risk Manager pipeline.

Implements Phase 2-3 of the project plan:
  1. Bull Agent  — argues FOR buying the coin
  2. Bear Agent  — argues AGAINST buying the coin
  3. Risk Manager — synthesizes both sides, produces final recommendation

All agents use Groq LLM (free tier). The pipeline runs after the scanner
picks a candidate and before the final recommendation is logged.

Usage:
    from src.agents.debate import run_debate

    result = run_debate(coin_data, ta_data, sentiment_data, enrichment_text)
    # result = {verdict, confidence, entry, stop_loss, take_profit, reasoning, ...}
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config
from src.utils.budget_tracker import log_llm_call, check_budget, BudgetExceededError


def _call_groq(system_prompt: str, user_prompt: str, label: str) -> str | None:
    """Make a single Groq API call with budget enforcement."""
    try:
        check_budget("groq")
    except BudgetExceededError as e:
        print(f"  [{label}] BUDGET EXCEEDED: {e}")
        return None

    try:
        from groq import Groq
    except ImportError:
        print(f"  [{label}] groq package not installed")
        return None

    client = Groq(api_key=config.GROQ_API_KEY)

    try:
        response = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=1200,
            temperature=0.3,
        )
    except Exception as e:
        print(f"  [{label}] Groq API error: {e}")
        return None

    try:
        usage = response.usage
        log_llm_call(
            model="groq",
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            endpoint=f"debate_{label.lower()}",
        )
    except Exception:
        pass

    return response.choices[0].message.content.strip()


def _build_coin_context(
    coin_data: dict,
    ta_text: str = "",
    sentiment_text: str = "",
    enrichment_text: str = "",
) -> str:
    """Build a shared context block that all agents receive."""
    symbol = coin_data.get("symbol", "?")
    price = coin_data.get("price", 0)
    change_24h = coin_data.get("change_24h", 0)
    change_7d = coin_data.get("change_7d", 0)
    rsi = coin_data.get("rsi")
    macd = coin_data.get("macd", "N/A")
    bb_pos = coin_data.get("bb_pos", "N/A")
    trend = coin_data.get("trend", "N/A")
    mcap = coin_data.get("market_cap", 0)

    rsi_str = f"{rsi:.1f}" if rsi else "N/A"
    mcap_str = f"${mcap/1e9:.2f}B" if mcap > 1e9 else f"${mcap/1e6:.0f}M"

    ctx = f"""COIN: {symbol}
Price: ${price:,.4f}
24h Change: {change_24h:+.1f}%
7d Change: {change_7d:+.1f}%
Market Cap: {mcap_str}
RSI(14): {rsi_str}
MACD Signal: {macd}
Bollinger Position: {bb_pos}
Trend: {trend}
Score: {coin_data.get('score', 0)} pts
Signals: {', '.join(coin_data.get('reasons', []))}"""

    if ta_text:
        ctx += f"\n\nTECHNICAL ANALYSIS:\n{ta_text}"
    if sentiment_text:
        ctx += f"\n\nSENTIMENT:\n{sentiment_text}"
    if enrichment_text:
        ctx += f"\n\nMARKET INTELLIGENCE:\n{enrichment_text}"

    return ctx


def run_bull_agent(coin_context: str) -> str | None:
    """Bull agent — argues for buying the coin."""
    system = (
        "You are a bullish crypto analyst. Your job is to make the strongest "
        "possible case FOR buying this coin right now. Focus on:\n"
        "1. Technical signals that suggest a bounce or breakout\n"
        "2. Positive sentiment or narrative catalysts\n"
        "3. Risk/reward ratio — how much upside vs downside\n"
        "4. Historical patterns (has this coin bounced from similar levels?)\n"
        "5. Macro factors that support crypto in general\n\n"
        "Be specific with numbers. Give entry price, target, and timeframe.\n"
        "Keep your argument to 150-200 words maximum."
    )
    user = f"Make the bull case for buying this coin:\n\n{coin_context}"

    print("  [BULL] Generating bullish argument...")
    return _call_groq(system, user, "BULL")


def run_bear_agent(coin_context: str) -> str | None:
    """Bear agent — argues against buying the coin."""
    system = (
        "You are a bearish crypto analyst and risk assessor. Your job is to make "
        "the strongest possible case AGAINST buying this coin right now. Focus on:\n"
        "1. Technical signals that suggest more downside\n"
        "2. Negative sentiment, regulatory risks, or red flags\n"
        "3. Why this might be a value trap or dead cat bounce\n"
        "4. Rug pull indicators (if any)\n"
        "5. Macro risks (Fed hawkish, BTC dominance rising, etc.)\n\n"
        "Be specific with numbers. Identify the key risk levels.\n"
        "Keep your argument to 150-200 words maximum."
    )
    user = f"Make the bear case against buying this coin:\n\n{coin_context}"

    print("  [BEAR] Generating bearish argument...")
    return _call_groq(system, user, "BEAR")


def run_risk_manager(
    coin_context: str,
    bull_case: str,
    bear_case: str,
) -> dict | None:
    """Risk manager — synthesizes both sides, produces final verdict."""
    system = (
        "You are a senior risk manager at a crypto fund. You've received arguments "
        "from a Bull analyst and a Bear analyst about the same coin.\n\n"
        "Your job:\n"
        "1. Weigh both arguments objectively\n"
        "2. Identify which side has stronger evidence\n"
        "3. Produce a FINAL VERDICT: BUY, SKIP, or WAIT\n"
        "4. If BUY: set specific entry, stop-loss, take-profit, position size (% of portfolio)\n"
        "5. If SKIP: explain why and suggest what to watch for\n"
        "6. If WAIT: explain what trigger would change the verdict\n\n"
        "You MUST respond with valid JSON only — no markdown, no explanation outside JSON.\n"
        "Use this exact structure:\n"
        '{"verdict": "BUY|SKIP|WAIT", "confidence": 0.0-1.0, '
        '"entry_price": <number|null>, "stop_loss": <number|null>, '
        '"take_profit": <number|null>, "position_pct": <number|null>, '
        '"timeframe": "<string>", "reasoning": "<200 word max>", '
        '"bull_strength": 0.0-1.0, "bear_strength": 0.0-1.0, '
        '"key_risk": "<one sentence>", "key_catalyst": "<one sentence>"}'
    )
    user = (
        f"COIN DATA:\n{coin_context}\n\n"
        f"BULL CASE:\n{bull_case}\n\n"
        f"BEAR CASE:\n{bear_case}\n\n"
        "Synthesize and produce your final verdict as JSON."
    )

    print("  [RISK MGR] Synthesizing verdict...")
    raw = _call_groq(system, user, "RISK_MGR")
    if not raw:
        return None

    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        return json.loads(raw[start:end]) if start >= 0 else None
    except Exception:
        print(f"  [RISK MGR] Failed to parse JSON:\n{raw[:200]}")
        return None


def run_debate(
    coin_data: dict,
    ta_text: str = "",
    sentiment_text: str = "",
    enrichment_text: str = "",
    fear_greed: dict | None = None,
) -> dict | None:
    """Run the full Bull/Bear/Risk Manager debate pipeline.

    Uses 3 Groq API calls total (~3500 tokens).

    Returns the risk manager's verdict dict or None if pipeline fails.
    """
    if not config.GROQ_API_KEY:
        print("  ERROR: GROQ_API_KEY not set")
        return None

    symbol = coin_data.get("symbol", "?")
    print(f"\n{'='*60}")
    print(f"  MULTI-AGENT DEBATE — {symbol}")
    print(f"{'='*60}")

    # Add Fear & Greed to context if available
    fg_line = ""
    if fear_greed:
        fg_line = f"\nFear & Greed Index: {fear_greed.get('value', '?')}/100 ({fear_greed.get('label', '?')})"

    coin_context = _build_coin_context(coin_data, ta_text, sentiment_text, enrichment_text)
    if fg_line:
        coin_context += fg_line

    # Step 1: Bull and Bear argue (could be parallel, but serial for budget safety)
    bull_case = run_bull_agent(coin_context)
    if not bull_case:
        print("  Bull agent failed — falling back to single-agent mode")
        return None

    bear_case = run_bear_agent(coin_context)
    if not bear_case:
        print("  Bear agent failed — falling back to single-agent mode")
        return None

    # Display arguments
    print(f"\n  {'─'*50}")
    print(f"  BULL CASE ({symbol}):")
    print(f"  {'─'*50}")
    for line in bull_case.splitlines():
        print(f"  {line}")

    print(f"\n  {'─'*50}")
    print(f"  BEAR CASE ({symbol}):")
    print(f"  {'─'*50}")
    for line in bear_case.splitlines():
        print(f"  {line}")

    # Step 2: Risk Manager synthesizes
    verdict = run_risk_manager(coin_context, bull_case, bear_case)
    if not verdict:
        print("  Risk manager failed — falling back to single-agent mode")
        return None

    # Display verdict
    v = verdict.get("verdict", "?")
    conf = verdict.get("confidence", 0)
    emoji = {"BUY": "🟢", "SKIP": "🔴", "WAIT": "🟡"}.get(v, "⚪")

    print(f"\n  {'='*50}")
    print(f"  {emoji}  VERDICT: {v}  (confidence: {conf:.0%})")
    print(f"  {'='*50}")

    if v == "BUY":
        def _fmt(val):
            if not (isinstance(val, (int, float)) and val):
                return "N/A"
            ev = val
            if ev >= 1:   return f"${ev:,.4f}"
            if ev >= 0.01: return f"${ev:.6f}"
            return f"${ev:.8f}"

        print(f"  Entry:        {_fmt(verdict.get('entry_price'))}")
        print(f"  Stop Loss:    {_fmt(verdict.get('stop_loss'))}")
        print(f"  Take Profit:  {_fmt(verdict.get('take_profit'))}")
        print(f"  Position:     {verdict.get('position_pct', '?')}% of portfolio")
        print(f"  Timeframe:    {verdict.get('timeframe', '?')}")

    print(f"\n  Reasoning: {verdict.get('reasoning', '?')}")
    print(f"  Key Risk:     {verdict.get('key_risk', '?')}")
    print(f"  Key Catalyst: {verdict.get('key_catalyst', '?')}")
    print(f"  Bull strength: {verdict.get('bull_strength', 0):.0%}")
    print(f"  Bear strength: {verdict.get('bear_strength', 0):.0%}")
    print(f"  {'='*50}")

    # Add the arguments to the result for logging
    verdict["bull_case"] = bull_case
    verdict["bear_case"] = bear_case
    verdict["coin"] = symbol

    return verdict
