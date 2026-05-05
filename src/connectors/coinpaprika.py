"""
CoinPaprika connector — free API, no key required.
Provides: coin events, ticker data (top 1000), OHLCV for TA.
Base URL: https://api.coinpaprika.com/v1
Rate limit: 10 req/sec (enforced by _rate_limit()).
"""
import time
import httpx
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_BASE = "https://api.coinpaprika.com/v1"

# Two separate caches with different TTLs
_cache: dict = {}        # events cache — 1 hour
_scan_cache: dict = {}   # scanner/OHLCV cache — 4 min

_CACHE_TTL          = 3600   # 1 hour  (events)
_SCAN_CACHE_TTL     = 240    # 4 min   (OHLCV — needs to be fresh for TA)
_TICKER_CACHE_TTL   = 21600  # 6 hours (full coin list — structure rarely changes)

# CoinGecko ID map cache — built dynamically from top-500 CG coins (24h TTL)
_cg_id_map: dict[str, str] = {}
_cg_id_map_ts: float = 0.0
_CG_ID_MAP_TTL = 86400  # 24 hours

# ATH date map — symbol → ISO date string of all-time high (e.g. "2021-11-10")
# Populated alongside _cg_id_map from the same CoinGecko /coins/markets fetch
_ath_date_map: dict[str, str] = {}

# Sliding-window rate limiter — max 10 req/sec
_rl_window: list[float] = []


def _rate_limit() -> None:
    """Block until we are within the 10 req/sec limit."""
    now = time.time()
    _rl_window[:] = [t for t in _rl_window if now - t < 1.0]
    if len(_rl_window) >= 10:
        sleep_for = 1.0 - (now - _rl_window[0]) + 0.01
        if sleep_for > 0:
            time.sleep(sleep_for)
        _rl_window[:] = [t for t in _rl_window if time.time() - t < 1.0]
    _rl_window.append(time.time())


def _cached(key):
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return data
    return None


def _set_cache(key, data):
    _cache[key] = (time.time(), data)


def _cached_scan(key, ttl: int | None = None):
    if key in _scan_cache:
        ts, data = _scan_cache[key]
        if time.time() - ts < (ttl or _SCAN_CACHE_TTL):
            return data
    return None


def _set_scan_cache(key, data):
    _scan_cache[key] = (time.time(), data)


# CoinPaprika uses "<ticker>-<name>" IDs
_SYMBOL_TO_ID: dict[str, str] = {
    "BTC":    "btc-bitcoin",
    "ETH":    "eth-ethereum",
    "SOL":    "sol-solana",
    "ADA":    "ada-cardano",
    "DOT":    "dot-polkadot",
    "LINK":   "link-chainlink",
    "AVAX":   "avax-avalanche",
    "ATOM":   "atom-cosmos",
    "XRP":    "xrp-xrp",
    "LTC":    "ltc-litecoin",
    "NEAR":   "near-near-protocol",
    "INJ":    "inj-injective",
    "RENDER": "rndr-render-token",
    "RNDR":   "rndr-render-token",
    "UNI":    "uni-uniswap",
    "AAVE":   "aave-aave",
    "CRV":    "crv-curve-dao-token",
    "MKR":    "mkr-maker",
    "GRT":    "grt-the-graph",
    "FIL":    "fil-filecoin",
    "APT":    "apt-aptos",
    "SUI":    "sui-sui",
    "OP":     "op-optimism",
    "ARB":    "arb-arbitrum",
    "TIA":    "tia-celestia",
    "SEI":    "sei-sei",
    "DOGE":   "doge-dogecoin",
    "SHIB":   "shib-shiba-inu",
    "PEPE":   "pepe-pepe",
    "WIF":    "wif-dogwifhat",
    "BONK":   "bonk-bonk",
    "FET":    "fet-fetch-ai",
    "PENDLE": "pendle-pendle",
    "JUP":    "jup-jupiter",
    "PYTH":   "pyth-pyth-network",
}

