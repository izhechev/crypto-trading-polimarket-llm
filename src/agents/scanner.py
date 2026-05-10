"""Smart Scanner — rank top 250 coins by opportunity score, exchange-filtered."""
import json
import re
import time
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config
from src.connectors.coingecko import fetch_ohlcv as _cg_fetch_ohlcv
from src.connectors.coinpaprika import (
    fetch_tickers_for_scanner as _cp_fetch_tickers,
    fetch_ohlcv as _cp_fetch_ohlcv,
    _build_cg_id_map as _cp_build_cg_id_map,
    get_ath_date_map as _cp_get_ath_date_map,
)
from src.agents.technical_analyst import compute_ta


def fetch_ohlcv(coin_id: str, days: int = 30) -> list[dict]:
    """Backwards-compat shim used by callers outside scanner.py."""
    return _cg_fetch_ohlcv(coin_id, days)


def _fetch_ohlcv_binance(symbol: str, days: int) -> list[dict]:
    """Binance public klines — free, no key, covers most coins via {SYM}USDT."""
    import httpx as _hx
    from datetime import datetime as _dt, timezone as _tz
    try:
        resp = _hx.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": f"{symbol}USDT", "interval": "1d", "limit": min(days + 2, 1000)},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        result = []
        for c in resp.json():
            try:
                result.append({
                    "timestamp": _dt.fromtimestamp(c[0] / 1000, tz=_tz.utc).replace(tzinfo=None),
                    "open":  float(c[1]),
                    "high":  float(c[2]),
                    "low":   float(c[3]),
                    "close": float(c[4]),
                })
            except (ValueError, IndexError):
                continue
        return result
    except Exception:
        return []


