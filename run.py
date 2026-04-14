#!/usr/bin/env python3
"""CryptoAdvisor — Phase 1 MVP.

Usage:
  python run.py                              # prices, F&G, TA, portfolio
  python run.py --scan                       # crypto + stocks + Polymarket (full)
  python run.py --crypto                     # crypto scanner only
  python run.py --crypto --exchange kraken   # crypto, Kraken-listed coins only
  python run.py --crypto --exchange revolut  # crypto, Revolut X only
  python run.py --crypto --exchange binance  # crypto, Binance only
  python run.py --crypto --exchange both     # crypto, Kraken + Revolut X
  python run.py --crypto --exchange all      # crypto, all exchanges
  python run.py --polymarket                 # Polymarket advisor only
  python run.py --stocks                     # stock scanner only
  python run.py --news                       # quick news check for open stock positions
  python run.py --schedule                   # run every 4 h (Groq + Telegram)
  python run.py --schedule --exchange kraken
  python run.py --scan --debate              # full scan + Bull/Bear debate
"""
import argparse
import json
import sys
from datetime import datetime, timezone

# Force UTF-8 output on Windows (avoids cp1252 UnicodeEncodeError for arrows/emoji)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import WATCHLIST, WATCHLIST_SYMBOLS, PORTFOLIO_PATH
from src.connectors.coingecko import fetch_prices, fetch_ohlcv, fetch_fear_greed
from src.agents.technical_analyst import compute_ta


def load_portfolio() -> dict:
    with open(PORTFOLIO_PATH) as f:
        return json.load(f)


_HEADER_ICONS = {
    "PRICES":      "💰",
    "FEAR":        "😨",
    "TECHNICAL":   "📐",
    "PORTFOLIO":   "💼",
    "SCANNER":     "🔍",
    "GROQ":        "🤖",
    "ENRICHMENT":  "📡",
    "SENTIMENT":   "🧠",
    "TRACK":       "🏆",
    "STOCK":       "📊",
    "POLYMARKET":  "🔮",
    "LLM":         "💸",
    "BREAKING":    "⚡",
    "DEBATE":      "🥊",
    "WHALE":       "🐋",
}


def print_header(text: str):
    icon = next((v for k, v in _HEADER_ICONS.items() if k in text.upper()), "▸")
    print(f"\n{'─'*62}")
    print(f"  {icon}  {text}")
    print(f"{'─'*62}")


def run_price_check():
    print_header("PRICES")
    prices = fetch_prices(WATCHLIST)
    for p in prices:
        icon = "🟢" if p.change_24h > 0 else "🔴" if p.change_24h < 0 else "⚪"
        decimals = 2 if p.price_eur >= 1 else 4 if p.price_eur >= 0.01 else 6 if p.price_eur >= 0.0001 else 8
        eur_str = f"€{p.price_eur:.{decimals}f}"
        usd_str = f"${p.price_usd:.{decimals}f}"
        print(f"  {icon} {p.symbol:8s}  {eur_str:>14s}  ({usd_str:>14s})  {p.change_24h:+.1f}% 24h  {p.change_7d:+.1f}% 7d  MCap €{p.market_cap/1e6:.0f}M")
    return {p.coin_id: p for p in prices}


def run_fear_greed():
    print_header("FEAR & GREED INDEX")
    fg = fetch_fear_greed()
    val = fg["value"]
    icon = "😱" if val < 20 else "😨" if val < 40 else "😐" if val < 60 else "😊" if val < 80 else "🤑"
    bar_len = val // 2
    bar = "█" * bar_len + "░" * (50 - bar_len)
    print(f"  {icon}  [{bar}] {val}/100 — {fg['label']}")
    return fg


def run_technical_analysis(prices: dict):
    print_header("TECHNICAL ANALYSIS")
    results = {}
    for coin_id in WATCHLIST:
        symbol = WATCHLIST_SYMBOLS.get(coin_id, coin_id)
        try:
            ohlcv = fetch_ohlcv(coin_id, days=30)
            ta = compute_ta(coin_id, symbol, ohlcv)
            results[coin_id] = ta
            trend_icon = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}
            rsi_str = f"  RSI {ta.rsi_14:.1f}" if ta.rsi_14 else ""
            macd_str = f"  MACD: {ta.macd_signal}" if ta.macd_signal else ""
            print(
                f"\n  {trend_icon.get(ta.trend,'?')} {symbol:8s}  {ta.trend}  ({ta.confidence:.0%})"
                f"  💲{ta.price:.4f}{rsi_str}{macd_str}"
            )
            if ta.support_levels:
                print(f"     📉 Support:    {', '.join(f'${s:.4f}' for s in ta.support_levels[:3])}")
            if ta.resistance_levels:
                print(f"     📈 Resistance: {', '.join(f'${r:.4f}' for r in ta.resistance_levels[:3])}")
            print(f"     💬 {ta.key_observation}")
        except Exception as e:
            print(f"\n  ❌ {symbol} — {e}")
    return results


def run_portfolio_check(prices: dict):
    print_header("PORTFOLIO")
    portfolio = load_portfolio()
    total_eur = total_cost_eur = 0.0
    DUST = 0.10
    for h in portfolio["holdings"]:
        p = prices.get(h["coin_id"])
        if not p:
            continue
        amt = h["amount"]
        eur_value = amt * p.price_eur
        usd_value = amt * p.price_usd
        cost_usd  = amt * h["entry_price_usd"]
        # Estimate EUR entry cost using current EUR/USD rate
        rate      = p.price_eur / p.price_usd if p.price_usd else 0.92
        cost_eur  = cost_usd * rate
        pnl_pct   = (p.price_usd - h["entry_price_usd"]) / h["entry_price_usd"] * 100

        if eur_value < DUST:
            print(f"  [dust] {h['asset']:8s}  €{p.price_eur:.4f}  value €{eur_value:.4f}")
            continue

        total_eur      += eur_value
        total_cost_eur += cost_eur
        icon = "🟢" if pnl_pct >= 0 else "🔴"
        decimals = 2 if p.price_eur >= 1 else 4 if p.price_eur >= 0.01 else 6 if p.price_eur >= 0.0001 else 8
        print(
            f"  {icon} {h['asset']:8s} {amt:>6.1f} × €{p.price_eur:.{decimals}f}"
            f" = €{eur_value:>8.2f}  (entry ${h['entry_price_usd']:.4f}, P&L: {pnl_pct:+.1f}%)"
        )
    total_pnl_eur = total_eur - total_cost_eur
    total_pnl_pct = (total_pnl_eur / total_cost_eur) * 100 if total_cost_eur else 0
    icon = "🟢" if total_pnl_eur >= 0 else "🔴"
    print(f"\n  {icon} TOTAL: €{total_eur:.2f} / invested ≈€{total_cost_eur:.2f} / P&L: €{total_pnl_eur:+.2f} ({total_pnl_pct:+.1f}%)")


