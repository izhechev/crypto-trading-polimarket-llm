"""
Stock Scanner — multi-signal approach (Holly AI / TrendSpider style).

Signal layers (weighted):
  NEWS & SENTIMENT  40%  — Yahoo headlines + Reddit + Google News → Groq
  TECHNICAL         30%  — RSI, MACD, BB, MA50/200 crossover
  FUNDAMENTALS      20%  — P/E vs sector avg, earnings surprise, revenue growth
  MOMENTUM          10%  — 7d direction, volume spike

Sends top 10 to Groq for the final pick with entry/SL/TP.
"""
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config
from src.agents.technical_analyst import compute_ta
from src.utils.budget_tracker import log_llm_call, check_budget, BudgetExceededError

LOG_PATH = config.DATA_DIR / "stock_recommendations.csv"
_LOG_HEADERS = [
    "date", "symbol", "name", "entry_price", "stop_loss", "take_profit",
    "status", "exit_price", "pnl_pct", "current_price",
    "pe_ratio", "market_cap", "rsi_at_entry", "reasoning",
]


# ── CSV logger ────────────────────────────────────────────────────────────

def _ensure_log() -> None:
    if not LOG_PATH.exists():
        with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_LOG_HEADERS).writeheader()


def _get_last_closed(sym: str) -> dict | None:
    """Return the most recent closed (WIN/LOSS) row for this symbol, or None."""
    try:
        with open(LOG_PATH, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f)
                    if r.get("symbol","").upper() == sym.upper()
                    and r.get("status") in ("WIN", "LOSS")]
        return rows[-1] if rows else None
    except Exception:
        return None


