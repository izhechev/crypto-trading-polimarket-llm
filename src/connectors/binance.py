"""Binance exchange connector — live prices, OHLCV, and portfolio via ccxt.

Supports both Binance.com and Binance US. Falls back gracefully if no
API keys are configured (public endpoints still work for prices/OHLCV).

Usage:
    from src.connectors.binance import (
        fetch_binance_ticker,
        fetch_binance_ohlcv,
        fetch_binance_portfolio,
        get_binance_symbols,
    )
"""
import time
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
    "BTC":    "bitcoin",
    "ETH":    "ethereum",
    "BNB":    "binancecoin",
    "SOL":    "solana",
    "XRP":    "ripple",
    "ADA":    "cardano",
    "DOT":    "polkadot",
    "LINK":   "chainlink",
    "AVAX":   "avalanche-2",
    "ATOM":   "cosmos",
    "LTC":    "litecoin",
    "BCH":    "bitcoin-cash",
    "UNI":    "uniswap",
    "AAVE":   "aave",
    "MKR":    "maker",
    "COMP":   "compound-governance-token",
    "GRT":    "the-graph",
    "CRV":    "curve-dao-token",
    "SNX":    "havven",
    "BAT":    "basic-attention-token",
    "FIL":    "filecoin",
    "INJ":    "injective-protocol",
    "RENDER": "render-token",
    "NEAR":   "near",
    "OP":     "optimism",
    "ARB":    "arbitrum",
    "SUI":    "sui",
    "APT":    "aptos",
    "TIA":    "celestia",
    "SEI":    "sei-network",
    "PEPE":   "pepe",
    "DOGE":   "dogecoin",
    "SHIB":   "shiba-inu",
    "MATIC":  "matic-network",
    "FTM":    "fantom",
    "ALGO":   "algorand",
    "XLM":    "stellar",
    "TRX":    "tron",
    "ETC":    "ethereum-classic",
    "MANA":   "decentraland",
    "SAND":   "the-sandbox",
    "AXS":    "axie-infinity",
    "ENJ":    "enjincoin",
    "FET":    "fetch-ai",
    "WIF":    "dogwifcoin",
    "BONK":   "bonk",
    "FLOKI":  "floki",
    "JUP":    "jupiter-exchange-solana",
    "WLD":    "worldcoin-wld",
    "PENDLE": "pendle",
    "STX":    "blockstack",
    "RUNE":   "thorchain",
    "ICP":    "internet-computer",
    "HBAR":   "hedera-hashgraph",
    "VET":    "vechain",
    "THETA":  "theta-token",
    "ENA":    "ethena",
    "TON":    "the-open-network",
    "TAO":    "bittensor",
    "ONDO":   "ondo-finance",
    "PYTH":   "pyth-network",
}

_FIAT_AND_STABLES = {
    "USD", "EUR", "GBP", "USDT", "USDC", "BUSD", "TUSD",
    "DAI", "FDUSD", "USDD", "PYUSD", "GUSD", "FRAX",
}


# ── Exchange factory ─────────────────────────────────────────────────────

def _get_exchange(authenticated: bool = False):
    """Create a ccxt Binance exchange instance."""
    try:
        import ccxt
    except ImportError:
        raise RuntimeError("ccxt not installed. Run: pip install ccxt")

    params = {"enableRateLimit": True}
    if authenticated and config.BINANCE_API_KEY and config.BINANCE_API_SECRET:
        params["apiKey"] = config.BINANCE_API_KEY
        params["secret"] = config.BINANCE_API_SECRET

    return ccxt.binance(params)


# ── Public endpoints (no API key needed) ─────────────────────────────────