def _fetch_ohlcv_kucoin(symbol: str, days: int) -> list[dict]:
    """KuCoin public klines — free, no key, covers long-tail coins."""
    import httpx as _hx
    import time as _time
    from datetime import datetime as _dt, timezone as _tz
    try:
        end_ts   = int(_time.time())
        start_ts = end_ts - days * 86400
        resp = _hx.get(
            "https://api.kucoin.com/api/v1/market/candles",
            params={"symbol": f"{symbol}-USDT", "type": "1day",
                    "startAt": start_ts, "endAt": end_ts},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        raw = resp.json().get("data") or []
        result = []
        for c in raw:
            try:
                result.append({
                    "timestamp": _dt.fromtimestamp(int(c[0]), tz=_tz.utc).replace(tzinfo=None),
                    "open":  float(c[1]),
                    "close": float(c[2]),
                    "high":  float(c[3]),
                    "low":   float(c[4]),
                })
            except (ValueError, IndexError):
                continue
        return result
    except Exception:
        return []


def _fetch_ohlcv_okx(symbol: str, days: int) -> list[dict]:
    """OKX public candles — free, no key, covers CORE and most altcoins."""
    import httpx as _hx
    from datetime import datetime as _dt, timezone as _tz
    try:
        resp = _hx.get(
            "https://www.okx.com/api/v5/market/candles",
            params={"instId": f"{symbol}-USDT", "bar": "1D", "limit": min(days + 2, 300)},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        raw = resp.json().get("data") or []
        result = []
        for c in raw:
            try:
                result.append({
                    "timestamp": _dt.fromtimestamp(int(c[0]) / 1000, tz=_tz.utc).replace(tzinfo=None),
                    "open":  float(c[1]),
                    "high":  float(c[2]),
                    "low":   float(c[3]),
                    "close": float(c[4]),
                })
            except (ValueError, IndexError):
                continue
        result.sort(key=lambda x: x["timestamp"])  # OKX returns newest-first
        return result
    except Exception:
        return []


def _fetch_ohlcv_kraken(symbol: str, days: int) -> list[dict]:
    """Kraken public OHLC — free, no key, good coverage of major/mid-cap coins."""
    import httpx as _hx
    from datetime import datetime as _dt, timezone as _tz
    try:
        for pair in (f"{symbol}USD", f"{symbol}USDT"):
            resp = _hx.get(
                "https://api.kraken.com/0/public/OHLC",
                params={"pair": pair, "interval": 1440},
                timeout=15,
            )
            d = resp.json()
            if d.get("error"):
                continue
            result_data = d.get("result", {})
            keys = [k for k in result_data if k != "last"]
            if not keys:
                continue
            raw = result_data[keys[0]]
            result = []
            for c in raw:
                try:
                    result.append({
                        "timestamp": _dt.fromtimestamp(int(c[0]), tz=_tz.utc).replace(tzinfo=None),
                        "open":  float(c[1]),
                        "high":  float(c[2]),
                        "low":   float(c[3]),
                        "close": float(c[4]),
                    })
                except (ValueError, IndexError):
                    continue
            if result:
                return result[-days:]  # Kraken returns up to 720 — trim to requested days
        return []
    except Exception:
        return []


_MIN_CANDLES = 14   # minimum candles needed for reliable RSI/MACD


# Pre-built CG ID map — populated once per scan in run_smart_scanner, reused per-coin.
_cg_id_cache: dict[str, str] = {}

def _fetch_ohlcv_for_coin(coin: dict, days: int = 30) -> list[dict]:
    """
    Fetch OHLCV with multi-source fallback chain:
    CoinGecko (static ID) → CoinGecko (search) → Binance → KuCoin → OKX → Kraken
    """
    from src.connectors.coingecko import search_cg_id as _cg_search

    sym   = coin.get("symbol", "").upper()
    cg_id = coin.get("_cg_id") or (coin.get("id") if not coin.get("_cp_id") else "")
    if not cg_id:
        cg_id = _cg_id_cache.get(sym, "")

    if cg_id:
        data = _cg_fetch_ohlcv(cg_id, days)
        if data and len(data) >= _MIN_CANDLES:
            return data

    found_id = _cg_search(sym)
    if found_id and found_id != cg_id:
        data = _cg_fetch_ohlcv(found_id, days)
        if data and len(data) >= _MIN_CANDLES:
            return data

    # Exchange fallbacks — free, no key required
    for _fetcher, _source in (
        (_fetch_ohlcv_binance, "Binance"),
        (_fetch_ohlcv_kucoin,  "KuCoin"),
        (_fetch_ohlcv_okx,     "OKX"),
        (_fetch_ohlcv_kraken,  "Kraken"),
    ):
        data = _fetcher(sym, days)
        if data and len(data) >= _MIN_CANDLES:
            print(f" [{_source}]", end="", flush=True)
            return data

    return []

STABLECOINS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDD", "FDUSD", "PYUSD",
    "GUSD", "FRAX", "LUSD", "SUSD", "CUSD", "RAI", "MIM", "UST", "USDP",
    "USDE", "USDS", "EURC", "EURT", "USD1", "STABLE",
    # Added Apr 2026
    "RLUSD", "CRVUSD", "SUSDE", "GHO",
    # Gold-backed tokens treated as stablecoins for scanner purposes
    "XAUT", "PAXG",
}


def _is_tokenized_stock(coin: dict) -> bool:
    """Return True if this coin is a tokenized equity/RWA that should be excluded.
    Catches Ondo-style tokens and generic 'Tokenized Stock X' entries."""
    name = coin.get("name", "").lower()
    cid  = coin.get("id", "").lower()
    sym  = coin.get("symbol", "").upper()
    return (
        "tokenized stock" in name       # e.g. "Circle Internet Group (Ondo Tokenized Stock)"
        or "tokenized-stock" in cid     # e.g. "circle-internet-group-ondo-tokenized-stock"
        or "(ondo" in name              # e.g. "Something (Ondo ...)"
        or name.startswith("ondo ")
        or " ondo" in name
        or cid.startswith("ondo-")
        or sym in {"CRCLON", "USDYM", "NVDAX", "AAPLX", "TSLAX"}  # known Ondo tokenized stocks
    )


def _is_price_stable(coin: dict) -> bool:
    """Return True if price behaviour marks this as an unregistered stablecoin.
    Criteria: price $0.99–$1.01 AND 7d change within ±0.5%."""
    price  = coin.get("current_price") or 0
    ch_7d  = coin.get("price_change_percentage_7d_in_currency") or 0
    return 0.99 <= price <= 1.01 and -0.5 <= ch_7d <= 0.5

WRAPPED_TOKENS = {
    "WBTC", "WETH", "WBNB", "WMATIC", "WSOL",
    "STETH", "CBETH", "RETH", "WSTETH", "OSETH",
}

# Tokenized stocks / real-world assets that are NOT crypto — exclude from scanner.
# These track equity prices, not crypto market dynamics; TA signals are meaningless.
# Symbol list as fallback; pattern matching via _is_tokenized_stock() catches new entries.
TOKENIZED_STOCKS = {
    "CRCLON",   # tokenized CLON stock (Ondo)
    "CRTESLA",  # tokenized Tesla
    "CRAAPL",   # tokenized Apple
    "CRNVDA",   # tokenized Nvidia
    "CRCOIN",   # tokenized Coinbase
    "CRSPY",    # tokenized S&P 500 ETF
}

# Permanently excluded — confirmed industrial wash trading, unrideable.
# These bypass even the whale ride logic; never appear anywhere in output.
WASH_TRADING_CONFIRMED = {
    "RIVER",    # 50% supply in 1 entity, 2,418 linked wallets, industrial wash trading
}

# Permanently excluded coins — web validation FAIL, sanctions links, confirmed bad actors.
# Added manually after investigation; excluded from scanner + whale ride logic.
PERMANENTLY_EXCLUDED = {
    "WLFI",     # sanctions-linked partner confirmed via web validation — do not trade
    "BTSE",     # exchange token with wash-traded CoinGecko data; real float ~$5M, 500% APR hype
    "EDGE",     # CoinGecko inflates both market_cap AND circulating_supply; real volume $576/day
    "LUNC",     # Terra Luna Classic — collapsed ecosystem, no real development, pure speculation
    "RIVER",    # permanently excluded
    "KOGE",     # permanently excluded
    "CRCLON",   # permanently excluded
    "RLC",      # delisted RLC/BTC on Binance Mar 2026, KuCoin margin ended Jan 2026
    "KNC",      # post-pump exhaustion, no organic protocol demand
    "ORDI",     # high BTC correlation + amplified downside, bearish 70% sentiment
}

# Rug pull / scam detection is now fully automatic via coin_risk_assessor.py
# No hardcoded lists — the system detects dead projects and scams in real-time.

# Tokens classified as commodities by the SEC/CFTC (updated Apr 2026).
# Commodity status reduces regulatory risk — +1 scoring bonus.
SEC_COMMODITY_TOKENS = {
    "BTC", "ETH", "ALGO", "SOL", "ADA", "DOT", "AVAX", "XRP", "LINK", "UNI",
    "LTC", "BCH", "XLM", "ATOM", "FIL", "NEAR",
}

# Narrative / sector groupings for momentum detection.
# Altcoins rotate through short-lived narrative-driven rallies — track sector, not TA.
_SECTOR_MAP: dict[str, set[str]] = {
    "privacy":  {"XMR", "DASH", "ZEC", "SCRT", "KEEP", "NYM"},
    "ai":       {"FET", "RENDER", "TAO", "AGIX", "OCEAN", "NMR", "RLC", "ALT", "AIOZ", "PAAL", "WLD"},
    "depin":    {"IOTX", "HNT", "MOBILE", "DIMO", "GEODNET", "ROAM", "REACT", "WIFI"},
    "layer1":   {"SOL", "ADA", "DOT", "AVAX", "NEAR", "SUI", "APT", "TIA", "INJ", "SEI", "ALGO"},
    "layer2":   {"OP", "ARB", "MATIC", "IMX", "BLUR", "LRC", "STX", "MANTA", "ZKEVM"},
    "defi":     {"UNI", "AAVE", "MKR", "CRV", "SUSHI", "SNX", "BAL", "1INCH", "YFI", "COMP", "GMX", "PENDLE"},
    "meme":     {"DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "MOG", "POPCAT"},
    "rwa":      {"ONDO", "POLYX", "CFG", "RIO", "REALT"},
}

# ACTION words that indicate a real catalyst event — generic price predictions,
# "what is X" articles, or exchange listing pages do NOT count.
# A headline must contain at least one of these to be considered a catalyst.
_NEWS_CATALYST_ACTIONS = {
    "launch", "launches", "launched",
    "partnership", "partners", "partnered",
    "upgrade", "upgrades", "upgraded",
    "listing", "listed", "lists",
    "etf", "etf approval", "etf approved",
    "integration", "integrates", "integrated",
    "approval", "approved", "approves",
    "acquisition", "acquires", "acquired",
    "funding", "raises", "raised",
    "mainnet", "testnet",
    "airdrop",
    "exploit", "hack", "breach",   # negative catalysts still move price
    "lawsuit", "sec", "investigation",
}
_NEWS_BEARISH = {
    "crash", "dump", "hack", "exploit", "lawsuit", "sec", "ban", "warning",
    "scam", "rug", "fraud", "vulnerability", "breach", "probe", "delay",
    "bearish", "plunge", "plummet", "plummets", "plummeting",
    "decline", "fall", "falls", "fell", "drops", "drop", "loss",
    # Governance / team crisis signals
    "accused", "accusation", "coerce", "coercion", "exits", "exit",
    "centralization", "centralized", "concerns", "controversy", "scandal",
    "resigns", "resignation", "leaves project", "abandons", "abandoned",
    "investigation", "charges", "indicted", "arrested",
}

# Extra search terms per ticker used when filtering news relevance.
# CoinGecko's coin name is already used automatically — add aliases only when
# the ticker alone is ambiguous or the project is known by multiple names.
_COIN_ALIASES: dict[str, list[str]] = {
    "BTC":   ["Bitcoin"],
    "ETH":   ["Ethereum", "Ether"],
    "SOL":   ["Solana"],
    "BNB":   ["BNB", "Binance"],
    "XRP":   ["Ripple", "XRP"],
    "ADA":   ["Cardano"],
    "DOGE":  ["Dogecoin"],
    "DOT":   ["Polkadot"],
    "AVAX":  ["Avalanche"],
    "LINK":  ["Chainlink"],
    "UNI":   ["Uniswap"],
    "AAVE":  ["Aave"],
    "ATOM":  ["Cosmos"],
    "NEAR":  ["NEAR Protocol"],
    "FIL":   ["Filecoin"],
    "ICP":   ["Internet Computer"],
    "MATIC": ["Polygon"],
    "ARB":   ["Arbitrum"],
    "OP":    ["Optimism"],
    "INJ":   ["Injective"],
    "TAO":   ["Bittensor"],
    "ENA":   ["Ethena"],
    "PEPE":  ["Pepe"],
    "COMP":  ["Compound"],
    "DASH":  ["Dash"],
    "LIT":   ["Litentry"],
    "SEI":   ["Sei"],
    "SUI":   ["Sui"],
    "WLD":   ["Worldcoin"],
    "CFX":   ["Conflux"],
    "NEO":   ["Neo"],
    "CHZ":   ["Chiliz"],
    "SUN":   ["Sun Token"],
    "EOS":   ["EOS"],
    "GRT":   ["The Graph"],
    "CRV":   ["Curve"],
    "SNX":   ["Synthetix"],
    "LRC":   ["Loopring"],
    "SAND":  ["The Sandbox", "Sandbox"],
    "MANA":  ["Decentraland"],
    "AXS":   ["Axie Infinity", "Axie"],
    "FLOW":  ["Flow"],
    "ALGO":  ["Algorand"],
    "XLM":   ["Stellar"],
    "TRX":   ["Tron"],
    "VET":   ["VeChain"],
    "ETC":   ["Ethereum Classic"],
    "XMR":   ["Monero"],
    "ZEC":   ["Zcash"],
}


def _get_binance_symbols() -> set[str]:
    """Fetch available base currencies from Binance (USDT pairs)."""
    try:
        from src.connectors.binance import get_binance_symbols
        symbols = get_binance_symbols()
        if symbols:
            return symbols
    except Exception as e:
        print(f"  Warning: Binance symbols fetch failed ({e}), using fallback")
    # Fallback: large subset of Binance-listed coins
    return {
        "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOT", "LINK", "AVAX",
        "ATOM", "LTC", "BCH", "UNI", "AAVE", "MKR", "COMP", "GRT", "CRV",
        "SNX", "BAT", "FIL", "INJ", "RENDER", "NEAR", "OP", "ARB", "SUI",
        "APT", "TIA", "SEI", "PEPE", "DOGE", "SHIB", "MATIC", "FTM", "ALGO",
        "XLM", "TRX", "ETC", "MANA", "SAND", "AXS", "ENJ", "FET", "WIF",
        "BONK", "FLOKI", "JUP", "WLD", "PENDLE", "STX", "RUNE", "ICP",
        "HBAR", "VET", "THETA", "ENA", "TON", "TAO", "ONDO", "PYTH",
    }


def _get_revolut_symbols() -> set[str]:
    """Return Revolut X tradeable coins from config."""
    return set(config.REVOLUT_X_COINS)


def _get_kraken_symbols() -> set[str]:
    """Fetch base currencies from Kraken public AssetPairs endpoint."""
    try:
        import httpx as _httpx
        with _httpx.Client(timeout=10) as _c:
            r = _c.get("https://api.kraken.com/0/public/AssetPairs",
                       params={"info": "leverage"})
        if r.status_code == 200:
            data = r.json().get("result", {})
            syms: set[str] = set()
            for pair_info in data.values():
                base = (pair_info.get("base") or "").upper()
                # Kraken prefixes with X/Z for legacy pairs; strip them
                if len(base) > 3 and base[0] in ("X", "Z"):
                    base = base[1:]
                if base and base not in ("ZUSD", "ZEUR", "ZGBP", "ZJPY"):
                    syms.add(base)
            return syms
    except Exception:
        pass
    # Fallback: known Kraken-listed assets
    return {
        "BTC", "ETH", "SOL", "XRP", "ADA", "DOT", "LINK", "AVAX", "ATOM",
        "LTC", "BCH", "UNI", "AAVE", "MKR", "GRT", "CRV", "SNX", "FIL",
        "INJ", "NEAR", "OP", "ARB", "SUI", "APT", "TIA", "PEPE", "DOGE",
        "SHIB", "MATIC", "FTM", "ALGO", "XLM", "TRX", "ETC", "AXS", "FET",
        "WIF", "BONK", "JUP", "PENDLE", "ICP", "HBAR", "VET", "RUNE", "ENA",
        "IMX", "COMP", "BAT", "ZEC", "DASH", "XMR", "OCEAN", "AGIX",
    }


def _fetch_top_coinpaprika(limit: int = 1000) -> list[dict]:
    """Fetch top coins from CoinPaprika (primary source). Single request, no pagination."""
    return _cp_fetch_tickers(limit=limit)


def _fetch_top_250_coingecko(pages: int = 1) -> list[dict]:
    """Fetch top coins from CoinGecko (fallback). pages=1→250, pages=4→1000."""
    url = "https://api.coingecko.com/api/v3/coins/markets"
    headers = {}
    if config.COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = config.COINGECKO_API_KEY

    all_coins: list[dict] = []
    with httpx.Client(timeout=30) as client:
        for page in range(1, pages + 1):
            params = {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 250,
                "page": page,
                "price_change_percentage": "24h,7d,14d",
                "sparkline": "false",
            }
            resp = client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            batch = resp.json()
            all_coins.extend(batch)
            if len(batch) < 250:
                break
            if page < pages:
                time.sleep(1.2)
    return all_coins


def _fetch_top_250(pages: int = 1) -> list[dict]:
    """Backwards-compat alias — uses CoinGecko directly (used by non-scanner callers)."""
    return _fetch_top_250_coingecko(pages)


def _check_rug_pull(coin: dict) -> tuple[bool, str]:
    """
    Auto-detect rug pulls / panic selling. Returns (is_rug_pull, reason).

    Rug rule 1:  7d drop > 70%  — flagged regardless of any 24h bounce.
                 A +25% daily bounce after an -86% weekly crash is a dead-cat;
                 no legitimate coin recovers meaningfully from a rug pull.
    Rug rule 2:  14d drop > 70%  — catches cases where the crash happened
                 8-14 days ago and the 7d metric alone would miss it.
    Panic sell:  vol/mcap > 0.9x  AND  24h drop > 20%  (BOTH required)

    High volume alone is NOT a rug signal — XPL (+27% 7d), MON (-6% 7d) etc.
    are legitimate projects with high trading interest and must not be excluded.
    """
    change_7d  = coin.get("price_change_percentage_7d_in_currency") or 0
    change_14d = coin.get("price_change_percentage_14d_in_currency") or 0
    change_24h = coin.get("price_change_percentage_24h") or 0
    volume     = coin.get("total_volume") or 0
    market_cap = coin.get("market_cap") or 1

    # Massive crash in any rolling window — 24h bounce does NOT override this
    if change_7d < -70:
        return True, f"7d crash {change_7d:.1f}% (24h bounce: {change_24h:+.1f}%)"
    if change_14d < -70:
        return True, f"14d crash {change_14d:.1f}% (7d: {change_7d:.1f}%)"

    # Panic selling: extreme volume spike AND sharp 24h drop (both required)
    if market_cap > 0 and (volume / market_cap) > 0.90 and change_24h < -20:
        return True, f"panic: vol/mcap {volume/market_cap:.2f}x + 24h {change_24h:.1f}%"

    return False, ""


_WASH_TRADING_WHITELIST = {
    "BTC", "ETH", "BNB", "USDT", "USDC", "SOL", "XRP",
}
"""High-liquidity coins that must never be flagged as wash trading.
Price pinning logic is meaningless for coins with genuine market depth."""


def _check_wash_trading(coin: dict, ohlcv: list[dict]) -> tuple[bool, str]:
    """
    Detect wash trading patterns. Returns (is_wash_trading, reason).

    Immediate exclusion (1 signal sufficient):
    - 24h change == 0.0% AND 7d change == 0.0% AND vol/mcap > 1.0x
      → price completely frozen despite enormous reported volume; definitive wash.

    Soft signals (any 2 of 3 trigger a flag):
    1. vol/mcap > 1.0x  — volume physically exceeds entire market cap
    2. 3+ of last 5 daily candles have price range < 0.5%  — price pinned flat
    3. Current price is a whole-dollar round number (e.g. $48.00, $5.00)
       for coins priced ≥ $1; sub-dollar round numbers are too common to flag.

    BTC, ETH, BNB, USDT, USDC, SOL, XRP are whitelisted — high liquidity coins
    never match wash-trading patterns; price-pinning logic does not apply to them.
    """
    # Whitelist: high-liquidity coins are never wash traders
    if coin.get("symbol", "").upper() in _WASH_TRADING_WHITELIST:
        return False, ""

    volume     = coin.get("total_volume") or 0
    market_cap = coin.get("market_cap") or 1
    price      = coin.get("current_price") or 0
    change_24h = coin.get("price_change_percentage_24h") or 0
    change_7d  = coin.get("price_change_percentage_7d_in_currency") or 0

    vol_mcap = volume / market_cap

    # Immediate exclusion: price completely frozen with outsized volume
    if change_24h == 0.0 and change_7d == 0.0 and vol_mcap > 1.0:
        return True, f"price frozen (0% 24h, 0% 7d) with vol/mcap {vol_mcap:.1f}x — definitive wash trading"

    signals: list[str] = []

    # Signal 1: vol/mcap > 1.0x
    if vol_mcap > 1.0:
        signals.append(f"vol/mcap {vol_mcap:.1f}x (volume exceeds market cap)")

    # Signal 2: 3+ flat days in last 5 candles (daily range < 0.5% of close)
    if len(ohlcv) >= 5:
        flat = sum(
            1 for c in ohlcv[-5:]
            if (c.get("close") or 0) > 0
            and ((c.get("high", c.get("close", 0)) - c.get("low", c.get("close", 0)))
                 / c["close"] * 100) < 0.5
        )
        if flat >= 3:
            signals.append(f"{flat}/5 days with <0.5% price range (price pinned)")

    # Signal 3: price is an exact whole-dollar number ($48.00, $5.00 etc.)
    if price >= 1.0 and abs(price - round(price)) < 0.001:
        signals.append(f"price pinned at round number ${price:.0f}.00")

    if len(signals) >= 2:
        return True, " + ".join(signals)
    return False, ""


def _quick_score(coin: dict, trending_symbols: set[str] | None = None) -> tuple[int, list[str]]:
    """
    Pre-filter score using bulk market data only (no OHLCV needed).
    Selects the 60 best candidates before expensive OHLCV/TA fetch.

    Uses every field already in the bulk response so the top-200 set
    is as close as possible to what full TA would rank highly.
    """
    score = 0
    reasons: list[str] = []

    sym        = coin.get("symbol", "").upper()
    change_7d  = coin.get("price_change_percentage_7d_in_currency") or 0
    change_24h = coin.get("price_change_percentage_24h") or 0
    volume     = coin.get("total_volume") or 0
    market_cap = coin.get("market_cap") or 1
    ath_pct    = coin.get("ath_change_percentage") or 0   # negative = below ATH
    circ       = coin.get("circulating_supply") or 0
    total_s    = coin.get("total_supply") or 0
    vm         = volume / market_cap

    # Hard pre-exclude: already pumped >200% or micro-cap (<$500k) — waste of TA slots
    if change_7d > 200:
        return -99, ["already pumped >200% 7d"]
    if 0 < market_cap < 500_000:
        return -99, ["micro-cap <$500k"]
    # Hard pre-exclude: circulating supply < 15% (unlock risk)
    if total_s > 0 and circ > 0 and (circ / total_s) < 0.15:
        return -99, ["circ supply <15%"]

    # ── 7d dip depth (oversold bounce signal) ─────────────────────────────
    if change_7d < -50:
        score += 3
        reasons.append(f"deep 7d dip {change_7d:.0f}% (+3)")
    elif change_7d < -30:
        score += 2
        reasons.append(f"7d dip {change_7d:.0f}% (+2)")
    elif change_7d < -15:
        score += 1
        reasons.append(f"7d dip {change_7d:.0f}% (+1)")
    elif change_7d > 30:
        score -= 1
        reasons.append(f"7d already up {change_7d:.0f}% (-1)")

    # ── Volume / market-cap ratio (buying pressure) ────────────────────────
    if vm > 0.50:
        score += 3
        reasons.append(f"vol/mcap {vm:.2f}x (+3)")
    elif vm > 0.30:
        score += 2
        reasons.append(f"vol/mcap {vm:.2f}x (+2)")
    elif vm > 0.10:
        score += 1
        reasons.append(f"vol/mcap {vm:.2f}x (+1)")
    elif vm < 0.02:
        score -= 1
        reasons.append(f"vol/mcap {vm:.3f}x dead (+−1)")

    # ── 24h momentum ──────────────────────────────────────────────────────
    if 2 <= change_24h <= 15:
        score += 1
        reasons.append(f"24h momentum {change_24h:+.1f}% (+1)")
    elif change_24h > 15:
        score -= 1
        reasons.append(f"24h already up {change_24h:+.1f}% (-1)")
    elif change_24h < -5:
        score -= 1
        reasons.append(f"24h bleeding {change_24h:+.1f}% (-1)")

    # ── ATH distance (coiled spring potential) ─────────────────────────────
    if ath_pct < -90:
        score += 2
        reasons.append(f"ATH {ath_pct:.0f}% — coiled spring (+2)")
    elif ath_pct < -70:
        score += 1
        reasons.append(f"ATH {ath_pct:.0f}% discount (+1)")

    # ── Oversold proxy: sharp 7d + 24h both negative → likely oversold ────
    if change_7d < -20 and change_24h < -3:
        score += 1
        reasons.append("oversold proxy: 7d+24h both down (+1)")

    # ── CMC trending ──────────────────────────────────────────────────────
    if trending_symbols and sym in trending_symbols:
        score += 1
        reasons.append("CMC trending (+1)")

    # ── SEC/CFTC commodity (known regulatory safety) ──────────────────────
    if sym in SEC_COMMODITY_TOKENS:
        score += 1
        reasons.append("SEC/CFTC commodity (+1)")

    return score, reasons


def _ta_score(rsi, macd_signal, bb_position, vol_mcap: float = 0.0) -> tuple[int, list[str]]:
    """
    Technical score (v1.0 reset).

    RSI:      <30 = +2 | 30-42 = +1 | 50-65 = +1 | 65-78 = +0 | >78 = gated out
    MACD:     bullish = +1 | bearish = -1
    Volume:   >0.50x = +2 | 0.30-0.50x = +1 | <0.30x = +0  (no exclusion — BTC/ETH have low ratio)
    BB:       below lower = +1 | above upper = -2
    """
    score = 0
    reasons = []

    # RSI
    if rsi is not None:
        if rsi < 30:
            score += 2
            reasons.append(f"RSI {rsi:.1f} strongly oversold (+2)")
        elif rsi < 42:
            score += 1
            reasons.append(f"RSI {rsi:.1f} oversold (+1)")
        elif 50 <= rsi <= 65:
            score += 1
            reasons.append(f"RSI {rsi:.1f} healthy momentum (+1)")
        # 65-72: +0, already below overbought gate

    # MACD (standalone signal)
    if macd_signal == "BULLISH":
        score += 1
        reasons.append("MACD bullish (+1)")
    elif macd_signal == "BEARISH":
        score -= 1
        reasons.append("MACD bearish (-1)")

    # Volume / mcap ratio
    if vol_mcap > 0.50:
        score += 2
        reasons.append(f"vol/mcap {vol_mcap:.2f}x strong interest (+2)")
    elif vol_mcap >= 0.30:
        score += 1
        reasons.append(f"vol/mcap {vol_mcap:.2f}x buying pressure (+1)")
    # 0.15-0.30: +0

    # Bollinger Bands
    if bb_position == "BELOW_LOWER":
        score += 1
        reasons.append("below lower BB (+1)")
    elif bb_position == "ABOVE_UPPER":
        score -= 2
        reasons.append("above upper BB — extended, risky entry (-2)")

    return score, reasons


def _catalyst_score(
    coin: dict,
    rsi: float | None,
    trend: str | None = None,
    change_7d: float = 0.0,
) -> tuple[int, list[str]]:
    """
    Catalyst + momentum score (v1.0 reset).

    Momentum 24h:  >+10% = +1 | +2-+10% = +0 | <+2% = -1
    Trend:         BULLISH = +1 | NEUTRAL = +0 | BEARISH = -1
    7d dip:        <-30% = +2 | -15 to -30% = +1 | -8 to -15% = +0  (Archetype A bonus)
    SEC commodity: +1
    Coiled spring: +1 (coin >90% from ATH AND RSI <35 — quality signal, keep at +1)
    """
    score   = 0
    reasons = []

    symbol     = coin.get("symbol", "").upper()
    ath_pct    = coin.get("ath_change_percentage") or 0
    change_24h = coin.get("price_change_percentage_24h") or 0

    # 24h momentum
    if change_24h > 10:
        score += 1
        reasons.append(f"strong 24h momentum {change_24h:+.1f}% (+1)")
    elif change_24h < 2:
        score -= 1
        reasons.append(f"weak 24h {change_24h:+.1f}% (-1)")

    # Trend (scored separately from MACD)
    if trend == "BULLISH":
        score += 1
        reasons.append("trend BULLISH (+1)")
    elif trend == "BEARISH":
        score -= 1
        reasons.append("trend BEARISH (-1)")

    # 7d dip depth — Archetype A bonus (oversold bounce setups)
    if change_7d < -30:
        score += 2
        reasons.append(f"deep 7d dip {change_7d:.0f}% (+2)")
    elif change_7d < -15:
        score += 1
        reasons.append(f"7d dip {change_7d:.0f}% (+1)")

    # Coiled spring: deep ATH discount + deeply oversold
    if ath_pct < -90 and rsi is not None and rsi < 35:
        score += 1
        reasons.append(f"coiled spring ({ath_pct:.0f}% from ATH, RSI {rsi:.1f}) (+1)")

    # SEC/CFTC commodity — quality signal
    if symbol in SEC_COMMODITY_TOKENS:
        score += 1
        reasons.append("SEC/CFTC commodity (+1)")

    return score, reasons


def _news_score(news_items: list[dict], symbol: str = "", coin_name: str = "") -> tuple[int, list[str]]:
    """
    +3 if a real news catalyst is detected in last 7 days.

    A headline only counts if it contains an ACTION word (launch, partnership,
    upgrade, listing, ETF, integration, approval, acquisition, funding, etc.).
    Generic price predictions, "what is X" articles, and exchange listing pages
    are excluded — they contain sentiment words but no real catalyst.

    Relevance gate: headline must mention the coin's ticker, full name, or a known
    alias before being scored. Headlines about Bitcoin/market-wide events that happen
    to contain action words are ignored.

    Threshold: ≥2 catalyst headlines in ≤7 days, with catalyst count > bearish count.
    """
    if not news_items:
        return 0, []

    sym   = symbol.upper()
    debug = False

    # Build the set of relevance terms: ticker + CoinGecko name + known aliases
    # Short tickers (≤2 chars) like "A" or "AI" match every headline — use coin name only.
    relevance_terms: set[str] = set()
    if len(sym) >= 3:
        relevance_terms.add(sym.lower())
    if coin_name:
        relevance_terms.add(coin_name.lower())
    for alias in _COIN_ALIASES.get(sym, []):
        relevance_terms.add(alias.lower())
    # Safety net: if we have nothing (no name + short ticker), fall back to ticker anyway
    if not relevance_terms:
        relevance_terms.add(sym.lower())

    if debug:
        print(f"\n  [DEBUG {sym}] News scoring — {len(news_items)} item(s) | relevance: {relevance_terms}")

    catalyst_count = 0
    bearish_count  = 0
    total_recent   = 0
    for item in news_items:
        age = item.get("age_days")
        src = item.get("source", "")
        src_tag = f"[{src}] " if src else ""

        # Source-aware age fallback:
        # GoogleNews RSS is already filtered to last 7 days by the search query —
        # any result is within the valid window, so treat unknown age as 0 (today).
        # Reddit posts also come from a "week" filter — treat unknown as 3 days.
        # Everything else with unknown age is treated as borderline (7d = last allowed).
        if age is None:
            if src == "GoogleNews":
                age = 0
            elif src == "Reddit":
                age = 3
            else:
                age = 7   # borderline — include but won't score as "fresh"

        if age > 7:
            if debug:
                print(f"    SKIP age={age}d {src_tag}{item.get('title', '')[:80]}")
            continue

        title     = item.get("title", "")
        title_low = title.lower()

        # Relevance gate — word-boundary match so "fil" doesn't hit "filed"/"profile" etc.
        relevant = any(
            re.search(r'\b' + re.escape(term) + r'\b', title_low)
            for term in relevance_terms
        )
        if not relevant:
            if debug:
                print(f"    SKIP irrelevant {src_tag}{title[:100]}")
            continue

        total_recent += 1
        has_action  = any(w in title_low for w in _NEWS_CATALYST_ACTIONS)
        has_bearish = any(w in title_low for w in _NEWS_BEARISH)

        if debug:
            matched_action  = [w for w in _NEWS_CATALYST_ACTIONS if w in title_low]
            matched_bearish = [w for w in _NEWS_BEARISH if w in title_low]
            verdict = ("CATALYST" if has_action and not has_bearish
                       else "BEARISH" if has_bearish
                       else "NEUTRAL")
            print(f"    [{verdict}] age={age}d {src_tag}| action={matched_action} | bearish={matched_bearish}")
            print(f"      headline: {title[:100]}")

        if has_action and not has_bearish:
            catalyst_count += 1
        elif has_bearish:
            bearish_count += 1

    if debug:
        print(f"  [DEBUG {sym}] catalyst={catalyst_count} bearish={bearish_count} total_relevant={total_recent}")

    # News scoring (v1.0 reset):
    #   Real catalyst confirmed (≥1 action headline, catalyst > bearish) → +1
    #   Bearish headlines dominate (majority negative, no catalyst)      → -1
    #   Otherwise                                                        →  0
    if catalyst_count >= 1 and catalyst_count > bearish_count:
        return 1, [f"real catalyst in news ({catalyst_count} headline(s) ≤7d) (+1)"]
    if bearish_count >= 1 and catalyst_count == 0 and total_recent > 0 and bearish_count > total_recent / 2:
        return -1, [f"bearish headlines dominate ({bearish_count}/{total_recent} ≤7d) (-1)"]
    return 0, []


def _compute_sector_avgs(coins: list[dict]) -> dict[str, float]:
    """Compute average 7d change per sector from the full coin list."""
    sector_changes: dict[str, list[float]] = {s: [] for s in _SECTOR_MAP}
    for coin in coins:
        sym  = coin.get("symbol", "").upper()
        ch7d = coin.get("price_change_percentage_7d_in_currency") or 0
        for sector, members in _SECTOR_MAP.items():
            if sym in members:
                sector_changes[sector].append(ch7d)
                break
    return {
        sector: (sum(vals) / len(vals)) if vals else 0.0
        for sector, vals in sector_changes.items()
    }


def _sector_score(symbol: str, sector_avgs: dict[str, float]) -> tuple[int, list[str]]:
    """
    +2 if coin's narrative sector is trending (avg 7d > 20%).
    Altcoin rotations are narrative-driven — track the sector, not individual TA.
    """
    sym = symbol.upper()
    for sector, members in _SECTOR_MAP.items():
        if sym in members:
            avg = sector_avgs.get(sector, 0.0)
            if avg > 20:
                return 2, [f"{sector} sector trending (avg 7d {avg:+.0f}%)"]
            break
    return 0, []


def _write_shadow_log(symbol: str, score: int, reason_skipped: str) -> None:
    """Append a score-1 coin to shadow_log.csv for post-cycle win-rate analysis."""
    import csv as _csv
    from datetime import datetime, timezone
    path = config.DATA_DIR / "shadow_log.csv"
    fieldnames = ["date", "coin", "score", "reason_skipped", "outcome_7d"]
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "date":           datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "coin":           symbol,
            "score":          score,
            "reason_skipped": reason_skipped,
            "outcome_7d":     "",   # filled manually after 7 days
        })