def log_stock_results(top10: list[dict]) -> None:
    """Append top-10 stock picks as OPEN positions.
    Skips tickers already OPEN. Re-opens tickers that were previously closed as WIN/LOSS."""
    _ensure_log()
    existing_open: set[str] = set()
    try:
        with open(LOG_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status") == "OPEN":
                    existing_open.add(row.get("symbol", "").upper())
    except Exception:
        pass

    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    logged = 0
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_LOG_HEADERS, extrasaction="ignore")
        for r in top10:
            sym = r["symbol"].upper()
            if sym in existing_open:
                print(f"  Skipped {sym} — already OPEN")
                continue
            # Check if this symbol was previously closed — show re-open notice + 48h cooldown
            prev = _get_last_closed(sym)
            if prev:
                if prev.get("status") == "LOSS":
                    try:
                        last_dt = datetime.strptime(prev["date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                        hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                        if hours_since < 48:
                            print(f"  Cooldown — {sym} closed as LOSS {hours_since:.0f}h ago, skipping for 48h")
                            continue
                    except Exception:
                        pass
                prev_pnl = prev.get("pnl_pct", "")
                pnl_str  = f"+{prev_pnl}%" if prev_pnl and not str(prev_pnl).startswith("-") else f"{prev_pnl}%"
                print(f"  Re-opening {sym} — new position (prev: {prev.get('status','')} {pnl_str})")
            price = r.get("price", 0)
            writer.writerow({
                "date":         now,
                "symbol":       sym,
                "name":         r.get("name", ""),
                "entry_price":  round(price, 4),
                "stop_loss":    round(price * 0.92, 4),
                "take_profit":  round(price * 1.10, 4),
                "status":       "OPEN",
                "exit_price":   "",
                "pnl_pct":      "",
                "current_price": round(price, 4),
                "pe_ratio":     r.get("pe_ratio", ""),
                "market_cap":   r.get("market_cap", ""),
                "rsi_at_entry": round(r["rsi"], 1) if r.get("rsi") else "",
                "reasoning":    f"Score {r['score']:.1f}. " + ", ".join(r.get("reasons", [])),
            })
            logged += 1
    if logged:
        print(f"  {logged} new stock picks logged -> {LOG_PATH.name}")


def update_stock_positions() -> None:
    """Refresh prices for OPEN stock positions. Close WIN/LOSS based on SL/TP."""
    _ensure_log()
    rows: list[dict] = []
    try:
        with open(LOG_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return

    open_rows = [r for r in rows if r.get("status") == "OPEN"]
    if not open_rows:
        return

    try:
        import yfinance as yf
    except ImportError:
        return

    symbols    = list({r["symbol"] for r in open_rows})
    price_map: dict[str, float] = {}
    for sym in symbols:
        try:
            info  = yf.Ticker(sym).info
            price = info.get("currentPrice") or info.get("regularMarketPrice") or 0.0
            price_map[sym] = float(price)
        except Exception:
            pass

    closed = 0
    for row in rows:
        if row.get("status") != "OPEN":
            continue
        sym = row.get("symbol", "").upper()
        px  = price_map.get(sym)
        if px is None:
            continue
        try:
            entry = float(row["entry_price"])
            sl    = float(row["stop_loss"])
            tp    = float(row["take_profit"])
        except (ValueError, KeyError):
            continue

        pnl_pct              = (px - entry) / entry * 100
        row["current_price"] = round(px, 4)
        row["pnl_pct"]       = round(pnl_pct, 2)

        if px >= tp:
            row["status"]     = "WIN"
            row["exit_price"] = round(px, 4)
            closed += 1
        elif px <= sl:
            row["status"]     = "LOSS"
            row["exit_price"] = round(px, 4)
            closed += 1

    with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_LOG_HEADERS, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)

    if closed:
        print(f"  {closed} stock position(s) closed (WIN/LOSS)")


# ── Signal scoring ────────────────────────────────────────────────────────

def _score_news(headlines: list[str], reddit_titles: list[str]) -> tuple[float, str, list[str]]:
    """
    Naive sentiment on headlines (no Groq call here — saved for batch).
    Returns (raw_score -4..+4, label, matched_reasons).
    """
    _BULLISH = {
        "beat","surge","rally","upgrade","buy","bullish","breakout","record",
        "strong","growth","profit","revenue","acquisition","partnership","approval",
        "launch","positive","gain","rise","soar","outperform","dividend","buyback",
    }
    _BEARISH = {
        "miss","crash","downgrade","sell","bearish","lawsuit","sec","probe","fraud",
        "loss","decline","fall","concern","risk","warning","layoff","recall","ban",
        "investigation","fine","hack","breach","delay","shortfall","default",
    }
    all_titles = headlines + reddit_titles
    b = be = 0
    for t in all_titles:
        tl = t.lower()
        if any(w in tl for w in _BULLISH):
            b  += 1
        if any(w in tl for w in _BEARISH):
            be += 1

    total = len(all_titles) or 1
    net   = (b - be) / total   # -1..+1 normalised
    score = round(net * 4, 2)  # scale to -4..+4
    score = max(-4, min(4, score))

    if score >= 2:
        label = "STRONG BULLISH"
    elif score >= 0.5:
        label = "BULLISH"
    elif score <= -2:
        label = "STRONG BEARISH"
    elif score <= -0.5:
        label = "BEARISH"
    else:
        label = "NEUTRAL"

    reasons = []
    if b:
        reasons.append(f"{b} bullish headlines")
    if be:
        reasons.append(f"{be} bearish headlines")
    return score, label, reasons


def _score_ta(rsi, macd, bb, ma50_cross, ma200_cross) -> tuple[float, list[str]]:
    """Technical score -3..+3."""
    score   = 0.0
    reasons = []

    if rsi is not None:
        if rsi < 30:
            score += 2; reasons.append(f"RSI {rsi:.1f} oversold")
        elif rsi < 40:
            score += 1; reasons.append(f"RSI {rsi:.1f} near oversold")
        elif rsi > 70:
            score -= 1; reasons.append(f"RSI {rsi:.1f} overbought")

    if macd == "BULLISH":
        score += 1; reasons.append("MACD bullish crossover")
    elif macd == "BEARISH":
        score -= 1; reasons.append("MACD bearish")

    if bb == "BELOW_LOWER":
        score += 1; reasons.append("below lower Bollinger Band")
    elif bb == "ABOVE_UPPER":
        score -= 1; reasons.append("above upper BB (stretched)")

    if ma50_cross == "GOLDEN":
        score += 1; reasons.append("50d MA golden cross")
    elif ma50_cross == "DEATH":
        score -= 1; reasons.append("50d MA death cross")

    return max(-3, min(3, score)), reasons


def _score_fundamentals(pe_ratio, sector_pe_avg, revenue_growth, earnings_surprise) -> tuple[float, list[str]]:
    """Fundamental score -2..+2."""
    score   = 0.0
    reasons = []

    if pe_ratio and isinstance(pe_ratio, (int, float)) and sector_pe_avg:
        ratio = pe_ratio / sector_pe_avg
        if ratio < 0.7:
            score += 1.5; reasons.append(f"P/E {pe_ratio:.1f} well below sector avg {sector_pe_avg}")
        elif ratio < 0.9:
            score += 0.5; reasons.append(f"P/E {pe_ratio:.1f} below sector avg {sector_pe_avg}")
        elif ratio > 1.5:
            score -= 1.0; reasons.append(f"P/E {pe_ratio:.1f} expensive vs sector avg {sector_pe_avg}")

    if revenue_growth is not None:
        if revenue_growth > 0.20:
            score += 0.5; reasons.append(f"revenue growth {revenue_growth*100:.0f}%")
        elif revenue_growth < -0.10:
            score -= 0.5; reasons.append(f"revenue declining {revenue_growth*100:.0f}%")

    if earnings_surprise is not None:
        if earnings_surprise > 5:
            score += 1.0; reasons.append(f"earnings beat by {earnings_surprise:.0f}%")
        elif earnings_surprise < -5:
            score -= 1.0; reasons.append(f"earnings miss by {abs(earnings_surprise):.0f}%")

    return max(-2, min(2, score)), reasons


def _score_momentum(change_7d: float, last_volume: float, avg_volume: float) -> tuple[float, list[str]]:
    """Momentum score -1..+1."""
    score   = 0.0
    reasons = []

    if change_7d > 5:
        score += 0.5; reasons.append(f"7d momentum +{change_7d:.1f}%")
    elif change_7d > 0:
        score += 0.25
    elif change_7d < -15:
        score -= 0.5; reasons.append(f"7d decline {change_7d:.1f}%")

    if avg_volume > 0 and last_volume > avg_volume * 2:
        score += 0.5; reasons.append(f"volume spike {last_volume/avg_volume:.1f}x avg")

    return max(-1, min(1, score)), reasons


def _compute_ma_cross(ohlcv: list) -> str:
    """Return 'GOLDEN', 'DEATH', or 'NONE' based on 50d vs 200d MA."""
    if len(ohlcv) < 50:
        return "NONE"
    closes = [bar["close"] if isinstance(bar, dict) else bar[4] for bar in ohlcv]
    ma50  = sum(closes[-50:]) / 50
    ma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else sum(closes) / len(closes)
    prev_ma50  = sum(closes[-51:-1]) / 50 if len(closes) >= 51 else ma50
    prev_ma200 = ma200   # approximation

    if prev_ma50 < prev_ma200 and ma50 > ma200:
        return "GOLDEN"
    if prev_ma50 > prev_ma200 and ma50 < ma200:
        return "DEATH"
    return "NONE"


def _total_score(news_s, ta_s, fund_s, mom_s) -> float:
    """Weighted total: NEWS 40%, TA 30%, FUNDAMENTALS 20%, MOMENTUM 10%."""
    return round(news_s * 0.40 + ta_s * 0.30 + fund_s * 0.20 + mom_s * 0.10, 2)


# ── Reddit headline fetch ─────────────────────────────────────────────────

def _fetch_reddit_headlines(symbol: str, name: str) -> list[str]:
    """Fetch recent Reddit post titles for a stock from wsb and r/stocks."""
    try:
        from src.connectors.web_research import search_reddit
        query = f"{symbol} {name}".strip()
        wsb    = search_reddit(query, subreddit="wallstreetbets", limit=3)
        stocks = search_reddit(symbol,  subreddit="stocks",        limit=3)
        return [p["title"] for p in wsb + stocks if p.get("title")]
    except Exception:
        return []


# ── Main scanner ──────────────────────────────────────────────────────────

def run_stock_scanner() -> list[dict]:
    """
    Multi-signal stock scanner. Fetches data, computes 4 signal layers,
    returns top 10 by weighted score.
    """
    from src.connectors.stocks import STOCK_WATCHLIST, fetch_stock_data, market_session

    print("\n" + "=" * 60)
    print("  STOCK SCANNER — Multi-Signal (News / TA / Fundamentals / Momentum)")
    print("=" * 60)

    session = market_session()
    _SESSION_LABEL = {
        "OPEN":        "🟢 LIVE",
        "PRE_MARKET":  "🟡 PRE-MARKET (04:00–09:30 ET)",
        "AFTER_HOURS": "🟡 AFTER-HOURS (16:00–20:00 ET)",
        "CLOSED":      "🔴 CLOSED",
    }
    print(f"\n  NYSE: {_SESSION_LABEL.get(session, session)}")

    if session == "CLOSED":
        print("  Skipping new recommendations — market is closed.")
        print("  (Positions and track record updated below.)\n")
        return []

    if session in ("PRE_MARKET", "AFTER_HOURS"):
        label = "Pre-market" if session == "PRE_MARKET" else "After-hours"
        print(f"  {label} session — rankings will update at market open (09:30 ET).")
        print("  Prices unreliable. Skipping Groq analysis.\n")
        return []

    print(f"  Fetching data for {len(STOCK_WATCHLIST)} stocks…")

    stocks = fetch_stock_data(STOCK_WATCHLIST)
    if not stocks:
        print("  No stock data — is yfinance installed?")
        return []

    print(f"  Got {len(stocks)} stocks — scoring signals…")

    scored = []
    for s in stocks:
        ohlcv   = s.get("ohlcv", [])
        rsi = macd = bb = None
        trend = "NEUTRAL"
        if ohlcv and len(ohlcv) >= 14:
            try:
                ta   = compute_ta(s["symbol"], s["symbol"], ohlcv)
                rsi  = ta.rsi_14
                macd = ta.macd_signal
                bb   = ta.bollinger_position
                trend = ta.trend
            except Exception:
                pass

        ma_cross = _compute_ma_cross(ohlcv)
        last_vol = float(ohlcv[-1]["volume"] if isinstance(ohlcv[-1], dict) else ohlcv[-1][5]) if ohlcv else 0.0

        # Fetch Reddit headlines (quick — 2 subreddits, limit 3 each)
        reddit_titles = _fetch_reddit_headlines(s["symbol"], s.get("name", ""))

        # Layer scores
        news_s, news_label, news_reasons = _score_news(s.get("headlines", []), reddit_titles)
        ta_s,   ta_reasons               = _score_ta(rsi, macd, bb, ma_cross, None)
        fund_s, fund_reasons             = _score_fundamentals(
            s.get("pe_ratio"), s.get("sector_pe_avg"), s.get("revenue_growth"), s.get("earnings_surprise")
        )
        mom_s, mom_reasons = _score_momentum(s.get("change_7d", 0), last_vol, s.get("avg_volume", 0))

        total = _total_score(news_s, ta_s, fund_s, mom_s)
        all_reasons = news_reasons + ta_reasons + fund_reasons + mom_reasons

        s.update({
            "rsi": rsi, "macd": macd, "bb_pos": bb, "trend": trend,
            "ma_cross": ma_cross,
            "news_score": news_s, "news_label": news_label,
            "ta_score": ta_s, "fund_score": fund_s, "mom_score": mom_s,
            "score":   total,
            "reasons": all_reasons,
            "reddit_titles": reddit_titles,
        })
        scored.append(s)

    scored.sort(key=lambda x: x["score"], reverse=True)
    top10 = scored[:10]

    print(f"\n  TOP 10 STOCK OPPORTUNITIES\n" + "-" * 60)
    for rank, s in enumerate(top10, 1):
        pe_str  = f"P/E {s['pe_ratio']:.1f}" if s.get("pe_ratio") else "P/E N/A"
        rsi_str = f"RSI {s['rsi']:.1f}"       if s.get("rsi")      else "RSI N/A"
        cap_str = f"${s['market_cap']/1e9:.1f}B" if s.get("market_cap") else ""
        print(f"\n  {rank}. {s['symbol']} ({s['name']})  —  score: {s['score']:.2f}")
        print(f"     Price: ${s['price']:,.2f}  |  24h: {s['change_24h']:+.1f}%  |  7d: {s['change_7d']:+.1f}%  |  {cap_str}")
        print(f"     {rsi_str}  |  MACD: {s['macd']}  |  BB: {s['bb_pos']}  |  MA: {s['ma_cross']}  |  {pe_str}")
        print(f"     News: {s['news_label']} ({s['news_score']:+.1f}) | TA: {s['ta_score']:+.1f} | Fund: {s['fund_score']:+.1f} | Mom: {s['mom_score']:+.1f}")
        print(f"     Signals: {', '.join(s['reasons'][:5]) if s['reasons'] else 'none'}")

    return top10


# ── Groq analysis ─────────────────────────────────────────────────────────

def analyze_stocks_with_groq(top10: list[dict]) -> list[dict]:
    """
    Send top 10 multi-signal stocks to Groq with pre-fetched web+Reddit headlines.
    Returns ranked list of all 10 with BUY/WATCH/SKIP verdicts (not just #1).
    """
    if not config.GROQ_API_KEY:
        print("  STOCK GROQ: GROQ_API_KEY not set — skipping")
        return []

    try:
        check_budget("groq")
    except BudgetExceededError as e:
        print(f"  STOCK GROQ: {e}")
        return []

    try:
        from groq import Groq
    except ImportError:
        print("  ERROR: groq not installed.")
        return []

    client = Groq(api_key=config.GROQ_API_KEY)

    # Pre-fetch fresh Google News + Reddit for each stock before Groq.
    # Reddit titles from the scanner (WSB + r/stocks) are already in each stock dict,
    # but we add fresh Google News + r/investing here for a stronger signal.
    print("  Fetching Google News + Reddit for all 10 stocks...")
    try:
        from src.connectors.web_research import search_google_news, search_reddit
        for s in top10:
            sym  = s["symbol"].upper()
            name = s.get("name", "")
            # Build match tokens for headline validation (ticker + key words from company name)
            name_words = [w.lower() for w in name.split() if len(w) > 3]
            match_tokens = {sym.lower()} | set(name_words[:3])

            def _is_relevant(headline: str) -> bool:
                """Return True only if headline actually mentions this stock."""
                hl = headline.lower()
                return any(tok in hl for tok in match_tokens)

            # Google News — search ticker + full company name together for precision
            gn   = search_google_news(f'"{sym}" {name}', limit=5)
            gn_hl = [n["title"] for n in gn if n.get("title") and _is_relevant(n["title"])]

            # Reddit — r/investing
            r_inv = search_reddit(f"{sym} {name}", subreddit="investing", limit=3)
            r_hl  = [p["title"] for p in r_inv if p.get("title") and _is_relevant(p["title"])]

            # Filter existing scanner headlines (yfinance / WSB) for relevance too
            existing_raw = s.get("headlines", []) + s.get("reddit_titles", [])
            existing = [h for h in existing_raw if _is_relevant(h)]

            # Merge: Google News first (freshest), then r/investing, then scanner
            all_hl = gn_hl + r_hl + existing
            seen: set = set()
            deduped: list = []
            for h in all_hl:
                if h not in seen:
                    seen.add(h)
                    deduped.append(h)
            s["headlines"] = deduped
            if not deduped:
                print(f"  ⚠️  {sym}: no relevant headlines found after validation")
    except Exception as e:
        print(f"  Web+Reddit pre-fetch failed: {e}")

    # Build enriched prompt — headlines are the primary signal
    lines = []
    for i, s in enumerate(top10, 1):
        pe_str  = f"{s['pe_ratio']:.1f}"         if s.get("pe_ratio")      else "N/A"
        rsi_str = f"{s['rsi']:.1f}"              if s.get("rsi")           else "N/A"
        cap_str = f"${s['market_cap']/1e9:.1f}B" if s.get("market_cap")    else "N/A"
        eg_str  = f"{s['earnings_surprise']:+.0f}%" if s.get("earnings_surprise") is not None else "N/A"
        rv_str  = f"{s['revenue_growth']*100:.0f}%" if s.get("revenue_growth") is not None else "N/A"

        # headlines now contains merged Google+Reddit from pre-fetch; reddit_titles are
        # the scanner's WSB/r/stocks posts fetched during scoring — combine without dupe
        web_hl    = s.get("headlines", [])           # Google News + r/investing (pre-fetch)
        reddit_hl = s.get("reddit_titles", [])       # WSB + r/stocks (scanner)
        seen_hl: set = set(web_hl)
        extra_reddit = [h for h in reddit_hl if h not in seen_hl]
        all_hl = web_hl + extra_reddit

        if all_hl:
            hl_block = "\n".join(f"     • {h[:100]}" for h in all_hl[:8])
        else:
            hl_block = "     (no web/reddit headlines found — rely on TA+fundamentals)"

        lines.append(
            f"{i}. {s['symbol']} ({s['name']}) | TOTAL={s['score']:+.2f}\n"
            f"   News={s['news_score']:+.1f}({s['news_label']}) TA={s['ta_score']:+.1f} "
            f"Fund={s['fund_score']:+.1f} Mom={s['mom_score']:+.1f}\n"
            f"   price=${s['price']:,.2f} | 24h={s['change_24h']:+.1f}% | 7d={s['change_7d']:+.1f}%\n"
            f"   RSI={rsi_str} | MACD={s['macd']} | BB={s['bb_pos']} | MA={s['ma_cross']}\n"
            f"   P/E={pe_str} vs sector {s.get('sector_pe_avg','N/A')} | "
            f"EarningsSurprise={eg_str} | RevGrowth={rv_str} | MCap={cap_str}\n"
            f"   Web+Reddit headlines (Google News, r/wallstreetbets, r/stocks, r/investing):\n{hl_block}"
        )

    from src.connectors.stocks import market_session
    session = market_session()
    session_note = ""
    if session == "PRE_MARKET":
        session_note = "\n⚠️ PRE-MARKET SESSION — prices are indicative only. Mark all verdicts as WATCH, not BUY. Real entries only at market open.\n"
    elif session == "AFTER_HOURS":
        session_note = "\n⚠️ AFTER-HOURS SESSION — prices unreliable due to low liquidity. Mark all verdicts as WATCH, not BUY.\n"

    prompt = (
        "You are a SHORT-TERM stock trader. Timeframe: 3–14 days maximum. "
        "Rank ALL 10 stocks below from best to worst short-term trade opportunity.\n\n"
        f"{session_note}"
        "You do NOT care about long-term fundamentals or analyst price targets. "
        "You care about: catalysts in the next 1–14 days, momentum, oversold bounces, and volume.\n\n"
        "Signal weights: CATALYST/NEWS 40% | TECHNICALS 30% | MOMENTUM 20% | FUNDAMENTALS 10%\n"
        "The catalyst (news headline) is the #1 signal. No catalyst = lower rank.\n\n"
        "TAKE-PROFIT / STOP-LOSS RULES (HARD CAPS — do not exceed):\n"
        "  • Take-profit: MAX entry × 1.10 (+10%) — system will cap any higher value\n"
        "  • Stop-loss: MIN entry × 0.92 (-8%) — system will floor any tighter value\n"
        "  • Never set TP above nearest major resistance\n"
        "  • In fear markets (F&G <30): reduce TP by 30% — short rallies in bear markets\n"
        "  BAD: stock at $850, TP $1020 (+20%) — exceeds hard cap\n"
        "  GOOD: stock at $850, TP $935 (+10%), SL $782 (-8%) — within rules\n\n"
        "Give BUY / WATCH / SKIP verdict with entry, stop-loss, take-profit, and the key catalyst.\n\n"
        'Return JSON: {"picks": [\n'
        '  {"rank":1,"stock":"SYM","verdict":"BUY","confidence":85,\n'
        '   "entry_price":150.00,"stop_loss":138.00,"take_profit":165.00,\n'
        '   "catalyst":"key headline or reason in 10 words","reasoning":"1-2 sentences"},\n'
        '  ...\n'
        ']}\n\n'
        "Stocks:\n\n" + "\n\n".join(lines)
    )

    try:
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.10,
            response_format={"type": "json_object"},
        )
        log_llm_call("groq", tokens_in=len(prompt) // 4, tokens_out=2000, endpoint="stock_analyst")
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        print(f"  STOCK GROQ ERROR: {e}")
        return []

    try:
        parsed = json.loads(raw)
        picks  = parsed.get("picks", [])
    except json.JSONDecodeError:
        print(f"  STOCK GROQ: JSON parse error:\n{raw}")
        return []

    if not picks:
        print("  STOCK GROQ: empty picks list returned")
        return []

    # Fill in prices from top10 where Groq left them null/zero
    # Also cap TP at +10% and floor SL at -8% (caps override Groq's values)
    sym_map = {s["symbol"].upper(): s for s in top10}
    for p in picks:
        sym  = (p.get("stock") or "").upper()
        data = sym_map.get(sym, {})
        price = data.get("price", 0)
        if price and not p.get("entry_price"):
            p["entry_price"] = price
            p["stop_loss"]   = round(price * 0.92, 4)
            p["take_profit"] = round(price * 1.10, 4)
        elif price and p.get("entry_price"):
            ep = p["entry_price"]
            # Cap Groq TP at entry × 1.10; enforce SL floor at entry × 0.92
            if p.get("take_profit") and p["take_profit"] > ep * 1.10:
                p["take_profit"] = round(ep * 1.10, 4)
            if p.get("stop_loss") and p["stop_loss"] < ep * 0.92:
                p["stop_loss"] = round(ep * 0.92, 4)

    # Print ranked list
    print("\n" + "=" * 60)
    print("  STOCK LLM RANKINGS — TOP 10")
    print("=" * 60)
    for p in picks:
        rank    = p.get("rank", "?")
        stock   = p.get("stock", "?")
        verdict = p.get("verdict", "?")
        conf    = p.get("confidence", 0)
        icon    = {"BUY": "BUY", "WATCH": "WATCH", "SKIP": "SKIP"}.get(verdict, "?")
        ep      = p.get("entry_price") or 0
        sl      = p.get("stop_loss") or 0
        tp      = p.get("take_profit") or 0
        cat     = p.get("catalyst", "")
        reason  = p.get("reasoning", "")
        print(f"\n  {rank}. [{icon}] {stock} — {conf}% confidence")
        if verdict == "BUY" and ep:
            print(f"     Entry: ${ep:,.2f}  SL: ${sl:,.2f}  TP: ${tp:,.2f}")
        if cat:
            print(f"     Catalyst: {cat[:90]}")
        if reason:
            print(f"     Reason: {reason[:110]}")
    print("=" * 60)

    # ── TOP 3 STOCK BUYS summary ──────────────────────────────────────────
    top_buys = [p for p in picks if p.get("verdict") == "BUY"][:3]
    n = len(top_buys)
    if n == 0:
        print("\n  NO STOCK BUY — no qualifying picks")
    else:
        label = f"TOP {n} STOCK BUY{'S' if n != 1 else ''}"
        print("\n" + "=" * 60)
        print(f"  {label}  (each $100 allocation)")
        print("=" * 60)
        for i, p in enumerate(top_buys, 1):
            sym  = p.get("stock", "?")
            conf = p.get("confidence", 0)
            ep   = p.get("entry_price") or 0
            sl   = p.get("stop_loss") or 0
            tp   = p.get("take_profit") or 0
            cat  = p.get("catalyst", "")
            icon = "🟢" if conf >= 75 else "🟡" if conf >= 50 else "🔴"
            print(f"  {i}. {sym:<8s} {icon} {conf}% confidence"
                  f"  — Entry ${ep:,.2f}, SL ${sl:,.2f}, TP ${tp:,.2f}")
            if cat:
                print(f"     Catalyst: {cat[:90]}")
        print("=" * 60)

    # Web research validation for #1 BUY pick only
    top_buy = next((p for p in picks if p.get("verdict") == "BUY"), None)
    if top_buy:
        ticker     = (top_buy.get("stock") or "").upper()
        name_match = sym_map.get(ticker, {}).get("name", "")
        print(f"\n  Running web research validation for #{top_buy.get('rank',1)} pick: {ticker}...")
        try:
            from src.connectors.web_research import research_stock, format_research_for_prompt, print_research
            research      = research_stock(ticker, name_match)
            print_research(research, ticker)
            research_text = format_research_for_prompt(research, ticker)

            if research_text:
                val_prompt = (
                    f"You ranked {ticker} ({name_match}) as the top BUY. Full web research:\n\n"
                    f"{research_text}\n\n"
                    "CONFIRM or CHANGE? Red flags = lawsuit, earnings miss, layoffs, fraud, hack.\n"
                    'Valid JSON only: {"verdict":"CONFIRM","new_stock":null,"web_summary":"1 sentence"}'
                )
                try:
                    check_budget("groq")
                    vresp = client.chat.completions.create(
                        model=config.LLM_MODEL,
                        messages=[{"role": "user", "content": val_prompt}],
                        max_tokens=200,
                        temperature=0.10,
                        response_format={"type": "json_object"},
                    )
                    log_llm_call("groq", tokens_in=len(val_prompt)//4, tokens_out=200, endpoint="stock_web_validation")
                    vmatch = re.search(r'\{.*\}', vresp.choices[0].message.content.strip(), re.DOTALL)
                    if vmatch:
                        vj      = json.loads(vmatch.group())
                        verdict = vj.get("verdict", "?")
                        new_sym = (vj.get("new_stock") or "").upper()
                        top_buy["web_research_verdict"] = verdict
                        top_buy["web_research_summary"] = vj.get("web_summary", "")
                        if verdict == "CHANGE" and new_sym and new_sym != ticker:
                            print(f"  WEB VALIDATION: CHANGE — downgrading {ticker}, promoting {new_sym}")
                            top_buy["verdict"] = "WATCH"
                            # Find the new_sym in picks and upgrade it
                            alt = next((p for p in picks if p.get("stock","").upper()==new_sym), None)
                            if alt:
                                alt["verdict"] = "BUY"
                                alt["web_research_verdict"] = "PROMOTED"
                        else:
                            print(f"  WEB VALIDATION: {verdict} ✅")
                except (BudgetExceededError, Exception) as ex:
                    print(f"  Web validation error: {ex}")
        except Exception as e:
            print(f"  Web research failed: {e}")

    return picks


# ── News-only check for open positions ────────────────────────────────────

def check_open_positions_news() -> list[dict]:
    """
    Fetch breaking news for open stock positions only.
    Returns list of alerts: {symbol, headline, is_breaking}.
    """
    _ensure_log()
    rows: list[dict] = []
    try:
        with open(LOG_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return []

    open_symbols = [r["symbol"] for r in rows if r.get("status") == "OPEN"]
    if not open_symbols:
        print("  No open stock positions to monitor.")
        return []

    try:
        import yfinance as yf
        from src.connectors.web_research import search_google_news
    except ImportError:
        return []

    _BREAKING_KEYWORDS = {
        "earnings", "beats", "misses", "merger", "acquisition", "buyout",
        "lawsuit", "sec", "investigation", "fda", "approval", "layoff",
        "hack", "breach", "guidance", "downgrade", "upgrade", "recall",
    }

    alerts: list[dict] = []
    print(f"\n  Checking news for {len(open_symbols)} open positions: {', '.join(open_symbols)}")

    for sym in open_symbols:
        headlines: list[str] = []
        try:
            news_items = yf.Ticker(sym).news or []
            headlines += [item.get("title", "") for item in news_items[:5] if item.get("title")]
        except Exception:
            pass

        google = search_google_news(f"{sym} stock", limit=3)
        headlines += [n["title"] for n in google if n.get("title")]

        for hl in headlines:
            hl_lower = hl.lower()
            if any(kw in hl_lower for kw in _BREAKING_KEYWORDS):
                alerts.append({"symbol": sym, "headline": hl, "is_breaking": True})
                print(f"  ⚡ {sym}: {hl[:90]}")
                break
        else:
            if headlines:
                print(f"  {sym}: no breaking news ({len(headlines)} headlines scanned)")

    return alerts


def print_stock_track_record() -> None:
    """Print OPEN/WIN/LOSS summary for stock picks."""
    _ensure_log()
    rows: list[dict] = []
    try:
        with open(LOG_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return
    if not rows:
        return

    n_open  = sum(1 for r in rows if r.get("status") == "OPEN")
    n_win   = sum(1 for r in rows if r.get("status") == "WIN")
    n_loss  = sum(1 for r in rows if r.get("status") == "LOSS")
    closed  = n_win + n_loss
    wr      = n_win / closed * 100 if closed else 0

    pnls = []
    for r in rows:
        if r.get("status") in ("WIN", "LOSS"):
            try:
                pnls.append(float(r["pnl_pct"]))
            except (ValueError, KeyError):
                pass
    avg_pnl = sum(pnls) / len(pnls) if pnls else 0

    print(f"\n  STOCK PICKS  ({len(rows)} total)")
    print(f"  Open: {n_open}  Win: {n_win}  Loss: {n_loss}  Win Rate: {wr:.0f}%  Avg P&L: {avg_pnl:+.1f}%")

    open_rows = [r for r in rows if r.get("status") == "OPEN"]
    if open_rows:
        print("\n  OPEN STOCK POSITIONS:")
        for r in open_rows:
            try:
                pnl  = float(r.get("pnl_pct") or 0)
                icon = "+" if pnl >= 0 else "-"
                print(f"    [{icon}] {r['symbol']:8s}  entry ${float(r['entry_price']):,.2f}"
                      f"  now ${float(r['current_price']):,.2f}  ({pnl:+.1f}%)  — {r['date'][:16]}")
            except (ValueError, KeyError):
                pass
