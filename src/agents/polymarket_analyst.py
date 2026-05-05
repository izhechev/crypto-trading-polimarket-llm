"""
Polymarket LLM Analyst — sends top markets to Groq for verdict + edge detection.
"""
import csv
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config
from src.utils.budget_tracker import log_llm_call, check_budget, BudgetExceededError

LOG_PATH = config.DATA_DIR / "polymarket_picks.csv"
_LOG_HEADERS = [
    "date", "market", "market_id", "current_odds_pct", "volume_usd",
    "llm_verdict", "llm_confidence_pct", "llm_edge_pct",
    "is_opportunity", "reason", "web_sentiment",
    "status", "resolved_outcome",
    "pick_type",   # TRIVIAL (market already ~certain) or REAL (genuine prediction)
    "url",         # polymarket.com/event/... for manual verification
]

def _compute_pick_type(llm_verdict, market_odds_pct) -> str:
    """
    TRIVIAL: market odds <= 1% (verdict NO) or >= 99% (verdict YES) when pick was made.
             The outcome was already near-certain — any correct call is a freebie.
             Covers cases like 99.9% YES or 0.1% NO (temperature markets).
    REAL:    everything else — genuine uncertainty existed at pick time.
    """
    try:
        odds = float(market_odds_pct)
    except (TypeError, ValueError):
        return "REAL"
    verdict = str(llm_verdict).upper()
    if odds <= 1.0 and verdict == "NO":
        return "TRIVIAL"
    if odds >= 99.0 and verdict == "YES":
        return "TRIVIAL"
    return "REAL"


def _is_near_certain_market(market_odds_pct) -> bool:
    """Return True for markets to skip: odds exactly 0% or 100%.
    These are already fully priced in — no analysis possible."""
    try:
        odds = float(market_odds_pct)
        return odds == 0.0 or odds == 100.0
    except (TypeError, ValueError):
        return False


# Keywords in market title that indicate a choice between named options.
# When API returns binary Yes/No for these, YES/NO is semantically ambiguous.
_MULTI_OUTCOME_TITLE_KEYWORDS = {"or", "vs", "vs.", "which", "what price", "who will", "o/u", "over/under"}

# Sports markets where the system has 0% edge: draws, spreads, over/under totals
_SPORTS_NO_EDGE_PATTERNS = [
    r'\bover\b', r'\bunder\b', r'\bo/u\b', r'\bou\b',
    r'\bdraw\b', r'\bdraws?\b', r'\bend in a draw\b',
    r'\bspread\b', r'\btotal goals\b', r'\btotal points\b',
    r'\bover [0-9]', r'\bunder [0-9]',
    r'\bgoals? over\b', r'\bgoals? under\b',
]


def _is_title_multi_outcome(question: str) -> bool:
    """Return True if title implies a choice between named options (not a YES/NO question)."""
    q_lc = question.lower()
    # "Will X or Y happen?" is still binary. True multi-outcome: "X or Y first?", "X vs Y"
    # Heuristic: contains keyword AND does NOT start with "will"
    starts_with_will = q_lc.strip().startswith("will ")
    return any(kw in q_lc for kw in _MULTI_OUTCOME_TITLE_KEYWORDS) and not starts_with_will


import re as _re


# ── Logging ───────────────────────────────────────────────────────────────