# ── Whale ride check (every 5 min) ───────────────────────────────────────────

# Cache Kraken symbols for 1 hour — Kraken list changes rarely, no need to refetch every 5 min
_kraken_symbols_cache: set[str] = set()
_kraken_symbols_ts: float = 0.0
_KRAKEN_CACHE_TTL = 3600  # seconds


def _get_kraken_symbols_cached() -> set[str]:
    """Return Kraken tradeable symbols, refreshing at most once per hour."""
    import time as _time
    global _kraken_symbols_cache, _kraken_symbols_ts
    if _kraken_symbols_cache and (_time.time() - _kraken_symbols_ts) < _KRAKEN_CACHE_TTL:
        return _kraken_symbols_cache
    try:
        from src.agents.scanner import _get_kraken_symbols
        syms = _get_kraken_symbols()
        if syms:
            _kraken_symbols_cache = syms
            _kraken_symbols_ts    = _time.time()
            print(f"  Kraken symbols refreshed: {len(syms)} tradeable assets")
    except Exception as e:
        print(f"  ⚠️  Kraken symbols fetch failed: {e}")
    return _kraken_symbols_cache


def _fetch_coins_whale(pages: int = 3) -> list[dict]:
    """
    Fetch up to pages×250 coins from CoinGecko for whale detection.
    pages=3 → 750 coins.  pages=4 → 1,000 coins.
    Paces requests at 1 s apart to stay within rate limits.
    """
    import time as _time
    import httpx
    import config as _cfg

    all_coins: list[dict] = []
    headers = {}
    if _cfg.COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = _cfg.COINGECKO_API_KEY

    for page in range(1, pages + 1):
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(
                    "https://api.coingecko.com/api/v3/coins/markets",
                    params={
                        "vs_currency":            "usd",
                        "order":                  "market_cap_desc",
                        "per_page":               250,
                        "page":                   page,
                        "price_change_percentage": "24h,7d",
                        "sparkline":              "false",
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                all_coins.extend(batch)
        except Exception as e:
            print(f"  ⚠️  CoinGecko page {page} failed: {e}")
            break
        if page < pages:
            _time.sleep(1.2)   # pace: ~0.8 req/s, well within 30 req/min limit

    return all_coins


def run_whale_check() -> None:
    """
    Lightweight 5-minute whale ride detector.
    Fetches top 1000 coins (4 pages), filters to Kraken-listed only,
    runs volume-anomaly detection, sends Telegram immediately on signal.
    No Groq, no full TA — fastest possible whale catch.
    """
    try:
        from src.agents.coin_risk_assessor import assess_coin_risks
        from src.agents.whale_rider import (
            detect_whale_rides,
            send_whale_ride_alerts,
            check_exit_signals,
            display_late_stage,
            update_volume_history,
        )
        from src.connectors.coingecko import fetch_fear_greed

        print(f"\n  🐋  Whale check — {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

        # Fetch top 1000 coins (4 pages × 250)
        coins = _fetch_coins_whale(pages=4)
        if not coins:
            print("  ⚠️  No coins fetched — skipping whale check")
            return

        # Filter to Kraken-listed coins only
        kraken_syms = _get_kraken_symbols_cached()
        if kraken_syms:
            before = len(coins)
            coins = [c for c in coins if c.get("symbol", "").upper() in kraken_syms]
            print(f"  {before} coins → {len(coins)} Kraken-listed (filtered)")
        else:
            print(f"  {len(coins)} coins (no Kraken filter — symbol list unavailable)")

        fg       = fetch_fear_greed()
        vol_hist = update_volume_history(coins)
        risk_map = assess_coin_risks(coins, fear_greed=fg)

        candidates = detect_whale_rides(coins, risk_map, vol_history=vol_hist)
        if candidates:
            syms_str = ", ".join(
                f"{c['symbol']} ({c['stage']} {c['change_24h']:+.1f}%)" for c in candidates
            )
            print(f"  🐋 {len(candidates)} whale ride candidate(s): {syms_str}")
            send_whale_ride_alerts(candidates, fg)
        else:
            print("  No whale ride signals this cycle")

        check_exit_signals(coins)
        display_late_stage(coins)

    except Exception as e:
        print(f"  ⚠️  Whale check failed: {e}")


# ── Scan cycle (used by --scan and --schedule) ─────────────────────────────

def run_scan_cycle(
    exchange: str | None = None,
    debate: bool = False,
    run_stocks: bool = True,
    run_polymarket: bool = True,
) -> None:
    """One full scan → Groq → log → Telegram cycle."""
    from src.agents.scanner import run_smart_scanner
    from src.agents.groq_analyst import analyze_with_groq
    from src.utils.logger import (
        log_scanner_results, update_scanner_sltp, update_open_positions,
        log_portfolio_positions, log_watchlist_prices,
        log_price_history, print_track_record, print_daily_activity,
    )
    from src.utils.enrichment import fetch_enrichment
    from src.utils.telegram import send_telegram

    print(f"\n  🕐  Started at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    # Update any open scanner positions before scanning
    print(f"  🔄  Updating open positions...")
    update_open_positions()

    # ── Portfolio management alerts ───────────────────────────────────────
    _open_count = 0
    try:
        import csv as _csv_pm
        import config as _cfg_pm
        _rec_pm = _cfg_pm.DATA_DIR / "recommendations.csv"
        if _rec_pm.exists():
            with open(_rec_pm, newline="", encoding="utf-8") as _f_pm:
                _pm_all = list(_csv_pm.DictReader(_f_pm))
            _open_rows = [r for r in _pm_all if r.get("status") == "OPEN"]
            # Count only actual bot positions (not PORTFOLIO tracking / WATCHLIST rows)
            _open_count = sum(
                1 for r in _open_rows
                if r.get("type", "SCANNER") in ("SCANNER", "", "WHALE_RIDE")
            )
            # current_price column is already updated by update_open_positions() above
            _sym_price_pm: dict[str, float] = {}
            for _r in _open_rows:
                _s = _r.get("coin", "").upper()
                try:
                    _sym_price_pm[_s] = float(_r.get("current_price") or 0)
                except (ValueError, TypeError):
                    pass
            from src.agents.scanner import _get_open_positions as _gop_pm
            _pm_positions = _gop_pm(_sym_price_pm)
            _tp_near   = [p for p in _pm_positions if p.get("is_approaching_tp")]
            _crit_loss = [p for p in _pm_positions if p.get("is_critical_loss")]
            _stale_pm  = [p for p in _pm_positions if p["is_stale"]]
            # Count: profitable = pnl > 0%; total = all open
            _profitable_count = sum(
                1 for p in _pm_positions if (p.get("pnl_pct") or 0) > 0
            )
            # Console summary
            if _tp_near or _crit_loss or _stale_pm or _open_count > 0:
                print(
                    f"  📊  Portfolio: {_profitable_count} profitable / {_open_count} total open | "
                    f"TP≥10%: {len(_tp_near)} | "
                    f"Loss≤-10%: {len(_crit_loss)} | "
                    f"Stale: {len(_stale_pm)}"
                )
            # Telegram alerts
            _pm_msgs: list[str] = []
            for _p in _tp_near:
                _pnl_s = f"{_p['pnl_pct']:+.1f}%" if _p["pnl_pct"] is not None else "N/A"
                _pm_msgs.append(
                    f"🎯 <b>APPROACHING TP — {_p['symbol']}</b>\n"
                    f"  PnL: {_pnl_s}  |  Age: {_p['age_days']}d\n"
                    f"  Entry: ${_p['entry']:.4f}  |  TP: ${_p['tp']:.4f}\n"
                    f"  👉 Consider taking profit soon"
                )
            for _p in _crit_loss:
                _pnl_s = f"{_p['pnl_pct']:+.1f}%" if _p["pnl_pct"] is not None else "N/A"
                _cur_pm = _sym_price_pm.get(_p["symbol"], 0)
                _sl_dist = (
                    f"  SL dist: {(_cur_pm - _p['sl']) / _cur_pm * 100:+.1f}%"
                    if _cur_pm > 0 and _p["sl"] > 0 else ""
                )
                _pm_msgs.append(
                    f"🔴 <b>CRITICAL LOSS — {_p['symbol']}</b>\n"
                    f"  PnL: {_pnl_s}  |  Age: {_p['age_days']}d\n"
                    f"  Entry: ${_p['entry']:.4f}  |  SL: ${_p['sl']:.4f}{_sl_dist}\n"
                    f"  ⚠️ Monitor closely — approaching stop loss"
                )
            if _stale_pm:
                _stale_lines = "\n".join(
                    f"  ⏰ {_p['symbol']:8s}  {(_p['pnl_pct'] or 0.0):+.1f}%  ({_p['age_days']}d)"
                    for _p in _stale_pm
                )
                _pm_msgs.append(
                    f"⏳ <b>STALE POSITIONS — FORCE CLOSE</b>\n{_stale_lines}\n"
                    f"  Reason: ≥7d held with &lt;+3% PnL — TIME EXIT"
                )
            # DATA COLLECTION MODE: no position cap — omit FULL / NEAR FULL alerts
            for _msg in _pm_msgs:
                try:
                    send_telegram(_msg)
                except Exception:
                    pass
    except Exception as _e_pm:
        print(f"  ⚠️  Portfolio alert check failed: {_e_pm}")

    # Log current portfolio holdings with live prices (Kraken → portfolio.json fallback)
    print(f"  💼  Logging portfolio positions...")
    log_portfolio_positions()

    # Log watchlist coin prices
    print(f"  👀  Logging watchlist prices...")
    log_watchlist_prices()

    # Log price history for all tracked coins
    print(f"  📜  Logging price history...")
    log_price_history()

    # Fear & Greed
    fg = fetch_fear_greed()
    print_header("FEAR & GREED INDEX")
    bar_len = fg["value"] // 2
    bar = "█" * bar_len + "░" * (50 - bar_len)
    print(f"  [{bar}] {fg['value']}/100 — {fg['label']}")

    # Scanner
    top10, pump_alerts, whale_rides, quality_count, tavily_catalysts = run_smart_scanner(exchange=exchange, fear_greed=fg, open_count=_open_count)
    if not top10:
        print("  No results from scanner — skipping analysis.")
        print_track_record()
        return

    # Log whale ride candidates + send immediate Telegram alert for each
    if whale_rides:
        from src.utils.logger import log_whale_ride
        from src.utils.telegram import send_telegram as _send_tg

        def _fp(v):
            if not isinstance(v, (int, float)) or v == 0:
                return "$0"
            if v >= 1:
                return f"${v:,.2f}"
            if v >= 0.01:
                return f"${v:.4f}"
            return f"${v:.8f}"

        for wr in whale_rides:
            log_whale_ride(wr, fg.get("value", 0))
            _sym  = wr["symbol"]
            _name = wr.get("name", _sym)
            _ep   = wr["entry"]
            _sl   = wr["stop_loss"]
            _tp   = wr["take_profit"]
            _hold = wr["max_hold_hours"]
            _cyc  = wr["cycle_number"]
            _ch24 = wr.get("change_24h", 0)
            _ch7d = wr.get("change_7d", 0)
            _scam = wr["is_serial_scam"]
            _cycles_str = " → ".join(wr["known_cycles"]) if wr["known_cycles"] else "first recorded"
            _risk_tag = "🚨 SERIAL SCAM" if _scam else "⚠️ HIGH RISK"
            _tg_msg = (
                f"🐋 <b>WHALE RIDE — {_sym}</b> ({_name})\n\n"
                f"  Price:   {_fp(_ep)}\n"
                f"  24h:     {_ch24:+.1f}%\n"
                f"  7d:      {_ch7d:+.1f}%\n\n"
                f"  Entry: {_fp(_ep)}  |  SL: {_fp(_sl)} (-15%)  |  TP: {_fp(_tp)} (+50%)\n"
                f"  Max hold: {_hold}h  |  Cycle #{_cyc}: {_cycles_str}\n\n"
                f"  {_risk_tag} — manipulated token\n"
                f"  Max 5% of portfolio — EXTREME RISK"
            )
            try:
                _send_tg(_tg_msg)
            except Exception as _te:
                print(f"  ⚠️  Whale ride Telegram failed for {_sym}: {_te}")

    # News for all top 10 — Tavily AI (if key set) or Google News RSS fallback.
    # fetch_news_for_coins is also called inside analyze_with_groq; this result
    # is used for the console summary and passed in to avoid a double fetch.
    import config as _cfg
    from src.connectors.web_research import fetch_news_for_coins
    _news_src = "Tavily AI" if _cfg.TAVILY_API_KEY else "Google News RSS"
    print(f"\n  📰  Fetching per-coin news for top 10 ({_news_src})...")
    per_coin_news_pre = fetch_news_for_coins(top10, limit_per_coin=5)
    found_count = sum(1 for v in per_coin_news_pre.values() if v)
    print(f"  ✅  News found for {found_count}/{len(top10)} coins")
    # Flatten to a text block for the Groq prompt header (per-coin detail added inside analyze_with_groq)
    news_lines: list[str] = []
    for sym, items in per_coin_news_pre.items():
        for it in items[:2]:
            age_tag = f"({it.get('age_days', '?')}d ago)" if it.get('age_days') is not None else "(date unknown)"
            news_lines.append(f"  {sym}: {age_tag} {it.get('title','')[:90]}")
    news_text = "\n".join(news_lines)

    # Sentiment analysis for top 10 coins — also adjusts scanner scores
    sentiment_text = ""
    try:
        from src.agents.sentiment_analyst import analyze_sentiment_batch, format_for_prompt as fmt_sentiment
        all_symbols   = [r["symbol"]       for r in top10]
        all_names     = [r.get("name", "") for r in top10]
        sentiments = analyze_sentiment_batch(all_symbols, coin_names=all_names, fear_greed=fg)
        sentiment_text = fmt_sentiment(sentiments)
        if sentiment_text:
            print_header("SENTIMENT ANALYSIS")
            _SENTIMENT_DELTA = {"VERY_BULLISH": 1, "BULLISH": 0, "NEUTRAL": 0, "BEARISH": 0, "VERY_BEARISH": -1}
            for sym, s in sentiments.items():
                delta = _SENTIMENT_DELTA.get(s.social_sentiment, 0)
                delta_str = f"  [{s.social_sentiment} score {delta:+d}]" if delta != 0 else ""
                print(f"  {sym:8s} {s.social_sentiment:12s} | F&G: {s.fear_greed}/100 | news: {s.news_sentiment:+.2f}{delta_str}")
                if delta != 0:
                    for r in top10:
                        if r["symbol"].upper() == sym.upper():
                            r["score"] += delta
                            r["reasons"].append(f"sentiment {s.social_sentiment} ({delta:+d})")
                            break
            # Re-sort top10 after sentiment adjustments (same tiebreaker as scanner)
            top10.sort(
                key=lambda x: (
                    x["score"],
                    x.get("clean_setup_tb", 0),
                    x.get("change_24h", 0),
                    x.get("vol_mcap", 0),
                    x.get("proven_wins_tb", 0),
                    1 if x.get("sec_commodity") else 0,
                    x.get("supply_capped_tb", 0),
                    x.get("momentum_stall_tb", 0),
                ),
                reverse=True,
            )
            # Refresh quality_count after sentiment may have boosted some scores
            quality_count = sum(1 for r in top10 if r.get("score", 0) >= 2)
    except Exception as e:
        print(f"  Warning: sentiment analysis failed: {e}")

    # Enrichment data (CMC, Etherscan, DeFiLlama, CoinPaprika, Polymarket)
    print_header("ENRICHMENT DATA")
    all_symbols = [r["symbol"] for r in top10]
    enrichment_text = fetch_enrichment(all_symbols)
    if not enrichment_text:
        print("  No enrichment data available")

    # ── DATA COLLECTION MODE: always open picks if score ≥ 2, no position cap ──
    from src.utils.logger import _read as _log_read, update_groq_rank
    _log_rows = _log_read()
    _already_open_scanner = {
        r.get("coin", "").upper()
        for r in _log_rows
        if r.get("type", "") in ("SCANNER", "") and r.get("status") == "OPEN"
    }
    _already_open_whale = {
        r.get("coin", "").upper()
        for r in _log_rows
        if r.get("type") == "WHALE_RIDE" and r.get("status") == "OPEN"
    }
    _already_open_syms = _already_open_scanner | _already_open_whale
    _open_quality_positions = len(_already_open_scanner)

    # Log pre-filter removals for coins that are open as whale rides
    _top10_syms = {r["symbol"].upper() for r in top10}
    for _wr_sym in _already_open_whale & _top10_syms:
        print(f"  Pre-filter removed: {_wr_sym} (already OPEN as whale_ride)")

    # Split top10 into already-open vs genuinely new candidates
    new_candidates    = [r for r in top10 if r["symbol"].upper() not in _already_open_syms]
    quality_count_new = sum(1 for r in new_candidates if r["score"] >= 2)
    recs: list[dict] = []

    # Run Groq whenever there are non-open candidates — even low-scoring ones.
    # Groq pre-filter (Step 0G) already gates on score ≤ 1; let Groq decide.
    skip_groq = False
    if not new_candidates:
        print(f"\n  ⚠️  NO NEW PICKS — all top10 coins are already OPEN positions.")
        skip_groq = True
    elif quality_count_new < 1:
        print(f"\n  ℹ️  No coins score ≥2 pts — passing top candidates to Groq for evaluation.")

    groq_candidates: list[dict] = []   # coins that passed Groq pre-filter — logged to CSV
    _groq_failed = False               # True = rate limit / network error, not "no picks"
    if not skip_groq:
        combined_context = "\n\n".join(filter(None, [enrichment_text, sentiment_text]))
        print_header("GROQ LLM ANALYSIS")
        try:
            groq_result = analyze_with_groq(
                top10, fg, news_text,
                pump_alerts=pump_alerts,
                enrichment_text=combined_context,
                per_coin_news=per_coin_news_pre,
                already_open=_already_open_syms,
                tavily_catalysts=tavily_catalysts,
            )
            recs_raw, groq_candidates = groq_result if isinstance(groq_result, tuple) else (groq_result, top10)
            recs = recs_raw if isinstance(recs_raw, list) else ([recs_raw] if recs_raw else [])
        except Exception as e:
            print(f"  ⚠️  Groq analysis failed: {e} — falling back to raw scanner top 3 by news")
            _groq_failed = True
            # Fallback: sort new non-open scanner picks by (has news, score) desc — pick top 3
            _fb_pool = [r for r in top10 if r["symbol"].upper() not in _already_open_syms]
            def _fb_sort(r):
                sym = r["symbol"].upper()
                has_news = bool((tavily_catalysts or {}).get(sym) or per_coin_news_pre.get(sym))
                return (1 if has_news else 0, r["score"])
            _fb_pool.sort(key=_fb_sort, reverse=True)
            _fallback_picks = _fb_pool[:3]
            for _fp in _fallback_picks:
                _sym = _fp["symbol"].upper()
                _cat = (tavily_catalysts or {}).get(_sym, "")
                if not _cat:
                    _items = per_coin_news_pre.get(_sym, [])
                    _cat = (_items[0].get("title") or "") if _items else ""
                recs.append({
                    "coin":        _sym,
                    "coin_id":     _fp.get("coin_id", ""),
                    "rank":        _fallback_picks.index(_fp) + 1,
                    "confidence":  "LOW",
                    "qualifier":   "GROQ_FALLBACK",
                    "reasoning":   f"scanner score {_fp['score']} pts",
                    "entry_price": _fp.get("price"),
                    "stop_loss":   None,
                    "take_profit": None,
                    "_groq_fallback": True,
                    "_fallback_news": _cat[:120] if _cat else "",
                })

    # Log only coins that passed Groq pre-filter — never log pre-filter rejects (e.g. ABOVE_UPPER BB).
    if not skip_groq and quality_count_new >= 1 and groq_candidates:
        log_scanner_results(groq_candidates, fg.get("value", 0))

    # After logging, find which coins got an OPEN SCANNER position this cycle.
    # Only scanner picks are eligible for display — whale ride coins must not leak through
    # even if Groq hallucinates them (they were already excluded from new_candidates).
    # Exception: fallback picks were never logged (Groq was down) — keep them as-is.
    _open_scanner_coins = {
        r.get("coin", "").upper() for r in _log_read()
        if r.get("status") == "OPEN" and r.get("type", "") in ("SCANNER", "")
    }
    recs = [r for r in recs if r.get("_groq_fallback") or r.get("coin", "").upper() in _open_scanner_coins]

    # Stamp Groq's rank + qualifier + key_signal onto each pick's CSV row
    for rec in recs:
        rec_sym = rec.get("coin", "").upper()
        _rank   = rec.get("rank") or (recs.index(rec) + 1)
        update_groq_rank(
            rec_sym,
            groq_rank  = int(_rank),
            qualifier  = rec.get("qualifier", "BASE_SCORE"),
            key_signal = rec.get("key_signal") or (rec.get("reasoning") or "")[:80],
        )

    msg: str = ""   # Telegram message built here, sent after stock + Polymarket data
    debate_verdict = None

    # Save Groq's picks for Telegram BEFORE filtering by logged status.
    # Even if quality_count < 3 (positions not logged), we still want to
    # show the top 3 scanner picks in Telegram with their confidence level.
    recs_for_display = list(recs)

    if recs:
        # Enrich each rec with coin_id
        for rec in recs:
            rec_symbol = rec.get("coin", "").upper()
            matched = next((r for r in top10 if r["symbol"] == rec_symbol), None)
            if matched:
                rec["coin_id"] = matched["coin_id"]

        # Multi-agent debate on pick #1 only
        rec1 = recs[0]
        rec1_symbol = rec1.get("coin", "").upper()
        matched1 = next((r for r in top10 if r["symbol"] == rec1_symbol), None)
        if debate and matched1:
            try:
                from src.agents.debate import run_debate
                debate_verdict = run_debate(
                    coin_data=matched1,
                    sentiment_text=sentiment_text,
                    enrichment_text=enrichment_text,
                    fear_greed=fg,
                )
                if debate_verdict:
                    if debate_verdict.get("verdict") == "BUY":
                        rec1["stop_loss"]   = debate_verdict.get("stop_loss")   or rec1.get("stop_loss")
                        rec1["take_profit"] = debate_verdict.get("take_profit") or rec1.get("take_profit")
                        rec1["entry_price"] = debate_verdict.get("entry_price") or rec1.get("entry_price")
                        rec1["reasoning"] = (
                            f"[Debate verdict: {debate_verdict['verdict']} "
                            f"({debate_verdict.get('confidence', 0):.0%} confidence)]\n"
                            f"{debate_verdict.get('reasoning', '')}\n\n"
                            f"Key risk: {debate_verdict.get('key_risk', 'N/A')}\n"
                            f"Key catalyst: {debate_verdict.get('key_catalyst', 'N/A')}"
                        )
                    elif debate_verdict.get("verdict") == "SKIP":
                        print(f"\n  ⚠  Debate says SKIP — still logging scanner pick but adding warning")
                        rec1["reasoning"] = (
                            f"[⚠ Debate verdict: SKIP — {debate_verdict.get('reasoning', '')}]\n\n"
                            f"Original rec: {rec1.get('reasoning', '')}"
                        )
            except Exception as e:
                print(f"  Warning: debate pipeline failed: {e}")

        # Sharpen SL/TP for all picks in recommendations.csv
        for rec in recs:
            rec_symbol = rec.get("coin", "").upper()
            if rec.get("stop_loss") and rec.get("take_profit"):
                update_scanner_sltp(
                    rec_symbol,
                    rec["stop_loss"],
                    rec["take_profit"],
                    rec.get("reasoning", ""),
                )

    # Build Telegram crypto section — unified TOP 3 format.
    # Slots 1..N: genuine new Groq picks (NEW ENTRY) — only as many as Groq returned.
    # Slots N+1..3: best open positions padded as HOLD (sorted by TP proximity).
    # Never invent new picks just to fill 3 slots.
    display_recs = recs_for_display or []

    # Bug 1 fix: skip non-fallback recs where entry_price is missing or zero
    _valid_display = []
    for _dr in display_recs:
        if _dr.get("_groq_fallback"):
            _valid_display.append(_dr)
            continue
        _ep = _dr.get("entry_price")
        if _ep is None or (isinstance(_ep, (int, float)) and _ep <= 0):
            print(f"  ⚠️  Skipped {_dr.get('coin', '?')}: price unavailable")
            continue
        _valid_display.append(_dr)
    display_recs = _valid_display

    # Open positions sorted by closeness to TP (ascending = closest first)
    open_portfolio_rows = [
        r for r in _log_rows
        if r.get("type", "") in ("SCANNER", "") and r.get("status") == "OPEN"
    ]
    def _pct_to_tp(row):
        try:
            tp = float(row.get("take_profit") or 0)
            cp = float(row.get("current_price") or row.get("entry_price") or 0)
            if tp > 0 and cp > 0:
                return (tp - cp) / tp * 100
        except (ValueError, TypeError):
            pass
        return 999.0
    open_portfolio_rows.sort(key=_pct_to_tp)

    # Exclude already-shown new picks from HOLD padding
    _new_pick_syms = {r.get("coin", "").upper() for r in display_recs}
    hold_candidates = [r for r in open_portfolio_rows if r.get("coin", "").upper() not in _new_pick_syms]

    def _fmt_price(v):
        if not isinstance(v, (int, float)):
            return str(v)
        if v == 0:
            return "$0"
        if v >= 1:
            return f"${v:,.2f}"
        if v >= 0.01:
            return f"${v:.4f}"
        return f"${v:.8f}"

    if display_recs or open_portfolio_rows:
        fg_value_msg = fg.get("value", "?")
        fg_label_msg = fg.get("label", "?")
        is_caution = any(r.get("_caution_buy") for r in display_recs)

        # Header label
        n_new = len(display_recs)
        _is_fallback = _groq_failed and any(r.get("_groq_fallback") for r in display_recs)
        if skip_groq or n_new == 0:
            header_label = "PORTFOLIO UPDATE"
        elif _is_fallback:
            header_label = f"TOP {n_new} SCANNER (GROQ DOWN)"
        elif is_caution:
            header_label = "⚠️ CAUTION BUY"
        else:
            header_label = f"TOP {n_new} BUY{'S' if n_new != 1 else ''}"

        lines_msg = [
            f"<b>CryptoAdvisor — {header_label}</b>\n",
            f"Fear &amp; Greed: {fg_value_msg}/100 ({fg_label_msg})\n",
        ]

        if skip_groq:
            lines_msg.append(f"<i>⚠️ No new picks — all top coins already OPEN</i>\n")
        elif _is_fallback:
            lines_msg.append(f"<i>⚠️ Groq rate-limited — showing raw scanner top 3 (no SL/TP — check manually)</i>\n")
        elif n_new == 0:
            lines_msg.append(f"<i>⚠️ Groq found no new entries this cycle</i>\n")
        elif is_caution:
            lines_msg.append("<i>No HIGH confidence picks — reduce position size in extreme fear</i>\n")

        slot = 1
        # New picks first
        for rec in display_recs:
            sym  = rec.get("coin", "?")
            ep   = rec.get("entry_price")
            conf = (rec.get("confidence") or "?").upper()
            icon = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(conf, "⚪")
            if rec.get("_groq_fallback"):
                # Fallback pick: show scanner score + news headline
                _score_str = rec.get("reasoning", "")
                _news_headline = rec.get("_fallback_news", "")
                _news_line = f"\n   📰 {_news_headline}" if _news_headline else ""
                lines_msg.append(
                    f"{slot}. <b>{sym}</b>  ⚪ SCANNER  {_score_str}{_news_line}"
                )
            else:
                lines_msg.append(
                    f"{slot}. <b>{sym}</b>  {icon} NEW ENTRY  entry {_fmt_price(ep)}"
                )
            slot += 1

        # Pad remaining slots up to 3 with best open positions (HOLD)
        for row in hold_candidates:
            if slot > 3:
                break
            coin    = row.get("coin", "?")
            pnl     = row.get("pnl_pct", "?")
            pnl_str = f"{float(pnl):+.1f}%" if pnl not in ("?", "", None) else "?"
            pct_left = _pct_to_tp(row)
            tp_tag  = f" ⏳ {pct_left:.1f}% to TP" if pct_left < 5 else ""
            lines_msg.append(
                f"{slot}. <b>{coin}</b>  ✅ HOLD  {pnl_str}{tp_tag}"
            )
            slot += 1

        msg = "\n".join(lines_msg)

        if debate_verdict and display_recs:
            verdict_emoji = {"BUY": "🟢", "SKIP": "🔴", "WAIT": "🟡"}.get(
                debate_verdict.get("verdict", ""), "⚪")
            msg += (
                f"\n\n{verdict_emoji} Debate: {debate_verdict.get('verdict', '?')} "
                f"({debate_verdict.get('confidence', 0):.0%})"
            )

    # Polymarket Advisor
    poly_picks: list[dict] = []
    if run_polymarket:
        try:
            from src.connectors.polymarket import fetch_top_markets
            from src.agents.polymarket_analyst import (
                analyze_polymarket, print_polymarket_picks, log_polymarket_picks,
                update_polymarket_positions, print_polymarket_track_record,
            )
            print_header("POLYMARKET ADVISOR")
            update_polymarket_positions()
            poly_markets = fetch_top_markets(limit=20)
            if poly_markets:
                print(f"  Fetched {len(poly_markets)} markets — sending top 10 to Groq…")
                poly_picks = analyze_polymarket(poly_markets[:10])
                if poly_picks:
                    print_polymarket_picks(poly_picks)
                    log_polymarket_picks(poly_picks)
            else:
                print("  No Polymarket data available")
        except Exception as e:
            print(f"  Warning: Polymarket advisor failed: {e}")

    # Stock scanner
    stock_picks: list[dict] = []
    stock_rec: dict | None = None   # top BUY for Telegram (backward compat)
    if run_stocks:
        try:
            from src.agents.stock_scanner import (
                run_stock_scanner, analyze_stocks_with_groq,
                log_stock_results, update_stock_positions, print_stock_track_record,
            )
            print_header("STOCK SCANNER")
            update_stock_positions()
            stock_top10 = run_stock_scanner()
            if stock_top10:
                log_stock_results(stock_top10)
                stock_picks = analyze_stocks_with_groq(stock_top10) or []
                # Top BUY for Telegram
                stock_rec = next((p for p in stock_picks if p.get("verdict") == "BUY"), None)
        except Exception as e:
            print(f"  Warning: stock scanner failed: {e}")

    # Daily activity summary (pure CSV, no API needed)
    print_daily_activity()

    # Combined track record
    print_header("TRACK RECORD")
    print_track_record()
    try:
        print_stock_track_record()
    except Exception:
        pass
    try:
        print_polymarket_track_record()
    except Exception:
        pass

    # Telegram — combined crypto + stock + Polymarket alert
    # Always send even if only stocks or Polymarket picks are available
    _has_stocks   = bool([p for p in stock_picks if p.get("verdict") == "BUY"])
    _has_poly     = bool(poly_picks)
    if msg or _has_stocks or _has_poly:
        if not msg:
            fg_value_msg = fg.get("value", "?")
            fg_label_msg = fg.get("label", "?")
            msg = (
                f"<b>CryptoAdvisor — SCAN REPORT</b>\n\n"
                f"Fear &amp; Greed: {fg_value_msg}/100 ({fg_label_msg})\n"
                f"<i>No crypto picks this cycle.</i>"
            )

        def _f2(v):
            return f"${v:,.2f}" if isinstance(v, (int, float)) else str(v)

        top_stock_buys = [p for p in stock_picks if p.get("verdict") == "BUY"][:3]
        if top_stock_buys:
            msg += "\n\n<b>TOP STOCK BUYS:</b>"
            for i, p in enumerate(top_stock_buys, 1):
                s_sym = p.get("stock", "?")
                s_ep  = p.get("entry_price", 0)
                s_sl  = p.get("stop_loss", 0)
                s_tp  = p.get("take_profit", 0)
                msg  += f"\n{i}. {s_sym} @ {_f2(s_ep)}  SL {_f2(s_sl)}  TP {_f2(s_tp)}"

        for wr in whale_rides:
            sym      = wr["symbol"]
            ep       = wr["entry"]
            sl       = wr["stop_loss"]
            tp       = wr["take_profit"]
            hold     = wr["max_hold_hours"]
            cyc      = wr["cycle_number"]
            scam_tag = "⚠️ SERIAL SCAM — same wallets as " + "/".join(wr.get("allies", [])) if wr["is_serial_scam"] else "⚠️ HIGH RISK"
            cycles   = " → ".join(wr["known_cycles"]) if wr["known_cycles"] else "first recorded"
            msg += (
                f"\n\n🐋 <b>WHALE RIDE:</b> {sym} @ {_f2(ep)}"
                f"  SL {_f2(sl)} TP {_f2(tp)} | max {hold}h"
                f"\nCycle #{cyc}: {cycles}"
                f"\n{scam_tag}"
                f"\nMax 5% of portfolio — EXTREME RISK"
            )

        if poly_picks:
            # Top 3 by edge (edge > 5pp), else by confidence
            # Hard filter: never show 0% or 100% odds markets (already certain, no value in betting)
            # Check both _auto_trivial flag AND raw probability — belt and suspenders
            def _prob_actionable(p) -> bool:
                if p.get("_auto_trivial"):
                    return False
                prob = p.get("probability")
                if prob is None:
                    return True
                pct = round(prob * 100, 1)
                return 0 < pct < 100  # exclude exactly 0% and 100%

            display_poly = [p for p in poly_picks if _prob_actionable(p)]
            edge_picks = sorted(
                [p for p in display_poly if p.get("is_opportunity") and float(p.get("edge_pct") or 0) > 5],
                key=lambda p: -float(p.get("edge_pct") or 0)
            )
            top3_poly = edge_picks[:3] or sorted(
                display_poly, key=lambda p: -(p.get("llm_confidence") or 0)
            )[:3]
            if top3_poly:
                msg += "\n\n<b>TOP POLYMARKET BETS:</b>"
                for i, pp in enumerate(top3_poly, 1):
                    v        = pp.get("llm_verdict") or "?"
                    q        = pp.get("question", "?")[:55]
                    prob_p   = pp.get("probability")
                    odds_s   = f"{prob_p*100:.0f}%" if prob_p is not None else "?"
                    edge_v   = pp.get("edge_pct")
                    edge_s   = f" edge +{float(edge_v):.0f}pp" if edge_v else ""
                    msg     += f"\n{i}. {v} on \"{q}\" @ {odds_s}{edge_s} — bet €100"

        send_telegram(msg)

    # CoinGecko call count for this cycle
    try:
        from src.connectors.coingecko import get_cg_call_count, reset_cg_call_count
        _cg = get_cg_call_count()
        print(f"\n  [CG USAGE] {_cg} calls this cycle | "
              f"monthly pace: {_cg * 30 * 6:,} | "
              f"limit: 10,000")
        reset_cg_call_count()
    except Exception:
        pass

    # LLM usage summary
    try:
        from src.utils.budget_tracker import print_daily_summary
        print_header("LLM USAGE TODAY")
        print_daily_summary()
    except Exception:
        pass


def run_polymarket_cycle() -> None:
    """Polymarket-only analysis — no crypto, no stocks."""
    from src.connectors.polymarket import fetch_top_markets
    from src.agents.polymarket_analyst import (
        analyze_polymarket, print_polymarket_picks, log_polymarket_picks,
        update_polymarket_positions, print_polymarket_track_record,
    )
    print(f"\n  CryptoAdvisor — Polymarket — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print_header("POLYMARKET ADVISOR")
    update_polymarket_positions()
    try:
        poly_markets = fetch_top_markets(limit=20)
        if poly_markets:
            print(f"  Fetched {len(poly_markets)} markets — sending top 10 to Groq…")
            poly_picks = analyze_polymarket(poly_markets[:10])
            if poly_picks:
                print_polymarket_picks(poly_picks)
                log_polymarket_picks(poly_picks)
        else:
            print("  No Polymarket data available")
    except Exception as e:
        print(f"  Polymarket scan failed: {e}")
    print_header("TRACK RECORD")
    try:
        print_polymarket_track_record()
    except Exception:
        pass


def run_stocks_cycle() -> None:
    """Stocks-only scan — no crypto."""
    from src.agents.stock_scanner import (
        run_stock_scanner, analyze_stocks_with_groq,
        log_stock_results, update_stock_positions, print_stock_track_record,
    )
    print(f"\n  Started at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print_header("STOCK SCANNER")
    update_stock_positions()
    top10 = run_stock_scanner()
    if top10:
        log_stock_results(top10)
        picks = analyze_stocks_with_groq(top10)
        if picks:
            buys = [p for p in picks if p.get("verdict") == "BUY"]
            print(f"\n  {len(buys)} BUY signal(s) out of {len(picks)} ranked stocks")
    print_header("STOCK TRACK RECORD")
    print_stock_track_record()
    try:
        from src.utils.budget_tracker import print_daily_summary
        print_header("LLM USAGE TODAY")
        print_daily_summary()
    except Exception:
        pass


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CryptoAdvisor LLM")
    parser.add_argument("--scan",             action="store_true", help="Full scan: crypto + stocks + Polymarket")
    parser.add_argument("--crypto",           action="store_true", help="Crypto-only scan (no stocks, no Polymarket)")
    parser.add_argument("--stocks",           action="store_true", help="Stock-only scan (no crypto)")
    parser.add_argument("--polymarket",       action="store_true", help="Polymarket-only analysis")
    parser.add_argument("--news",             action="store_true", help="Quick news check for open stock positions only")
    parser.add_argument("--schedule",         action="store_true", help="Run full scan every 1 hour (Telegram + log)")
    parser.add_argument("--crypto_scheduler", action="store_true", help="Run crypto-only scan every 1 hour (Telegram + log)")
    parser.add_argument("--debate",           action="store_true", help="Enable Bull/Bear/Risk Manager debate pipeline")
    parser.add_argument("--whale",            action="store_true", help="Run one whale ride check now (top 750 Kraken coins)")
    parser.add_argument("--tavily-status",    action="store_true", help="Show Tavily AI monthly credit usage")
    parser.add_argument(
        "--exchange",
        choices=["kraken", "revolut", "binance", "both", "all"],
        default=None,
        help="Filter to coins available on a specific exchange",
    )
    args = parser.parse_args()

    print(f"\n  CryptoAdvisor LLM — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    if args.schedule:
        try:
            from apscheduler.schedulers.blocking import BlockingScheduler
        except ImportError:
            print("  ERROR: apscheduler not installed. Run: pip install apscheduler")
            sys.exit(1)

        # Start Telegram command bot (non-blocking daemon thread)
        from src.utils.telegram_bot import start_bot_thread
        start_bot_thread()

        # Start price alert monitor — checks every 15min for milestone/proximity alerts
        import threading
        from src.utils.price_alerts import run_alert_loop
        alert_thread = threading.Thread(target=run_alert_loop, args=(15,), daemon=True, name="price-alerts")
        alert_thread.start()
        print("  Price alert monitor started (every 15 min)")

        # Run immediately, then every hour (full scan) + every 15 min (whale check)
        run_scan_cycle(exchange=args.exchange, debate=args.debate)

        scheduler = BlockingScheduler(timezone="UTC")
        scheduler.add_job(
            run_scan_cycle,
            trigger="interval",
            hours=1,
            kwargs={"exchange": args.exchange, "debate": args.debate},
            id="scan_cycle",
        )
        scheduler.add_job(
            run_whale_check,
            trigger="interval",
            minutes=5,
            id="whale_check",
        )
        print(f"\n  Scheduler running — full scan every 1h, whale check every 5min. Ctrl+C to stop.\n")
        try:
            scheduler.start()
        except KeyboardInterrupt:
            print("\n  Scheduler stopped.")

    elif args.crypto_scheduler:
        try:
            from apscheduler.schedulers.blocking import BlockingScheduler
        except ImportError:
            print("  ERROR: apscheduler not installed. Run: pip install apscheduler")
            sys.exit(1)

        from src.utils.telegram_bot import start_bot_thread
        start_bot_thread()

        import threading
        from src.utils.price_alerts import run_alert_loop
        alert_thread = threading.Thread(target=run_alert_loop, args=(15,), daemon=True, name="price-alerts")
        alert_thread.start()
        print("  Price alert monitor started (every 15 min)")

        # Run immediately, then every hour (full scan) + every 15 min (whale check)
        run_scan_cycle(exchange=args.exchange, debate=args.debate,
                       run_stocks=False, run_polymarket=False)

        scheduler = BlockingScheduler(timezone="UTC")
        scheduler.add_job(
            run_scan_cycle,
            trigger="interval",
            hours=1,
            kwargs={"exchange": args.exchange, "debate": args.debate,
                    "run_stocks": False, "run_polymarket": False},
            id="crypto_cycle",
        )
        scheduler.add_job(
            run_whale_check,
            trigger="interval",
            minutes=5,
            id="whale_check",
        )
        print(f"\n  Crypto scheduler running — full scan every 1h, whale check every 5min. Ctrl+C to stop.\n")
        try:
            scheduler.start()
        except KeyboardInterrupt:
            print("\n  Scheduler stopped.")

    elif args.news:
        from src.agents.stock_scanner import check_open_positions_news
        from src.utils.telegram import send_telegram
        print_header("BREAKING NEWS CHECK — OPEN STOCK POSITIONS")
        alerts = check_open_positions_news()
        if alerts:
            lines = ["<b>⚡ Breaking Stock News</b>\n"]
            for a in alerts:
                lines.append(f"<b>{a['symbol']}:</b> {a['headline'][:100]}")
            send_telegram("\n".join(lines))
        else:
            print("  No breaking news detected.")

    elif args.crypto:
        run_scan_cycle(exchange=args.exchange, debate=args.debate,
                       run_stocks=False, run_polymarket=False)

    elif args.polymarket:
        run_polymarket_cycle()

    elif args.stocks:
        run_stocks_cycle()

    elif args.scan:
        run_scan_cycle(exchange=args.exchange, debate=args.debate)

    elif getattr(args, "whale", False):
        run_whale_check()

    elif getattr(args, "tavily_status", False):
        from src.utils.budget_tracker import print_tavily_status
        print_tavily_status()

    else:
        # Default: prices, F&G, TA, portfolio
        prices = run_price_check()
        run_fear_greed()
        run_technical_analysis(prices)
        run_portfolio_check(prices)
        print(f"\n{'─'*62}")
        print(f"  🚀  QUICK START")
        print(f"{'─'*62}")
        print(f"  🔍  python run.py --scan                     full crypto + stocks + Polymarket")
        print(f"  🪙  python run.py --crypto                   crypto scanner only")
        print(f"  🔮  python run.py --polymarket               Polymarket advisor only")
        print(f"  📊  python run.py --stocks                   stock scanner only")
        print(f"  🔑  python run.py --crypto --exchange kraken Kraken-only coin filter")
        print(f"  🥊  python run.py --scan --debate            + Bull/Bear agent debate")
        print(f"  ⏱   python run.py --schedule                 auto-scan every 4h + Telegram")
        print(f"  🖥   streamlit run dashboard.py               → http://localhost:8501")
        print(f"{'─'*62}\n")


if __name__ == "__main__":
    main()