# Symbol → CoinGecko ID mapping (used as OHLCV fallback when CP historical API is unavailable)
SYMBOL_TO_CG_ID: dict[str, str] = {
    "BTC":    "bitcoin",
    "ETH":    "ethereum",
    "BNB":    "binancecoin",
    "SOL":    "solana",
    "XRP":    "ripple",
    "ADA":    "cardano",
    "DOGE":   "dogecoin",
    "TON":    "the-open-network",
    "TRX":    "tron",
    "AVAX":   "avalanche-2",
    "SHIB":   "shiba-inu",
    "LINK":   "chainlink",
    "DOT":    "polkadot",
    "BCH":    "bitcoin-cash",
    "LTC":    "litecoin",
    "NEAR":   "near",
    "UNI":    "uniswap",
    "ICP":    "internet-computer",
    "ETC":    "ethereum-classic",
    "APT":    "aptos",
    "SUI":    "sui",
    "AAVE":   "aave",
    "MKR":    "maker",
    "CRV":    "curve-dao-token",
    "SNX":    "havven",
    "COMP":   "compound-governance-token",
    "YFI":    "yearn-finance",
    "GRT":    "the-graph",
    "LDO":    "lido-dao",
    "RPL":    "rocket-pool",
    "PENDLE": "pendle",
    "GMX":    "gmx",
    "DYDX":   "dydx-chain",
    "SSV":    "ssv-network",
    "MATIC":  "matic-network",
    "POL":    "matic-network",
    "OP":     "optimism",
    "ARB":    "arbitrum",
    "IMX":    "immutable-x",
    "STRK":   "starknet",
    "ZK":     "zksync",
    "MANTA":  "manta-network",
    "FET":    "fetch-ai",
    "AGIX":   "singularitynet",
    "OCEAN":  "ocean-protocol",
    "RNDR":   "render-token",
    "RENDER": "render-token",
    "TAO":    "bittensor",
    "WLD":    "worldcoin-wld",
    "PEPE":   "pepe",
    "FLOKI":  "floki",
    "WIF":    "dogwifhat",
    "BONK":   "bonk",
    "ATOM":   "cosmos",
    "INJ":    "injective-protocol",
    "TIA":    "celestia",
    "SEI":    "sei-network",
    "DYM":    "dymension",
    "KAVA":   "kava",
    "OSMO":   "osmosis",
    "AXL":    "axelar",
    "XMR":    "monero",
    "XLM":    "stellar",
    "ALGO":   "algorand",
    "FTM":    "fantom",
    "VET":    "vechain",
    "HBAR":   "hedera-hashgraph",
    "THETA":  "theta-token",
    "FIL":    "filecoin",
    "AR":     "arweave",
    "STX":    "blockstack",
    "CFX":    "conflux-token",
    "RUNE":   "thorchain",
    "ZEC":    "zcash",
    "DASH":   "dash",
    "XTZ":    "tezos",
    "IOTA":   "iota",
    "ONE":    "harmony",
    "ZIL":    "zilliqa",
    "CELO":   "celo",
    "FLOW":   "flow",
    "ROSE":   "oasis-network",
    "BAND":   "band-protocol",
    "GNO":    "gnosis",
    "BAT":    "basic-attention-token",
    "MANA":   "decentraland",
    "SAND":   "the-sandbox",
    "AXS":    "axie-infinity",
    "ENJ":    "enjincoin",
    "GALA":   "gala",
    "CHZ":    "chiliz",
    "RONIN":  "ronin",
    "KAS":    "kaspa",
    "CORE":   "coredaoorg",
    "OM":     "mantra-dao",
    "BEAM":   "beam-2",
    "ENS":    "ethereum-name-service",
    "BLUR":   "blur",
    "ORDI":   "ordi",
    "JUP":    "jupiter-exchange-solana",
    "PYTH":   "pyth-network",
    "EIGEN":  "eigenlayer",
    "ENA":    "ethena",
    "W":      "wormhole",
    "HYPE":   "hyperliquid",
    "S":      "sonic-3",
    "VIRTUAL":"virtual-protocol",
    "ORCA":   "orca",
    "BRETT":  "brett",
    "FARTCOIN":"fartcoin",
    "AERO":   "aerodrome-finance",
    "POPCAT": "popcat",
    "SAFE":   "safe-global",
    "IP":     "story-protocol",
    "RAY":    "raydium",
    "TRUMP":  "official-trump",
    "PENGU":  "pudgy-penguins",
    "ZRO":    "layerzero",
    "JTO":    "jito-governance-token",
    "BONK":   "bonk",
    "RUNE":   "thorchain",
    "RSR":    "reserve-rights-token",
    "LUNC":   "terra-luna",
    "WLD":    "worldcoin-wld",
    "INJ":    "injective-protocol",
    "OP":     "optimism",
    "FIL":    "filecoin",
    "LDO":    "lido-dao",
    "PEPE":   "pepe",
    "SAND":   "the-sandbox",
    "GALA":   "gala",
    "DGB":    "digibyte",
    "ZEN":    "horizen",
    "RED":    "redstone",
    "AXL":    "axelar",
    "LINEA":  "linea",
    "CYS":    "cysic",
    # Oracle / infrastructure
    "API3":   "api3",
    "BAND":   "band-protocol",
    "TRB":    "tellor",
    "UMA":    "uma",
    "DIA":    "dia-data",
    # DeFi
    "SUSHI":  "sushi",
    "1INCH":  "1inch",
    "CAKE":   "pancakeswap-token",
    "BAL":    "balancer",
    "CVX":    "convex-finance",
    "FXS":    "frax-share",
    "SPELL":  "spell-token",
    "ICX":    "icon",
    "KSM":    "kusama",
    "SCRT":   "secret",
    "EGLD":   "elrond-erd-2",
    "CKB":    "nervos-network",
    "WAVES":  "waves",
    "NEO":    "neo",
    "EOS":    "eos",
    "XDC":    "xdce-crowd-sale",
    "IOST":   "iostoken",
    "XNO":    "nano",
    "QTUM":   "qtum",
    "ZKP":    "panther-protocol",
    "PRL":    "oyster-pearl",
    "SIREN":  "siren",
    "MINA":   "mina-protocol",
    "GLMR":   "moonbeam",
    "MOVR":   "moonriver",
    "ANKR":   "ankr",
    "CLV":    "clover-finance",
    "LOKA":   "league-of-kingdoms",
    "DUSK":   "dusk-network",
    "ALICE":  "my-neighbor-alice",
    "AUCTION":"bounce-token",
    "POLS":   "polkastarter",
    "TORN":   "tornado-cash",
    "INDEX":  "index-cooperative",
    "BADGER": "badger-dao",
    "ALPHA":  "alpha-finance",
    "PERP":   "perpetual-protocol",
    "DODO":   "dodo",
    "MDX":    "mdex",
    "BAKE":   "bakerytoken",
    "XVS":    "venus",
    "TWT":    "trust-wallet-token",
    "CHESS":  "tranchess",
    "AUTO":   "auto",
    "QNT":    "quant-network",
    "CTSI":   "cartesi",
    "NMR":    "numeraire",
    "MLN":    "melon",
    "PROM":   "prometeus",
    "LINA":   "linear",
    "BEL":    "bella-protocol",
    "WING":   "wing-finance",
    "DEGO":   "dego-finance",
    "SFP":    "safepal",
    "XVG":    "verge",
    "REN":    "republic-protocol",
    "STORJ":  "storj",
    "OXT":    "orchid-protocol",
    "NKN":    "nkn",
    "COTI":   "coti",
    "KEEP":   "keep-network",
    "NU":     "nucypher",
    "RLC":    "iexec-rlc",
    "OGN":    "origin-protocol",
    "LRC":    "loopring",
    "STMX":   "storm",
    "NULS":   "nuls",
    "WAN":    "wanchain",
    "HIVE":   "hive",
    "STEEM":  "steem",
    "LSK":    "lisk",
    "ARK":    "ark",
    "MBOX":   "mobox",
    # Meme / trending
    "BOME":   "book-of-meme",
    "MOODENG":"moo-deng",
    "NEIRO":  "neiro",
    "SNEK":   "snek",
    "APE":    "apecoin",
    # DePIN / infrastructure
    "GRASS":  "grass",
    "AKT":    "akash-network",
    "AITECH": "solidus-ai-tech",
    # DeFi / exchange
    "AEVO":   "aevo",
    "STG":    "stargate-finance",
    "ALT":    "altlayer",
    "CFG":    "centrifuge",
    "AUDIO":  "audius",
    # Gaming / NFT
    "PIXEL":  "pixels",
    # AI
    "AIXBT":  "aixbt-by-virtuals",
    "SKYAI":  "skyai",
    # RWA / new chains
    "PLUME":  "plume",
    # Bio / science
    "BIO":    "bio-protocol",
    # Other
    "PNUT":   "peanut-the-squirrel",
}


