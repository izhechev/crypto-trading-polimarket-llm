"""GoPlus Security API connector for token contract audits."""
import httpx
import time

_cache: dict[str, tuple[float, any]] = {}
CACHE_TTL = 3600  # 1 hour

def _get_cached(key: str):
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None

def _set_cache(key: str, data):
    _cache[key] = (time.time(), data)

# Map common chain names/IDs to GoPlus chain IDs
_CHAIN_MAP = {
    "ethereum": "1",
    "binance-smart-chain": "56",
    "polygon-pos": "137",
    "solana": "solana",
    "arbitrum-one": "42161",
    "optimistic-ethereum": "10",
    "base": "8453",
    "avalanche": "43114",
}

def fetch_token_security(chain_id: str, contract_address: str) -> dict | None:
    """
    Perform a security audit for a token contract.
    Returns dict with security flags or None on failure.
    """
    # Normalize chain ID
    goplus_chain = _CHAIN_MAP.get(chain_id.lower(), chain_id)
    
    cache_key = f"goplus_{goplus_chain}_{contract_address}"
    cached = _get_cached(cache_key)
    if cached is not None: return cached

    try:
        url = f"https://api.gopluslabs.io/api/v1/token_security/{goplus_chain}?contract_addresses={contract_address}"
        with httpx.Client(timeout=15) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json().get("result", {}).get(contract_address.lower(), {})
                if not data:
                    # Try case-sensitive if lowercase fails
                    data = resp.json().get("result", {}).get(contract_address, {})
                
                if data:
                    _set_cache(cache_key, data)
                    return data
    except Exception:
        pass
    
    return None

def is_honeypot(security_data: dict) -> bool:
    """Check if the security data indicates a honeypot."""
    if not security_data: return False
    # is_honeypot: "1" = yes, "0" = no
    return security_data.get("is_honeypot") == "1"

def get_total_tax(security_data: dict) -> float:
    """Return the combined buy and sell tax as a percentage."""
    if not security_data: return 0.0
    try:
        buy_tax = float(security_data.get("buy_tax", 0)) * 100
        sell_tax = float(security_data.get("sell_tax", 0)) * 100
        return buy_tax + sell_tax
    except (ValueError, TypeError):
        return 0.0
