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
    per_coin_news: dict | None = None,
    already_open: set[str] | None = None,
    tavily_catalysts: dict | None = None,
) -> tuple[list[dict] | None, list[dict]]:
    """
    Send top 10 scanner results + F&G + optional news to Groq.
    per_coin_news: pre-fetched {symbol: [{title, age_days, source}]} — skips internal fetch if provided.
    already_open: set of coin symbols currently OPEN — hard-disqualified from new picks.

    Returns (picks_or_none, groq_candidates) where groq_candidates is the list of coins
    that passed the pre-filter (regardless of whether the API call succeeded).
    Callers must log only groq_candidates, not the full top10.
    """
    groq_candidates: list[dict] = []  # pre-filter survivors — set before any early return

    if not config.GROQ_API_KEY:
        print("  ERROR: GROQ_API_KEY not set in .env")
        return None, []

    # ── Budget check (hard stop if daily limit reached) ───────────────────
    try:
        check_budget("groq")
    except BudgetExceededError as e:
        print(f"  BUDGET EXCEEDED: {e}")
        return None, []

    try:
        from groq import Groq
    except ImportError:
        print("  ERROR: groq package not installed. Run: pip install groq")
        return None, []

    client = Groq(api_key=config.GROQ_API_KEY)

    fg_value = fear_greed.get("value", 50)
    if not isinstance(fg_value, (int, float)):
        fg_value = 50
    fg_label = fear_greed.get("label", "Unknown")

    # Use pre-fetched per-coin news if provided by caller; otherwise fetch now.
    if per_coin_news is None:
        per_coin_news = {}
        try:
            import config as _cfg
            from src.connectors.web_research import fetch_news_for_coins
            _src = "Tavily AI" if _cfg.TAVILY_API_KEY else "Google News RSS"
            print(f"  Fetching per-coin news for top 10 ({_src})...")
            per_coin_news = fetch_news_for_coins(top10, limit_per_coin=5)
            found = sum(1 for v in per_coin_news.values() if v)
            print(f"  News found for {found}/{len(top10)} coins")
        except Exception as e:
            print(f"  Per-coin news fetch failed: {e}")

    # ── Pre-filter: hard disqualifiers + score caps ───────────────────────────
    # Step 0A: MACD BEARISH + Trend BEARISH → remove; MACD BEARISH + Trend NEUTRAL → cap score at 2
    # Step 0B: above_upper_BB → remove (already extended)
    # Step 0C: supply_risk != NONE → remove (unlock risk)
    # Step 0F: 24h < +2.0% AND RSI ≥ 42 → remove (no momentum; Archetype A carve-out for RSI < 42)
    # Step 0G: score = 1 → remove (never open a new position on minimum score)
    # Step 0H: coin already OPEN → remove (no duplicate positions)
    # NOTE: vol/mcap is NOT an exclusion gate here — BTC/ETH/SOL have low ratio by design.
    _SCORE_CAP_MACD_NEUTRAL = 2
    _open_set = {s.upper() for s in (already_open or set())}

    hard_removed: list[str] = []
    score_capped: list[str] = []

    for r in top10:
        sym     = r["symbol"].upper()
        macd_v  = (r.get("macd")  or "").upper()
        trend_v = (r.get("trend") or "").upper()
        bb_pos  = r.get("bb_pos", "")
        s_risk  = r.get("supply_risk", "NONE")
        rsi     = r.get("rsi") or 0.0
        vol_mcap= r.get("vol_mcap") or 0.0
        ch24    = r.get("change_24h") or 0.0
        ch7d    = r.get("change_7d")  or 0.0
        score   = r.get("score", 0)

        reason = None

        # 0B/0C: structural flags
        if bb_pos == "ABOVE_UPPER":
            reason = "above upper BB"
        elif s_risk != "NONE":
            reason = f"supply risk={s_risk}"
        # 0A: full bearish alignment — MACD+Trend both bearish + hard crash
        elif (macd_v == "BEARISH" and trend_v == "BEARISH"
              and ch7d < -10 and ch24 < -3):
            reason = f"full bearish alignment (MACD+Trend+7d{ch7d:+.0f}%+24h{ch24:+.0f}%)"
        # 0G: negative/zero score — never open
        elif score <= 0:
            reason = f"score={score} (negative — skip)"
        # 0H: already open — no duplicate positions
        elif sym in _open_set:
            reason = "already OPEN"
        # 0F (revised): three-path qualification gate
        #   Path A — momentum:  24h > +2%,   RSI 50–70, MACD bullish
        #   Path B — reversal:  7d < -15%,   MACD bullish, RSI 35–55
        #   Path C — override:  score ≥ 9,   vol/mcap > 0.5x, MACD bullish
        # Reject only if NONE of the three qualifies.
        else:
            _macd_bull = macd_v == "BULLISH"
            path_a = ch24 > 2.0    and 50 <= rsi <= 70 and _macd_bull
            path_b = ch7d < -15.0  and _macd_bull       and 35 <= rsi <= 55
            path_c = score >= 9    and vol_mcap > 0.5   and _macd_bull
            if not (path_a or path_b or path_c):
                reason = (
                    f"no path qualified — "
                    f"A(24h{ch24:+.1f}%,RSI{rsi:.0f},MACD{macd_v[:4]}) "
                    f"B(7d{ch7d:+.0f}%,RSI{rsi:.0f}) "
                    f"C(sc={score},vol={vol_mcap:.2f}x)"
                )

        if reason:
            hard_removed.append(f"{sym}({reason})")
            continue

        # 0A soft: MACD bearish + neutral/bearish trend → cap score at 2
        # (Coin may still have bullish catalysts but downward technical momentum)
        candidate = dict(r)
        if macd_v == "BEARISH" and trend_v != "BULLISH":
            if candidate["score"] > _SCORE_CAP_MACD_NEUTRAL:
                candidate["score"] = _SCORE_CAP_MACD_NEUTRAL
                candidate["_score_capped"] = True
                score_capped.append(sym)

        groq_candidates.append(candidate)

    if hard_removed:
        print(f"  Pre-filter ❌ removed: {', '.join(hard_removed)}")
    if score_capped:
        print(f"  Pre-filter ⚠️  capped (MACD bearish+neutral, score→{_SCORE_CAP_MACD_NEUTRAL}): {', '.join(score_capped)}")
    if 0 < len(groq_candidates) < 5:
        print(f"  ⚠️ Filter too tight — only {len(groq_candidates)} candidates. Consider relaxing thresholds.")
    if not groq_candidates:
        print("  No clean candidates for Groq after pre-filter — skipping analysis.")
        return None, groq_candidates

    # Build enriched coin list
    lines = []
    for i, r in enumerate(groq_candidates, 1):
        rsi_str    = f"{r['rsi']:.1f}" if r.get("rsi") else "N/A"
        ath_str    = f"ATH_pct={r['ath_pct']:+.0f}%" if r.get("ath_pct") is not None else ""
        spring_str = "  [COILED SPRING]" if r.get("coiled_spring") else ""
        sec_str    = "  [SEC COMMODITY]" if r.get("sec_commodity") else ""
        vol_str    = f"vol/mcap={r['vol_mcap']:.2f}x" if r.get("vol_mcap") is not None else ""
        cap_str    = "  [SCORE CAPPED — MACD bearish, trend neutral]" if r.get("_score_capped") else ""

        coin_header = (
            f"{i}. {r['symbol']} ({r['name']}) | score={r['score']}{' ⚠️' if r.get('_score_capped') else ''} | "
            f"price=${r['price']:,.4f} | 24h={r['change_24h']:+.1f}% | "
            f"7d={r['change_7d']:+.1f}% | RSI={rsi_str} | "
            f"MACD={r['macd']} | BB={r['bb_pos']} | trend={r['trend']}"
        )
        extras = " | ".join(filter(None, [ath_str, vol_str]))
        if extras:
            coin_header += f" | {extras}"
        coin_header += spring_str + sec_str + cap_str

        signals = f"   signals: {', '.join(r['reasons']) if r['reasons'] else 'none'}"

        # Per-coin news headlines — each item is {"title": str, "age_days": int|None, "source": str}
        sym_news = per_coin_news.get(r["symbol"], [])
        if sym_news:
            news_lines_parts = []
            for item in sym_news:
                title  = item.get("title", "") if isinstance(item, dict) else str(item)
                age    = item.get("age_days") if isinstance(item, dict) else None
                source = item.get("source", "") if isinstance(item, dict) else ""
                if age is None:
                    age = 0 if source == "GoogleNews" else (3 if source == "Reddit" else None)
                age_tag = f"({age}d ago)" if age is not None else "(date unknown)"
                news_lines_parts.append(f"     • {age_tag} [{source}] {title[:90]}")
            news_block = "   news:\n" + "\n".join(news_lines_parts)
        else:
            news_block = "   news: none found"

        # Tavily web catalyst — 1-sentence AI summary from live web search
        tavily_line = ""
        if tavily_catalysts:
            cat = tavily_catalysts.get(r["symbol"].upper(), "")
            if cat:
                tavily_line = f"\n   web catalyst (Tavily): {cat}"

        lines.append(f"{coin_header}\n{signals}\n{news_block}{tavily_line}")

    coins_text = "\n\n".join(lines)

    news_section       = f"\nADDITIONAL MARKET NEWS:\n{news_text}\n" if news_text else ""
    enrichment_section = f"\nMARKET INTELLIGENCE:\n{enrichment_text}\n" if enrichment_text else ""

    # Load risk cache to tag manipulated/scam pump alerts
    _risk_cache: dict = {}
    try:
        from src.agents.coin_risk_assessor import _load_cache
        _risk_cache = {k: v for k, v in _load_cache().items()}
    except Exception:
        pass

    pump_section = ""
    if pump_alerts:
        pump_lines = []
        for c in [c for c in pump_alerts if c.get("symbol", "").upper() not in _open_set]:
            sym  = c.get("symbol", "").upper()
            ch7d = c.get("price_change_percentage_7d_in_currency") or 0
            ch24 = c.get("price_change_percentage_24h") or 0
            vol  = c.get("total_volume") or 0
            mcap = c.get("market_cap") or 1
            risk = _risk_cache.get(sym)
            if risk and risk.category in ("ACTIVE_SCAM", "MANIPULATED_REAL"):
                scam_tag = f" ⚠️ {risk.category} — {risk.reasoning[:60]} — NOT a genuine breakout"
            else:
                scam_tag = ""
            pump_lines.append(
                f"  {sym} | 7d={ch7d:+.1f}% | 24h={ch24:+.1f}%"
                f" | vol/mcap={vol/mcap:.2f}x | price=${c.get('current_price', 0):,.6f}{scam_tag}"
            )
        pump_section = (
            "\n\nPUMP ALERTS (>100% gain in 7d — could be real breakout OR pump & dump):\n"
            + "\n".join(pump_lines)
            + "\nFor each pump alert: if marked ACTIVE_SCAM or MANIPULATED_REAL, always assess as "
            "'manipulation/pump-dump cycle — NOT a genuine breakout'. "
            "For others, state if it looks like a genuine breakout or P&D, and why."
        )

    prompt = f"""SHORT-TERM SCAN — top candidates with news (timeframe: 3–14 days max):

IMPORTANT — RANKING ORDER IS MANDATORY:
The coins below are pre-ranked by the scanner. Pick from the list IN ORDER (#1 first, then #2, etc.).
Do NOT skip a higher-ranked coin for a lower-ranked one unless the higher-ranked coin has RSI >75.
All coins with above_upper_BB or supply flags have already been removed — every coin below is clean.
If a coin has RSI >75, skip it and take the next one in order.

{coins_text}

Fear & Greed Index: {fg_value}/100 ({fg_label}).{news_section}{enrichment_section}{pump_section}

SHORT-TERM SCORING — apply these adjustments before deciding:
+3 pts: Concrete catalyst in news (0–7d ago): upgrade, ETF, institutional buy, mainnet, partnership, CME futures
+3 pts: Coiled spring flagged — exhausted sellers, price ready to snap
+2 pts: Whale accumulation flagged — smart money entering
+1 pt:  SEC/CFTC commodity flagged — regulatory safety net
-3 pts: NO catalyst, NO setup quality — falling knife risk
-2 pts: RSI already overbought (>70) — missed the move, late entry
-1 pt:  Volume below $1M/day — not liquid enough for short-term trade

FOR EACH COIN, identify:
1. VOLUME: is 24h volume > $1M? (tradeable) or < $1M? (skip if no catalyst)
2. CATALYST TYPE: regulatory | tech_upgrade | partnership | narrative | institutional | token_economics | none
3. CATALYST TIMING: upcoming | just_happened (0–7d) | old_news (8d+) | none
4. SETUP QUALITY: coiled_spring | accumulation | oversold_only | none
   IMPORTANT: NEVER label setup_quality as "oversold_only" if RSI > 70. RSI > 70 = overbought, not oversold.
   A coin with RSI 80 after a +50% weekly pump is a HIGH-RISK ENTRY, not an oversold bounce.

RANKING PRIORITY RULES — apply in this exact order:

RULE 1 — INSTANT TOP-3 QUALIFIER (auto-promote, check first):
  A coin qualifies instantly if it meets ALL THREE:
    • vol/mcap > 0.50x   (real liquidity, active buying)
    • MACD bullish
    • RSI < 50
  → Assign qualifier="INSTANT_QUALIFIER". RSI < 50 includes oversold AND mid-range momentum.
  → These coins go to rank 1/2/3 before any other coin is considered.

RULE 2 — NEWS CATALYST BOOST (0–2 days old):
  A coin with verified news from 0–2 days ago gets +2 to its effective score for ranking.
  → RSI up to 65 is acceptable when a fresh catalyst is confirmed.
  → Assign qualifier="NEWS_BOOST". key_signal = the headline that triggered the boost.

RULE 3 — OVERSOLD + VOLUME COMBO:
  RSI < 35 AND vol/mcap > 0.15x → +1 to effective score.
  → Assign qualifier="OVERSOLD_VOL".

RULE 4 — BASE SCORE TIE-BREAK:
  When effective scores are equal after Rules 1–3, prefer higher vol/mcap.
  → Assign qualifier="BASE_SCORE".

CONFIDENCE TIERS (apply after ranking) — MINIMUM 2.5:1 RISK:REWARD ENFORCED:
- HIGH:   Rule 1 or (Rule 2 + quality setup) → aggressive BUY, TP 1.40x, SL 0.88x  (R:R ≈ 3.3:1)
- MEDIUM: Rule 2 alone or Rule 3 → cautious BUY, TP 1.28x, SL 0.90x               (R:R ≈ 2.8:1)
- LOW:    Rule 4 only, no catalyst → TP 1.20x, SL 0.92x — ADVISORY ONLY, no real entry  (R:R ≈ 2.5:1)

CRITICAL: LOW confidence = advisory only. Mark "advisory_only": true for LOW confidence picks.
NEVER set TP closer than 1.20x or SL tighter than 0.92x — minimum R:R is 2.5:1 always.

High ATH drop (95%+) is NOT a reason to avoid — it is a SETUP. ATL coins with vol and catalysts are prime bounces.

Return this JSON object with EXACTLY 10 picks. Always pick 10 — never fewer.
Rank all candidates from best to worst. If fewer than 10 have strong setups, fill remaining slots with LOW confidence picks.
Having 10 picks is mandatory so the portfolio always has fresh candidates to open:
{{
  "picks": [
    {{
      "rank": 1,
      "coin": "SYMBOL",
      "confidence": "high|medium|low",
      "qualifier": "INSTANT_QUALIFIER|NEWS_BOOST|OVERSOLD_VOL|BASE_SCORE",
      "key_signal": "the ONE signal that pushed this coin into top 3",
      "entry_price": <number or null>,
      "stop_loss": <number or null>,
      "take_profit": <number or null>,
      "timeframe": "e.g. 3-7 days or null",
      "reasoning": "specific reasoning referencing which rule triggered, vol/mcap, RSI, catalyst",
      "catalyst_type": "regulatory|tech_upgrade|partnership|narrative|institutional|token_economics|none",
      "catalyst_timing": "upcoming|just_happened|old_news|none",
      "setup_quality": "coiled_spring|accumulation|oversold_only|none",
      "pump_assessment": "brief assessment of pump alerts, or null if none",
      "advisory_only": false
    }}
  ]
}}"""

    # print("\n  Sending to Groq (llama-3.3-70b)...")

    try:
        response = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a SHORT-TERM trader. Your timeframe is 3–14 days maximum. "
                        "You always respond with valid JSON only — no markdown, no explanation outside the JSON.\n\n"

                        "CORE PHILOSOPHY:\n"
                        "  You do NOT care about long-term fundamentals, roadmaps, or whether a project is 'dead'.\n"
                        "  You care about: momentum, catalysts, oversold bounces, and volume spikes.\n"
                        "  A coin 95% below ATH with a catalyst and volume spike is a PERFECT short-term trade.\n"
                        "  ALGO was 97% below ATH when Google named it — it ran +57% in days. That is your edge.\n\n"

                        "HISTORICAL WIN PROFILE — what has worked in this system:\n"
                        "  ARCHETYPE A — Oversold Bounce (highest win rate):\n"
                        "    RSI < 42  |  BB position: below lower band or mid  |  7d dip: −8% to −85%\n"
                        "    MACD: bullish or turning bullish  |  vol/mcap > 0.10x\n"
                        "    Example winners: ALGO (RSI 32, −97% ATH, Google catalyst), XPL (RSI 38, coiled spring)\n"
                        "    → These are your highest-conviction BUYs. Assign HIGH or MEDIUM confidence.\n\n"
                        "  ARCHETYPE B — Momentum Continuation (moderate win rate):\n"
                        "    RSI: 50–72  |  24h change > +2%  |  vol/mcap > 0.15x\n"
                        "    MACD: bullish  |  trend: bullish or neutral\n"
                        "    Example winners: coins already moving with fresh catalyst in last 2 days\n"
                        "    → Valid picks when combined with NEWS_BOOST. Assign MEDIUM confidence.\n\n"

                        "PRE-FILTER STEPS (already applied before you receive the list):\n"
                        "  Step 0A (revised): MACD+Trend signal:\n"
                        "    MACD BEARISH + Trend BEARISH → removed entirely ❌ (both confirm downtrend, no entry)\n"
                        "    MACD BEARISH + Trend NEUTRAL → kept, but score CAPPED at 2 ⚠️ (flagged with ⚠️ on score)\n"
                        "    MACD BULLISH → full scoring access ✅\n"
                        "  Step 0B: Above upper Bollinger Band removed — already extended.\n"
                        "  Step 0C: Supply risk coins removed — unlock schedule risk.\n"
                        "  When you see score=N ⚠️ it means the score was capped by Step 0A. "
                        "Do NOT promote a capped coin above a clean coin of equal or higher score.\n\n"

                        "HARD DISQUALIFIERS (skip the coin entirely — take the next one):\n"
                        "  • RSI > 75 — late entry, overbought, risk of reversal\n"
                        "  • 7d gain > +80% with RSI > 65 — pump already happened, dangerous entry\n"
                        "  • vol/mcap < 0.02x — illiquid, cannot exit cleanly\n"
                        "  • Coin explicitly marked ACTIVE_SCAM or MANIPULATED_REAL in pump alerts\n\n"

                        "SIREN DEEP-DIP BONUS RULE:\n"
                        "  If a coin has ALL FOUR: 7d dip > −50%  +  RSI < 40  +  MACD bullish  +  vol/mcap > 0.15x\n"
                        "  → Assign qualifier='OVERSOLD_VOL', label it 'DEEP DIP', boost confidence to HIGH.\n"
                        "  → These are extreme coiled-spring setups — the SIREN pattern. Prioritize them.\n\n"

                        "RANKING IS RULE-BASED — apply the 4 rules in order:\n"
                        "  Rule 1 (INSTANT_QUALIFIER): vol/mcap >0.50x + MACD bullish + RSI <50 → auto top-3.\n"
                        "  Rule 2 (NEWS_BOOST): fresh news 0–2d → +2 effective score, RSI up to 65 ok.\n"
                        "  Rule 3 (OVERSOLD_VOL): RSI <35 + vol/mcap >0.15x → +1 effective score.\n"
                        "    SIREN bonus: if also 7d dip >−50%, upgrade to HIGH confidence.\n"
                        "  Rule 4 (BASE_SCORE): tie-break by vol/mcap.\n"
                        "Always assign qualifier and key_signal for each pick.\n\n"

                        "TIERED CONFIDENCE:\n"
                        "  HIGH:   Rule 1 OR (Archetype A + catalyst) OR SIREN deep-dip → TP 1.30x, SL 0.85x\n"
                        "  MEDIUM: Rule 2 alone OR Archetype A without catalyst OR Archetype B → TP 1.20x, SL 0.88x\n"
                        "  LOW:    Rule 4 only, no catalyst, mediocre setup → TP 1.15x, SL 0.90x\n\n"

                        "CATALYST FRAMEWORK:\n"
                        "  Each headline is tagged: (0d ago), (3d ago), (8d ago), etc.\n"
                        "  USE THESE TAGS — do NOT guess from wording:\n"
                        "    (0–7d ago)  = just_happened  → strong entry signal if price hasn't reacted yet\n"
                        "    (8–30d ago) = old_news       → likely priced in already\n"
                        "    (31d+ ago)  = old_news       → definitely priced in\n"
                        "    (date unknown) = assume old_news unless wording implies imminent event\n\n"

                        "WHAT TO IGNORE (not your job in short-term trading):\n"
                        "  - Whether the project has long-term value\n"
                        "  - Whether the team is active\n"
                        "  - ATH percentage (irrelevant — oversold is bullish for bounces)\n"
                        "  - Bearish long-term narratives\n\n"

                        "PERFECT SHORT-TERM SETUP EXAMPLES:\n"
                        "  HIGH: ALGO — 97% below ATH, RSI 32, Google Quantum AI catalyst (0d ago), vol/mcap 0.28x → Archetype A + NEWS_BOOST\n"
                        "  HIGH: XPL — coiled spring, RSI 38, 7d dip −42%, MACD bullish, vol/mcap 0.18x → SIREN-adjacent\n"
                        "  MEDIUM: SIREN — 94% below ATH, RSI 22, no news but extreme oversold + volume → Archetype A\n"
                        "  A deep ATH drop is NOT a reason to avoid — it IS the setup."
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
        return None, groq_candidates

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
        parsed = json.loads(content[start:end]) if start >= 0 else {}
    except Exception:
        print(f"\n  Groq raw response:\n{content}")
        return None, groq_candidates

    raw_picks: list[dict] = parsed.get("picks", [])
    # Fallback: Groq returned a single pick dict (old format)
    if not raw_picks and parsed.get("coin"):
        raw_picks = [parsed]

    # Deduplicate: if the same coin appears multiple times, keep the highest-confidence variant
    _CONF_ORDER = {"high": 3, "medium": 2, "low": 1}
    _seen_coins: dict[str, dict] = {}
    for _pick in raw_picks:
        _sym = (_pick.get("coin") or "").upper()
        if not _sym:
            continue
        _conf = (_pick.get("confidence") or "low").lower()
        if _sym not in _seen_coins:
            _seen_coins[_sym] = _pick
        else:
            _existing_conf = (_seen_coins[_sym].get("confidence") or "low").lower()
            if _CONF_ORDER.get(_conf, 0) > _CONF_ORDER.get(_existing_conf, 0):
                _seen_coins[_sym] = _pick
    if len(_seen_coins) < len(raw_picks):
        _dupes = len(raw_picks) - len(_seen_coins)
        # print(f"  [dedup] removed {_dupes} duplicate coin(s) from Groq output")
    raw_picks = list(_seen_coins.values())

    if not raw_picks:
        print("  Groq returned no picks.")
        return None, groq_candidates

    # Backfill entry_price from scanner data when Groq returns null/0.
    # Groq is not reliable at echoing prices — use the scanner's live price instead.
    _price_map = {r.get("symbol", "").upper(): r.get("price", 0) for r in top10}
    for _pick in raw_picks:
        _sym = (_pick.get("coin") or "").upper()
        _ep  = _pick.get("entry_price")
        if (not _ep or (isinstance(_ep, (int, float)) and _ep <= 0)) and _sym in _price_map:
            _pick["entry_price"] = _price_map[_sym]
            print(f"  [entry_price] {_sym}: backfilled from scanner (Groq returned null)")

    def _fmt(val):
        if not isinstance(val, (int, float)):
            return str(val)
        v = val
        if v == 0:   return "$0"
        if v >= 1:   return f"${v:,.2f}"
        if v >= 0.01: return f"${v:.4f}"
        return f"${v:.8f}"

    def _apply_guards(rec: dict) -> dict:
        """Apply TP cap, RSI guard, and F&G filter to a single pick. Mutates in place."""
        # ── Hard TP cap — only in Extreme Fear (< 20) ──
        try:
            entry = float(rec.get("entry_price") or 0)
            tp    = float(rec.get("take_profit") or 0)
            if entry > 0 and tp > 0 and fg_value < 20:
                max_gain  = 0.25
                cap_label = "extreme fear cap (+25%)"
                max_tp = round(entry * (1 + max_gain), 8)
                if tp > max_tp:
                    print(f"  ⚠️  TP capped {tp:.4f} → {max_tp:.4f} [{cap_label}]")
                    rec["take_profit"] = max_tp
                    rec["reasoning"] = (rec.get("reasoning", "") +
                        f" [TP reduced to {max_gain*100:.0f}% — {cap_label}]")
        except (TypeError, ValueError):
            pass

        # ── RSI overbought guard ──
        coin_pick    = rec.get("coin") or "?"
        matched_coin = next((r for r in top10 if r.get("symbol", "").upper() == coin_pick.upper()), None)
        coin_rsi     = matched_coin.get("rsi") if matched_coin else None

        if coin_rsi is not None:
            if coin_rsi > 70 and rec.get("setup_quality") == "oversold_only":
                rec["setup_quality"] = "none"
                rec["reasoning"] = (
                    f"[RSI {coin_rsi:.1f} — overbought, not oversold. Setup quality corrected.] "
                    + (rec.get("reasoning") or "")
                )
                print(f"  ⚠️  RSI guard: {coin_pick} RSI={coin_rsi:.1f} — setup_quality corrected")
            if coin_rsi > 75:
                _downgrade = {"high": "medium", "medium": "low", "low": "low"}
                orig_conf  = (rec.get("confidence") or "").lower()
                new_conf   = _downgrade.get(orig_conf, orig_conf)
                if new_conf != orig_conf:
                    rec["confidence"] = new_conf
                    rec["reasoning"] = (
                        f"[RSI {coin_rsi:.1f} > 75 — confidence downgraded "
                        f"{orig_conf.upper()} → {new_conf.upper()}] "
                        + (rec.get("reasoning") or "")
                    )
                    print(f"  ⚠️  RSI guard: {coin_pick} RSI={coin_rsi:.1f} — confidence {orig_conf.upper()} → {new_conf.upper()}")

        # ── Quality label from scanner score ──
        scanner_score = matched_coin["score"] if matched_coin else 0
        if scanner_score >= 7:
            quality = "🟢 STRONG BUY"
        elif scanner_score >= 4:
            quality = "🟡 SPECULATIVE"
        else:
            quality = "🔴 WEAK"
        rec["quality_label"] = quality
        rec["scanner_score"] = scanner_score
        return rec

    # Apply guards to all picks (up to 10)
    guarded = [_apply_guards(r) for r in raw_picks[:10]]

    # Enforce minimum 2.5:1 R:R on every pick
    for _g in guarded:
        try:
            _ep = float(_g.get("entry_price") or 0)
            _sl = float(_g.get("stop_loss")   or 0)
            _tp = float(_g.get("take_profit") or 0)
            if _ep > 0 and _sl > 0 and _tp > 0:
                _risk   = _ep - _sl
                _reward = _tp - _ep
                if _risk > 0 and _reward / _risk < 2.5:
                    _new_tp = round(_ep + _risk * 2.5, 8)
                    # print(f"  ⚠️  R:R fix: {_g.get('coin')} TP {_tp:.6f} → {_new_tp:.6f} (enforcing 2.5:1)")
                    _g["take_profit"] = _new_tp
        except (TypeError, ValueError):
            pass
        # All picks open real positions; advisory_only=False for all confidence tiers
        _g["advisory_only"] = False

    # All picks are actionable — confidence level is informational only
    actionable = guarded
    advisory   = []
    filtered   = guarded
    if not filtered:
        print(f"  ⛔  NO BUY — no picks returned from Groq")
        return None, groq_candidates

    # ── Display TOP N BUYS ──
    n = len(actionable)
    label = f"TOP {n} BUY{'S' if n != 1 else ''}"
    print("\n" + "=" * 60)
    print(f"  GROQ LLM RECOMMENDATION — {label}")
    print("=" * 60)
    for i, rec in enumerate(filtered, 1):
        coin_pick  = rec.get("coin") or "?"
        confidence = (rec.get("confidence") or "").upper() or "?"
        conf_icon  = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(confidence, "⚪")
        ep  = rec.get("entry_price")
        sl  = rec.get("stop_loss")
        tp  = rec.get("take_profit")
        tf  = rec.get("timeframe", "?")
        adv_tag = "  [ADVISORY ONLY — no entry]" if rec.get("advisory_only") else ""
        sltp = f"  Entry {_fmt(ep)}, SL {_fmt(sl)}, TP {_fmt(tp)}" if ep else ""
        qualifier  = rec.get("qualifier", "")
        key_signal = rec.get("key_signal", "")
        print(f"\n  {i}. {coin_pick:<8s} {conf_icon} {confidence} confidence  [{rec.get('quality_label','?')}]{sltp}{adv_tag}")
        if qualifier:
            print(f"     Qualifier: {qualifier}  |  {key_signal[:80]}" if key_signal else f"     Qualifier: {qualifier}")
        print(f"     Timeframe: {tf}")
        if rec.get("catalyst_type") and rec["catalyst_type"] != "none":
            print(f"     Catalyst:  {rec['catalyst_type']} ({rec.get('catalyst_timing', '?')})")
        if rec.get("setup_quality") and rec["setup_quality"] != "none":
            print(f"     Setup:     {rec['setup_quality']}")
        print(f"     Reason:    {rec.get('reasoning', '?')[:120]}")
        if rec.get("pump_assessment"):
            print(f"     Pump:      {rec['pump_assessment'][:100]}")
    print("=" * 60)

    # ── Web research validation on pick #1 (actionable only) ──
    if actionable:
        actionable[0] = _validate_with_web_research(actionable[0], top10, client,
                                                     groq_candidates=groq_candidates)
        filtered[0] = actionable[0]

    # Return only actionable picks for logging; advisory ones are informational only
    return actionable if actionable else None, groq_candidates