def fetch_coin_events(symbol: str, limit: int = 3) -> list[dict]:
    """
    Fetch upcoming/recent events for a coin.
    Returns [] on failure or unknown symbol.
    """
    coin_id = _SYMBOL_TO_ID.get(symbol.upper())
    if not coin_id:
        return []
    key = f"cp_events_{symbol.upper()}"
    cached = _cached(key)
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=12) as client:
            resp = client.get(f"{_BASE}/coins/{coin_id}/events")
        if resp.status_code != 200:
            _set_cache(key, [])
            return []
        events = resp.json() or []
        result = [
            {
                "date":        e.get("date", ""),
                "name":        e.get("name", ""),
                "description": (e.get("description") or "")[:80],
            }
            for e in events[:limit]
        ]
        _set_cache(key, result)
        return result
    except Exception:
        _set_cache(key, [])
        return []


def fetch_events_for_coins(symbols: list[str]) -> dict[str, list[dict]]:
    """Fetch events for multiple coins. Returns {SYMBOL: [events]}."""
    result = {}
    for sym in symbols:
        events = fetch_coin_events(sym)
        if events:
            result[sym.upper()] = events
    return result


def format_for_prompt(events_by_coin: dict[str, list[dict]]) -> str:
    """Format CoinPaprika events for LLM prompt."""
    if not events_by_coin:
        return ""
    lines = ["UPCOMING EVENTS (CoinPaprika):"]
    for sym, events in events_by_coin.items():
        for e in events:
            lines.append(f"  {sym}: [{e['date']}] {e['name']}")
    return "\n".join(lines) if len(lines) > 1 else ""


