"""
Polymarket connector — crypto prediction market odds.
No API key required. Uses the public Gamma API.
"""
import json
import time
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_BASE = "https://gamma-api.polymarket.com"
_cache: dict = {}
_CACHE_TTL = 1800  # 30 min


def _cached(key):
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return data
    return None


def _set_cache(key, data):
    _cache[key] = (time.time(), data)


def _parse_probability(outcome_prices) -> float | None:
    """Parse first outcome probability from various formats Polymarket returns."""
    if outcome_prices is None:
        return None
    try:
        if isinstance(outcome_prices, list) and outcome_prices:
            return float(outcome_prices[0])
        if isinstance(outcome_prices, str):
            parsed = json.loads(outcome_prices)
            if parsed:
                return float(parsed[0])
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def fetch_crypto_markets(limit: int = 10) -> list[dict]:
    """
    Fetch open, high-volume crypto prediction markets from Polymarket.
    Returns list of {question, probability, volume_usd}.
    Returns [] on failure.
    """
    cached = _cached("poly_crypto")
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f"{_BASE}/markets",
                params={
                    "tag_slug":  "crypto",
                    "active":    "true",
                    "closed":    "false",
                    "limit":     limit,
                    "order":     "volume",
                    "ascending": "false",
                },
            )
        if resp.status_code != 200:
            _set_cache("poly_crypto", [])
            return []
        markets = resp.json() or []
        result = []
        for m in markets[:limit]:
            prob = _parse_probability(m.get("outcomePrices"))
            volume = 0.0
            raw_vol = m.get("volumeNum") or m.get("volume") or 0
            try:
                volume = float(raw_vol)
            except (ValueError, TypeError):
                pass
            result.append({
                "question":    m.get("question", ""),
                "probability": prob,
                "volume_usd":  volume,
            })
        _set_cache("poly_crypto", result)
        return result
    except Exception:
        _set_cache("poly_crypto", [])
        return []


def format_for_prompt(markets: list[dict]) -> str:
    """Format Polymarket data for LLM prompt."""
    visible = [m for m in markets if m.get("question")]
    if not visible:
        return ""
    lines = ["POLYMARKET PREDICTION ODDS:"]
    for m in visible[:8]:
        prob = m.get("probability")
        prob_str = f"{prob * 100:.0f}%" if prob is not None else "?"
        vol = m.get("volume_usd", 0)
        vol_str = f"${vol / 1000:.0f}k" if vol >= 1000 else f"${vol:.0f}"
        lines.append(f"  {m['question']} → {prob_str}  (vol: {vol_str})")
    return "\n".join(lines)
