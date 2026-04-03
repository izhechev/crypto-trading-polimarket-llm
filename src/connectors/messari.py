"""
Messari connector — asset fundamentals (developer activity, ATH distance, real volume).
Config key: MESSARI_API_KEY (optional; free tier works without auth for basic metrics).
"""
import time
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

_BASE = "https://data.messari.io/api"
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


def _headers():
    h = {}
    if config.MESSARI_API_KEY:
        h["x-messari-api-key"] = config.MESSARI_API_KEY
    return h


def fetch_asset_metrics(symbol: str) -> dict:
    """
    Fetch key metrics for a coin: developer activity, ATH distance, real volume.
    Returns {} on failure.
    """
    key = f"messari_{symbol.lower()}"
    cached = _cached(key)
    if cached is not None:
        return cached
    try:
        url = f"{_BASE}/v1/assets/{symbol.lower()}/metrics"
        with httpx.Client(timeout=12) as client:
            resp = client.get(url, headers=_headers())
        if resp.status_code != 200:
            _set_cache(key, {})
            return {}
        data = resp.json().get("data", {})
        result = {
            "symbol":                 symbol.upper(),
            "developer_stars":        data.get("developer_activity", {}).get("stars"),
            "ath_usd":                data.get("all_time_high", {}).get("price"),
            "ath_breakeven_multiple": data.get("all_time_high", {}).get("breakeven_multiple"),
            "real_volume_24h":        data.get("market_data", {}).get("real_volume_last_24_hours"),
        }
        _set_cache(key, result)
        return result
    except Exception:
        _set_cache(key, {})
        return {}


def fetch_metrics_batch(symbols: list[str]) -> list[dict]:
    """Fetch metrics for multiple symbols. Skips empties."""
    return [m for sym in symbols if (m := fetch_asset_metrics(sym))]


def format_for_prompt(metrics: list[dict]) -> str:
    """Format Messari data for LLM prompt."""
    if not metrics:
        return ""
    lines = ["MESSARI FUNDAMENTALS:"]
    for m in metrics:
        sym = m.get("symbol", "?")
        parts = []
        stars = m.get("developer_stars")
        if stars is not None:
            parts.append(f"dev_stars={stars}")
        mult = m.get("ath_breakeven_multiple")
        if mult is not None:
            parts.append(f"from_ATH={mult:.2f}x")
        real_vol = m.get("real_volume_24h")
        if real_vol:
            parts.append(f"real_vol_24h=${real_vol/1e6:.1f}M")
        if parts:
            lines.append(f"  {sym}: {', '.join(parts)}")
    return "\n".join(lines) if len(lines) > 1 else ""
