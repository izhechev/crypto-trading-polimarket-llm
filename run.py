#!/usr/bin/env python3
"""CryptoAdvisor — Phase 1 MVP.

Usage:
  python run.py                              # prices, F&G, TA, portfolio
  python run.py --scan                       # scan top 250 + Groq pick
  python run.py --scan --exchange kraken     # Kraken-only filter
  python run.py --scan --exchange revolut    # Revolut X filter
  python run.py --scan --exchange both       # Kraken + Revolut X
  python run.py --schedule                   # run every 4 h (Groq + Telegram)
  python run.py --schedule --exchange kraken
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import WATCHLIST, WATCHLIST_SYMBOLS, PORTFOLIO_PATH
from src.connectors.coingecko import fetch_prices, fetch_ohlcv, fetch_fear_greed
from src.agents.technical_analyst import compute_ta


def load_portfolio() -> dict:
    with open(PORTFOLIO_PATH) as f:
        return json.load(f)


def print_header(text: str):
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


def run_price_check():
    print_header("PRICES")
    prices = fetch_prices(WATCHLIST)
    for p in prices:
        arrow = "↑" if p.change_24h > 0 else "↓" if p.change_24h < 0 else "→"
        decimals = 2 if p.price_eur >= 1 else 4
        eur_str = f"€{p.price_eur:.{decimals}f}"
        usd_str = f"(${p.price_usd:.{decimals}f})"
        print(f"  {p.symbol:8s} {eur_str:>12s} {usd_str:>12s}  {arrow} {p.change_24h:+.1f}% (24h)  {p.change_7d:+.1f}% (7d)  MCap: €{p.market_cap/1e6:.0f}M")
    return {p.coin_id: p for p in prices}


def run_fear_greed():
    print_header("FEAR & GREED INDEX")
    fg = fetch_fear_greed()
    bar_len = fg["value"] // 2
    bar = "█" * bar_len + "░" * (50 - bar_len)
    print(f"  [{bar}] {fg['value']}/100 — {fg['label']}")
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
            print(f"\n  {symbol} — {trend_icon.get(ta.trend, '?')} {ta.trend} (confidence: {ta.confidence:.0%})")
            print(f"    Price: ${ta.price:.4f}")
            if ta.rsi_14:
                print(f"    RSI(14): {ta.rsi_14:.1f}")
            if ta.macd_signal:
                print(f"    MACD: {ta.macd_signal}")
            if ta.support_levels:
                print(f"    Support: {', '.join(f'${s:.4f}' for s in ta.support_levels[:3])}")
            if ta.resistance_levels:
                print(f"    Resistance: {', '.join(f'${r:.4f}' for r in ta.resistance_levels[:3])}")
            print(f"    Note: {ta.key_observation}")
        except Exception as e:
            print(f"\n  {symbol} — ERROR: {e}")
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
        decimals = 2 if p.price_eur >= 1 else 4
        print(
            f"  {icon} {h['asset']:8s} {amt:>6.1f} × €{p.price_eur:.{decimals}f}"
            f" = €{eur_value:>8.2f}  (entry ${h['entry_price_usd']:.4f}, P&L: {pnl_pct:+.1f}%)"
        )
    total_pnl_eur = total_eur - total_cost_eur
    total_pnl_pct = (total_pnl_eur / total_cost_eur) * 100 if total_cost_eur else 0
    icon = "🟢" if total_pnl_eur >= 0 else "🔴"
    print(f"\n  {icon} TOTAL: €{total_eur:.2f} / invested ≈€{total_cost_eur:.2f} / P&L: €{total_pnl_eur:+.2f} ({total_pnl_pct:+.1f}%)")


# ── Scan cycle (used by --scan and --schedule) ─────────────────────────────

def run_scan_cycle(exchange: str | None = None) -> None:
    """One full scan → Groq → log → Telegram cycle."""
    from src.agents.scanner import run_smart_scanner
    from src.agents.groq_analyst import analyze_with_groq
    from src.connectors.cryptopanic import fetch_news, format_for_prompt
    from src.utils.logger import (
        log_scanner_results, update_scanner_sltp, update_open_positions,
        log_portfolio_positions, log_watchlist_prices,
        log_price_history, print_track_record,
    )
    from src.utils.enrichment import fetch_enrichment
    from src.utils.telegram import send_telegram, format_recommendation

    print(f"\n  Started at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    # Update any open scanner positions before scanning
    update_open_positions()

    # Log current portfolio holdings with live prices (Kraken → portfolio.json fallback)
    print(f"\n  Logging portfolio positions...")
    log_portfolio_positions()

    # Log watchlist coin prices
    print(f"  Logging watchlist prices...")
    log_watchlist_prices()

    # Log price history for all tracked coins
    print(f"  Logging price history...")
    log_price_history()

    # Fear & Greed
    fg = fetch_fear_greed()
    print_header("FEAR & GREED INDEX")
    bar_len = fg["value"] // 2
    bar = "█" * bar_len + "░" * (50 - bar_len)
    print(f"  [{bar}] {fg['value']}/100 — {fg['label']}")

    # Scanner
    top10, pump_alerts = run_smart_scanner(exchange=exchange)
    if not top10:
        print("  No results from scanner — skipping analysis.")
        print_track_record()
        return

    # News for the top 5 symbols — pass full names so fallback queries work
    top_symbols = [r["symbol"] for r in top10[:5]]
    top_names   = [r.get("name", "") for r in top10[:5]]
    print(f"\n  Fetching news for {', '.join(top_symbols)}...")
    news = fetch_news(top_symbols, names=top_names)
    news_text = format_for_prompt(news)
    if news:
        print(f"  {len(news)} headlines loaded")
    else:
        print("  No news found")

    # Sentiment analysis for top 5 coins
    sentiment_text = ""
    try:
        from src.agents.sentiment_analyst import analyze_sentiment_batch, format_for_prompt as fmt_sentiment
        all_symbols = [r["symbol"] for r in top10]
        sentiments = analyze_sentiment_batch(all_symbols, fear_greed=fg)
        sentiment_text = fmt_sentiment(sentiments)
        if sentiment_text:
            print_header("SENTIMENT ANALYSIS")
            for sym, s in sentiments.items():
                print(f"  {sym:8s} {s.social_sentiment:12s} | F&G: {s.fear_greed}/100 | news: {s.news_sentiment:+.2f}")
    except Exception as e:
        print(f"  Warning: sentiment analysis failed: {e}")

    # Enrichment data (CMC, Messari, Etherscan, DeFiLlama, CoinPaprika, Polymarket)
    print_header("ENRICHMENT DATA")
    all_symbols = [r["symbol"] for r in top10]
    enrichment_text = fetch_enrichment(all_symbols)
    if not enrichment_text:
        print("  No enrichment data available")

    # Groq analysis — picks the single best coin
    # Combine news + sentiment into enrichment context
    combined_context = "\n\n".join(filter(None, [enrichment_text, sentiment_text]))
    print_header("GROQ LLM ANALYSIS")
    rec = analyze_with_groq(top10, fg, news_text, pump_alerts=pump_alerts, enrichment_text=combined_context)

    # Log ALL top 10 as OPEN scanner picks (SL=-20%, TP=+30%). Skip duplicates.
    log_scanner_results(top10, fg.get("value", 0))

    if rec:
        rec_symbol = rec.get("coin", "").upper()
        matched = next((r for r in top10 if r["symbol"] == rec_symbol), None)
        if matched:
            rec["coin_id"] = matched["coin_id"]

        # Sharpen Groq's pick: overwrite SL/TP + store LLM reasoning
        if rec.get("stop_loss") and rec.get("take_profit"):
            update_scanner_sltp(
                rec_symbol,
                rec["stop_loss"],
                rec["take_profit"],
                rec.get("reasoning", ""),
            )

        # Telegram alert
        msg = format_recommendation(rec, fg)
        send_telegram(msg)

    # Track record summary
    print_header("TRACK RECORD")
    print_track_record()

    # LLM usage summary
    try:
        from src.utils.budget_tracker import print_daily_summary
        print_header("LLM USAGE TODAY")
        print_daily_summary()
    except Exception:
        pass


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CryptoAdvisor LLM")
    parser.add_argument("--scan", action="store_true", help="Run smart scanner + Groq analysis once")
    parser.add_argument("--schedule", action="store_true", help="Run scan every 4 hours (Telegram + log)")
    parser.add_argument(
        "--exchange",
        choices=["kraken", "revolut", "both"],
        default=None,
        help="Filter to coins available on a specific exchange",
    )
    args = parser.parse_args()

    print(f"\n  CryptoAdvisor LLM — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    if args.schedule:
        try:
            from apscheduler.schedulers.blocking import BlockingScheduler
        except ImportError:
            print("  ERROR: apscheduler not installed. Run: pip install apscheduler")
            sys.exit(1)

        # Start Telegram command bot (non-blocking daemon thread)
        from src.utils.telegram_bot import start_bot_thread
        start_bot_thread()

        # Run immediately, then every 4 hours
        run_scan_cycle(exchange=args.exchange)

        scheduler = BlockingScheduler(timezone="UTC")
        scheduler.add_job(
            run_scan_cycle,
            trigger="interval",
            hours=4,
            kwargs={"exchange": args.exchange},
            id="scan_cycle",
        )
        print(f"\n  Scheduler running — next scan in 4 hours. Ctrl+C to stop.\n")
        try:
            scheduler.start()
        except KeyboardInterrupt:
            print("\n  Scheduler stopped.")

    elif args.scan:
        run_scan_cycle(exchange=args.exchange)

    else:
        # Default: prices, F&G, TA, portfolio
        prices = run_price_check()
        run_fear_greed()
        run_technical_analysis(prices)
        run_portfolio_check(prices)
        print(f"\n{'='*60}")
        print(f"  python run.py --scan                       one-shot scan + Groq")
        print(f"  python run.py --scan --exchange kraken     Kraken-only")
        print(f"  python run.py --schedule                   every 4 h + Telegram")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