def fetch_binance_futures_data(symbol: str) -> dict | None:
    """
    Fetch Funding Rate and Open Interest for a symbol (e.g. 'BTC').
    Uses the Binance Futures API via ccxt.
    """
    cache_key = f"bn_futures_{symbol.upper()}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    sym = symbol.upper()
    try:
        exchange = _get_unified_exchange()
        
        # CCXT futures pairs are typically SYMBOL/USDT:USDT
        pair = f"{sym}/USDT:USDT"
        if pair not in exchange.markets:
            # Try alternate formats
            if f"{sym}/USDC:USDC" in exchange.markets: pair = f"{sym}/USDC:USDC"
            else: return None
        
        # 1. Fetch Funding Rate
        funding = exchange.fetch_funding_rate(pair)
        funding_rate = funding.get("fundingRate", 0) * 100  # as percentage
        
        # 2. Fetch Open Interest
        oi_data = exchange.fetch_open_interest(pair)
        oi_usd = oi_data.get("baseVolume", 0) * oi_data.get("last", 0)
        
        result = {
            "symbol":        sym,
            "funding_rate":  round(funding_rate, 5),
            "oi_usd":        round(oi_usd, 0),
            "timestamp":     time.time(),
        }
        _set_cache(cache_key, result)
        return result
    except Exception:
        return None


def fetch_binance_ticker(symbol: str) -> dict | None:
    """Fetch 24h ticker for a symbol (e.g. 'BTC').

    Returns dict with: price, change_24h, high_24h, low_24h, volume_24h,
    quote_volume, bid, ask.
    Returns None on failure.
    """
    cache_key = f"bn_ticker_{symbol.upper()}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    pair = f"{symbol.upper()}/USDT"
    try:
        exchange = _get_exchange()
        ticker = exchange.fetch_ticker(pair)
        result = {
            "symbol":       symbol.upper(),
            "coin_id":      _COIN_IDS.get(symbol.upper()),
            "price":        ticker.get("last", 0),
            "change_24h":   ticker.get("percentage", 0),
            "high_24h":     ticker.get("high", 0),
            "low_24h":      ticker.get("low", 0),
            "volume_24h":   ticker.get("baseVolume", 0),
            "quote_volume": ticker.get("quoteVolume", 0),
            "bid":          ticker.get("bid", 0),
            "ask":          ticker.get("ask", 0),
            "timestamp":    ticker.get("timestamp"),
        }
        _set_cache(cache_key, result)
        return result
    except Exception:
        return None


def fetch_binance_ohlcv(
    symbol: str,
    timeframe: str = "1d",
    limit: int = 90,
) -> list[list] | None:
    """Fetch OHLCV candles from Binance.

    Returns list of [timestamp, open, high, low, close, volume] or None.
    timeframe: '1m', '5m', '15m', '1h', '4h', '1d', '1w'
    """
    cache_key = f"bn_ohlcv_{symbol.upper()}_{timeframe}_{limit}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    pair = f"{symbol.upper()}/USDT"
    try:
        exchange = _get_exchange()
        candles = exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit)
        _set_cache(cache_key, candles)
        return candles
    except Exception:
        return None


_EXCHANGE_INST = None

def _get_unified_exchange():
    global _EXCHANGE_INST
    if _EXCHANGE_INST is None:
        import ccxt
        _EXCHANGE_INST = ccxt.binance({"enableRateLimit": True})
        _EXCHANGE_INST.load_markets()
    return _EXCHANGE_INST

def fetch_binance_orderbook(symbol: str, limit: int = 20) -> dict | None:
    """Fetch order book for a symbol. Returns {bids, asks, spread}."""
    sym = symbol.upper()
    try:
        exchange = _get_unified_exchange()
        
        # 1. Try standard Spot USDT pair
        # 2. Try standard Futures USDT pair (often where newer coins are)
        # 3. Try standard Spot USDC pair
        pairs_to_try = [f"{sym}/USDT", f"{sym}/USDT:USDT", f"{sym}/USDC"]
        
        book = None
        for pair in pairs_to_try:
            if pair in exchange.markets:
                try:
                    book = exchange.fetch_order_book(pair, limit=limit)
                    break
                except Exception: continue
        
        if not book: return None

        best_bid = book["bids"][0][0] if book.get("bids") else 0
        best_ask = book["asks"][0][0] if book.get("asks") else 0
        spread = ((best_ask - best_bid) / best_bid * 100) if best_bid else 0
        return {
            "bids": book.get("bids", [])[:10],
            "asks": book.get("asks", [])[:10],
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_pct": round(spread, 4),
        }
    except Exception:
        return None


