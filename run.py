#!/usr/bin/env python3
"""CryptoAdvisor — Phase 1 MVP.

Usage:
  python run.py                              # prices, F&G, TA, portfolio
  python run.py --scan                       # crypto + stocks + Polymarket (full)
  python run.py --crypto                     # crypto scanner only
  python run.py --crypto --exchange revolut  # crypto, Revolut X only
  python run.py --crypto --exchange binance  # crypto, Binance only
  python run.py --crypto --exchange all      # crypto, Revolut + Binance
  python run.py --polymarket                 # Polymarket advisor only
  python run.py --stocks                     # stock scanner only
  python run.py --news                       # quick news check for open stock positions
  python run.py --schedule                   # run every 4 h (Groq + Telegram)
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
        decimals = 2 if p.price_usd >= 1 else 4 if p.price_usd >= 0.01 else 6 if p.price_usd >= 0.0001 else 8
        usd_str = f"${p.price_usd:.{decimals}f}"
        print(f"  {icon} {p.symbol:8s}  {usd_str:>14s}  {p.change_24h:+.1f}% 24h  {p.change_7d:+.1f}% 7d  MCap ${p.market_cap/1e6:.0f}M")
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
    total_usd = total_cost_usd = 0.0
    DUST = 0.12
    for h in portfolio["holdings"]:
        p = prices.get(h["coin_id"])
        if not p:
            continue
        amt       = h["amount"]
        usd_value = amt * p.price_usd
        entry_usd = h.get("entry_price_usd")
        cost_usd  = amt * entry_usd if entry_usd else 0.0
        pnl_pct   = (p.price_usd - entry_usd) / entry_usd * 100 if entry_usd else None

        if usd_value < DUST:
            print(f"  [dust] {h['asset']:8s}  ${p.price_usd:.4f}  value ${usd_value:.4f}")
            continue

        total_usd      += usd_value
        total_cost_usd += cost_usd
        icon = "🟢" if (pnl_pct or 0) >= 0 else "🔴"
        decimals = 2 if p.price_usd >= 1 else 4 if p.price_usd >= 0.01 else 6 if p.price_usd >= 0.0001 else 8
        pnl_str = f"P&L: {pnl_pct:+.1f}%" if pnl_pct is not None else "P&L: n/a"
        print(
            f"  {icon} {h['asset']:8s} {amt:>6.1f} × ${p.price_usd:.{decimals}f}"
            f" = ${usd_value:>8.2f}  ({pnl_str})"
        )
    total_pnl_usd = total_usd - total_cost_usd
    total_pnl_pct = (total_pnl_usd / total_cost_usd) * 100 if total_cost_usd else 0
    icon = "🟢" if total_pnl_usd >= 0 else "🔴"
    print(f"\n  {icon} TOTAL: ${total_usd:.2f} / invested ≈${total_cost_usd:.2f} / P&L: ${total_pnl_usd:+.2f} ({total_pnl_pct:+.1f}%)")


# ── Whale ride check (every 15 min) ──────────────────────────────────────────

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


_whale_coins_cache: list[dict] = []
_whale_coins_ts: float = 0.0
_WHALE_COINS_TTL: int = 14400   # 4 hours — coin list structure rarely changes
_WHALE_COINS_STALE_PATH = Path(__file__).parent / "data" / "_whale_coins_stale.json"


def _fetch_coins_whale(pages: int = 4) -> list[dict]:
    """
    Fetch top coins for whale detection.
    Fallback chain: CoinPaprika → CoinGecko → CoinCap.
    Coin list cached 4h in-memory + on-disk stale cache survives restarts.
    """
    import time as _time

    global _whale_coins_cache, _whale_coins_ts

    age = _time.time() - _whale_coins_ts
    if _whale_coins_cache and age < _WHALE_COINS_TTL:
        print(f"  [cache] whale coins: {len(_whale_coins_cache)} coins "
              f"({age/60:.0f}min old, refreshes in {(_WHALE_COINS_TTL - age)/60:.0f}min)")
        return _whale_coins_cache

    all_coins: list[dict] = []

    # ── 1. CoinPaprika (primary, single request, ~2000 coins) ─────────────
    try:
        from src.connectors.coinpaprika import fetch_tickers_for_scanner as _cp_tickers
        all_coins = _cp_tickers(limit=1000)
        if all_coins:
            print(f"  [CP] whale coins: {len(all_coins)} coins")
    except Exception as e:
        print(f"  ⚠️  CoinPaprika failed: {e}")

    # ── 2. CoinGecko fallback (pages×250, free tier 30/min) ───────────────
    if not all_coins:
        from src.connectors.coingecko import _cg_get, _base_url, _headers
        import httpx
        headers = _headers()
        for page in range(1, pages + 1):
            try:
                with httpx.Client(timeout=30) as client:
                    resp = _cg_get(
                        client,
                        f"{_base_url()}/coins/markets",
                        params={
                            "vs_currency":             "usd",
                            "order":                   "market_cap_desc",
                            "per_page":                250,
                            "page":                    page,
                            "price_change_percentage": "24h,7d",
                            "sparkline":               "false",
                        },
                        headers=headers,
                    )
                    resp.raise_for_status()
                    batch = resp.json()
                    if not batch:
                        break
                    all_coins.extend(batch)
            except Exception as e:
                print(f"  ⚠️  CoinGecko page {page} failed: {e} — continuing")
                continue  # don't break — try remaining pages and next fallback
            if page < pages:
                _time.sleep(1.2)
        if all_coins:
            print(f"  [CG] whale coins: {len(all_coins)} coins")

    # ── 3. CoinCap fallback (free, no key, up to 2000 assets) ─────────────
    if not all_coins:
        try:
            import httpx
            with httpx.Client(timeout=30) as client:
                resp = client.get(
                    "https://api.coincap.io/v2/assets",
                    params={"limit": 2000, "offset": 0},
                )
                resp.raise_for_status()
                raw = resp.json().get("data", [])
            for c in raw:
                try:
                    price = float(c.get("priceUsd") or 0)
                    mcap  = float(c.get("marketCapUsd") or 0)
                    vol   = float(c.get("volumeUsd24Hr") or 0)
                    ch24  = float(c.get("changePercent24Hr") or 0)
                    all_coins.append({
                        "id":             c.get("id", ""),
                        "symbol":         (c.get("symbol") or "").upper(),
                        "name":           c.get("name", ""),
                        "current_price":  price,
                        "market_cap":     mcap,
                        "total_volume":   vol,
                        "price_change_percentage_24h":            ch24,
                        "price_change_percentage_7d_in_currency": 0.0,
                        "circulating_supply": float(c.get("supply") or 0),
                        "total_supply":       float(c.get("maxSupply") or 0),
                        "_from_coincap":  True,
                    })
                except (ValueError, TypeError):
                    continue
            if all_coins:
                print(f"  [CoinCap] whale coins: {len(all_coins)} coins")
        except Exception as e:
            print(f"  ⚠️  CoinCap failed: {e}")

    # ── 4. Disk stale cache (last resort — survives restarts) ──────────────
    if not all_coins:
        try:
            if _WHALE_COINS_STALE_PATH.exists():
                all_coins = json.loads(_WHALE_COINS_STALE_PATH.read_text(encoding="utf-8"))
                print(f"  [stale] whale coins: {len(all_coins)} coins from disk cache")
        except Exception:
            pass

    if all_coins:
        _whale_coins_cache = all_coins
        _whale_coins_ts    = _time.time()
        # Persist to disk for next restart
        try:
            _WHALE_COINS_STALE_PATH.parent.mkdir(exist_ok=True)
            _WHALE_COINS_STALE_PATH.write_text(
                json.dumps(all_coins, default=str), encoding="utf-8"
            )
        except Exception:
            pass

    return all_coins


def run_whale_check() -> None:
    """
    Lightweight whale ride detector — runs every 15 min.
    Coin list cached 1 hour (4 pages × 250 = 1,000 coins) — fetched once
    per hour, reused across the 3 checks within that hour.
    No Groq, no full TA — fastest possible whale catch.
    """
    try:
        from src.utils.logger import update_open_positions
        update_open_positions()

        from src.agents.coin_risk_assessor import assess_coin_risks
        from src.agents.whale_rider import (
            detect_whale_rides,
            send_whale_ride_alerts,
            check_exit_signals,
            display_late_stage,
            update_volume_history,
            detect_sector_rotation,
            send_sector_rotation_alerts,
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

        # ── Live Price Refresh ────────────────────────────────────────────────
        # The coin list above is heavily cached (4 hours) to save API calls.
        # We MUST refresh the prices/volumes right now to prevent stale alerts.
        try:
            import httpx
            from src.connectors.coingecko import _headers as _cg_headers
            # Get CG IDs for all coins
            cg_ids = [c.get("id") or c.get("_cg_id") for c in coins if c.get("id") or c.get("_cg_id")]
            if cg_ids:
                # We need both price and volume to detect whale rides correctly.
                # simple/price gives us 24h vol and price in one fast call.
                cg_resp = httpx.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={
                        "ids": ",".join(cg_ids),
                        "vs_currencies": "usd",
                        "include_24hr_vol": "true",
                        "include_24hr_change": "true"
                    },
                    headers=_cg_headers(),
                    timeout=15,
                )
                if cg_resp.status_code == 200:
                    live_data = cg_resp.json()
                    updated = 0
                    for c in coins:
                        cid = c.get("id") or c.get("_cg_id")
                        if cid and cid in live_data:
                            c["current_price"] = live_data[cid].get("usd", c.get("current_price"))
                            c["total_volume"]  = live_data[cid].get("usd_24h_vol", c.get("total_volume"))
                            c["price_change_percentage_24h"] = live_data[cid].get("usd_24h_change", c.get("price_change_percentage_24h"))
                            updated += 1
                    print(f"  Live price/vol refreshed for {updated} coins")
        except Exception as e:
            print(f"  ⚠️  Live price refresh failed (falling back to cache): {e}")

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
            rotations = detect_sector_rotation(candidates)
            if rotations:
                send_sector_rotation_alerts(rotations, fg)
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
        log_price_history, print_track_record, print_daily_activity, print_scan_summary,
    )
    from src.utils.enrichment import fetch_enrichment
    from src.utils.telegram import send_telegram

    # print(f"\n  🕐  Started at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    # Update any open scanner positions before scanning
    # print(f"  🔄  Updating open positions...")
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
            # if _tp_near or _crit_loss or _stale_pm or _open_count > 0:
            #     print(
            #         f"  📊  Portfolio: {_profitable_count} profitable / {_open_count} total open | "
            #         f"TP≥10%: {len(_tp_near)} | "
            #         f"Loss≤-10%: {len(_crit_loss)} | "
            #         f"Stale: {len(_stale_pm)}"
            #     )
            # Telegram alerts
            # Proximity alerts disabled for 24h strict window strategy
            pass
    except Exception as _e_pm:
        print(f"  ⚠️  Portfolio alert check failed: {_e_pm}")

    # log_portfolio_positions()  # disabled — portfolio price fetch causing RetryError

    # Log watchlist coin prices
    # log_watchlist_prices()  # disabled — same RetryError as portfolio

    # Log price history for all tracked coins
    # print(f"  📜  Logging price history...")
    log_price_history()

    # Fear & Greed
    fg = fetch_fear_greed()
    print_header("FEAR & GREED INDEX")
    bar_len = fg["value"] // 2
    bar = "█" * bar_len + "░" * (50 - bar_len)
    print(f"  [{bar}] {fg['value']}/100 — {fg['label']}")

    # Scanner - returns only valuable_wr + bundled categories
    top10, pump_alerts, whale_rides, quality_count, tavily_catalysts, categories = run_smart_scanner(exchange=exchange, fear_greed=fg, open_count=_open_count)

    if not top10:
        print("  No results from scanner — skipping analysis.")
        print_scan_summary(top10=[], whale_rides=whale_rides, fear_greed=fg)
        print_track_record()
        return

    # News for all top 10
    import config as _cfg
    from src.connectors.web_research import fetch_news_for_coins
    per_coin_news_pre = fetch_news_for_coins(top10, limit_per_coin=5)
    news_lines: list[str] = []
    for sym, items in per_coin_news_pre.items():
        for it in items[:2]:
            age_tag = f"({it.get('age_days', '?')}d ago)" if it.get('age_days') is not None else "(date unknown)"
            news_lines.append(f"  {sym}: {age_tag} {it.get('title','')[:90]}")
    news_text = "\n".join(news_lines)

    # Sentiment analysis
    sentiment_text = ""
    try:
        from src.agents.sentiment_analyst import analyze_sentiment_batch, format_for_prompt as fmt_sentiment
        all_symbols = [r["symbol"] for r in top10]
        sentiments = analyze_sentiment_batch(all_symbols, coin_names=[r.get("name","") for r in top10], fear_greed=fg)
        sentiment_text = fmt_sentiment(sentiments)
        if sentiment_text:
            _SENTIMENT_DELTA = {"VERY_BULLISH": 1, "BULLISH": 0, "NEUTRAL": 0, "BEARISH": 0, "VERY_BEARISH": -1}
            for sym, s in sentiments.items():
                delta = _SENTIMENT_DELTA.get(s.social_sentiment, 0)
                if delta != 0:
                    for r in top10:
                        if r["symbol"].upper() == sym.upper():
                            r["score"] += delta; break
            top10.sort(key=lambda x: (x["score"], x.get("change_24h", 0)), reverse=True)
    except Exception: pass

    # Enrichment
    all_symbols = [r["symbol"] for r in top10]
    enrichment_text = fetch_enrichment(all_symbols)

    # Groq Analysis
    from src.utils.logger import _read as _log_read, log_recommendation, log_whale_ride
    _all_rows = _log_read()
    _already_open_syms = {r.get("coin", "").upper() for r in _all_rows if r.get("status") == "OPEN"}
    
    new_candidates = [r for r in top10 if r["symbol"].upper() not in _already_open_syms]
    recs: list[dict] = []
    groq_candidates = []
    
    if new_candidates:
        combined_context = "\n\n".join(filter(None, [enrichment_text, sentiment_text]))
        print_header("GROQ LLM ANALYSIS")
        try:
            groq_result = analyze_with_groq(
                top10, fg, news_text,
                enrichment_text=combined_context,
                per_coin_news=per_coin_news_pre,
                already_open=_already_open_syms,
                tavily_catalysts=tavily_catalysts,
            )
            recs_raw, groq_candidates = groq_result if isinstance(groq_result, tuple) else (groq_result, top10)
            recs = recs_raw if isinstance(recs_raw, list) else ([recs_raw] if recs_raw else [])
        except Exception as e:
            print(f"  ⚠️  Groq failed: {e}")

    # ── High-Conviction Filtering ──
    # User requested to "open based cuz it has high confidence" and "open only high confidence picks".
    # We will accept BUY signals with at least Medium confidence (>= 0.6) to ensure we don't miss 
    # good setups, but we still enforce strict technical score checks.
    best_picks = [r for r in recs if r.get("verdict") == "BUY" and r.get("confidence_score", 0) >= 0.6]
    
    if best_picks:
        print_header("HIGH-CONVICTION OPPORTUNITIES")
        for i, p in enumerate(best_picks, 1):
            _sym  = p["coin"].upper()
            _side = p.get("recommended_order", "SPOT")
            _conf = p.get("confidence", "LOW")
            
            # Additional safety check: only open if scanner score was very high
            # (Long/Short >= 8, Spot >= 6)
            _score = p.get("scanner_score", 0)
            _meets_extreme_score = (_side in ("LONG", "SHORT") and _score >= 8) or (_side == "SPOT" and _score >= 6)

            if _meets_extreme_score:
                print(f"  {i}. 🟢 {_side} | {_sym} (Conf: {_conf}, Score: {_score}) | Price: ${p.get('entry_price', 0):.4f}")
                print(f"     💬 {p.get('reasoning', '')[:120]}...")

                if _sym not in _already_open_syms:
                    _ep = float(p.get("entry_price") or 0)
                    _cid = p.get("coin_id")
                    if not _cid:
                        matched = next((r for r in top10 if r["symbol"].upper() == _sym), None)
                        if matched: _cid = matched["coin_id"]

                    if _ep > 0 and _cid:
                        if _side == "SHORT": _tp, _sl = _ep * 0.90, _ep * 1.10
                        else: _tp, _sl = _ep * 1.10, _ep * 0.90
                        
                        log_recommendation({
                            "coin": _sym, "coin_id": _cid,
                            "entry_price": round(_ep, 8), "stop_loss": round(_sl, 8), "take_profit": round(_tp, 8),
                            "timeframe": "24h Window", "reasoning": f"High Conviction BUY. Conf: {_conf}, Score: {_score}.",
                            "recommended_order": _side,
                        }, fg.get("value", 50))
                        _already_open_syms.add(_sym)
            else:
                print(f"  ❌ {_sym} rejected: LLM confidence is {_conf}, but technical score ({_score}) is too low.")
    else:
        print("\n  ℹ️  No high-conviction BUY opportunities this cycle.")

    # Auto-log valuable Whale Rides
    # Only log if they meet a high threshold (Cycle >= 2 or Score >= 4)
    for wr in whale_rides:
        _sym = wr.get("symbol", "").upper()
        if _sym not in _already_open_syms:
            if wr.get("cycle_number", 0) >= 2 or wr.get("hc_score", 0) >= 4:
                log_whale_ride(wr, fg.get("value", 50))
                _already_open_syms.add(_sym)

    # ── Telegram Final Report ──
    # Build a single unified message for High-Conviction only
    msg = f"<b>💎 BEST OPPORTUNITIES — F&G {fg['value']}/100</b>\n"
    
    if best_picks:
        msg += "\n🚀 <b>HIGH-CONVICTION BUY SIGNALS:</b>\n"
        for i, p in enumerate(best_picks, 1):
            _side = p.get("recommended_order", "SPOT")
            msg += f"  {i}. <b>{p['coin']}</b> ({_side}) @ ${p.get('entry_price', 0):.4f} (Conf: {p.get('confidence','?')})\n"
    
    if whale_rides:
        msg += "\n🐋 <b>VALUABLE WHALE RIDES:</b>\n"
        for wr in whale_rides[:3]:
            msg += f"  🐋 <b>{wr['symbol']}</b> (Cycle #{wr['cycle_number']} | Score {wr.get('hc_score','?')})\n"

    if not best_picks and not whale_rides:
        msg += "\n<i>No high-conviction opportunities found this cycle.</i>"

    send_telegram(msg)

    # Polymarket & Stocks (minimal terminal output, as requested)
    if run_polymarket:
        try:
            from src.connectors.polymarket import fetch_top_markets
            from src.agents.polymarket_analyst import analyze_polymarket, update_polymarket_positions
            update_polymarket_positions()
            poly_markets = fetch_top_markets(limit=10)
            if poly_markets: analyze_polymarket(poly_markets)
        except Exception: pass

    if run_stocks:
        try:
            from src.agents.stock_scanner import run_stock_scanner, update_stock_positions
            update_stock_positions(); run_stock_scanner()
        except Exception: pass

    print_daily_activity()
    print_scan_summary(top10=top10, whale_rides=whale_rides, fear_greed=fg)
    print_track_record()


    # CoinGecko call count for this cycle
    try:
        from src.connectors.coingecko import get_cg_call_count, reset_cg_call_count
        _cg = get_cg_call_count()
        # print(f"\n  [CG USAGE] {_cg} calls this cycle | "
        #       f"monthly pace: {_cg * 30 * 6:,} | "
        #       f"limit: 10,000")
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
    # print(f"\n  Started at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
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
    parser.add_argument("--crypto_scheduler", "-cs", "-scheduler", action="store_true", help="Run crypto-only scan every 1 hour (Telegram + log)")
    parser.add_argument("--debate",           action="store_true", help="Enable Bull/Bear/Risk Manager debate pipeline")
    parser.add_argument("--whale",            action="store_true", help="Run one whale ride check now (top 750 Kraken coins)")
    parser.add_argument("--tavily-status",    action="store_true", help="Show Tavily AI monthly credit usage")
    parser.add_argument(
        "--exchange",
        choices=["revolut", "binance", "all"],
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

        # Run immediately, then every 3h (full scan) + every 24h (whale check)
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
            minutes=15,
            id="whale_check",
        )
        print(f"\n  Scheduler running — full scan every 1h, whale check every 15m. Ctrl+C to stop.\n")
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

        # Run immediately, then every 3h (full scan) + every 24h (whale check)
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
            minutes=15,
            id="whale_check",
        )
        print(f"\n  Crypto scheduler running — full scan every 1h, whale check every 15m. Ctrl+C to stop.\n")
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
        print(f"  🔑  python run.py --crypto --exchange revolut Revolut-only coin filter")
        print(f"  🥊  python run.py --scan --debate            + Bull/Bear agent debate")
        print(f"  ⏱   python run.py --schedule                 auto-scan every 4h + Telegram")
        print(f"  🖥   streamlit run dashboard.py               → http://localhost:8501")
        print(f"{'─'*62}\n")


if __name__ == "__main__":
    main()