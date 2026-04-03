"""Smart Scanner — rank top 250 coins by opportunity score, exchange-filtered."""
import json
import time
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config
from src.connectors.coingecko import fetch_ohlcv
from src.agents.technical_analyst import compute_ta

STABLECOINS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDD", "FDUSD", "PYUSD",
    "GUSD", "FRAX", "LUSD", "SUSD", "CUSD", "RAI", "MIM", "UST", "USDP",
    "USDE", "USDS", "EURC", "EURT", "USD1", "STABLE",
    # Gold-backed tokens treated as stablecoins for scanner purposes
    "XAUT", "PAXG",
}

WRAPPED_TOKENS = {
    "WBTC", "WETH", "WBNB", "WMATIC", "WSOL",
    "STETH", "CBETH", "RETH", "WSTETH", "OSETH",
}

# Kraken uses non-standard tickers for a few coins
_KRAKEN_REMAP = {"XBT": "BTC", "XDG": "DOGE"}


def _get_kraken_symbols() -> set[str]:
    """Fetch available base currencies from Kraken public API (no auth needed)."""
    try:
        url = "https://api.kraken.com/0/public/AssetPairs"
        with httpx.Client(timeout=15) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()

        bases = set()
        for pair in data.get("result", {}).values():
            wsname = pair.get("wsname", "")
            if "/" in wsname:
                base = wsname.split("/")[0]
                base = _KRAKEN_REMAP.get(base, base)
                bases.add(base.upper())
        return bases
    except Exception as e:
        print(f"  Warning: Kraken API failed ({e}), using fallback list")
        return {
            "BTC", "ETH", "SOL", "ADA", "DOT", "LINK", "AVAX", "ATOM", "XRP",
            "LTC", "BCH", "UNI", "AAVE", "MKR", "COMP", "YFI", "GRT", "FIL",
            "EOS", "XTZ", "ALGO", "XLM", "TRX", "VET", "ETC", "XMR", "ZEC",
            "DASH", "MANA", "SAND", "AXS", "FLOW", "CHZ", "ENJ", "LRC", "STORJ",
            "OCEAN", "KAVA", "CRV", "1INCH", "SUSHI", "SNX", "ZRX", "BAT",
            "MATIC", "NEAR", "FTM", "HBAR", "ICP", "EGLD", "THETA", "KSM",
            "RUNE", "INJ", "OP", "ARB", "SUI", "APT", "TIA", "SEI", "PYTH",
            "WIF", "BONK", "PEPE", "RENDER", "STX", "IMX", "BLUR", "ENS",
        }


def _get_revolut_symbols() -> set[str]:
    """Return Revolut X tradeable coins from config."""
    return set(config.REVOLUT_X_COINS)


def _fetch_top_250() -> list[dict]:
    """Fetch top 250 coins by market cap from CoinGecko /coins/markets."""
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 250,
        "page": 1,
        "price_change_percentage": "24h,7d",
        "sparkline": "false",
    }
    headers = {}
    if config.COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = config.COINGECKO_API_KEY

    with httpx.Client(timeout=30) as client:
        resp = client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()


def _check_rug_pull(coin: dict) -> tuple[bool, str]:
    """
    Auto-detect rug pulls / panic selling. Returns (is_rug_pull, reason).

    Rug pull:    7d drop > 70%  AND  24h price still negative (still falling)
    Panic sell:  vol/mcap > 0.9x  AND  24h drop > 20%  (BOTH required)

    High volume alone is NOT a rug signal — XPL (+27% 7d), MON (-6% 7d) etc.
    are legitimate projects with high trading interest and must not be excluded.
    """
    change_7d  = coin.get("price_change_percentage_7d_in_currency") or 0
    change_24h = coin.get("price_change_percentage_24h") or 0
    volume     = coin.get("total_volume") or 0
    market_cap = coin.get("market_cap") or 1

    # Massive sustained crash — dead project or rug
    if change_7d < -70 and change_24h < 0:
        return True, f"7d drop {change_7d:.1f}% + still falling {change_24h:.1f}% 24h"

    # Panic selling: extreme volume spike AND sharp 24h drop (both required)
    if market_cap > 0 and (volume / market_cap) > 0.90 and change_24h < -20:
        return True, f"panic: vol/mcap {volume/market_cap:.2f}x + 24h {change_24h:.1f}%"

    return False, ""


