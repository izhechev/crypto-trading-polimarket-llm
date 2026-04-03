"""
CoinMarketCap connector — trending coins and gainers/losers.
Config key: COIN_MARKET_CAP_API_KEY
"""
import time
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

_BASE = "https://pro-api.coinmarketcap.com"
_cache: dict = {}
_CACHE_TTL = 600  # 10 min


def _cached(key):
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return data
    return None


def _set_cache(key, data):
    _cache[key] = (time.time(), data)


def _headers():
    return {
        "X-CMC_PRO_API_KEY": config.COIN_MARKET_CAP_API_KEY,
        "Accept": "application/json",
    }


def fetch_trending(limit: int = 10) -> list[str]:
    """Return list of trending coin symbols (uppercase). [] if key missing."""
    if not config.COIN_MARKET_CAP_API_KEY:
        return []
    cached = _cached("cmc_trending")
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=12) as client:
            resp = client.get(
                f"{_BASE}/v1/cryptocurrency/trending/latest",
                headers=_headers(),
                params={"limit": limit, "convert": "USD"},
            )
        if resp.status_code != 200:
            _set_cache("cmc_trending", [])
            return []
        data = resp.json().get("data", {})
        # API returns {"trending": [...]} inside data
        coins = data.get("trending", data) if isinstance(data, dict) else data
        result = [c["symbol"].upper() for c in (coins or [])[:limit] if c.get("symbol")]
        _set_cache("cmc_trending", result)
        return result
    except Exception:
        _set_cache("cmc_trending", [])
        return []


def fetch_gainers_losers(limit: int = 5) -> dict:
    """Return {gainers: [{symbol, change_24h}], losers: [...]}. Empty if key missing."""
    if not config.COIN_MARKET_CAP_API_KEY:
        return {"gainers": [], "losers": []}
    cached = _cached("cmc_gl")
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=12) as client:
            resp = client.get(
                f"{_BASE}/v1/cryptocurrency/trending/gainers-losers",
                headers=_headers(),
                params={"limit": limit, "convert": "USD", "time_period": "24h"},
            )
        if resp.status_code != 200:
            empty = {"gainers": [], "losers": []}
            _set_cache("cmc_gl", empty)
            return empty
        data = resp.json().get("data", {})

        def _extract(items):
            return [
                {
                    "symbol": c["symbol"].upper(),
                    "change_24h": c.get("quote", {}).get("USD", {}).get("percent_change_24h", 0),
                }
                for c in (items or [])[:limit]
                if c.get("symbol")
            ]

        result = {
            "gainers": _extract(data.get("gainers", [])),
            "losers":  _extract(data.get("losers", [])),
        }
        _set_cache("cmc_gl", result)
        return result
    except Exception:
        empty = {"gainers": [], "losers": []}
        _set_cache("cmc_gl", empty)
        return empty


def format_for_prompt(trending: list[str], gainers_losers: dict) -> str:
    """Format CMC data for LLM prompt."""
    lines = []
    if trending:
        lines.append(f"CMC TRENDING: {', '.join(trending)}")
    gainers = gainers_losers.get("gainers", [])
    losers  = gainers_losers.get("losers", [])
    if gainers:
        g_str = ", ".join(f"{g['symbol']} {g['change_24h']:+.1f}%" for g in gainers)
        lines.append(f"CMC TOP GAINERS (24h): {g_str}")
    if losers:
        l_str = ", ".join(f"{l['symbol']} {l['change_24h']:+.1f}%" for l in losers)
        lines.append(f"CMC TOP LOSERS (24h): {l_str}")
    return "\n".join(lines)
