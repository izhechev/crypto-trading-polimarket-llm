"""Binance exchange connector — live prices, OHLCV, and portfolio via ccxt.

Supports both Spot and Futures (USDT-M) markets to resolve all tradeable assets.
Public endpoints work without API keys.
"""
import time
import ccxt
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

# ── In-memory cache ──────────────────────────────────────────────────────
_cache: dict[str, tuple[float, object]] = {}
CACHE_TTL = 120  # 2 minutes

def _get_cached(key: str):
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None

def _set_cache(key: str, data):
    _cache[key] = (time.time(), data)

# ── Symbol mapping (Binance symbol → CoinGecko coin_id) ─────────────────
_COIN_IDS: dict[str, str] = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "POPCAT": "popcat",
    "MOODENG": "moo-deng", "BRETT": "based-brett", "MOG": "mog-coin",
    "MEW": "cat-in-a-dogs-world", "WIF": "dogwifcoin", "BONK": "bonk",
}

_FIAT_AND_STABLES = {"USD", "EUR", "USDT", "USDC", "BUSD", "DAI"}

# ── Singletons for efficiency ───────────────────────────────────────────
_EXCHANGE_SPOT = None
_EXCHANGE_FUT  = None

def _get_exchanges():
    global _EXCHANGE_SPOT, _EXCHANGE_FUT
    if _EXCHANGE_SPOT is None:
        _EXCHANGE_SPOT = ccxt.binance({"enableRateLimit": True})
        try: _EXCHANGE_SPOT.load_markets()
        except Exception: pass
    if _EXCHANGE_FUT is None:
        _EXCHANGE_FUT = ccxt.binanceusdm({"enableRateLimit": True})
        try: _EXCHANGE_FUT.load_markets()
        except Exception: pass
    return _EXCHANGE_SPOT, _EXCHANGE_FUT

def fetch_binance_futures_data(symbol: str) -> dict | None:
    """Fetch Funding Rate and Open Interest for a symbol."""
    cache_key = f"bn_futures_{symbol.upper()}"
    cached = _get_cached(cache_key)
    if cached is not None: return cached

    sym = symbol.upper()
    try:
        _, fut = _get_exchanges()
        if not fut: return None
        
        pair = f"{sym}/USDT:USDT"
        if pair not in fut.markets:
            if f"{sym}/USDC:USDC" in fut.markets: pair = f"{sym}/USDC:USDC"
            else: return None
        
        funding = fut.fetch_funding_rate(pair)
        oi_data = fut.fetch_open_interest(pair)
        
        result = {
            "symbol": sym,
            "funding_rate": round(funding.get("fundingRate", 0) * 100, 5),
            "oi_usd": round(oi_data.get("baseVolume", 0) * oi_data.get("last", 0), 0),
            "timestamp": time.time(),
        }
        _set_cache(cache_key, result)
        return result
    except Exception:
        return None

def fetch_binance_orderbook(symbol: str, limit: int = 20) -> dict | None:
    """Fetch order book for a symbol. NO ERROR PRINTS."""
    sym = symbol.upper()
    try:
        spot, fut = _get_exchanges()
        book = None
        # Try Spot first, then Futures
        if spot and f"{sym}/USDT" in spot.markets:
            book = spot.fetch_order_book(f"{sym}/USDT", limit=limit)
        elif fut and f"{sym}/USDT:USDT" in fut.markets:
            book = fut.fetch_order_book(f"{sym}/USDT:USDT", limit=limit)
        elif spot and f"{sym}/USDC" in spot.markets:
            book = spot.fetch_order_book(f"{sym}/USDC", limit=limit)
        
        if not book: return None

        best_bid = book["bids"][0][0] if book.get("bids") else 0
        best_ask = book["asks"][0][0] if book.get("asks") else 0
        spread = ((best_ask - best_bid) / best_bid * 100) if best_bid else 0
        return {
            "bids": book.get("bids", [])[:10],
            "asks": book.get("asks", [])[:10],
            "spread_pct": round(spread, 4),
        }
    except Exception:
        return None

def get_binance_symbols() -> set[str]:
    """Return all tradeable USDT base symbols from Binance."""
    cache_key = "bn_symbols_all"
    cached = _get_cached(cache_key)
    if cached is not None: return cached
    try:
        spot, _ = _get_exchanges()
        symbols = {m["base"] for m in spot.markets.values() if m.get("quote") == "USDT" and m.get("active")}
        _cache[cache_key] = (time.time(), symbols)
        return symbols
    except Exception:
        return set()

def fetch_binance_portfolio():
    """Fetch live balances (needs API key)."""
    if not config.BINANCE_API_KEY: return None, "no credentials"
    try:
        spot = ccxt.binance({"apiKey": config.BINANCE_API_KEY, "secret": config.BINANCE_API_SECRET})
        bal = spot.fetch_balance()
        return [{"asset": k, "amount": v["total"]} for k, v in bal["total"].items() if v["total"] > 0], "Binance"
    except Exception as e:
        return None, str(e)
