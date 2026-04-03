"""
CoinPaprika connector — upcoming coin events and community data.
No API key required for free endpoints.
"""
import time
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_BASE = "https://api.coinpaprika.com/v1"
_cache: dict = {}
_CACHE_TTL = 3600  # 1 hour


def _cached(key):
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return data
    return None


def _set_cache(key, data):
    _cache[key] = (time.time(), data)


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