# ── Scanner data (tickers + OHLCV) ────────────────────────────────────────────

def _build_cg_id_map() -> dict[str, str]:
    """
    Build symbol→CoinGecko-ID map from top-500 CG coins (market-cap sorted).
    Cached for 24 hours. Static SYMBOL_TO_CG_ID overrides dynamic for correctness.
    """
    global _cg_id_map, _cg_id_map_ts
    if _cg_id_map and time.time() - _cg_id_map_ts < _CG_ID_MAP_TTL:
        return _cg_id_map
    merged: dict[str, str] = {}
    try:
        ath_dates: dict[str, str] = {}
        for page in (2, 1):  # fetch page 2 first so page 1 (higher mcap) wins on symbol conflict
            with httpx.Client(timeout=20) as client:
                resp = client.get(
                    "https://pro-api.coingecko.com/api/v3/coins/markets",
                    params={"vs_currency": "usd", "order": "market_cap_desc",
                            "per_page": 250, "page": page},
                )
            if resp.status_code == 200:
                for c in resp.json():
                    sym = (c.get("symbol") or "").upper()
                    if sym:
                        merged[sym] = c["id"]
                        if c.get("ath_date"):
                            ath_dates[sym] = c["ath_date"][:10]  # "2021-11-10T..."→ "2021-11-10"
        _ath_date_map.update(ath_dates)
    except Exception:
        pass
    merged.update(SYMBOL_TO_CG_ID)  # static curated map wins over dynamic
    _cg_id_map = merged
    _cg_id_map_ts = time.time()
    return _cg_id_map


