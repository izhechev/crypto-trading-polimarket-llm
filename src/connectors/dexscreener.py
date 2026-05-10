"""DexScreener API connector for real-time liquidity and pair data."""
import httpx
import time

_cache: dict[str, tuple[float, any]] = {}
CACHE_TTL = 300  # 5 minutes

def _get_cached(key: str):
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None

def _set_cache(key: str, data):
    _cache[key] = (time.time(), data)

def fetch_dex_liquidity(symbol: str, contract_address: str | None = None) -> float:
    """
    Fetch the highest USD liquidity found for a token across all DEXs.
    Returns USD liquidity as float, or 0 on failure.
    """
    search_query = contract_address if contract_address else symbol.upper()
    if not search_query: return 0.0

    cache_key = f"dex_liq_{search_query}"
    cached = _get_cached(cache_key)
    if cached is not None: return cached

    try:
        url = f"https://api.dexscreener.com/latest/dex/search?q={search_query}"
        with httpx.Client(timeout=10) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                pairs = resp.json().get("pairs", [])
                if not pairs:
                    _set_cache(cache_key, 0.0)
                    return 0.0
                
                # If we have a contract address, filter for exact matches to be safe
                if contract_address:
                    pairs = [p for p in pairs if p.get("baseToken", {}).get("address", "").lower() == contract_address.lower()]
                
                if not pairs:
                    _set_cache(cache_key, 0.0)
                    return 0.0

                # Find the pair with the highest liquidity
                max_liq = 0.0
                for p in pairs:
                    liq = p.get("liquidity", {}).get("usd", 0)
                    if liq > max_liq:
                        max_liq = liq
                
                _set_cache(cache_key, max_liq)
                return max_liq
    except Exception:
        pass
    
    return 0.0