def _ensure_log() -> None:
    if not LOG_PATH.exists():
        with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_LOG_HEADERS).writeheader()
        return
    with open(LOG_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        old_headers = reader.fieldnames or []
        needs_migration = "status" not in old_headers or "pick_type" not in old_headers
        if not needs_migration:
            return
        rows = list(reader)

    # Migrate: add status/resolved_outcome if missing
    if "status" not in old_headers:
        for row in rows:
            old_result = row.pop("result", "") or ""
            row["status"]           = "OPEN" if not old_result else old_result
            row["resolved_outcome"] = ""

    # Migrate: compute pick_type for rows that lack it
    if "pick_type" not in old_headers:
        for row in rows:
            verdict    = row.get("llm_verdict", "")
            market_pct = row.get("current_odds_pct", "")
            row["pick_type"] = _compute_pick_type(verdict, market_pct)

    with open(LOG_PATH, "w", newline="", encoding="utf-8") as fw:
        writer = csv.DictWriter(fw, fieldnames=_LOG_HEADERS, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Polymarket CSV migrated ({len(rows)} rows)")


def log_polymarket_picks(picks: list[dict]) -> None:
    """Append Groq-analysed picks to data/polymarket_picks.csv. Skips markets already OPEN."""
    _ensure_log()
    # Read existing open markets to avoid duplicates
    existing_open: set[str] = set()
    try:
        with open(LOG_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status") == "OPEN":
                    existing_open.add(row.get("market", "")[:120])
    except Exception:
        pass

    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    logged = 0
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_LOG_HEADERS, extrasaction="ignore")
        for p in picks:
            market_key = p.get("question", "")[:120]
            if market_key in existing_open:
                continue
            market_pct = round(p.get("probability", 0) * 100, 1) if p.get("probability") is not None else ""
            llm_prob   = p.get("llm_edge", "")   # LLM's estimated event probability
            writer.writerow({
                "date":               now,
                "market":             market_key,
                "market_id":          p.get("market_id", ""),
                "current_odds_pct":   market_pct,
                "volume_usd":         p.get("volume_usd", ""),
                "llm_verdict":        p.get("llm_verdict", ""),
                "llm_confidence_pct": p.get("llm_confidence", ""),
                "llm_edge_pct":       llm_prob,
                "is_opportunity":     "yes" if p.get("is_opportunity") else "no",
                "reason":             p.get("llm_reason", ""),
                "web_sentiment":      p.get("web_sentiment", ""),
                "status":             "OPEN",
                "resolved_outcome":   "",
                "pick_type":          "TRIVIAL" if p.get("_auto_trivial") else ("REAL" if p.get("is_multi_outcome") else _compute_pick_type(p.get("llm_verdict", ""), market_pct)),
                "url":                p.get("url", ""),
            })
            logged += 1
    print(f"  Polymarket picks logged ({logged} new) -> {LOG_PATH.name}")


def update_polymarket_positions() -> None:
    """Check resolution of OPEN Polymarket picks via the API. Update WIN/LOSS."""
    _ensure_log()
    rows: list[dict] = []
    try:
        with open(LOG_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return

    open_rows = [r for r in rows if r.get("status") in ("OPEN", "")]
    if not open_rows:
        return

    try:
        from src.connectors.polymarket import check_market_resolution
    except ImportError:
        return

    closed = 0
    for row in open_rows:
        question  = row.get("market", "")
        market_id = row.get("market_id", "")
        result    = check_market_resolution(question, market_id=market_id)
        if not result or not result.get("resolved"):
            continue
        # For multi-outcome rows, compare against outcome_name (e.g. "$80K")
        # For binary rows, compare against "YES"/"NO"
        outcome_name = result.get("outcome_name", result["outcome"])
        verdict      = row.get("llm_verdict", "")
        winner_idx   = result.get("winner_idx", -1)
        is_binary_verdict = verdict.upper() in ("YES", "NO")

        if is_binary_verdict and result["outcome"] in ("YES", "NO"):
            # Normal binary market — direct compare
            correct = verdict == result["outcome"]
            outcome = result["outcome"]
        elif is_binary_verdict and result["outcome"] not in ("YES", "NO"):
            # Binary verdict on a multi-outcome market (e.g. "YES" on "Team A vs Team B")
            # YES = first outcome (idx 0), NO = second outcome (idx 1)
            correct = (verdict.upper() == "YES" and winner_idx == 0) or \
                      (verdict.upper() == "NO"  and winner_idx == 1)
            outcome = outcome_name
        else:
            # Multi-outcome verdict (e.g. "$80K", "Hurricanes")
            correct = verdict.strip().lower() == outcome_name.strip().lower()
            outcome = outcome_name
        pick_type = row.get("pick_type", "TRIVIAL")

        row["resolved_outcome"] = outcome
        row["status"] = "WIN" if correct else "LOSS"
        closed += 1

        icon  = "[OK]" if correct else "[X] "
        label = "CORRECT" if correct else "WRONG"
        ptype = f"[{pick_type}]"
        url   = row.get("url", "")
        print(f"  {icon} {label} {ptype}: '{question[:60]}'")
        print(f"     Our verdict: {verdict} | Actual result: {outcome}")
        if url:
            print(f"     {url}")

    if closed:
        with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_LOG_HEADERS, extrasaction="ignore", restval="")
            writer.writeheader()
            writer.writerows(rows)
        print(f"  {closed} Polymarket position(s) resolved")


def print_polymarket_track_record() -> None:
    """Print OPEN/WIN/LOSS summary for Polymarket picks, split by EDGE vs TRIVIAL."""
    _ensure_log()
    rows: list[dict] = []
    try:
        with open(LOG_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return
    if not rows:
        return

    n_open   = sum(1 for r in rows if r.get("status") in ("OPEN", ""))
    resolved = [r for r in rows if r.get("status") in ("WIN", "LOSS")]

    real_wins    = [r for r in resolved if r.get("pick_type") == "REAL"    and r["status"] == "WIN"]
    real_losses  = [r for r in resolved if r.get("pick_type") == "REAL"    and r["status"] == "LOSS"]
    triv_wins    = [r for r in resolved if r.get("pick_type") == "TRIVIAL" and r["status"] == "WIN"]
    triv_losses  = [r for r in resolved if r.get("pick_type") == "TRIVIAL" and r["status"] == "LOSS"]

    real_total = len(real_wins) + len(real_losses)
    triv_total = len(triv_wins) + len(triv_losses)
    real_acc   = len(real_wins) / real_total * 100 if real_total else 0
    triv_acc   = len(triv_wins) / triv_total * 100 if triv_total else 0
    total_cls  = len(resolved)
    total_acc  = (len(real_wins) + len(triv_wins)) / total_cls * 100 if total_cls else 0

    print(f"\n  POLYMARKET PICKS  ({len(rows)} total | {n_open} open | {total_cls} resolved)")
    print(f"  Overall accuracy: {len(real_wins)+len(triv_wins)}/{total_cls} = {total_acc:.0f}%")
    print(f"  REAL picks (market not near-certain):  "
          f"{len(real_wins)}W / {len(real_losses)}L  ({real_acc:.0f}%)")
    print(f"  TRIVIAL picks (market ~0% or ~100%):   "
          f"{len(triv_wins)}W / {len(triv_losses)}L  ({triv_acc:.0f}%)")

    # Per-pick resolution log
    if resolved:
        print(f"\n  {'-'*56}")
        print("  RESOLVED PICKS:")
        for r in resolved:
            correct  = r["status"] == "WIN"
            icon     = "[OK]" if correct else "[X] "
            ptype    = r.get("pick_type", "TRIVIAL")
            verdict  = r.get("llm_verdict", "?")
            outcome  = r.get("resolved_outcome", "?")
            question = r.get("market", "")[:55]
            label    = "CORRECT" if correct else "WRONG"
            url      = r.get("url", "")

            # Compute edge at time of pick: llm_edge_pct (LLM probability) − market odds
            edge_str = ""
            try:
                llm_p  = float(r.get("llm_edge_pct", ""))
                mkt_p  = float(r.get("current_odds_pct", ""))
                edge   = round(llm_p - mkt_p, 1)
                sign   = "+" if edge >= 0 else ""
                edge_str = f"  edge {sign}{edge:.0f}pp"
            except (TypeError, ValueError):
                pass

            print(f"  {icon} [{ptype:7s}] {label:7s}{edge_str}  '{question}'")
            print(f"               Our: {verdict} | Result: {outcome}")
            if url:
                print(f"               {url}")


# ── Groq analysis ─────────────────────────────────────────────────────────

def analyze_polymarket(markets: list[dict]) -> list[dict]:
    """
    Send top markets to Groq for YES/NO verdict + edge detection.
    Returns markets list enriched with llm_* fields.
    """
    if not config.GROQ_API_KEY:
        print("  POLYMARKET: GROQ_API_KEY not set — skipping LLM analysis")
        return []

    try:
        check_budget("groq")
    except BudgetExceededError as e:
        print(f"  POLYMARKET: {e}")
        return []

    try:
        from groq import Groq
    except ImportError:
        print("  ERROR: groq not installed.")
        return []

    client = Groq(api_key=config.GROQ_API_KEY)

    # Auto-resolve 0%/100% markets as TRIVIAL — no Groq needed, saves tokens.
    # 0% odds → auto-verdict NO (outcome already certain), 100% → YES.
    auto_resolved: list[dict] = []
    remaining_markets = []
    for m in markets:
        odds_pct = round(m.get("probability", 0.5) * 100, 1) if m.get("probability") is not None else 50
        if _is_near_certain_market(odds_pct):
            auto_verdict = "YES" if odds_pct >= 99.0 else "NO"
            auto_resolved.append({
                **m,
                "llm_verdict":     auto_verdict,
                "llm_confidence":  100.0,
                "llm_probability": odds_pct,
                "llm_edge":        odds_pct,
                "llm_reason":      f"Auto-resolved: market odds {odds_pct:.0f}% — outcome already certain",
                "is_opportunity":  False,
                "edge_pct":        0.0,
                "web_sentiment":   "NEUTRAL",
                "_auto_trivial":   True,
            })
        else:
            remaining_markets.append(m)

    if auto_resolved:
        print(f"  Auto-resolved {len(auto_resolved)} trivial market(s) (0%/100% odds) — skipping Groq")
    markets = remaining_markets

    if not markets:
        if auto_resolved:
            print("  No actionable Polymarket markets this scan (all trivial 0%/100% odds)")
        return auto_resolved

    # Skip sports markets — system has 0/6 track record on sports (draws, O/U, match winners)
    # Primary: skip if API category is "sports" (fast, reliable)
    # Secondary: keyword/pattern detection on question title
    import re as _re_sports
    _sports_no_edge = [_re_sports.compile(p, _re_sports.IGNORECASE) for p in _SPORTS_NO_EDGE_PATTERNS]

    _SPORTS_KEYWORDS = {
        "nhl", "nba", "nfl", "mlb", "nba", "epl", "ligue", "bundesliga",
        "lfl", "lec", "lcs", "lck", "lpl",   # esports leagues
        "fc ", " fc", "united", "city fc", "athletic",
        "blackhawks", "hurricanes", "rangers", "bruins", "leafs", "maple",
    }

    def _is_sports_market(m: dict) -> bool:
        cat = (m.get("category") or "").lower()
        if "sport" in cat:
            return True
        q = (m.get("question") or "").strip()
        if any(pat.search(q) for pat in _sports_no_edge):
            return True
        q_lc = q.lower()
        if any(kw in q_lc for kw in _SPORTS_KEYWORDS):
            return True
        # "Team A vs Team B" winner markets — no "Will" prefix + "vs" = match winner
        if not q_lc.startswith("will ") and re.search(r'\bvs\.?\b', q_lc, re.IGNORECASE):
            crypto_election = any(kw in q_lc for kw in (
                "$", "%", "bitcoin", "ethereum", "btc", "eth",
                "election", "vote", "party", "president"
            ))
            if not crypto_election:
                return True
        return False

    before_sports = len(markets)
    markets = [m for m in markets if not _is_sports_market(m)]
    skipped_sports = before_sports - len(markets)
    if skipped_sports:
        print(f"  Skipped {skipped_sports} sports market(s) — 0/6 track record, no system edge")

    if not markets:
        return []

    # Pre-fetch Google News + Reddit for ALL markets BEFORE Groq so the LLM
    # uses real web intelligence to drive verdicts, not just its training data.
    print(f"  Fetching web+Reddit research for all {len(markets)} markets...")
    market_research: dict[int, dict] = {}   # idx → {google: [...], reddit: [...]}
    try:
        from src.connectors.web_research import search_google_news, search_reddit
        for idx, m in enumerate(markets):
            q = m.get("question", "")[:80]

            # Google News — fast, no delay
            gn       = search_google_news(q, limit=4)
            gn_hl    = [n["title"] for n in gn if n.get("title")]

            # Reddit — r/polymarket (most relevant) + r/news (broader context)
            r_poly   = search_reddit(q, subreddit="polymarket", limit=3)
            r_news   = search_reddit(q, subreddit="news", limit=2)
            r_hl     = [p["title"] for p in r_poly + r_news if p.get("title")]

            market_research[idx] = {"google": gn_hl, "reddit": r_hl}

        found_any = sum(
            1 for v in market_research.values()
            if v.get("google") or v.get("reddit")
        )
        print(f"  Research found for {found_any}/{len(markets)} markets")
    except Exception as e:
        print(f"  Polymarket web research failed: {e}")

    # Build prompt with Google News + Reddit embedded per market
    lines = []
    for i, m in enumerate(markets, 1):
        prob     = m.get("probability")
        vol      = m.get("volume_usd", 0)
        vol_str  = f"${vol/1000:.0f}k" if vol >= 1000 else f"${vol:.0f}"

        res       = market_research.get(i - 1, {})
        gn_hl     = res.get("google", [])
        r_hl      = res.get("reddit", [])

        web_lines = []
        for h in gn_hl[:4]:
            web_lines.append(f"   [WEB]    {h[:100]}")
        for h in r_hl[:3]:
            web_lines.append(f"   [REDDIT] {h[:100]}")
        web_block = "\n".join(web_lines) if web_lines else "   [NO WEB/REDDIT RESULTS FOUND]"

        days_left = m.get("days_left")
        if days_left is not None:
            if days_left < 1:
                time_str = "resolves in <1 day"
            elif days_left < 2:
                time_str = "resolves tomorrow"
            else:
                time_str = f"resolves in {days_left:.0f} days"
        else:
            time_str = "resolution date unknown"

        # Augment is_multi_outcome with title-keyword detection as fallback
        # (catches markets where API returns Yes/No but title implies a named choice)
        if not m.get("is_multi_outcome") and _is_title_multi_outcome(m.get("question", "")):
            m["is_multi_outcome"] = True

        if m.get("is_multi_outcome"):
            if m.get("outcome_probs"):
                # API returned named outcomes with probabilities
                outcome_lines = "  ".join(
                    f"{name} ({pct:.0f}%)" for name, pct in m["outcome_probs"]
                )
                lines.append(
                    f"{i}. [MULTI-OUTCOME] MARKET: {m.get('question','')}\n"
                    f"   OUTCOMES: {outcome_lines} | VOLUME: {vol_str} | {time_str}\n"
                    f"{web_block}"
                )
            else:
                # Title implies a choice but API gave binary Yes/No — ask Groq to name the outcome
                prob_str = f"{prob*100:.0f}%" if prob is not None else "unknown"
                lines.append(
                    f"{i}. [MULTI-OUTCOME] MARKET: {m.get('question','')}\n"
                    f"   ODDS: {prob_str} (first option) | VOLUME: {vol_str} | {time_str}\n"
                    f"   NOTE: title implies a named choice — output the specific outcome, not YES/NO\n"
                    f"{web_block}"
                )
        else:
            prob_str = f"{prob*100:.0f}%" if prob is not None else "unknown"
            lines.append(
                f"{i}. MARKET: {m.get('question','')}\n"
                f"   ODDS: {prob_str} (YES) | VOLUME: {vol_str} | {time_str}\n"
                f"{web_block}"
            )
    markets_text = "\n\n".join(lines)

    prompt = f"""You are a prediction market analyst. Each market includes current odds, Google News, and Reddit posts. This is real, live web intelligence — use it as the primary signal.

CRITICAL: confidence ≠ estimated_probability. They measure completely different things:
- confidence: how sure you are of your analysis (0–100%)
- estimated_probability: what you think the actual chance of this event happening is (0–100%)

Example — BTC above $78K by April 12:
  verdict: NO
  confidence: 70%  (I am fairly sure of my analysis)
  estimated_probability: 8%  (I think there is only an 8% chance BTC actually hits $78K in 3 days)
  edge = estimated_probability − market_odds = 8 − 1 = +7pp  (not an opportunity, gap too small)

SPORTS MARKETS: Always check head-to-head record before making a verdict.
  - "Team A vs Team B draw" at 20% odds — if these teams drew 4 of their last 7 meetings (57%),
    the market is WRONG and the draw is likely. Say YES with that H2H data in your reason.
  - Never use generic draw rates ("draws are rare in football") without checking the specific
    team matchup. H2H record overrides general statistics.
  - If you have no H2H data in the web results, set confidence <= 50% (do not guess).

For BINARY markets (YES/NO) output EXACTLY:
VERDICT: YES/NO | Confidence: X%
ESTIMATED_PROBABILITY: X%
REASON: [1 sentence using web/reddit evidence — for sports include H2H if available]
OPPORTUNITY: YES   (only when estimated_probability − market_odds >= 15 percentage points AND positive)

For [MULTI-OUTCOME] markets output EXACTLY:
OUTCOME: <one of the listed outcome names, copied exactly> | Confidence: X%
ESTIMATED_PROBABILITY: X% (probability that your chosen outcome wins)
REASON: [1 sentence]
SKIP: YES   (output this instead if you cannot confidently pick one outcome)

If web and Reddit contradict the current odds, that IS the edge — flag it.
If there are no web results, rely on training data but lower confidence.
No extra text between markets.

Markets:

{markets_text}"""

    try:
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role":"user","content":prompt}],
            max_tokens=1500,
            temperature=0.15,
        )
        log_llm_call("groq", tokens_in=len(prompt)//4, tokens_out=1500, endpoint="polymarket_analyst")
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        print(f"  POLYMARKET GROQ ERROR: {e}")
        return []

    print("\n" + "="*60)
    print("  POLYMARKET LLM ANALYSIS")
    print("="*60)
    print(raw)

    # Parse response and enrich markets.
    # Split on "N." market-number lines rather than blank lines — Groq sometimes
    # inserts extra blank lines or omits them, making positional \n\n splits unreliable.
    import re as _re2
    # Build a map: market_number (1-based) → block text
    raw_blocks: dict[int, str] = {}
    current_num  = None
    current_lines: list[str] = []
    for line in raw.splitlines():
        m_num = _re2.match(r'^(\d+)\.\s', line)
        if m_num:
            if current_num is not None:
                raw_blocks[current_num] = "\n".join(current_lines)
            current_num  = int(m_num.group(1))
            current_lines = [line]
        else:
            if current_num is not None:
                current_lines.append(line)
    if current_num is not None:
        raw_blocks[current_num] = "\n".join(current_lines)

    # Fallback: if no numbered blocks found (Groq ignored numbering), split on blank lines
    if not raw_blocks:
        for idx, block in enumerate(raw.strip().split("\n\n"), 1):
            raw_blocks[idx] = block

    enriched = []
    for i, m in enumerate(markets):
        block   = raw_blocks.get(i + 1, "")
        lines_b = block.strip().splitlines()

        verdict     = ""
        confidence  = ""
        reason      = ""
        llm_prob    = ""   # LLM's estimated event probability (distinct from confidence)
        is_opp      = False
        edge_pct    = ""
        skip_market = False
        is_multi    = m.get("is_multi_outcome", False)

        for line in lines_b:
            l = line.strip()
            if l.startswith("SKIP: YES"):
                skip_market = True
            elif l.startswith("VERDICT:"):
                parts = l.split("|")
                v = parts[0].replace("VERDICT:", "").strip()
                verdict = "YES" if "YES" in v.upper() else "NO" if "NO" in v.upper() else ""
                if len(parts) > 1:
                    c = parts[1].replace("Confidence:", "").replace("%", "").strip()
                    try:
                        confidence = float(c)
                    except ValueError:
                        confidence = ""
            elif l.startswith("OUTCOME:"):
                # Multi-outcome: Groq picked a specific outcome name
                parts = l.split("|")
                verdict = parts[0].replace("OUTCOME:", "").strip()
                if len(parts) > 1:
                    c = parts[1].replace("Confidence:", "").replace("%", "").strip()
                    try:
                        confidence = float(c)
                    except ValueError:
                        confidence = ""
            elif l.startswith("ESTIMATED_PROBABILITY:"):
                try:
                    llm_prob = float(l.replace("ESTIMATED_PROBABILITY:", "").replace("%", "").strip())
                except ValueError:
                    pass
            elif l.startswith("REASON:"):
                reason = l.replace("REASON:", "").strip()
            elif l.startswith("OPPORTUNITY: YES"):
                is_opp = True

        # Skip multi-outcome markets where Groq couldn't pick
        if skip_market or (is_multi and not verdict):
            q = m.get("question", "")[:60]
            print(f"  [!] Multi-outcome skip: '{q}' — Groq could not pick one outcome")
            continue

        # Compute edge = LLM's event probability estimate − market odds.
        # Positive edge means LLM thinks YES is more likely than market implies.
        # Negative edge (LLM more bearish than market) is NOT an opportunity to buy YES.
        if llm_prob != "" and m.get("probability") is not None:
            current_pct   = m["probability"] * 100
            computed_edge = round(float(llm_prob) - current_pct, 1)
            edge_pct      = computed_edge
            if computed_edge >= 15:   # positive edge only — never flag negative edge as opportunity
                is_opp = True
            else:
                is_opp = False   # override any OPPORTUNITY: YES Groq may have emitted

        enriched.append({
            **m,
            "llm_verdict":    verdict,
            "llm_confidence": confidence,
            "llm_probability": llm_prob,   # estimated event probability (not LLM certainty)
            "llm_reason":     reason,
            "llm_edge":       llm_prob,    # kept for downstream compat — equals llm_probability
            "is_opportunity": is_opp,
            "edge_pct":       edge_pct,
            "web_sentiment":  "NEWS_PRELOADED",
        })

    # Attach the pre-fetched research sentiment to each enriched market
    for i, m in enumerate(enriched):
        res    = market_research.get(i, {})
        all_hl = res.get("google", []) + res.get("reddit", [])
        bull   = sum(1 for h in all_hl if any(w in h.lower() for w in
                     {"win","pass","approve","yes","surge","confirm","bullish","rise","gain","up"}))
        bear   = sum(1 for h in all_hl if any(w in h.lower() for w in
                     {"fail","reject","no","fall","deny","bearish","drop","lose","miss"}))
        if bull > bear:
            sent = "BULLISH"
        elif bear > bull:
            sent = "BEARISH"
        else:
            sent = "NEUTRAL"
        m["web_sentiment"] = sent
        m["web_bullish"]   = bull
        m["web_bearish"]   = bear

    # Single quality filter: drop picks where confidence < 50%.
    # Both historical wrong picks (Damac, LoL) had confidence 30-60% with "limited information".
    # No category exceptions — sports, weather, esports all allowed if confidence is sufficient.
    final = []
    for m in enriched:
        conf = m.get("llm_confidence")
        if isinstance(conf, float) and conf < 50.0:
            print(f"  Dropped (conf {conf:.0f}% < 50%): '{m.get('question','')[:55]}'")
            continue
        final.append(m)

    if len(final) < len(enriched):
        print(f"  {len(enriched) - len(final)} pick(s) dropped (confidence < 50%)")
    enriched = final

    # Append auto-resolved trivial picks (0%/100% odds) — logged for stats, not shown in TOP BETS
    return enriched + auto_resolved


def print_polymarket_picks(picks: list[dict]) -> None:
    """Print the Polymarket picks section to console."""
    # Auto-trivial picks are logged silently — don't clutter the display
    display_picks = [p for p in picks if not p.get("_auto_trivial")]
    if not display_picks:
        trivial_count = len(picks)
        if trivial_count:
            print(f"  No actionable Polymarket markets this scan ({trivial_count} trivial auto-resolved)")
        return
    print("\n" + "="*60)
    print("  🔮 POLYMARKET PICKS  ⏰ Short-term only (resolves <7 days)")
    print("="*60)
    for p in display_picks:
        prob      = p.get("probability")
        prob_str  = f"{prob*100:.0f}%" if prob is not None else "?"
        vol       = p.get("volume_usd", 0)
        vol_str   = f"${vol/1000:.0f}k" if vol >= 1000 else f"${vol:.0f}"
        verdict   = p.get("llm_verdict", "?")
        conf      = p.get("llm_confidence", "?")
        conf_str  = f"{conf:.0f}%" if isinstance(conf, float) else str(conf)
        reason    = p.get("llm_reason", "")
        is_opp    = p.get("is_opportunity", False)
        edge      = p.get("edge_pct", "")
        days_left = p.get("days_left")

        time_tag = ""
        if days_left is not None:
            time_tag = f"  ⏰ {days_left:.0f}d left" if days_left >= 1 else "  ⏰ resolves today"

        llm_prob_val = p.get("llm_probability", "")
        prob_est_str = f"  Est.prob: {llm_prob_val:.0f}%" if isinstance(llm_prob_val, (int, float)) else ""

        opp_tag = ""
        if is_opp:
            edge_str = f"{edge:+.0f}pp" if isinstance(edge, float) else ""
            opp_tag  = f"  🎯 EDGE {edge_str}"

        verdict_icon = "✅" if verdict == "YES" else "❌" if verdict == "NO" else "❓"
        web_sent = p.get("web_sentiment", "")
        web_str  = f"  Web: {web_sent}" if web_sent else ""
        url      = p.get("url", "")
        print(f"\n  {verdict_icon} {p.get('question','?')[:70]}{time_tag}")
        print(f"     Odds: {prob_str}  Vol: {vol_str}")
        print(f"     Verdict: {verdict} | Confidence: {conf_str}{prob_est_str}{opp_tag}{web_str}")
        if reason:
            print(f"     Reason: {reason}")
        if url:
            print(f"     🔗 {url}")
    print(f"\n  {'─'*48}")

    # ── TOP 3 BETS summary (edge > 5pp) — exclude auto-trivial ──────────────────────────────────
    bets = []
    for p in display_picks:
        edge = p.get("edge_pct")
        try:
            edge_f = float(edge)
        except (TypeError, ValueError):
            continue
        if edge_f > 5 and p.get("is_opportunity"):
            bets.append((edge_f, p))
    bets.sort(key=lambda x: -x[0])
    top_bets = bets[:3]

    if top_bets:
        n = len(top_bets)
        label = f"TOP {n} POLYMARKET BET{'S' if n != 1 else ''}"
        print("\n" + "=" * 60)
        print(f"  {label}  (each $100 allocation)")
        print("=" * 60)
        for i, (edge_f, p) in enumerate(top_bets, 1):
            verdict  = p.get("llm_verdict", "?")
            question = p.get("question", "?")
            prob     = p.get("probability")
            odds_pct = round(prob * 100, 0) if prob is not None else None
            llm_prob = p.get("llm_probability", "")
            url      = p.get("url", "")
            odds_str = f"{odds_pct:.0f}%" if odds_pct is not None else "?"
            est_str  = f", est.prob {llm_prob:.0f}%" if isinstance(llm_prob, (int, float)) else ""
            sign     = "+" if edge_f >= 0 else ""
            print(f"  {i}. {verdict} on \"{question[:60]}\"")
            print(f"     odds {odds_str}{est_str}, edge {sign}{edge_f:.0f}pp — bet $100")
            if url:
                print(f"     {url}")
        print("=" * 60)
    else:
        print("\n  NO POLYMARKET BETS — no picks with edge > 5pp")
