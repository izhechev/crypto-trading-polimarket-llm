"""
DeFiLlama connector — total DeFi TVL and per-protocol TVL.
No API key required.
"""
import time
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_BASE = "https://api.llama.fi"
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


# Map common coin symbols to DeFiLlama protocol slugs
_SYMBOL_TO_SLUG: dict[str, str] = {
    "UNI":    "uniswap",
    "AAVE":   "aave",
    "CRV":    "curve-dex",
    "MKR":    "makerdao",
    "COMP":   "compound-finance",
    "SNX":    "synthetix",
    "SUSHI":  "sushi",
    "BAL":    "balancer",
    "YFI":    "yearn-finance",
    "1INCH":  "1inch-network",
    "LDO":    "lido",
    "INJ":    "injective",
    "NEAR":   "ref-finance",
    "APT":    "pancakeswap-aptos",
    "SUI":    "cetus",
    "OP":     "velodrome",
    "ARB":    "gmx",
    "GRT":    "the-graph",
    "RPL":    "rocket-pool",
    "PENDLE": "pendle",
}


def fetch_total_tvl() -> float | None:
    """Return latest total DeFi TVL in USD. None on failure."""
    cached = _cached("total_tvl")
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{_BASE}/v2/historicalChainTvl")
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data and isinstance(data, list):
            tvl = data[-1].get("tvl")
            _set_cache("total_tvl", tvl)
            return tvl
        return None
    except Exception:
        return None


def fetch_top_protocols(limit: int = 10) -> list[dict]:
    """Return top DeFi protocols by TVL, sorted descending."""
    cached = _cached("top_protocols")
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{_BASE}/protocols")
        if resp.status_code != 200:
            _set_cache("top_protocols", [])
            return []
        protocols = resp.json()
        protocols.sort(key=lambda x: x.get("tvl", 0), reverse=True)
        result = [
            {
                "name":      p.get("name", ""),
                "symbol":    (p.get("symbol") or "").upper(),
                "tvl":       float(p["tvl"]) if isinstance(p.get("tvl"), (int, float)) else 0.0,
                "change_7d": p.get("change_7d"),
            }
            for p in protocols[:limit]
        ]
        _set_cache("top_protocols", result)
        return result
    except Exception:
        _set_cache("top_protocols", [])
        return []


def fetch_protocol_tvl(slug: str) -> dict:
    """Fetch TVL for a specific protocol by slug. Returns {} on failure."""
    key = f"protocol_{slug}"
    cached = _cached(key)
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=12) as client:
            resp = client.get(f"{_BASE}/protocol/{slug}")
        if resp.status_code != 200:
            _set_cache(key, {})
            return {}
        data = resp.json()
        # /protocol/{slug} returns tvl as a historical list [{date, totalLiquidityUSD}, ...]
        # The scalar current TVL lives in the last element.
        raw_tvl = data.get("tvl")
        if isinstance(raw_tvl, list) and raw_tvl:
            tvl = float(raw_tvl[-1].get("totalLiquidityUSD", 0))
        elif isinstance(raw_tvl, (int, float)):
            tvl = float(raw_tvl)
        else:
            tvl = 0.0
        result = {
            "name":      data.get("name", ""),
            "symbol":    (data.get("symbol") or "").upper(),
            "tvl":       tvl,
            "change_1d": data.get("change_1d"),
            "change_7d": data.get("change_7d"),
        }
        _set_cache(key, result)
        return result
    except Exception:
        _set_cache(key, {})
        return {}


def fetch_tvl_for_coins(symbols: list[str]) -> list[dict]:
    """Fetch TVL for coins that have a known DeFiLlama slug. Skips unknowns."""
    results = []
    for sym in symbols:
        slug = _SYMBOL_TO_SLUG.get(sym.upper())
        if slug:
            data = fetch_protocol_tvl(slug)
            if data and data.get("tvl"):
                results.append(data)
    return results


def format_for_prompt(total_tvl: float | None, top_protocols: list[dict], coin_tvls: list[dict]) -> str:
    """Format DeFiLlama data for LLM prompt."""
    lines = []
    if isinstance(total_tvl, (int, float)) and total_tvl > 0:
        lines.append(f"TOTAL DEFI TVL: ${total_tvl / 1e9:.2f}B")
    if coin_tvls:
        lines.append("PROTOCOL TVL (scanner coins):")
        for p in coin_tvls:
            tvl = p["tvl"]
            tvl_str = f"${tvl/1e9:.2f}B" if tvl >= 1e9 else f"${tvl/1e6:.0f}M"
            change_str = f" ({p['change_7d']:+.1f}% 7d)" if p.get("change_7d") is not None else ""
            lines.append(f"  {p['symbol'] or p['name']}: {tvl_str}{change_str}")
    elif top_protocols:
        lines.append("TOP DEFI PROTOCOLS:")
        for p in top_protocols[:5]:
            tvl = p["tvl"]
            tvl_str = f"${tvl/1e9:.2f}B" if tvl >= 1e9 else f"${tvl/1e6:.0f}M"
            change_str = f" ({p['change_7d']:+.1f}% 7d)" if p.get("change_7d") is not None else ""
            lines.append(f"  {p['symbol'] or p['name']}: {tvl_str}{change_str}")
    return "\n".join(lines)
