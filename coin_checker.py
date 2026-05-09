#!/usr/bin/env python3
"""
coin_checker.py  -  Quick price + technical snapshot for any CoinGecko coin.

Usage:
    python coin_checker.py notcoin
    python coin_checker.py NOT
    python coin_checker.py bitcoin
    python coin_checker.py ethereum --days 7
"""
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import argparse
import httpx
import pandas as pd
import pandas_ta as ta

try:
    import config
    _CG_KEY = getattr(config, "COINGECKO_API_KEY", "")
except Exception:
    _CG_KEY = ""

_BASE = "https://pro-api.coingecko.com/api/v3" if _CG_KEY else "https://api.coingecko.com/api/v3"
_HDR  = {"x-cg-pro-api-key": _CG_KEY} if _CG_KEY else {}


# ── helpers ──────────────────────────────────────────────────────────────────

def _get(path: str, **params):
    r = httpx.get(f"{_BASE}{path}", params=params, headers=_HDR, timeout=30)
    r.raise_for_status()
    return r.json()


def resolve(query: str) -> tuple[str, str, str]:
    """Return (coin_id, name, symbol) for a name/symbol/id query."""
    # 1. Try exact ID match first (cheapest call)
    try:
        d = _get(
            f"/coins/{query.lower()}",
            localization="false", tickers="false",
            market_data="false", community_data="false",
        )
        return d["id"], d["name"], d["symbol"].upper()
    except Exception:
        pass

    # 2. Full-text search
    coins = _get("/search", query=query).get("coins", [])
    q_up  = query.upper()
    match = (
        next((c for c in coins if c.get("symbol", "").upper() == q_up), None)
        or next((c for c in coins if query.lower() in c.get("name", "").lower()), None)
        or (coins[0] if coins else None)
    )
    if not match:
        raise SystemExit(f"Coin not found: {query}")
    return match["id"], match["name"], match["symbol"].upper()


def _series_from_chart(data: dict) -> pd.Series:
    pts = data["prices"]
    return pd.Series(
        [p[1] for p in pts],
        index=pd.to_datetime([p[0] for p in pts], unit="ms", utc=True),
        name="price",
    ).sort_index()


def price_history_short(coin_id: str) -> pd.Series:
    """1-day window — CoinGecko returns ~5-min granularity."""
    return _series_from_chart(_get(f"/coins/{coin_id}/market_chart", vs_currency="usd", days=1))


def price_history_long(coin_id: str) -> pd.Series:
    """7-day window — CoinGecko returns hourly granularity."""
    return _series_from_chart(_get(f"/coins/{coin_id}/market_chart", vs_currency="usd", days=7))


def ohlcv_for_ta(coin_id: str, days: int) -> pd.DataFrame:
    """Fetch OHLC candles for TA indicators (14-period RSI needs ≥14 bars)."""
    data = _get(f"/coins/{coin_id}/ohlc", vs_currency="usd", days=str(days))
    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close"])
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    return df.sort_index()


def nearest_price(series: pd.Series, minutes_ago: int) -> float | None:
    target = pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=minutes_ago)
    if series.empty:
        return None
    diffs = abs(series.index - target)
    gap   = diffs.min()
    # Allow up to 90-min gap for short windows, 4-hour gap for multi-day windows
    tolerance = pd.Timedelta(minutes=90) if minutes_ago <= 720 else pd.Timedelta(hours=4)
    if gap > tolerance:
        return None
    return float(series.iloc[diffs.argmin()])


def fmt_price(price: float) -> str:
    if price >= 1_000:
        return f"${price:,.2f}"
    elif price >= 1:
        return f"${price:,.4f}"
    elif price >= 0.0001:
        return f"${price:.6f}"
    else:
        return f"${price:.8f}"