def _get_previous_closed_trades(symbol: str) -> list[dict]:
    """Read recommendations.csv and return all WIN/LOSS trades for this symbol."""
    try:
        csv_path = config.DATA_DIR / "recommendations.csv"
        if not csv_path.exists():
            return []
        import csv as _csv
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        return [
            r for r in rows
            if r.get("coin", "").upper() == symbol.upper()
            and r.get("status") in ("WIN", "LOSS")
        ]
    except Exception:
        return []


def _build_excluded_cooldown_set() -> set[str]:
    """
    Return symbols currently on EXCLUDED cooldown (within last 168h).
    These coins should be hidden from the top10 display entirely.
    """
    try:
        csv_path = config.DATA_DIR / "recommendations.csv"
        if not csv_path.exists():
            return set()
        import csv as _csv
        from datetime import datetime, timezone, timedelta
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=168)
        excluded = set()
        for r in rows:
            if r.get("status") != "EXCLUDED":
                continue
            raw_date = r.get("close_date") or r.get("date", "")
            if not raw_date:
                continue
            try:
                rec_dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                if rec_dt.tzinfo is None:
                    rec_dt = rec_dt.replace(tzinfo=timezone.utc)
                if rec_dt >= cutoff:
                    excluded.add(r.get("coin", "").upper())
            except ValueError:
                pass
        return excluded
    except Exception:
        return set()