def _quick_score(coin: dict, trending_symbols: set[str] | None = None) -> tuple[int, list[str]]:
    """Score from market data alone (no OHLCV needed)."""
    score = 0
    reasons = []

    change_7d  = coin.get("price_change_percentage_7d_in_currency") or 0
    volume     = coin.get("total_volume") or 0
    market_cap = coin.get("market_cap") or 1

    if change_7d < -15:
        score += 1
        reasons.append(f"7d dip {change_7d:.1f}%")

    if volume > market_cap * 0.1:
        score += 1
        reasons.append(f"vol/mcap {volume/market_cap:.2f}x")

    if trending_symbols and coin.get("symbol", "").upper() in trending_symbols:
        score += 1
        reasons.append("CMC trending")

    return score, reasons


def _ta_score(rsi, macd_signal, bb_position) -> tuple[int, list[str]]:
    """Score from TA indicators."""
    score = 0
    reasons = []

    if rsi is not None:
        if rsi < 30:
            score += 3
            reasons.append(f"RSI {rsi:.1f} oversold")
        elif rsi < 40:
            score += 1
            reasons.append(f"RSI {rsi:.1f} near oversold")

    if macd_signal == "BULLISH":
        score += 2
        reasons.append("MACD bullish")

    if bb_position == "BELOW_LOWER":
        score += 2
        reasons.append("below lower BB")

    return score, reasons