def fmt_pct(old: float | None, new: float) -> str:
    if old is None:
        return ""
    pct = (new - old) / old * 100
    sign = "+" if pct >= 0 else ""
    arrow = "▲" if pct > 0.005 else ("▼" if pct < -0.005 else "─")
    return f"  {arrow} {sign}{pct:.2f}%"


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Coin technical snapshot")
    parser.add_argument("coin", nargs="+", help="Coin name, symbol, or CoinGecko ID")
    parser.add_argument("--days", type=int, default=14,
                        help="Days of OHLC history for TA indicators (default: 14)")
    args = parser.parse_args()
    query = " ".join(args.coin)

    print(f"\nLooking up: {query} ...")
    coin_id, name, symbol = resolve(query)
    print(f"Found: {name} ({symbol})  |  CoinGecko ID: {coin_id}\n")

    print("Fetching price history (1d fine-grained) ...")
    ph_short = price_history_short(coin_id)

    print("Fetching price history (7d hourly) ...")
    ph_long = price_history_long(coin_id)

    print(f"Fetching OHLC ({args.days}d) for indicators ...")
    ohlcv = ohlcv_for_ta(coin_id, args.days)

    current = float(ph_short.iloc[-1]) if not ph_short.empty else None
    # Short-window lookbacks use fine-grained 1-day series
    p15m = nearest_price(ph_short, 15)
    p1h  = nearest_price(ph_short, 60)
    p4h  = nearest_price(ph_short, 240)
    p6h  = nearest_price(ph_short, 360)
    p8h  = nearest_price(ph_short, 480)
    p12h = nearest_price(ph_short, 720)
    # Long-window lookbacks use 7-day hourly series
    p24h = nearest_price(ph_long, 1440)
    p3d  = nearest_price(ph_long, 4320)
    p7d  = nearest_price(ph_long, 10080)

    close = ohlcv["close"]

    rsi_s    = ta.rsi(close, length=14)
    macd_df  = ta.macd(close, fast=12, slow=26, signal=9)
    bb_df    = ta.bbands(close, length=20, std=2.0)
    ema20_s  = ta.ema(close, length=20)
    ema50_s  = ta.ema(close, length=50)

    rsi      = float(rsi_s.dropna().iloc[-1])         if rsi_s   is not None and not rsi_s.dropna().empty   else None
    macd_val = float(macd_df["MACD_12_26_9"].dropna().iloc[-1])  if macd_df is not None else None
    macd_sig = float(macd_df["MACDs_12_26_9"].dropna().iloc[-1]) if macd_df is not None else None
    macd_h   = float(macd_df["MACDh_12_26_9"].dropna().iloc[-1]) if macd_df is not None else None
    # BB column names vary by pandas_ta version — pick by prefix
    def _bb_col(df, prefix):
        col = next((c for c in df.columns if c.startswith(prefix)), None)
        return float(df[col].dropna().iloc[-1]) if col else None

    bb_u = _bb_col(bb_df, "BBU_") if bb_df is not None else None
    bb_m = _bb_col(bb_df, "BBM_") if bb_df is not None else None
    bb_l = _bb_col(bb_df, "BBL_") if bb_df is not None else None
    e20      = float(ema20_s.dropna().iloc[-1])        if ema20_s is not None and not ema20_s.dropna().empty else None
    e50      = float(ema50_s.dropna().iloc[-1])        if ema50_s is not None and not ema50_s.dropna().empty else None

    W = 60
    BAR = "─" * W
    print(f"\n{'═' * W}")
    print(f"  {name} ({symbol})   [{coin_id}]")
    print(f"{'═' * W}")

    # ── Price history ──
    print(f"\n  PRICE HISTORY")
    print(f"  {BAR}")
    rows = [
        ("Now",              current),
        ("15 min ago",       p15m),
        ("1 hour ago",       p1h),
        ("4 hours ago",      p4h),
        ("6 hours ago",      p6h),
        ("8 hours ago",      p8h),
        ("12 hours ago",     p12h),
        ("24h / 1d ago",     p24h),
        ("3 days ago",       p3d),
        ("7 days ago",       p7d),
    ]
    for label, price in rows:
        price_str = fmt_price(price) if price else "N/A"
        pct_str   = fmt_pct(price, current) if (price and current and price != current) else ""
        print(f"  {label:<18s}  {price_str:>14s}{pct_str}")

    # ── Technical indicators ──
    print(f"\n  TECHNICAL INDICATORS  (OHLC  {args.days}d)")
    print(f"  {BAR}")

    if rsi is not None:
        zone = "Overbought (>70)" if rsi > 70 else ("Oversold (<30)" if rsi < 30 else "Neutral")
        print(f"  {'RSI (14)':<22s}  {rsi:6.1f}   [{zone}]")

    if macd_val is not None:
        trend = "Bullish" if macd_val > macd_sig else "Bearish"
        print(f"  {'MACD (12/26/9)':<22s}  {macd_val:+.8f}  [{trend}]")
        print(f"  {'  Signal':<22s}  {macd_sig:+.8f}")
        print(f"  {'  Histogram':<22s}  {macd_h:+.8f}")

    if bb_u is not None and current:
        pos = "Above upper band" if current > bb_u else ("Below lower band" if current < bb_l else "Inside bands")
        print(f"  {'BB Upper (20, 2σ)':<22s}  {fmt_price(bb_u):>14s}")
        print(f"  {'BB Middle':<22s}  {fmt_price(bb_m):>14s}")
        print(f"  {'BB Lower':<22s}  {fmt_price(bb_l):>14s}  [{pos}]")

    if e20 is not None and current:
        rel = "above" if current > e20 else "below"
        print(f"  {'EMA 20':<22s}  {fmt_price(e20):>14s}  [price {rel}]")
    if e50 is not None and current:
        rel = "above" if current > e50 else "below"
        print(f"  {'EMA 50':<22s}  {fmt_price(e50):>14s}  [price {rel}]")

    print(f"\n{'═' * W}\n")


if __name__ == "__main__":
    main()