def get_binance_symbols() -> set[str]:
    """Return set of symbols tradeable on Binance (USDT pairs only).

    Cached for 1 hour. Falls back to hardcoded top coins on failure.
    """
    cache_key = "bn_symbols_all"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    try:
        exchange = _get_exchange()
        markets = exchange.load_markets()
        symbols = set()
        for market_id, market in markets.items():
            if market.get("quote") == "USDT" and market.get("active"):
                base = market.get("base", "")
                if base and base not in _FIAT_AND_STABLES:
                    symbols.add(base)
        # Cache for 1 hour
        _cache[cache_key] = (time.time() + 3600 - CACHE_TTL, symbols)
        return symbols
    except Exception:
        return set(_COIN_IDS.keys())


def symbol_to_coin_id(symbol: str) -> str | None:
    """Convert Binance symbol to CoinGecko coin_id."""
    return _COIN_IDS.get(symbol.upper())


# ── Authenticated endpoints (need API key) ───────────────────────────────

def fetch_binance_portfolio() -> tuple[list[dict] | None, str]:
    """Fetch live balances from Binance.

    Returns (holdings, source_description):
      holdings = None   → Binance unreachable / no credentials
      holdings = [...]  → real balances (filters dust < $1)
    """
    if not config.BINANCE_API_KEY or not config.BINANCE_API_SECRET:
        return None, "no Binance credentials"

    try:
        exchange = _get_exchange(authenticated=True)
        balance = exchange.fetch_balance()
    except Exception as e:
        return None, f"Binance API error: {e}"

    total = balance.get("total", {})
    holdings = []

    for symbol, amount in total.items():
        if not amount or float(amount) <= 0:
            continue
        sym = symbol.upper()
        if sym in _FIAT_AND_STABLES:
            continue

        coin_id = _COIN_IDS.get(sym)
        holdings.append({
            "asset":           sym,
            "coin_id":         coin_id,
            "amount":          round(float(amount), 8),
            "entry_price_usd": None,  # Binance doesn't expose avg cost
        })

    return holdings, "Binance API"


def fetch_binance_trades(symbol: str, limit: int = 50) -> list[dict]:
    """Fetch recent trades for a symbol (requires API key).

    Returns list of {side, price, amount, cost, timestamp, fee}.
    """
    if not config.BINANCE_API_KEY or not config.BINANCE_API_SECRET:
        return []

    pair = f"{symbol.upper()}/USDT"
    try:
        exchange = _get_exchange(authenticated=True)
        trades = exchange.fetch_my_trades(pair, limit=limit)
        return [
            {
                "side":      t.get("side"),
                "price":     t.get("price"),
                "amount":    t.get("amount"),
                "cost":      t.get("cost"),
                "timestamp": t.get("datetime"),
                "fee":       t.get("fee", {}).get("cost", 0),
            }
            for t in trades
        ]
    except Exception as e:
        print(f"  [Binance] trades error for {symbol}: {e}")
        return []


# ── WebSocket stream (real-time prices) ──────────────────────────────────

