"""
Stock market connector using yfinance (free, no API key required).
Fetches price, fundamentals, OHLCV, earnings data, and headlines.
"""
import sys
from datetime import datetime, timezone, timedelta, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# US federal holidays (month, day) — NYSE is closed these days
_NYSE_HOLIDAYS = {
    (1, 1),   # New Year's Day
    (1, 20),  # MLK Day (approx — 3rd Mon Jan)
    (2, 17),  # Presidents' Day (approx — 3rd Mon Feb)
    (4, 18),  # Good Friday 2025
    (4, 3),   # Good Friday 2026 (update annually)
    (5, 26),  # Memorial Day 2025 (last Mon May)
    (5, 25),  # Memorial Day 2026
    (6, 19),  # Juneteenth
    (7, 4),   # Independence Day
    (9, 1),   # Labor Day 2025
    (9, 7),   # Labor Day 2026
    (11, 27), # Thanksgiving 2025
    (11, 26), # Thanksgiving 2026
    (12, 25), # Christmas
}


def _et_now() -> datetime:
    """Return current time in US Eastern Time (DST-aware approximation)."""
    now_utc = datetime.now(timezone.utc)
    month   = now_utc.month
    # DST: 2nd Sun Mar → 1st Sun Nov = EDT (UTC-4); else EST (UTC-5)
    offset  = timedelta(hours=-4 if 4 <= month <= 10 else -5)
    return now_utc + offset


def market_session() -> str:
    """
    Return current NYSE session status:
      'OPEN'        — regular trading hours (09:30-16:00 ET)
      'PRE_MARKET'  — 04:00-09:30 ET weekday
      'AFTER_HOURS' — 16:00-20:00 ET weekday
      'CLOSED'      — weekend, holiday, or outside all sessions
    """
    now_et = _et_now()
    # Weekend
    if now_et.weekday() >= 5:
        return "CLOSED"
    # Holiday
    if (now_et.month, now_et.day) in _NYSE_HOLIDAYS:
        return "CLOSED"
    t = now_et.time()
    if dtime(9, 30) <= t < dtime(16, 0):
        return "OPEN"
    if dtime(4, 0) <= t < dtime(9, 30):
        return "PRE_MARKET"
    if dtime(16, 0) <= t < dtime(20, 0):
        return "AFTER_HOURS"
    return "CLOSED"


def is_nyse_open() -> bool:
    """Return True only during regular NYSE trading hours."""
    return market_session() == "OPEN"

# S&P 500 top 30 by market cap
SP500_TOP30 = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","BRK-B","LLY","JPM",
    "AVGO","TSLA","UNH","V","XOM","MA","COST","HD","PG","JNJ",
    "MRK","ABBV","CRM","BAC","NFLX","AMD","ORCL","KO","WMT","CVX",
]

# NASDAQ tech watchlist (SQ removed — delisted/merged)
NASDAQ_TECH = [
    "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","AMD","INTC",
    "CRM","NFLX","SNOW","PLTR","COIN","MSTR","PYPL","SHOP","UBER","ABNB",
]

# Crypto-related stocks
CRYPTO_STOCKS = [
    "COIN","MSTR","MARA","RIOT","CLSK","HUT","BITF",
]

# Combined unique watchlist
STOCK_WATCHLIST = sorted(set(SP500_TOP30 + NASDAQ_TECH + CRYPTO_STOCKS))

# Approximate sector P/E averages for fundamental scoring
SECTOR_PE = {
    "Technology":           35,
    "Communication Services": 25,
    "Consumer Cyclical":    30,
    "Consumer Defensive":   22,
    "Healthcare":           25,
    "Financial Services":   15,
    "Energy":               12,
    "Industrials":          22,
    "Basic Materials":      18,
    "Real Estate":          40,
    "Utilities":            20,
}


def fetch_stock_data(symbols: list[str]) -> list[dict]:
    """
    Fetch price, fundamentals, OHLCV (3mo), headlines, and earnings data.

    Returns list of dicts with keys:
        symbol, name, price, change_24h, change_7d, market_cap,
        pe_ratio, sector, sector_pe_avg, revenue_growth,
        earnings_surprise,   # +pct if beat, -pct if miss, None if N/A
        ohlcv,               # [[ts, o, h, l, c, v], ...]  3mo daily
        avg_volume,          # 30-day average volume
        headlines,           # list[str] from yfinance .news
    """
    try:
        import yfinance as yf
    except ImportError:
        print("  ERROR: yfinance not installed. Run: pip install yfinance")
        return []

    results = []
    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)
            info   = ticker.info or {}

            # Skip if no price data (delisted / bad ticker)
            price = info.get("currentPrice") or info.get("regularMarketPrice") or 0.0
            if not price:
                continue

            prev_close = info.get("previousClose") or price
            change_24h = ((price - prev_close) / prev_close * 100) if prev_close else 0.0

            # 3mo daily OHLCV — enough bars for RSI(14), MACD, BB, MA50/200
            hist = ticker.history(period="3mo", interval="1d")

            change_7d = 0.0
            avg_volume = 0.0
            ohlcv: list[list] = []

            if not hist.empty:
                closes = hist["Close"]
                if len(hist) >= 8:
                    change_7d = (float(closes.iloc[-1]) - float(closes.iloc[-8])) / float(closes.iloc[-8]) * 100
                elif len(hist) >= 2:
                    change_7d = (float(closes.iloc[-1]) - float(closes.iloc[0])) / float(closes.iloc[0]) * 100

                volumes = hist["Volume"]
                avg_volume = float(volumes.mean()) if len(volumes) >= 5 else 0.0

                for ts, row in hist.iterrows():
                    ohlcv.append({
                        "timestamp": ts,
                        "open":   float(row["Open"]),
                        "high":   float(row["High"]),
                        "low":    float(row["Low"]),
                        "close":  float(row["Close"]),
                        "volume": float(row["Volume"]),
                    })

            # Fundamentals
            sector      = info.get("sector", "")
            sector_pe   = SECTOR_PE.get(sector, 25)
            pe_ratio    = info.get("trailingPE") or info.get("forwardPE")
            rev_growth  = info.get("revenueGrowth")  # e.g. 0.12 = 12% YoY

            # Earnings surprise from last quarter
            earnings_surprise = None
            try:
                cal = ticker.earnings_dates
                if cal is not None and not cal.empty:
                    recent = cal.dropna(subset=["Surprise(%)"]).head(1)
                    if not recent.empty:
                        earnings_surprise = float(recent["Surprise(%)"].iloc[0])
            except Exception:
                pass

            # Recent headlines from yfinance
            headlines: list[str] = []
            try:
                news_items = ticker.news or []
                for item in news_items[:8]:
                    title = item.get("title") or ""
                    if title:
                        headlines.append(title)
            except Exception:
                pass

            results.append({
                "symbol":            sym,
                "name":              info.get("longName") or info.get("shortName") or sym,
                "price":             float(price),
                "change_24h":        round(change_24h, 2),
                "change_7d":         round(change_7d, 2),
                "market_cap":        info.get("marketCap") or 0,
                "pe_ratio":          pe_ratio,
                "sector":            sector,
                "sector_pe_avg":     sector_pe,
                "revenue_growth":    rev_growth,
                "earnings_surprise": earnings_surprise,
                "avg_volume":        avg_volume,
                "ohlcv":             ohlcv,
                "headlines":         headlines,
            })
        except Exception as e:
            print(f"  Warning: could not fetch {sym}: {e}")
            continue

    return results