def get_ath_date_map() -> dict[str, str]:
    """Return symbol → ATH date (ISO date string). Populated by _build_cg_id_map."""
    return _ath_date_map


def fetch_tickers_for_scanner(limit: int = 3000) -> list[dict]:
    """
    Fetch top `limit` coins from CoinPaprika /tickers.
    Returns list of dicts with CoinGecko-compatible field names so the scanner
    can use them without modification. Coins are sorted by market cap descending.
    Each dict includes `_cp_id` (CoinPaprika ID) and `_from_cp=True` flags.
    Cached for 4 minutes. The full sorted list is cached once; limit only slices on return.
    """
    _FULL_KEY = "cp_scan_tickers_full"
    full = _cached_scan(_FULL_KEY, ttl=_TICKER_CACHE_TTL)

    if full is None:
        _rate_limit()
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{_BASE}/tickers", params={"quotes": "USD"})
            resp.raise_for_status()
            raw: list[dict] = resp.json()

        raw.sort(
            key=lambda c: (c.get("quotes", {}).get("USD", {}).get("market_cap") or 0),
            reverse=True,
        )

        full = []
        for coin in raw:
            usd       = coin.get("quotes", {}).get("USD", {})
            price     = usd.get("price") or 0
            mcap      = usd.get("market_cap") or 0
            vol       = usd.get("volume_24h") or 0
            ch24      = usd.get("percent_change_24h") or 0
            ch7d      = usd.get("percent_change_7d") or 0
            ath_price = usd.get("ath_price") or 0
            ath_pct   = usd.get("percent_from_price_ath") or 0
            circ      = coin.get("circulating_supply") or 0
            total     = coin.get("total_supply") or 0

            _sym = (coin.get("symbol") or "").upper()
            full.append({
                "id":             coin["id"],
                "_cp_id":         coin["id"],
                "_cg_id":         SYMBOL_TO_CG_ID.get(_sym, ""),
                "_from_cp":       True,
                "symbol":         _sym,
                "name":           coin.get("name", ""),
                "current_price":  price,
                "market_cap":     mcap,
                "total_volume":   vol,
                "price_change_percentage_24h":              ch24,
                "price_change_percentage_7d_in_currency":   ch7d,
                "price_change_percentage_14d_in_currency":  None,
                "circulating_supply":    circ,
                "total_supply":          total,
                "ath":                   ath_price,
                "ath_change_percentage": ath_pct,
            })

        _set_scan_cache(_FULL_KEY, full)

    return full[:limit]


def fetch_ohlcv(cp_coin_id: str, days: int = 30) -> list[dict]:
    """
    Fetch daily OHLCV candles from CoinPaprika.
    Returns same format as coingecko.fetch_ohlcv:
      [{"timestamp": datetime, "open": float, "high": float, "low": float, "close": float}]
    Cached for 4 minutes.
    """
    key = f"cp_ohlcv_{cp_coin_id}_{days}"
    cached = _cached_scan(key)
    if cached is not None:
        return cached

    end_dt   = datetime.utcnow()
    start_dt = end_dt - timedelta(days=days + 1)

    try:
        _rate_limit()
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{_BASE}/coins/{cp_coin_id}/ohlcv/historical",
                params={
                    "start": start_dt.strftime("%Y-%m-%d"),
                    "end":   end_dt.strftime("%Y-%m-%d"),
                    "quote": "usd",
                },
            )
            resp.raise_for_status()
            raw: list[dict] = resp.json()
    except Exception:
        # Historical OHLCV requires a paid plan on CoinPaprika free tier —
        # return [] so the caller can fall back to CoinGecko.
        return []

    result = []
    for candle in raw:
        time_open = candle.get("time_open", "")
        try:
            ts = datetime.fromisoformat(time_open.rstrip("Z"))
        except Exception:
            continue
        result.append({
            "timestamp": ts,
            "open":  candle.get("open")  or 0,
            "high":  candle.get("high")  or 0,
            "low":   candle.get("low")   or 0,
            "close": candle.get("close") or 0,
        })

    _set_scan_cache(key, result)
    return result