class BinanceStreamer:
    """Lightweight WebSocket price streamer using python-binance.

    Usage:
        streamer = BinanceStreamer(symbols=["BTC", "ETH", "SOL"])
        streamer.start(callback=my_handler)
        # ... later ...
        streamer.stop()

    callback receives: {"symbol": "BTC", "price": 64321.5, "volume": 1234.5, "timestamp": ...}
    """

    def __init__(self, symbols: list[str] | None = None):
        self.symbols = [s.upper() for s in (symbols or ["BTC", "ETH"])]
        self._manager = None
        self._conn_keys = []
        self._running = False

    def start(self, callback=None):
        """Start WebSocket streams for configured symbols."""
        try:
            from binance import ThreadedWebsocketManager
        except ImportError:
            raise RuntimeError(
                "python-binance not installed. Run: pip install python-binance"
            )

        api_key = config.BINANCE_API_KEY or ""
        api_secret = config.BINANCE_API_SECRET or ""

        self._callback = callback or self._default_callback
        self._manager = ThreadedWebsocketManager(
            api_key=api_key,
            api_secret=api_secret,
        )
        self._manager.start()
        self._running = True

        # Subscribe to mini ticker streams for each symbol
        for sym in self.symbols:
            pair = f"{sym.lower()}usdt"
            conn_key = self._manager.start_symbol_miniticker_socket(
                callback=self._handle_message,
                symbol=pair,
            )
            self._conn_keys.append(conn_key)

        print(f"  [Binance WS] Streaming {len(self.symbols)} symbols: "
              f"{', '.join(self.symbols)}")

    def stop(self):
        """Stop all WebSocket streams."""
        if self._manager:
            for key in self._conn_keys:
                try:
                    self._manager.stop_socket(key)
                except Exception:
                    pass
            self._manager.stop()
            self._running = False
            self._conn_keys = []
            print("  [Binance WS] Streams stopped")

    def _handle_message(self, msg):
        """Parse incoming WebSocket message and forward to callback."""
        if msg.get("e") == "error":
            print(f"  [Binance WS] Error: {msg}")
            return

        try:
            # Mini ticker format: s=symbol, c=close price, v=volume, E=timestamp
            raw_symbol = msg.get("s", "")
            if raw_symbol.endswith("USDT"):
                symbol = raw_symbol[:-4]
            else:
                symbol = raw_symbol

            data = {
                "symbol":    symbol,
                "price":     float(msg.get("c", 0)),
                "volume":    float(msg.get("v", 0)),
                "high":      float(msg.get("h", 0)),
                "low":       float(msg.get("l", 0)),
                "timestamp": msg.get("E"),
            }
            # Update cache
            _cache[f"bn_ws_{symbol}"] = (time.time(), data)
            self._callback(data)
        except Exception as e:
            print(f"  [Binance WS] Parse error: {e}")

    @staticmethod
    def _default_callback(data: dict):
        """Default handler — just prints."""
        print(f"  ${data['symbol']}: ${data['price']:,.2f}  "
              f"Vol: {data['volume']:,.0f}")

    @property
    def is_running(self) -> bool:
        return self._running

    def get_latest(self, symbol: str) -> dict | None:
        """Get latest WebSocket price for a symbol (from cache)."""
        cached = _get_cached(f"bn_ws_{symbol.upper()}")
        return cached


# ── Quick test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Testing Binance connector...")

    # Test public endpoints
    ticker = fetch_binance_ticker("BTC")
    if ticker:
        print(f"\nBTC Ticker: ${ticker['price']:,.2f}  "
              f"24h: {ticker['change_24h']:+.1f}%  "
              f"Vol: ${ticker['quote_volume']:,.0f}")
    else:
        print("  Could not fetch BTC ticker")

    ohlcv = fetch_binance_ohlcv("ETH", timeframe="1d", limit=5)
    if ohlcv:
        print(f"\nETH OHLCV (last 5 daily candles):")
        for candle in ohlcv:
            print(f"  O={candle[1]:.2f} H={candle[2]:.2f} "
                  f"L={candle[3]:.2f} C={candle[4]:.2f} V={candle[5]:,.0f}")

    symbols = get_binance_symbols()
    print(f"\nBinance tradeable symbols: {len(symbols)}")

    # Test authenticated endpoint
    portfolio, source = fetch_binance_portfolio()
    if portfolio is not None:
        print(f"\nBinance portfolio ({source}):")
        for h in portfolio[:10]:
            print(f"  {h['asset']}: {h['amount']}")
    else:
        print(f"\nPortfolio: {source}")