def run_smart_scanner(exchange: str | None = None) -> tuple[list[dict], list[dict]]:
    """
    Fetch top 250 coins, exclude stablecoins/wrapped tokens, optionally filter
    by exchange, score by TA opportunity, return top 10 ranked results.

    exchange: None (no filter) | "kraken" | "revolut" | "both"
    """
    label = exchange.upper() if exchange else "ALL EXCHANGES"
    print("\n" + "=" * 60)
    print(f"  SMART SCANNER — Top 250 Coins [{label}]")
    print("=" * 60)

    # 1. Build allowed symbol set (None = no exchange filter)
    allowed: set[str] | None = None
    if exchange:
        ex = exchange.lower()
        if ex == "kraken":
            print("\n  Fetching Kraken tradeable pairs...")
            allowed = _get_kraken_symbols()
        elif ex == "revolut":
            allowed = _get_revolut_symbols()
        elif ex == "both":
            print("\n  Fetching Kraken tradeable pairs...")
            allowed = _get_kraken_symbols() | _get_revolut_symbols()
        if allowed is not None:
            print(f"  {len(allowed)} unique base assets on {label}")

    # 1b. Fetch CMC trending symbols for bonus scoring (optional, no-op if key missing)
    trending_symbols: set[str] = set()
    try:
        from src.connectors.coinmarketcap import fetch_trending as _cmc_trending
        trending_symbols = set(_cmc_trending())
        if trending_symbols:
            print(f"  CMC trending: {', '.join(sorted(trending_symbols))}")
    except Exception:
        pass

    # 2. Top 250 market data
    print("  Fetching top 250 from CoinGecko...")
    try:
        coins = _fetch_top_250()
    except Exception as e:
        print(f"  ERROR: {e}")
        return [], []
    print(f"  Got {len(coins)} coins")

    # 3. Filter out stablecoins and wrapped tokens
    excluded = STABLECOINS | WRAPPED_TOKENS
    clean_coins = [c for c in coins if c.get("symbol", "").upper() not in excluded]
    print(f"  {len(coins) - len(clean_coins)} stablecoins/wrapped tokens removed → {len(clean_coins)} remain")

    # 4. Filter to exchange-available only (skip if no exchange specified)
    if allowed is not None:
        exchange_coins = [c for c in clean_coins if c.get("symbol", "").upper() in allowed]
        print(f"  {len(exchange_coins)} coins available on {label}")
    else:
        exchange_coins = clean_coins

    # 4b. Separate pumped coins (>100% 7d gain) — assess separately, not in main ranking
    pump_coins = [
        c for c in exchange_coins
        if (c.get("price_change_percentage_7d_in_currency") or 0) > 100
    ]
    exchange_coins = [c for c in exchange_coins if c not in pump_coins]
    if pump_coins:
        print(f"  {len(pump_coins)} pump alert(s) (>100% 7d) — sent to LLM for review")

    # 4c. Exclude coins already in the user's portfolio (no point recommending what's owned)
    try:
        with open(config.PORTFOLIO_PATH) as _pf:
            portfolio_symbols = {
                h["asset"].upper()
                for h in json.load(_pf).get("holdings", [])
            }
        pre_pf = len(exchange_coins)
        exchange_coins = [
            c for c in exchange_coins
            if c.get("symbol", "").upper() not in portfolio_symbols
        ]
        excluded_pf = pre_pf - len(exchange_coins)
        if excluded_pf:
            print(f"  {excluded_pf} portfolio coin(s) excluded ({', '.join(portfolio_symbols)})")
    except Exception:
        pass

    # 4d. Auto-detect rug pulls — exclude from scoring, show in separate section
    rug_pull_coins: list[tuple[str, float, str]] = []  # (symbol, price, reason)
    safe_coins = []
    for coin in exchange_coins:
        is_rug, reason = _check_rug_pull(coin)
        if is_rug:
            rug_pull_coins.append((
                coin.get("symbol", "").upper(),
                coin.get("current_price", 0),
                reason,
            ))
        else:
            safe_coins.append(coin)
    if rug_pull_coins:
        print(f"  {len(rug_pull_coins)} rug pull(s) detected and excluded from scoring")
    exchange_coins = safe_coins

    # 5. Quick-score all, take top 40 for OHLCV analysis
    quick_scored = []
    for coin in exchange_coins:
        qs, qr = _quick_score(coin, trending_symbols)
        quick_scored.append((coin, qs, qr))
    quick_scored.sort(key=lambda x: x[1], reverse=True)
    candidates = quick_scored[:40]

    # 6. Fetch OHLCV + compute TA for each candidate
    print(f"\n  Computing TA for {len(candidates)} candidates (~{len(candidates) * 2}s)...")
    results = []
    for i, (coin, qs, qr) in enumerate(candidates):
        coin_id = coin["id"]
        symbol = coin["symbol"].upper()
        try:
            ohlcv = fetch_ohlcv(coin_id, days=30)
            if not ohlcv or len(ohlcv) < 20:
                continue
            ta = compute_ta(coin_id, symbol, ohlcv)
            ts, tr = _ta_score(ta.rsi_14, ta.macd_signal, ta.bollinger_position)

            results.append({
                "coin_id": coin_id,
                "symbol": symbol,
                "name": coin.get("name", ""),
                "price": coin.get("current_price", 0),
                "change_24h": coin.get("price_change_percentage_24h") or 0,
                "change_7d": coin.get("price_change_percentage_7d_in_currency") or 0,
                "market_cap": coin.get("market_cap") or 0,
                "score": qs + ts,
                "reasons": qr + tr,
                "rsi": ta.rsi_14,
                "macd": ta.macd_signal,
                "bb_pos": ta.bollinger_position,
                "trend": ta.trend,
            })
        except Exception:
            pass

        time.sleep(2)  # Stay within CoinGecko rate limits (~30/min)
        if (i + 1) % 10 == 0:
            print(f"    {i+1}/{len(candidates)} done...")

    results.sort(key=lambda x: x["score"], reverse=True)
    top10 = results[:10]

    # 7. Display
    def _pfmt(p: float) -> str:
        if p >= 1:      return f"${p:,.2f}"
        if p >= 0.01:   return f"${p:.4f}"
        if p >= 0.0001: return f"${p:.6f}"
        return f"${p:.8f}"

    print(f"\n  TOP 10 OPPORTUNITIES ({label})\n" + "-" * 60)
    for rank, r in enumerate(top10, 1):
        print(f"\n  {rank}. {r['symbol']} ({r['name']})  —  score: {r['score']} pts")
        print(f"     Price: {_pfmt(r['price'])}  |  24h: {r['change_24h']:+.1f}%  |  7d: {r['change_7d']:+.1f}%")
        rsi_str = f"RSI {r['rsi']:.1f}" if r['rsi'] else "RSI N/A"
        print(f"     {rsi_str}  |  MACD: {r['macd']}  |  BB: {r['bb_pos']}  |  Trend: {r['trend']}")
        print(f"     Signals: {', '.join(r['reasons']) if r['reasons'] else 'none'}")

    if pump_coins:
        print(f"\n  ⚠  PUMP ALERTS  (>100% 7d gain — breakout or P&D?)\n" + "-" * 60)
        for c in pump_coins:
            sym      = c.get("symbol", "").upper()
            price    = c.get("current_price", 0)
            ch24     = c.get("price_change_percentage_24h") or 0
            ch7d     = c.get("price_change_percentage_7d_in_currency") or 0
            mcap_m   = (c.get("market_cap") or 0) / 1e6
            print(
                f"  !! {sym:10s}  {_pfmt(price)}  |  24h: {ch24:+.1f}%"
                f"  |  7d: {ch7d:+.1f}%  |  MCap: ${mcap_m:.0f}M"
            )

    if rug_pull_coins:
        print(f"\n  ⛔  RUG PULL DETECTED  (excluded — not sent to LLM)\n" + "-" * 60)
        for sym, price, reason in rug_pull_coins:
            print(f"  ✗  {sym:10s}  {_pfmt(price)}  —  {reason}")

    return top10, pump_coins
