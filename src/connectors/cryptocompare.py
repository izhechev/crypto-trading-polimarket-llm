"""
CryptoCompare news connector — standalone module.
Config key: CRYPTOCOMPARE_API_KEY (also accepts CRYPTO_COMPARE_API_KEY).
"""
import time
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

_BASE = "https://min-api.cryptocompare.com"
_cache: dict = {}
_CACHE_TTL = 900  # 15 min


def _cached(key):
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return data
    return None


def _set_cache(key, data):
    _cache[key] = (time.time(), data)


def _headers():
    if not config.CRYPTOCOMPARE_API_KEY:
        return {}
    return {"authorization": f"Apikey {config.CRYPTOCOMPARE_API_KEY}"}


def fetch_news_for_coin(symbol: str, name: str = "", limit: int = 5) -> list[dict]:
    """
    Fetch recent news for a coin by symbol.
    Returns [] on failure or missing API key.
    """
    if not config.CRYPTOCOMPARE_API_KEY:
        return []
    key = f"cc_{symbol.upper()}"
    cached = _cached(key)
    if cached is not None:
        return cached

    categories = f"{symbol.upper()},{name}" if name else symbol.upper()
    try:
        params = {"categories": categories, "excludeCategories": "Sponsored", "lang": "EN"}
        with httpx.Client(timeout=12) as client:
            resp = client.get(f"{_BASE}/data/v2/news/", params=params, headers=_headers())
        if resp.status_code != 200:
            _set_cache(key, [])
            return []
        items = resp.json().get("Data", [])
        result = [
            {
                "title":  item.get("title", ""),
                "source": item.get("source_info", {}).get("name", item.get("source", "")),
                "sentiment": "neutral",
            }
            for item in items[:limit]
        ]
        _set_cache(key, result)
        return result
    except Exception:
        _set_cache(key, [])
        return []


def fetch_general_news(limit: int = 5) -> list[dict]:
    """Fetch top general crypto news (not coin-specific)."""
    if not config.CRYPTOCOMPARE_API_KEY:
        return []
    cached = _cached("cc_general")
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=12) as client:
            resp = client.get(
                f"{_BASE}/data/v2/news/",
                params={"lang": "EN", "sortOrder": "popular"},
                headers=_headers(),
            )
        if resp.status_code != 200:
            _set_cache("cc_general", [])
            return []
        items = resp.json().get("Data", [])[:limit]
        result = [
            {
                "title":  item.get("title", ""),
                "source": item.get("source_info", {}).get("name", item.get("source", "")),
            }
            for item in items
        ]
        _set_cache("cc_general", result)
        return result
    except Exception:
        _set_cache("cc_general", [])
        return []