def _build_approaching_tp_set(current_prices: dict[str, float], threshold_pct: float = 3.0) -> dict[str, float]:
    """
    Return {symbol: pct_away} for OPEN positions within threshold_pct % of their take_profit.
    These coins should be excluded from the top10 (they're about to close as WIN — no new entry needed).
    """
    result: dict[str, float] = {}
    try:
        csv_path = config.DATA_DIR / "recommendations.csv"
        if not csv_path.exists():
            return result
        import csv as _csv
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        for r in rows:
            if r.get("status") != "OPEN":
                continue
            sym = r.get("coin", "").upper()
            try:
                tp = float(r.get("take_profit") or 0)
            except (ValueError, TypeError):
                tp = 0.0
            if tp <= 0:
                continue
            price = current_prices.get(sym, 0.0)
            if price <= 0:
                continue
            pct_away = (tp - price) / tp * 100
            if 0 <= pct_away <= threshold_pct:
                result[sym] = round(pct_away, 1)
    except Exception:
        pass
    return result


PUMP_WATCHLIST_PATH = config.DATA_DIR / "pump_watchlist.json"
# Position sizing for auto whale ride entries
WHALE_RIDE_MAX_USD       = 18.0   # max $ per position (portfolio / 5)
WHALE_CRASH_TRIGGER      = 0.60   # >60% crash from peak → standard whale ride (TP +100%, SL -15%)
WHALE_CRASH_RISKY_MIN    = 0.40   # 40-60% crash → risky ride (TP +50%, SL -10%)
WHALE_CRASH_RISKY_MAX    = 0.60
WHALE_RISKY_MAX_MCAP_USD = 50_000_000   # only for coins < $50M mcap (high volatility tier)