def _validate_with_web_research(
    rec: dict,
    top10: list[dict],
    client,
    groq_candidates: list[dict] | None = None,
) -> dict:
    """
    Fetch web research for the #1 pick and ask Groq to confirm or change.
    Returns updated rec dict with web_research_verdict and web_research_summary.
    groq_candidates: coins that passed pre-filter — fallback substitute is restricted to this set.
    """
    # Build approved symbol set: fallback must come from pre-filter survivors only
    _approved_syms = (
        {r.get("symbol", "").upper() for r in groq_candidates}
        if groq_candidates else
        {r.get("symbol", "").upper() for r in top10}  # no filter info → allow all
    )
    coin_sym  = (rec.get("coin") or "").upper()
    matched   = next((r for r in top10 if r.get("symbol", "").upper() == coin_sym), {})
    coin_name = matched.get("name", "")
    # For short/ambiguous symbols, prefer coin_id as a more specific search term
    coin_id   = matched.get("coin_id", "")
    if coin_id and len(coin_sym) <= 3 and not coin_name:
        coin_name = coin_id.replace("-", " ")

    print(f"\n  Running web research validation for {coin_sym}…")
    try:
        import config as _cfg
        if _cfg.TAVILY_API_KEY:
            from src.connectors.web_research import _tavily_search
            query = (
                f"{coin_name} cryptocurrency latest news risks"
                if coin_name else f"{coin_sym} crypto latest news risks"
            )
            data   = _tavily_search(query, max_results=5)
            answer = (data.get("answer") or "").strip()
            titles = [
                (r.get("title") or "").strip()
                for r in data.get("results", [])[:5]
                if (r.get("title") or "").strip()
            ]
            lines = []
            if answer:
                lines.append(f"Tavily summary: {answer[:300]}")
            if titles:
                lines.append("Recent headlines:")
                lines.extend(f"  • {t[:100]}" for t in titles)
            research_text = "\n".join(lines)
            if research_text:
                print(f"  Tavily: {answer[:120] if answer else f'{len(titles)} headlines'}")
        else:
            from src.connectors.web_research import research_crypto, format_research_for_prompt, print_research
            research = research_crypto(coin_sym, coin_name)
            print_research(research, coin_sym)
            research_text = format_research_for_prompt(research, coin_sym)
    except Exception as e:
        print(f"  Web research failed: {e}")
        rec["web_research_verdict"] = "SKIPPED"
        return rec

    if not research_text.strip():
        rec["web_research_verdict"] = "NO DATA"
        return rec

    try:
        check_budget("groq")
    except BudgetExceededError:
        rec["web_research_verdict"] = "SKIPPED (budget)"
        return rec

    validation_prompt = f"""You recommended {coin_sym} as the best buy. Here is fresh web research:

{research_text}

Based on this additional context, do you CONFIRM or CHANGE your recommendation?

Rules:
- If you find red flags (scam reports, hack, lawsuit, team drama, rug pull news): CHANGE to the next best coin from your original list
- If sentiment is mixed or neutral: CONFIRM with a note
- If sentiment confirms your bullish thesis: CONFIRM with higher confidence
- Be decisive — choose CONFIRM or CHANGE, not both

Respond in valid JSON only. All values must be strings or numbers — no unquoted + signs.
{{"verdict": "CONFIRM", "new_coin": null, "confidence_adjustment": 10, "web_summary": "1-2 sentence summary"}}"""

    try:
        resp2 = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role":"user","content":validation_prompt}],
            max_tokens=300,
            temperature=0.10,
            response_format={"type":"json_object"},
        )
        log_llm_call("groq", tokens_in=len(validation_prompt)//4, tokens_out=300, endpoint="web_validation")
        val_raw = resp2.choices[0].message.content.strip()
    except Exception as e:
        print(f"  Web validation Groq error: {e}")
        rec["web_research_verdict"] = "ERROR"
        return rec

    import re
    match = re.search(r'\{.*\}', val_raw, re.DOTALL)
    if not match:
        rec["web_research_verdict"] = "PARSE_ERROR"
        return rec

    try:
        val = json.loads(match.group())
    except json.JSONDecodeError:
        rec["web_research_verdict"] = "PARSE_ERROR"
        return rec

    verdict  = val.get("verdict","?")
    new_coin = val.get("new_coin")
    summary  = val.get("web_summary","")

    print(f"\n  WEB VALIDATION: {verdict} {'✅' if verdict=='CONFIRM' else '⚠️ → ' + (new_coin or '?')}")
    if summary:
        print(f"  Web summary: {summary}")

    rec["web_research_verdict"] = verdict
    rec["web_research_summary"] = summary
    if verdict == "CHANGE" and new_coin != coin_sym:
        # Close the original coin in recommendations.csv — it was logged as OPEN by log_scanner_results
        # before Groq ran. Web validation rejected it, so exclude it now.
        try:
            import csv as _csv
            from pathlib import Path as _Path
            import config as _cfg
            _lp = _cfg.DATA_DIR / "recommendations.csv"
            with open(_lp, newline="", encoding="utf-8") as _f:
                _rows = list(_csv.DictReader(_f))
            _fnames = list(_rows[0].keys()) if _rows else []
            _changed = False
            for _r in _rows:
                if (_r.get("coin","").upper() == coin_sym
                        and _r.get("status") == "OPEN"
                        and _r.get("type","SCANNER") in ("SCANNER","")):
                    _r["status"]   = "EXCLUDED"
                    _r["reasoning"] = f"[WEB VALIDATION: {summary[:80]}] " + _r.get("reasoning","")
                    _changed = True
                    reason_label = (summary[:60] + "…") if len(summary) > 60 else (summary or "web validation flag")
                    print(f"  Excluded {coin_sym} from recommendations (web validation: {reason_label})")
                    break
            if _changed and _fnames:
                with open(_lp, "w", newline="", encoding="utf-8") as _f:
                    _w = _csv.DictWriter(_f, fieldnames=_fnames)
                    _w.writeheader()
                    _w.writerows(_rows)
        except Exception as _ex:
            print(f"  Warning: could not exclude {coin_sym} from CSV: {_ex}")

        # Find substitute: Groq's suggestion first, else next best from top10 without OPEN position
        from src.utils.logger import _read as _log_read
        open_coins = {
            r.get("coin", "").upper()
            for r in _log_read()
            if r.get("status") == "OPEN" and r.get("type", "SCANNER") in ("SCANNER", "")
        }
        # Groq's suggested new_coin, validated against pre-filter survivors only
        candidate = None
        if new_coin:
            candidate = next(
                (r for r in top10 if r.get("symbol","").upper() == new_coin.upper()
                 and r.get("symbol","").upper() not in open_coins
                 and r.get("symbol","").upper() in _approved_syms),
                None
            )
        # Fallback: next pre-filter survivor in top10 that's not #1 and not already open
        if not candidate:
            candidate = next(
                (r for r in top10
                 if r.get("symbol","").upper() != coin_sym
                 and r.get("symbol","").upper() not in open_coins
                 and r.get("symbol","").upper() in _approved_syms),
                None
            )
        if candidate:
            price = candidate.get("price", 0)
            sym   = candidate.get("symbol","").upper()
            print(f"  ⚠️  Web validation CHANGE: {coin_sym} → {sym}")
            rec["original_coin"] = coin_sym
            rec["coin"]          = sym
            rec["coin_id"]       = candidate.get("coin_id", "")
            rec["entry_price"]   = price
            rec["stop_loss"]     = round(price * 0.85, 8)
            rec["take_profit"]   = round(price * 1.10, 8)
            rec["reasoning"]     = f"[WEB RESEARCH: switched from {coin_sym} — {summary}]"
        else:
            print(f"  ⚠️  Web validation CHANGE: no valid substitute found, keeping {coin_sym}")

    return rec