def _load_pump_watchlist() -> dict:
    """Load pump watchlist from disk. Keys = symbol, values = {peak_price, added_at, peak_7d}."""
    try:
        if PUMP_WATCHLIST_PATH.exists():
            return json.loads(PUMP_WATCHLIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_pump_watchlist(watchlist: dict) -> None:
    try:
        PUMP_WATCHLIST_PATH.write_text(
            json.dumps(watchlist, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"  Warning: could not save pump watchlist: {e}")


def _quick_scam_news_check(symbol: str, name: str = "") -> str:
    """
    Search news for scam/rug signals on a pump coin.
    Returns a short reason string if found, "" if clean.
    """
    _SCAM_WORDS = {
        "rug pull", "rugpull", "exit scam", "scam", "fraud",
        "ponzi", "honeypot", "abandoned", "team dumped", "manipulation",
    }
    try:
        from src.connectors.web_research import search_cryptocompare_news
        for article in search_cryptocompare_news(symbol, limit=6):
            title = article.get("title", "").lower()
            for w in _SCAM_WORDS:
                if w in title:
                    return article["title"][:70]
    except Exception:
        pass
    return ""


def _classify_pump_coins(pump_coins: list[dict]) -> list[dict]:
    """
    Classify each pump coin (>100% 7d) into an action category.
    Never enter during a pump — wait for the crash.

    Actions:
    - DO_NOT_CHASE : known manipulation/whale-ride pattern; wait for >60% crash then enter
    - MONITORING   : unknown coin; no scam signals found; watchlist for post-crash entry
    - SKIP         : confirmed unrideable (serial scam with zero bounce history, or scam news)
    """
    if not pump_coins:
        return []

    from src.agents.coin_risk_assessor import assess_coin_risks
    risk_map = assess_coin_risks(pump_coins, fear_greed={"value": 50, "label": "Neutral"})

    classified = []
    for coin in pump_coins:
        sym   = coin.get("symbol", "").upper()
        price = coin.get("current_price", 0)
        ch7d  = coin.get("price_change_percentage_7d_in_currency") or 0
        ch24  = coin.get("price_change_percentage_24h") or 0
        mcap_m = (coin.get("market_cap") or 0) / 1e6
        risk  = risk_map.get(sym)
        cat   = risk.category if risk else "NORMAL"

        prev_trades = _get_previous_closed_trades(sym)
        prev_wins   = [t for t in prev_trades if t.get("status") == "WIN"]
        prev_losses = [t for t in prev_trades if t.get("status") == "LOSS"]

        # Is it a serial scam with no bounce history?
        is_serial = risk and any(
            "serial" in (f or "").lower() or "repeat" in (f or "").lower()
            for f in (risk.flags or [])
        )
        all_losses_no_wins = prev_losses and not prev_wins

        if cat in ("ACTIVE_SCAM", "MANIPULATED_REAL"):
            if is_serial and all_losses_no_wins:
                action  = "SKIP"
                reason  = "serial scam — no successful bounce on record"
                rideable = False
            else:
                action  = "DO_NOT_CHASE"
                wins_str = f" ({len(prev_wins)} WIN{'s' if len(prev_wins)!=1 else ''} on record)" if prev_wins else " (first cycle)"
                reason   = ((risk.reasoning or "manipulation pattern")[:70]) + wins_str
                rideable = True
        else:
            # Unknown coin: quick scam news check
            scam_hit = _quick_scam_news_check(sym, coin.get("name", ""))
            if scam_hit:
                action  = "SKIP"
                reason  = f"scam signal in news: {scam_hit}"
                rideable = False
            else:
                action  = "MONITORING"
                reason  = "new pump — no scam signals; watchlisting for post-crash entry"
                rideable = True

        classified.append({
            "symbol":   sym,
            "name":     coin.get("name", sym),
            "price":    price,
            "ch7d":     ch7d,
            "ch24":     ch24,
            "mcap_m":   mcap_m,
            "action":   action,
            "reason":   reason,
            "rideable": rideable,
            "prev_wins": prev_wins,
        })

    return classified


def _check_watchlist_crashes(all_coins: list[dict]) -> list[dict]:
    """
    Compare current prices against saved pump peaks.
    If a watchlisted coin has crashed >60% from its peak, create a whale ride entry.
    Returns list of auto whale ride dicts (entry_type="auto_watchlist").
    """
    watchlist = _load_pump_watchlist()
    if not watchlist:
        return []

    coin_map = {c.get("symbol", "").upper(): c for c in all_coins}
    auto_rides: list[dict] = []
    triggered:  list[str]  = []

    # Load already-open WHALE_RIDE symbols so we don't re-trigger the same position
    _open_whale_syms: set[str] = set()
    try:
        import csv as _csv_cw
        _rec_path = config.DATA_DIR / "recommendations.csv"
        if _rec_path.exists():
            with open(_rec_path, newline="", encoding="utf-8") as _rf:
                _open_whale_syms = {
                    r.get("coin", "").upper()
                    for r in _csv_cw.DictReader(_rf)
                    if r.get("type") == "WHALE_RIDE" and r.get("status") == "OPEN"
                }
    except Exception:
        pass

    for sym, entry in watchlist.items():
        if sym in WASH_TRADING_CONFIRMED or sym in PERMANENTLY_EXCLUDED:
            continue   # never auto-ride confirmed wash traders or permanently excluded coins
        # Already open as a WHALE_RIDE — clean up watchlist entry silently
        if sym in _open_whale_syms:
            triggered.append(sym)
            continue
        coin = coin_map.get(sym)
        if not coin:
            continue
        peak    = entry.get("peak_price", 0)
        current = coin.get("current_price", 0)
        if peak <= 0 or current <= 0:
            continue

        drop_from_peak = (current - peak) / peak  # negative value
        abs_drop = abs(drop_from_peak)
        mcap = coin.get("market_cap") or 0

        if abs_drop >= WHALE_CRASH_TRIGGER:
            # Standard whale ride: >60% crash, TP +100%, SL -15%
            prev_trades = _get_previous_closed_trades(sym)
            prev_wins   = [t for t in prev_trades if t.get("status") == "WIN"]
            known_cycles = [f"{float(t['pnl_pct']):+.0f}%" for t in prev_wins if t.get("pnl_pct")]
            auto_rides.append({
                "symbol":         sym,
                "name":           entry.get("name", sym),
                "coin_id":        coin.get("id", ""),
                "price":          current,
                "entry":          current,
                "stop_loss":      round(current * 0.85, 8),   # -15% pre-milestone
                "take_profit":    round(current * 2.00, 8),   # +100%
                "crash_reason":   f"pump {entry.get('peak_7d', 0):+.0f}% → crash {abs_drop*100:.0f}% from peak",
                "max_hold_hours": 48,
                "is_serial_scam": False,
                "allies":         [],
                "known_cycles":   known_cycles,
                "cycle_number":   len(prev_trades) + 1,
                "prev_wins":      prev_wins,
                "change_24h":     coin.get("price_change_percentage_24h") or 0,
                "change_7d":      coin.get("price_change_percentage_7d_in_currency") or 0,
                "max_usd":        WHALE_RIDE_MAX_USD,
                "entry_type":     "auto_watchlist",
                "drop_from_peak": round(drop_from_peak * 100, 1),
                "ride_tier":      "standard",
            })
            triggered.append(sym)

        elif (WHALE_CRASH_RISKY_MIN <= abs_drop < WHALE_CRASH_RISKY_MAX
              and mcap > 0 and mcap < WHALE_RISKY_MAX_MCAP_USD):
            # Risky whale ride: 40-60% crash on small-cap, TP +50%, SL -10%
            prev_trades = _get_previous_closed_trades(sym)
            prev_wins   = [t for t in prev_trades if t.get("status") == "WIN"]
            known_cycles = [f"{float(t['pnl_pct']):+.0f}%" for t in prev_wins if t.get("pnl_pct")]
            auto_rides.append({
                "symbol":         sym,
                "name":           entry.get("name", sym),
                "coin_id":        coin.get("id", ""),
                "price":          current,
                "entry":          current,
                "stop_loss":      round(current * 0.90, 8),   # -10% tight SL
                "take_profit":    round(current * 1.50, 8),   # +50% TP
                "crash_reason":   f"partial crash {abs_drop*100:.0f}% from peak (risky ride)",
                "max_hold_hours": 24,
                "is_serial_scam": False,
                "allies":         [],
                "known_cycles":   known_cycles,
                "cycle_number":   len(prev_trades) + 1,
                "prev_wins":      prev_wins,
                "change_24h":     coin.get("price_change_percentage_24h") or 0,
                "change_7d":      coin.get("price_change_percentage_7d_in_currency") or 0,
                "max_usd":        WHALE_RIDE_MAX_USD / 2,   # half size for risky tier
                "entry_type":     "auto_watchlist_risky",
                "drop_from_peak": round(drop_from_peak * 100, 1),
                "ride_tier":      "risky",
            })
            triggered.append(sym)

    # Remove triggered coins from watchlist
    if triggered:
        for sym in triggered:
            watchlist.pop(sym, None)
        _save_pump_watchlist(watchlist)

    return auto_rides


def _build_whale_ride(coin: dict, crash_reason: str, prev_trades: list[dict]) -> dict:
    """Build a whale ride candidate dict from coin market data and trade history."""
    symbol   = coin.get("symbol", "").upper()
    price    = coin.get("current_price", 0)
    # max_hold: 48h default; crash_reason may contain "ACTIVE_SCAM" or "MANIPULATED_REAL"
    is_scam  = any(kw in crash_reason.upper() for kw in ("SCAM", "SERIAL", "MANIPULATION"))
    max_hold = 24 if is_scam else 48

    prev_wins = [t for t in prev_trades if t.get("status") == "WIN"]
    cycle_num = len(prev_trades) + 1  # this would be cycle N+1

    # Build cycle history from previous scanner wins
    known_cycles: list[str] = []
    for t in prev_wins:
        try:
            pnl = float(t["pnl_pct"])
            known_cycles.append(f"{pnl:+.0f}%")
        except (ValueError, KeyError):
            pass

    return {
        "symbol":         symbol,
        "name":           coin.get("name", symbol),
        "coin_id":        coin.get("id", ""),
        "price":          price,
        "entry":          price,
        "stop_loss":      round(price * 0.85, 8),
        "take_profit":    round(price * 1.50, 8),
        "crash_reason":   crash_reason,
        "max_hold_hours": max_hold,
        "is_serial_scam": is_scam,
        "allies":         [],
        "known_cycles":   known_cycles,
        "cycle_number":   cycle_num,
        "prev_wins":      prev_wins,
        "change_24h":     coin.get("price_change_percentage_24h") or 0,
        "change_7d":      coin.get("price_change_percentage_7d_in_currency") or 0,
        "market_cap":     coin.get("market_cap") or 0,
    }


def _get_open_positions(current_prices: dict[str, float]) -> list[dict]:
    """
    Read OPEN positions from recommendations.csv.
    Returns list with symbol, entry, tp, sl, age_days, current_pnl_pct, is_stale,
    is_approaching_tp (pnl >= 8%), is_critical_loss (pnl <= -8%).

    Stale tiers:
      Tier 1: age >= 7d AND pnl < +3%  → force close (TIME EXIT)
      Tier 2: age > 10d AND pnl < +5%  → force close regardless
    """
    try:
        csv_path = config.DATA_DIR / "recommendations.csv"
        if not csv_path.exists():
            return []
        import csv as _csv
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        result = []
        for r in rows:
            if r.get("status") != "OPEN":
                continue
            # HOLD slots: only SCANNER positions (never Kraken portfolio / WHALE_RIDE)
            if r.get("type", "SCANNER") not in ("SCANNER", ""):
                continue
            sym = r.get("coin", "").upper()
            try:
                entry = float(r.get("entry") or 0)
                tp    = float(r.get("take_profit") or 0)
                sl    = float(r.get("stop_loss") or 0)
            except (ValueError, TypeError):
                entry, tp, sl = 0.0, 0.0, 0.0
            # Never surface rows with missing entry or TP — they can't be displayed
            if entry == 0.0 or tp == 0.0:
                continue
            raw_date = r.get("date") or r.get("open_date", "")
            try:
                open_dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                if open_dt.tzinfo is None:
                    open_dt = open_dt.replace(tzinfo=timezone.utc)
                age_days = (now - open_dt).days
            except Exception:
                age_days = 0
            current = current_prices.get(sym, 0.0)
            pnl_pct = ((current - entry) / entry * 100) if entry > 0 and current > 0 else None
            is_stale = (
                pnl_pct is not None and (
                    (age_days >= 7  and pnl_pct < 3.0)
                    or (age_days > 10 and pnl_pct < 5.0)
                )
            )
            is_approaching_tp = pnl_pct is not None and pnl_pct >= 8.0
            is_critical_loss  = pnl_pct is not None and pnl_pct <= -8.0
            result.append({
                "symbol":           sym,
                "entry":            entry,
                "tp":               tp,
                "sl":               sl,
                "age_days":         age_days,
                "pnl_pct":          pnl_pct,
                "is_stale":         is_stale,
                "is_approaching_tp": is_approaching_tp,
                "is_critical_loss":  is_critical_loss,
            })
        # Sort by pnl_pct DESC — best performers fill HOLD slots first
        result.sort(key=lambda x: (x["pnl_pct"] or 0.0), reverse=True)
        return result
    except Exception:
        return []


def run_smart_scanner(
    exchange: str | None = None,
    fear_greed: dict | None = None,
    open_count: int = 0,
) -> tuple[list[dict], list[dict], list[dict], int, dict]:
    """
    Fetch top coins, exclude stablecoins/wrapped tokens, optionally filter
    by exchange, score by TA opportunity.

    Returns (top10, pump_alerts, whale_rides, quality_count, catalysts).
    exchange:    None (no filter) | "revolut" | "binance" | "all"
    fear_greed:  dict with "value" (0-100) and "label" — used for macro filter
    """
    label    = exchange.upper() if exchange else "ALL EXCHANGES"
    fg_value = (fear_greed or {}).get("value", 50)   # 0-100; used for macro gates below
    # Fetch 3000 coins — CP sorts strictly by market cap, pump coins can sit at rank 1001-3000
    # while CoinGecko ranked them higher due to activity weighting. TA loop touches top 200
    # candidates (quick_score cap) — sized to stay within the CoinGecko pro 100k/month budget.
    _pages = 4
    _top_n = 3000
    print("\n" + "=" * 60)
    print(f"  SMART SCANNER — Top {_top_n} Coins [{label}]")
    print("=" * 60)

    from src.connectors.coingecko import get_eur_usd_rate as _get_eur_rate
    _eur = _get_eur_rate()

    # 1. Build allowed symbol set (None = no exchange filter)
    allowed: set[str] | None = None
    if exchange:
        ex = exchange.lower()
        if ex == "binance":
            print("\n  Fetching Binance tradeable pairs...")
            allowed = _get_binance_symbols()
        elif ex == "revolut":
            allowed = _get_revolut_symbols()
        elif ex == "all":
            print("\n  Fetching all exchange pairs...")
            allowed = _get_revolut_symbols() | _get_binance_symbols()
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

    # 2. Top coins market data — CoinPaprika (primary, single free request), CoinGecko fallback
    _cp_ok = False
    coins: list[dict] = []
    print(f"  Fetching top {_top_n} from CoinPaprika (primary)...")
    try:
        coins = _fetch_top_coinpaprika(limit=_top_n)
        if coins:
            _cp_ok = True
            # print(f"  Got {len(coins)} coins from CoinPaprika")
    except Exception as e:
        print(f"  CoinPaprika failed: {e} — falling back to CoinGecko")

    if not coins:
        # print(f"  Fetching top 1000 from CoinGecko ({_pages} page{'s' if _pages > 1 else ''})...")
        try:
            coins = _fetch_top_250_coingecko(pages=_pages)
            # print(f"  Got {len(coins)} coins from CoinGecko")
        except Exception as e:
            print(f"  ERROR: CoinGecko also failed: {e}")
            return [], [], [], 0, {}

    if not coins:
        print("  ERROR: no coin data from any source")
        return [], [], [], 0, {}

    # 3. Filter out stablecoins, wrapped tokens, tokenized stocks, wash traders, and permanently excluded
    excluded = STABLECOINS | WRAPPED_TOKENS | TOKENIZED_STOCKS | WASH_TRADING_CONFIRMED | PERMANENTLY_EXCLUDED
    MIN_MCAP   = 20_000_000   # $20M minimum market cap — allows Kraken small-caps
    MIN_VOLUME =    100_000   # $100K minimum daily volume — still tradeable with $500 positions

    def _real_mcap(c: dict) -> float:
        """
        CoinGecko's market_cap field can be based on total supply (inflated).
        Compute real mcap from circulating_supply × current_price as a sanity check.
        Use the LOWER of the two — if either is below threshold the coin is illiquid.
        """
        reported = c.get("market_cap") or 0
        circ     = c.get("circulating_supply") or 0
        price    = c.get("current_price") or 0
        derived  = circ * price if circ > 0 and price > 0 else reported
        return min(reported, derived)

    clean_coins = [
        c for c in coins
        if c.get("symbol", "").upper() not in excluded
        and not _is_price_stable(c)
        and not _is_tokenized_stock(c)
        and _real_mcap(c) >= MIN_MCAP
        and (c.get("total_volume") or 0) >= MIN_VOLUME
    ]
    micro_caps = sum(
        1 for c in coins
        if c.get("symbol", "").upper() not in excluded
        and not _is_price_stable(c) and not _is_tokenized_stock(c)
        and (_real_mcap(c) < MIN_MCAP or (c.get("total_volume") or 0) < MIN_VOLUME)
    )
    # print(f"  {len(coins) - len(clean_coins)} stablecoins/wrapped/excluded/micro-caps removed → {len(clean_coins)} remain")
    if micro_caps:
        # print(f"  ({micro_caps} illiquid coins excluded: real mcap <$20M or volume <$100K/day)")
        pass

    # 4. Filter to exchange-available only (skip if no exchange specified)
    if allowed is not None:
        exchange_coins = [c for c in clean_coins if c.get("symbol", "").upper() in allowed]
        print(f"  {len(exchange_coins)} coins available on {label}")
    else:
        exchange_coins = clean_coins

    # 4b. Dead project / rug exclusions handled in step 4e via coin_risk_assessor.

    # 4c. Separate pumped coins (>100% 7d gain) — classify and watchlist; never chase
    raw_pump_coins = [
        c for c in exchange_coins
        if (c.get("price_change_percentage_7d_in_currency") or 0) > 100
    ]
    exchange_coins = [c for c in exchange_coins if c not in raw_pump_coins]

    # Check existing watchlist for crashes that now qualify for whale ride entry
    auto_watchlist_rides = _check_watchlist_crashes(exchange_coins + raw_pump_coins)
    if auto_watchlist_rides:
        print(f"  🐋 {len(auto_watchlist_rides)} AUTO WHALE RIDE(S) triggered from pump watchlist!")

    # Classify pump coins and update watchlist
    pump_classified: list[dict] = []
    if raw_pump_coins:
        # print(f"  {len(raw_pump_coins)} pump alert(s) (>100% 7d) — classifying...")
        pass

    # 4d. Manual exclusions from portfolio.json
    portfolio_symbols = set()
    try:
        from config import PORTFOLIO_PATH
        if PORTFOLIO_PATH.exists():
            with open(PORTFOLIO_PATH) as f:
                pf = json.load(f)
            for h in pf.get("holdings", []):
                portfolio_symbols.add(h["asset"].upper())
    except Exception:
        pass

    if portfolio_symbols:
        pre_pf = len(exchange_coins)
        exchange_coins = [
            c for c in exchange_coins
            if c.get("symbol", "").upper() not in portfolio_symbols
        ]
        excluded_pf = pre_pf - len(exchange_coins)
        portfolio_in_scan = excluded_pf
        if excluded_pf:
            # print(f"  🚫 {excluded_pf} portfolio coin(s) excluded")
            pass

    # 4d-2. Also exclude coins with OPEN scanner recommendations.
    # portfolio.json only tracks actual held coins; recommendations.csv tracks
    # bot-logged entries. Without this, a coin like ARB can appear as a "new" pick
    # even while it already has an OPEN recommendation.
    _open_scanner_syms:  set[str] = set()  # used later to guard whale_rides building
    _recent_whale_syms:  set[str] = set()  # OPEN/EXCLUDED whale ride within 7d — skip re-log
    try:
        import csv as _csv_mod
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td

        def _parse_rec_date(s: str):
            try:
                return _dt.fromisoformat(s.replace(" UTC", "+00:00"))
            except Exception:
                return _dt.min.replace(tzinfo=_tz.utc)

        _7d_ago = _dt.now(_tz.utc) - _td(days=7)
        _rec_path = config.DATA_DIR / "recommendations.csv"
        if _rec_path.exists():
            with open(_rec_path, newline="", encoding="utf-8") as _rf:
                _all_open_rows = list(_csv_mod.DictReader(_rf))
            _now_utc = _dt.now(_tz.utc)
            _open_rec_syms = {
                r.get("coin", "").upper()
                for r in _all_open_rows
                if r.get("status") == "OPEN"
                and (_now_utc - _parse_rec_date(r.get("date", ""))).total_seconds() / 3600 < 24
            }
            # Track SCANNER-only open positions — used to block whale_rides category mixing
            _open_scanner_syms = {
                r.get("coin", "").upper()
                for r in _all_open_rows
                if r.get("status") == "OPEN"
                and r.get("type", "SCANNER") in ("SCANNER", "")
                and (_now_utc - _parse_rec_date(r.get("date", ""))).total_seconds() / 3600 < 24
            }
            # Track recently logged whale rides (OPEN or EXCLUDED within 7d) — skip re-display
            _recent_whale_syms = {
                r.get("coin", "").upper()
                for r in _all_open_rows
                if r.get("type") == "WHALE_RIDE"
                and r.get("status") in ("OPEN", "EXCLUDED")
                and _parse_rec_date(r.get("date", "")) >= _7d_ago
            }
            if _open_rec_syms:
                # Mark open-position coins instead of removing them from scoring.
                # They still appear in top10 for monitoring context (shown as HOLD),
                # but Step 0H in Groq pre-filter prevents them from becoming new entries.
                for c in exchange_coins:
                    if c.get("symbol", "").upper() in _open_rec_syms:
                        c["_already_open"] = True
                # print(f"  🔒 {len(_open_rec_syms)} OPEN position(s) kept in top10 as HOLD display: "
                #       f"{', '.join(sorted(_open_rec_syms))}")
                pass
    except Exception:
        pass

    # 4e. Real-time risk assessment — replaces all hardcoded scam/rug-pull lists.
    from src.agents.coin_risk_assessor import assess_coin_risks
    risk_map = assess_coin_risks(exchange_coins, fear_greed={"value": 50, "label": "Neutral"})

    rug_pull_coins: list[tuple[str, float, str]] = []
    whale_rides:    list[dict] = []
    safe_coins      = []

    for coin in exchange_coins:
        sym  = coin.get("symbol", "").upper()
        risk = risk_map.get(sym)
        cat  = risk.category if risk else "NORMAL"

        if cat in ("ACTIVE_SCAM", "MANIPULATED_REAL"):
            if sym in WASH_TRADING_CONFIRMED or sym in PERMANENTLY_EXCLUDED:
                continue   # never whale-ride confirmed wash traders or permanently excluded coins
            # Never label a coin as whale ride if it's already open as SCANNER
            if sym in _open_scanner_syms:
                continue
            # Skip if already logged as OPEN or EXCLUDED WHALE_RIDE within 7d — prevents
            # repeated display/logging of the same manipulation event
            if sym in _recent_whale_syms:
                continue
            prev         = _get_previous_closed_trades(sym)
            crash_reason = risk.reasoning if risk else cat
            if risk and risk.flags:
                crash_reason = risk.flags[0] + " | " + (risk.reasoning or "")
            whale_rides.append(_build_whale_ride(coin, crash_reason[:200], prev))

        elif cat in ("DEAD_PROJECT", "SUSPICIOUS"):
            # Short-term focus: a "dead" coin with volume and a catalyst is a valid bounce trade
            # Flag it for Groq awareness but don't exclude — volume/setup scoring decides
            if risk:
                coin["_risk_warning"] = risk.reasoning[:120]
            safe_coins.append(coin)

        else:
            safe_coins.append(coin)

    if rug_pull_coins:
        print(f"  🚨 {len(rug_pull_coins)} active scam/rug coin(s) → whale ride candidates")
    if whale_rides:
        print(f"  🐋 {len(whale_rides)} whale ride candidate(s)")
    exchange_coins = safe_coins

    # 5. Pre-filter score all, take top 200 for OHLCV/TA analysis
    quick_scored = []
    for coin in exchange_coins:
        qs, qr = _quick_score(coin, trending_symbols)
        if qs == -99:
            continue   # hard pre-exclude (pumped/micro-cap/low-float)
        quick_scored.append((coin, qs, qr))
    quick_scored.sort(key=lambda x: x[1], reverse=True)
    candidates = quick_scored[:250]

    # 5b. Compute sector averages from the full coin list (narrative momentum)
    sector_avgs = _compute_sector_avgs(exchange_coins)
    trending_sectors = [s for s, avg in sector_avgs.items() if avg > 20]
    if trending_sectors:
        # print(f"  Trending sectors: {', '.join(trending_sectors)}")
        pass

    # 5c. Batch-fetch news for all 40 candidates (used for news catalyst scoring)
    candidate_coins = [c for c, _, _ in candidates]
    per_coin_news: dict[str, list[dict]] = {}
    try:
        import config as _cfg
        from src.connectors.web_research import fetch_news_for_coins
        _src = "Tavily AI" if _cfg.TAVILY_API_KEY else "Google News RSS"
        # print(f"  Fetching news for {len(candidate_coins)} candidates ({_src})...")
        per_coin_news = fetch_news_for_coins(candidate_coins, limit_per_coin=5)
        found = sum(1 for v in per_coin_news.values() if v)
        # print(f"  News found for {found}/{len(candidate_coins)} candidates")
    except Exception as e:
        print(f"  News fetch skipped: {e}")

    # 6. Pre-build CG ID map once (avoids one CG API call per coin in the loop)
    global _cg_id_cache
    try:
        _cg_id_cache = _cp_build_cg_id_map()
        # print(f"  CG ID map loaded ({len(_cg_id_cache)} symbols)")
    except Exception as _cg_map_e:
        print(f"  CG ID map failed ({_cg_map_e}) — using static map only")
        _cg_id_cache = {}

    _ath_date_cache = _cp_get_ath_date_map()

    # 6b. Fetch OHLCV + compute TA for each candidate
    # print(f"\n  Computing TA for {len(candidates)} candidates (top250 by pre-filter score)...")
    results      = []
    wash_trading = []  # symbols excluded for wash trading
    _bearish_skip_count = 0       # tracks market-wide bearish alignment skips
    _market_wide_announced = False  # print the relaxed-mode banner only once
    for i, (coin, qs, qr) in enumerate(candidates):
        coin_id = coin["id"]
        symbol = coin["symbol"].upper()
        # print(f"  [{i+1}/{len(candidates)}] {symbol:<12} score={qs:+d}  fetching OHLCV...", end="", flush=True)
        try:
            ohlcv = _fetch_ohlcv_for_coin(coin, days=30)
            if not ohlcv or len(ohlcv) < _MIN_CANDLES:
                # print(f" ⚠️  no OHLCV — continuing with neutral TA (RSI/MACD unknown)")
                ohlcv = []  # compute_ta handles empty list → neutral signals

            # Wash trading check — runs before TA to avoid wasting cycles
            is_wash, wash_reason = _check_wash_trading(coin, ohlcv)
            if is_wash:
                wash_trading.append((symbol, wash_reason))
                # print(f" ⚠️  WASH TRADING — {wash_reason}")
                continue

            ta    = compute_ta(coin_id, symbol, ohlcv)
            vm    = round((coin.get("total_volume") or 0) / max(coin.get("market_cap") or 1, 1), 3)
            # These must be resolved before scoring calls
            trend_val = getattr(ta, "trend", None)
            change_7d = coin.get("price_change_percentage_7d_in_currency") or 0
            ath_pct   = coin.get("ath_change_percentage") or 0

            ts, tr = _ta_score(ta.rsi_14, ta.macd_signal, ta.bollinger_position, vol_mcap=vm)
            cs, cr = _catalyst_score(coin, ta.rsi_14, trend=trend_val, change_7d=change_7d)
            ns, nr = _news_score(per_coin_news.get(symbol, []), symbol=symbol, coin_name=coin.get("name", ""))
            ss, sr = _sector_score(symbol, sector_avgs)

            coiled_spring = ath_pct < -90 and ta.rsi_14 is not None and ta.rsi_14 < 35

            # Proven winner bonus/penalty based on full W/L record and net P&L
            prev_closed   = _get_previous_closed_trades(symbol)
            prev_wins     = [t for t in prev_closed if t.get("status") == "WIN"]
            prev_losses   = [t for t in prev_closed if t.get("status") == "LOSS"]
            proven_score  = 0
            proven_reason = []
            if prev_closed:
                n_wins   = len(prev_wins)
                n_losses = len(prev_losses)
                def _pnl(t):
                    try:
                        return float(t.get("pnl_pct") or 0)
                    except (ValueError, TypeError):
                        return 0.0
                net_pnl = sum(_pnl(t) for t in prev_closed)
                record_str = f"{n_wins}W/{n_losses}L"
                if n_wins > n_losses and net_pnl > 0:
                    proven_score = 1
                    proven_reason = [f"proven winner ({record_str}, net {net_pnl:+.1f}%)"]
                elif n_losses > n_wins:
                    proven_score = -1
                    proven_reason = [f"proven loser ({record_str}, net {net_pnl:+.1f}%)"]
                else:
                    proven_reason = [f"mixed record ({record_str}, net {net_pnl:+.1f}%)"]

            # ── Pre-filter Step 0: directional & momentum gates ──────────
            change_24h_raw = coin.get("price_change_percentage_24h") or 0
            momentum_stall = False

            # Gate: POST-TGE DUMP — ATH set within 45 days + 25%+ below ATH + still falling
            # Catches airdrop/listing dumps where sell pressure is mechanical, not structural.
            _ath_date_str = _ath_date_cache.get(symbol, "")
            if _ath_date_str:
                try:
                    from datetime import datetime as _dt_ath
                    _ath_dt = _dt_ath.strptime(_ath_date_str, "%Y-%m-%d")
                    _days_since_ath = (_dt_ath.utcnow() - _ath_dt).days
                    if _days_since_ath <= 45 and ath_pct < -25 and change_7d < -5:
                        print(f"  ❌ SKIP {symbol}: POST-TGE DUMP — ATH {_days_since_ath}d ago, "
                              f"{ath_pct:.0f}% from ATH, 7d {change_7d:+.1f}%")
                        continue
                except (ValueError, TypeError):
                    pass

            # Gate: LOSS < 48h ago → re-entry cooldown
            if prev_closed:
                from datetime import datetime as _dt, timezone as _tz
                _now_utc = _dt.now(_tz.utc)
                _recent_loss = False
                for _t in reversed(prev_closed):
                    if _t.get("status") != "LOSS":
                        continue
                    try:
                        _cd = _t.get("close_date", "")
                        _close_dt = _dt.strptime(_cd, "%Y-%m-%d %H:%M UTC").replace(tzinfo=_tz.utc)
                        _hrs_ago = (_now_utc - _close_dt).total_seconds() / 3600
                        if _hrs_ago < 48:
                            _recent_loss = True
                            print(f"  ❌ SKIP {symbol}: LOSS cooldown — {_hrs_ago:.0f}h ago (48h required)")
                    except Exception:
                        pass
                    break  # only check most recent LOSS
                if _recent_loss:
                    continue

                # Gate: re-entry price > previous TP → chasing above target
                _last_tp = None
                for _t in reversed(prev_closed):
                    _tp_raw = _t.get("take_profit") or _t.get("tp", "")
                    if _tp_raw:
                        try:
                            _last_tp = float(_tp_raw)
                        except (ValueError, TypeError):
                            pass
                        break
                if _last_tp and _last_tp > 0:
                    _cur_p = coin.get("current_price", 0)
                    if _cur_p > _last_tp:
                        # print(f"  ❌ SKIP {symbol}: current ${_cur_p:.4f} > prev TP ${_last_tp:.4f} — re-entry too late")
                        continue

            # Gate: extreme pump — coin already ran >200% in 7 days → not a fresh entry
            if change_7d > 200:
                # print(f"  ❌ SKIP {symbol}: already pumped +{change_7d:.0f}% 7d — whale ride territory, not scanner")
                continue

            # Gate: full bearish alignment — skip only if ALL conditions AND not oversold.
            # Oversold coins (RSI < 45) are EXEMPTED: bearish trend on an oversold coin
            # is the contrarian SETUP we want, not a reason to skip.
            _rsi_val = ta.rsi_14 if ta.rsi_14 is not None else 50
            _oversold_exempt = _rsi_val < 45
            _market_wide_bearish = _bearish_skip_count >= len(candidates) * 0.5
            if _market_wide_bearish:
                if not _market_wide_announced:
                    # print("  ⚠️ MARKET-WIDE BEARISH — filter relaxed (only hard-crash coins skipped)")
                    _market_wide_announced = True
                if change_7d < -15 and change_24h_raw < -8 and not _oversold_exempt:
                    _bearish_skip_count += 1
                    # print(f"  ❌ SKIP {symbol}: market-wide bearish, hard crash (7d {change_7d:.1f}%, 24h {change_24h_raw:.1f}%)")
                    continue
            elif (ta.macd_signal == "BEARISH" and trend_val == "BEARISH"
                  and change_7d < -10 and change_24h_raw < -5
                  and not _oversold_exempt):
                _bearish_skip_count += 1
                # print(f"  ❌ SKIP {symbol}: full bearish (MACD+Trend BEARISH, 7d {change_7d:.1f}%, 24h {change_24h_raw:.1f}%)")
                continue

            # Gate: extreme fear (F&G < 30) + no MACD bullish = 49% loss rate (data-driven)
            # Require MACD bullish confirmation when market is in extreme fear — neutral/bearish
            # MACD in fear means no momentum confirmation and historically near coin-flip losses.
            if fg_value < 30 and ta.macd_signal != "BULLISH":
                _write_shadow_log(symbol, 0, f"extreme fear gate (F&G={fg_value}, MACD={ta.macd_signal})")
                # print(f"  ❌ SKIP {symbol}: F&G {fg_value} (extreme fear) — MACD {ta.macd_signal}, not bullish")
                continue

            # Momentum stall flag: MACD bearish + trend neutral → cap score at 2
            # (MACD bearish and trend penalties already counted in _ta_score / _catalyst_score)
            _stall_reason: list[str] = []
            if ta.macd_signal == "BEARISH" and trend_val == "NEUTRAL":
                momentum_stall = True
                _stall_reason  = ["⚠️ MOMENTUM STALL (MACD bearish + trend neutral)"]

            # ── Circulating supply tiers v3 ───────────────────────────────
            #   < 15%  EXCLUDED: unlock risk too high — hard skip
            #   15-30% MEDIUM:   score capped at 3, HALF SIZE
            #   > 30%  NONE:     no penalty
            circ_supply  = coin.get("circulating_supply") or 0
            total_supply = coin.get("total_supply") or 0
            circ_pct_val = (circ_supply / total_supply * 100) if total_supply > 0 and circ_supply > 0 else 100.0

            # Gate: circ supply < 15% → hard skip
            if circ_pct_val < 15.0:
                # print(f"  ❌ SKIP {symbol}: circ supply {circ_pct_val:.0f}% < 15% (unlock risk)")
                continue

            # Gate: POST-PUMP SUPPLY RISK — 24h > +20% AND circ supply < 25%
            # High 24h gain on a low-float token = supply overhang; insiders can dump.
            # Do NOT open position; do NOT send to Groq.
            if change_24h_raw > 20.0 and circ_pct_val < 25.0:
                # print(f"  ❌ SKIP {symbol}: POST-PUMP SUPPLY RISK — "
                #       f"24h {change_24h_raw:+.1f}% > +20% with only {circ_pct_val:.0f}% circ supply")
                continue

            # Gate: RSI > 78 → overbought, hard skip
            if ta.rsi_14 is not None and ta.rsi_14 > 78:
                # print(f"  ❌ SKIP {symbol}: RSI {ta.rsi_14:.1f} > 78 (overbought)")
                continue


            circ_cap_reason = []
            raw_score = qs + ts + cs + ns + ss + proven_score

            # Gate: score ≤ 0 → skip entirely
            if raw_score <= 0:
                continue

            # Scores 1-2 → shadow log only; Groq pre-filter (Step 0G) blocks entries for score ≤ 2
            if raw_score <= 2:
                _write_shadow_log(symbol, raw_score, f"score={raw_score} (display only, need ≥3 for entry)")

            # Momentum stall cap: MACD bearish + trend neutral → score capped at 2
            if momentum_stall and raw_score > 2:
                # print(f"  ⚠️  {symbol} MOMENTUM STALL — score capped 2 (was {raw_score})")
                raw_score = 2

            if circ_pct_val < 30.0:
                supply_risk = "MEDIUM"
                if raw_score > 3:
                    # print(f"  ⚠️  {symbol} circ supply {circ_pct_val:.0f}% MEDIUM — score capped 3 (was {raw_score})")
                    raw_score = 3
                circ_cap_reason = [f"circ supply {circ_pct_val:.0f}% MEDIUM (score capped 3 — HALF SIZE)"]
            else:
                supply_risk = "NONE"

            risk_warning  = coin.get("_risk_warning", "")
            extra_reasons = qr + tr + cr + nr + sr + proven_reason + _stall_reason + circ_cap_reason
            if risk_warning and supply_risk == "NONE":
                # Only prepend risk_warning for coins not already flagged by supply tier
                extra_reasons = [f"⚠️ {risk_warning[:80]}"] + extra_reasons

            # ── Tiebreaker fields (all used when main score ties) ──────────
            macd_v  = ta.macd_signal or ""
            trend_v = trend_val or ""

            # TB-0: supply-restricted coins lose to clean coins at same score
            # HIGH risk excluded from top10 entirely; MEDIUM loses tiebreakers
            supply_capped_tb  = -1 if supply_risk == "MEDIUM" else 0
            # TB-1b: momentum stall coins rank last among tied coins
            momentum_stall_tb = -1 if momentum_stall else 0

            # TB-clean: coin WITHOUT supply flag AND WITHOUT above_upper_BB ranks
            # above a coin that has either flag — checked before 24h momentum.
            clean_setup_tb = -1 if (supply_risk != "NONE" or ta.bollinger_position == "ABOVE_UPPER") else 0

            # TB-1: MACD+Trend alignment (both bullish=2 … both bearish=-2)
            if macd_v == "BULLISH" and trend_v == "BULLISH":
                macd_trend_tb = 2
            elif macd_v == "BULLISH" or trend_v == "BULLISH":
                macd_trend_tb = 1
            elif macd_v == "BEARISH" and trend_v == "BEARISH":
                macd_trend_tb = -2
            elif macd_v == "BEARISH" or trend_v == "BEARISH":
                macd_trend_tb = -1
            else:
                macd_trend_tb = 0

            # TB-2: BB position — above upper band = extended/risky, loses tiebreaker
            bb_tb = -1 if ta.bollinger_position == "ABOVE_UPPER" else (
                     1 if ta.bollinger_position == "BELOW_LOWER" else 0)

            # ── Archetype classification ───────────────────────────────────
            # A — Oversold Bounce: deep dip + BB oversold + MACD bullish reversal
            archetype_a = (
                ta.rsi_14 is not None and ta.rsi_14 < 42
                and ta.bollinger_position == "BELOW_LOWER"
                and -85 <= change_7d <= -8
                and ta.macd_signal == "BULLISH"
            )
            # B — Momentum Continuation: RSI mid-range + active 24h + healthy vol
            archetype_b = (
                ta.rsi_14 is not None and 50 <= ta.rsi_14 <= 72
                and 2 <= change_24h_raw <= 15
                and 0.15 <= vm <= 0.70
                and ta.macd_signal == "BULLISH"
                and trend_val in ("BULLISH", "NEUTRAL")
            )
            archetype = "A" if archetype_a else ("B" if archetype_b else "")

            # SIREN/DEEP DIP bonus — ultra-high upside: 7d > 50% dip + deeply oversold + MACD bullish
            deep_dip = (
                change_7d < -50
                and ta.rsi_14 is not None and ta.rsi_14 < 40
                and ta.macd_signal == "BULLISH"
                and vm > 0.15
            )
            deep_dip_tb = 1 if deep_dip else 0
            # if deep_dip:
            #     print(f"  ⭐ DEEP DIP: {symbol} — 7d {change_7d:.1f}%, RSI {ta.rsi_14:.1f}, MACD bullish")
            #     pass

            _price_usd = coin.get("current_price", 0)
            results.append({
                "coin_id":         coin_id,
                "symbol":          symbol,
                "name":            coin.get("name", ""),
                "price":           _price_usd,
                "price_eur":       coin.get("current_price_eur") or _price_usd * _eur,
                "change_24h":      coin.get("price_change_percentage_24h") or 0,
                "change_7d":       change_7d,
                "market_cap":      coin.get("market_cap") or 0,
                "score":           raw_score,
                "supply_capped_tb": supply_capped_tb,  # TB-0: supply-capped coins lose
                "macd_trend_tb":   macd_trend_tb,      # TB-1: MACD+trend alignment
                "bb_tb":           bb_tb,               # TB-2: BB position
                "proven_wins_tb":  proven_score,         # TB-4: +1 winner, -1 loser, 0 mixed/unknown
                "reasons":       extra_reasons,
                "rsi":           ta.rsi_14,
                "macd":          ta.macd_signal,
                "bb_pos":        ta.bollinger_position,
                "trend":         ta.trend,
                "recommended_order": ta.recommended_order,
                "ath_pct":       round(ath_pct, 1),
                "coiled_spring": coiled_spring,
                "sec_commodity": symbol in SEC_COMMODITY_TOKENS,
                "risk_warning":  risk_warning,
                "vol_mcap":      vm,
                "supply_risk":      supply_risk,       # "MEDIUM" | "NONE"
                "circ_pct":         round(circ_pct_val, 1),
                "momentum_stall_tb": momentum_stall_tb,  # tiebreaker: stalled coins rank last
                "clean_setup_tb":   clean_setup_tb,    # tiebreaker: no supply flag + no above BB
                "archetype":        archetype,          # "A" | "B" | ""
                "deep_dip":         deep_dip,           # SIREN bonus rule
                "deep_dip_tb":      deep_dip_tb,        # +1 → prioritized in top 3
            })
            rsi_str  = f"RSI={ta.rsi_14:.0f}" if ta.rsi_14 else "RSI=n/a"
            bb_str   = f"BB={ta.bollinger_position}" if ta.bollinger_position else "BB=n/a"
            # print(f" score={raw_score:+d}  {rsi_str}  {bb_str}  candles={len(ohlcv)}")
        except Exception as _e:
            print(f" ERROR: {_e}")

        time.sleep(0.11 if _cp_ok else 2)  # CP: 10 req/sec; CG: ~30 req/min

    if wash_trading:
        print(f"  ⚠️  {len(wash_trading)} wash trading coin(s) excluded from ranking")

    # Remove coins currently on EXCLUDED cooldown (7-day ban — don't display them at all)
    excluded_cooldown = _build_excluded_cooldown_set()
    if excluded_cooldown:
        pre_excl = len(results)
        results = [r for r in results if r["symbol"].upper() not in excluded_cooldown]
        removed = pre_excl - len(results)
        if removed:
            print(f"  🚫 {removed} coin(s) hidden — on EXCLUDED cooldown: {', '.join(excluded_cooldown)}")

    # Sort: primary = score, then tiebreakers in order.
    # clean_setup_tb is checked BEFORE 24h momentum: a coin with supply flag or
    # above upper BB ranks below a clean coin at the same score, regardless of how
    # much it pumped today.
    results.sort(
        key=lambda x: (
            x["score"],
            x.get("deep_dip_tb", 0),         # TB-0: DEEP DIP (SIREN bonus) prioritized first
            x.get("clean_setup_tb", 0),      # TB-1: clean (no flag, no above-BB) beats flagged
            x.get("change_24h", 0),          # TB-2: highest 24h momentum wins
            x.get("vol_mcap", 0),            # TB-3: higher vol/mcap = more active trading
            x.get("proven_wins_tb", 0),      # TB-4: proven winner is more reliable
            1 if x.get("sec_commodity") else 0,  # TB-5: SEC/CFTC commodity = quality filter
            x.get("supply_capped_tb", 0),    # TB-6: supply-restricted coins rank lower
            x.get("momentum_stall_tb", 0),   # TB-7: stalled coins rank last among ties
        ),
        reverse=True,
    )

    # With v3 thresholds, <15% supply is hard-skipped; no HIGH risk tier remains in results.
    normal_results = results

    # Remove coins approaching their TP (within 3% away) — they're about to close WIN,
    # no new entry needed; display them as a note instead.
    current_prices = {r["symbol"].upper(): r.get("price", 0.0) for r in normal_results}
    approaching_tp = _build_approaching_tp_set(current_prices, threshold_pct=3.0)
    if approaching_tp:
        for sym, pct in approaching_tp.items():
            # print(f"  ⏳ {sym} approaching TP ({pct:.1f}% away) — excluded from new picks")
            pass
        normal_results = [r for r in normal_results if r["symbol"].upper() not in approaching_tp]

    # ── Most Valuable Only ──
    # Show Top Long/Short/Spot picks (no score gate, user wants "top" of each)
    top_longs  = [r for r in normal_results if r["recommended_order"] == "LONG"][:5]
    top_shorts = [r for r in normal_results if r["recommended_order"] == "SHORT"][:5]
    top_spots  = [r for r in normal_results if r["recommended_order"] == "SPOT"][:5]
    top10 = normal_results[:10]

    # Fetch 1-sentence news catalysts for actual picks
    picks_to_fetch = top_longs + top_shorts + top_spots
    if not picks_to_fetch: picks_to_fetch = top10
    _catalysts: dict[str, str] = {}
    try:
        from src.connectors.web_research import get_top10_catalysts as _get_catalysts
        _catalysts = _get_catalysts(picks_to_fetch)
    except Exception:
        pass

    print(f"\n  MOST VALUABLE OPPORTUNITIES (TOP PICKS BY CATEGORY)\n" + "=" * 60)
    
    if top_longs:
        print(f"\n  🚀  TOP LONG PICKS")
        print("-" * 30)
        for rank, r in enumerate(top_longs, 1):
            _print_pick(rank, r, _catalysts)
    else:
        print("\n  🚀  TOP LONG PICKS: none found")

    if top_shorts:
        print(f"\n  📉  TOP SHORT PICKS")
        print("-" * 30)
        for rank, r in enumerate(top_shorts, 1):
            _print_pick(rank, r, _catalysts)
    else:
        print("\n  📉  TOP SHORT PICKS: none found")

    if top_spots:
        print(f"\n  💰  TOP SPOT PICKS")
        print("-" * 30)
        for rank, r in enumerate(top_spots, 1):
            _print_pick(rank, r, _catalysts)
    else:
        print("\n  💰  TOP SPOT PICKS: none found")

    # ── HOLD fill: show open positions when fewer than 3 quality new picks ──
    quality_picks = top_longs + top_shorts + top_spots
    if len(quality_picks) < 3 and open_positions:
        needed      = 3 - len(quality_picks)
        hold_fills  = [p for p in open_positions if not p["is_stale"]][:needed]
        if hold_fills:
            print(f"\n  📌  HOLD POSITIONS  (filling {needed} slot(s) — fewer than 3 new picks)\n" + "-" * 60)
            for p in hold_fills:
                pnl_str = f"{p['pnl_pct']:+.1f}%" if p["pnl_pct"] is not None else "N/A"
                print(f"  📌 {p['symbol']:8s} HOLD  |  entry: ${p['entry']:.4f}  TP: ${p['tp']:.4f}  "
                      f"PnL: {pnl_str}  age: {p['age_days']}d")

    # ── Pump alerts — classified display ──────────────────────────────────
    if pump_classified:
        # print(f"\n  ⚠  PUMP ALERTS  (>100% 7d — NEVER chase; wait for crash)\n" + "-" * 60)
        pass
        for pc in pump_classified:
            sym  = pc["symbol"]
            ch7d = pc["ch7d"]
            ch24 = pc["ch24"]
            p    = pc["price"]
            m    = pc["mcap_m"]
            act  = pc["action"]

            if act == "DO_NOT_CHASE":
                wins = pc.get("prev_wins", [])
                wins_str = f" [{len(wins)} prev WIN{'s' if len(wins)!=1 else ''}]" if wins else ""
                print(f"\n  🐋 {sym:8s} {_pfmt(p)}  |  7d: {ch7d:+.0f}%  24h: {ch24:+.1f}%  MCap: ${m:.0f}M")
                print(f"     DO NOT CHASE — wait for crash >60% from peak, then auto whale ride{wins_str}")
                print(f"     Reason: {pc['reason']}")
                print(f"     Watchlisted ✓  |  Target entry: SL -25% / TP +100% / max 48h / max ${WHALE_RIDE_MAX_USD:.0f}")
            elif act == "MONITORING":
                print(f"\n  🔍 {sym:8s} {_pfmt(p)}  |  7d: {ch7d:+.0f}%  24h: {ch24:+.1f}%  MCap: ${m:.0f}M")
                print(f"     NEW PUMP — monitoring for post-crash whale ride opportunity")
                print(f"     {pc['reason']}")
            else:  # SKIP
                print(f"\n  ⛔ {sym:8s} {_pfmt(p)}  |  7d: {ch7d:+.0f}%  — SKIP — {pc['reason']}")

    # ── Auto whale rides from pump watchlist ──────────────────────────────
    all_whale_rides = whale_rides + auto_watchlist_rides
    if auto_watchlist_rides:
        print(f"\n  🐋  AUTO WHALE RIDE — crash triggered from watchlist\n" + "-" * 60)
        for wr in auto_watchlist_rides:
            sym   = wr["symbol"]
            p     = wr["price"]
            sl    = wr["stop_loss"]
            tp    = wr["take_profit"]
            drop  = wr.get("drop_from_peak", 0)
            tier  = wr.get("ride_tier", "standard")
            cycles_str = " → ".join(wr["known_cycles"]) if wr["known_cycles"] else "first recorded"
            if tier == "risky":
                sl_pct = "-10%"
                tp_pct = "+50%"
                max_usd = wr.get("max_usd", WHALE_RIDE_MAX_USD / 2)
                hold_h  = wr.get("max_hold_hours", 24)
                print(f"\n  ⚡ RISKY WHALE RIDE: {sym} {_pfmt(p)}  (partial crash {abs(drop):.0f}% from peak)")
                print(f"     Entry: {_pfmt(p)} | SL: {_pfmt(sl)} ({sl_pct}) | TP: {_pfmt(tp)} ({tp_pct})")
                print(f"     Max hold: {hold_h}h | Max position: ${max_usd:.0f} (HALF SIZE — high risk)")
                print(f"     Pattern: {cycles_str}")
                print(f"     Reason: {wr['crash_reason']}")
            else:
                print(f"\n  🐋 AUTO WHALE RIDE: {sym} {_pfmt(p)}  (crashed {abs(drop):.0f}% from pump peak)")
                print(f"     Entry: {_pfmt(p)} | SL: {_pfmt(sl)} (-15% pre-milestone) | TP: {_pfmt(tp)} (+100%)")
                print(f"     Max hold: 48h | Max position: ${WHALE_RIDE_MAX_USD:.0f}")
                print(f"     Pattern: {cycles_str}")
                print(f"     Reason: {wr['crash_reason']}")

    all_rug_display = rug_pull_coins
    if all_rug_display:
        print(f"\n  ⛔  RUG PULL DETECTED  (excluded)\n" + "-" * 60)
        for sym, price, reason in all_rug_display:
            print(f"  ✗  {sym:10s}  {_pfmt(price)}  —  {reason}")

    if wash_trading:
        print(f"\n  ⚠️  WASH TRADING SUSPECTED  (excluded)\n" + "-" * 60)
        for sym, reason in wash_trading:
            print(f"  ✗  {sym:10s}  —  {reason}")

    # ── Regular whale ride candidates (from risk assessor) ─────────────
    if whale_rides:
        print(f"\n  🐋  WHALE RIDE CANDIDATES\n" + "-" * 60)
        for wr in whale_rides:
            sym        = wr["symbol"]
            price      = wr["price"]
            sl         = wr["stop_loss"]
            tp         = wr["take_profit"]
            crash      = wr["crash_reason"]
            hold       = wr["max_hold_hours"]
            cycles     = wr["known_cycles"]
            cyc_num    = wr["cycle_number"]
            scam_tag   = "  ⚠️ SERIAL SCAM" if wr["is_serial_scam"] else ""
            allies     = wr.get("allies", [])
            ally_str   = f" — same wallets as {'/'.join(allies)}" if allies else ""
            cycles_str = " → ".join(cycles) if cycles else "first recorded"

            ch24_wr = wr.get("change_24h", 0)
            ch7d_wr = wr.get("change_7d", 0)
            print(f"\n  🐋 {sym} {_pfmt(price)} — WHALE RIDE{scam_tag}")
            print(f"     24h: {ch24_wr:+.1f}%  |  7d: {ch7d_wr:+.1f}%")
            print(f"     Crash: {crash}")
            print(f"     Pattern: {cycles_str}")
            print(f"     Entry: {_pfmt(price)} | SL: {_pfmt(sl)} (-15%) | TP: {_pfmt(tp)} (+50%)")
            print(f"     Max hold: {hold}h | Cycle #{cyc_num}{ally_str}")
            print(f"     ⚠️ EXTREME RISK — manipulated token, max 5% of portfolio")

    # ── Whale Rider — volume anomaly detection (Telegram alert, no auto-trade) ──
    try:
        from src.agents.whale_rider import (
            detect_whale_rides       as _wr_detect,
            send_whale_ride_alerts   as _wr_alert,
            check_exit_signals       as _wr_exit,
            display_late_stage       as _wr_late,
            check_post_crash_bounces as _wr_crash,
            update_volume_history    as _wr_update_vol,
        )
        # Update volume history first (one entry per day per coin)
        _wr_vol_hist = _wr_update_vol(coins)
        _wr_candidates = _wr_detect(coins, risk_map, vol_history=_wr_vol_hist)
        if _wr_candidates:
            _wr_alert(_wr_candidates, fear_greed)
        _wr_rsi_map = {r["symbol"]: r.get("rsi") for r in results if r.get("rsi") is not None}
        _wr_exit(coins, rsi_map=_wr_rsi_map)
        _wr_late(coins)
        _wr_crash(coins, rsi_map=_wr_rsi_map)
    except Exception as _wr_e:
        print(f"  ⚠️  Whale rider module error: {_wr_e}")

    # ── Telegram summary ────────
    try:
        _send_telegram_valuable(top_longs, top_shorts, top_spots, all_whale_rides, fear_greed)
    except Exception as _tg_e:
        print(f"  ⚠️  Telegram valuable summary failed: {_tg_e}")

    # ── Auto-Log All Top Picks ──
    from src.utils.logger import log_recommendation, log_whale_ride
    
    # 1. Scanner Picks (Long/Short/Spot)
    for r in top_longs + top_shorts + top_spots:
        # Use +10% TP and -10% SL for all, as requested
        entry = r.get("price", 0)
        if not entry: continue
        rec = {
            "coin":        r["symbol"],
            "coin_id":     r["coin_id"],
            "entry_price": round(entry, 8),
            "stop_loss":   round(entry * 0.90, 8),
            "take_profit": round(entry * 1.10, 8),
            "timeframe":   "24h Window",
            "reasoning":   f"Top {r['recommended_order']} Pick. Score {r['score']}.",
            "recommended_order": r["recommended_order"],
        }
        log_recommendation(rec, fear_greed.get("value", 50))

    # 2. Valuable Whale Rides
    valuable_wr = [wr for wr in all_whale_rides if wr.get("cycle_number", 0) >= 1]
    for wr in valuable_wr:
        log_whale_ride(wr, fear_greed.get("value", 50))

    return top10, pump_coins, all_whale_rides, quality_count, _catalysts


def _print_pick(rank: int, r: dict, catalysts: dict) -> None:
    """Helper to print a pick's details."""
    supply_tag   = "  [⚠️ MED SUPPLY — HALF SIZE]" if r.get("supply_risk") == "MEDIUM" else ""
    hold_tag      = "  [📌 OPEN — HOLD]" if r.get("_already_open") else ""
    print(f"\n  {rank}. {r['symbol']} ({r['name']})  —  score: {r['score']} pts{supply_tag}{hold_tag}")
    print(f"     Price: ${r['price']:.4f}  |  24h: {r['change_24h']:+.1f}%  |  7d: {r['change_7d']:+.1f}%")
    print(f"     RSI {r['rsi']:.1f}  |  MACD: {r['macd']}  |  Trend: {r['trend']}")
    print(f"     Signals: {', '.join(r['reasons']) if r['reasons'] else 'none'}")
    cat = catalysts.get(r["symbol"].upper(), "No recent news found")
    print(f"     📰 News:  {cat}")


def _send_telegram_valuable(
    longs: list[dict],
    shorts: list[dict],
    spots: list[dict],
    whale_rides: list[dict],
    fear_greed: dict,
) -> None:
    """Send valuable LONG/SHORT/SPOT picks to Telegram."""
    from src.utils.telegram import send_telegram

    def _f(v):
        if v >= 1:    return f"${v:,.2f}"
        if v >= 0.01: return f"${v:.4f}"
        return f"${v:.8f}"

    fg_val   = (fear_greed or {}).get("value", 50)
    fg_label = (fear_greed or {}).get("label", "?")
    
    msg = f"<b>💎 MOST VALUABLE PICKS — F&amp;G {fg_val}/100</b>\n"
    
    if longs:
        msg += "\n🚀 <b>TOP LONGS:</b>\n"
        for i, r in enumerate(longs, 1):
            msg += f"  {i}. <b>{r['symbol']}</b> @ {_f(r['price'])} (Score {r['score']})\n"
    
    if shorts:
        msg += "\n📉 <b>TOP SHORTS:</b>\n"
        for i, r in enumerate(shorts, 1):
            msg += f"  {i}. <b>{r['symbol']}</b> @ {_f(r['price'])} (Score {r['score']})\n"

    if spots:
        msg += "\n💰 <b>TOP SPOTS:</b>\n"
        for i, r in enumerate(spots, 1):
            msg += f"  {i}. <b>{r['symbol']}</b> @ {_f(r['price'])} (Score {r['score']})\n"

    # Add best Whale Rides
    val_wr = [wr for wr in whale_rides if wr.get("cycle_number", 0) >= 2][:3]
    if val_wr:
        msg += "\n🐋 <b>WHALE RIDES (Proven):</b>\n"
        for wr in val_wr:
            msg += f"  🐋 <b>{wr['symbol']}</b> (Cycle #{wr['cycle_number']}) — {wr['crash_reason'][:50]}...\n"

    if not longs and not shorts and not spots and not val_wr:
        msg += "\n<i>No high-confidence opportunities found this cycle.</i>"

    send_telegram(msg)
