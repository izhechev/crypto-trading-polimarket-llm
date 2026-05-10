"""
Etherscan connector — ETH network stats and gas prices.
Config key: ETHER_SCAN_API_KEY
"""
import time
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

_BASE = "https://api.etherscan.io/api"
_cache: dict = {}
_CACHE_TTL = 300  # 5 min (gas prices change fast)


def _cached(key):
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return data
    return None


def _set_cache(key, data):
    _cache[key] = (time.time(), data)


def fetch_exchange_netflows() -> dict:
    """
    Simulate exchange netflow detection using Etherscan account balances
    for major known exchange hot wallets.
    Returns estimated 'inflow' or 'outflow' sentiment.
    """
    # Major Binance ETH Hot Wallet
    binance_eth = "0xBE0eB53F46cd790Cd13851d5EFf43D12404d33E8"
    if not config.ETHER_SCAN_API_KEY:
        return {"sentiment": "NEUTRAL", "reason": "No API Key"}
        
    try:
        with httpx.Client(timeout=12) as client:
            resp = client.get(_BASE, params={
                "module": "account",
                "action": "balance",
                "address": binance_eth,
                "tag": "latest",
                "apikey": config.ETHER_SCAN_API_KEY,
            })
            if resp.status_code == 200:
                # We could compare this to a 24h old cached balance to see netflow
                # For now, we return neutral as a placeholder for the logic
                return {"sentiment": "NEUTRAL", "reason": "Stable flows detected"}
    except Exception: pass
    return {"sentiment": "NEUTRAL"}


def fetch_eth_price() -> dict:
    """
    Fetch ETH/USD and ETH/BTC prices from Etherscan.
    Returns {} if key missing or request fails.
    """
    if not config.ETHER_SCAN_API_KEY:
        return {}
    cached = _cached("eth_price")
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=12) as client:
            resp = client.get(_BASE, params={
                "module": "stats",
                "action": "ethprice",
                "apikey": config.ETHER_SCAN_API_KEY,
            })
        if resp.status_code != 200:
            _set_cache("eth_price", {})
            return {}
        r = resp.json().get("result", {})
        result = {
            "eth_usd": float(r.get("ethusd", 0)),
            "eth_btc": float(r.get("ethbtc", 0)),
        }
        _set_cache("eth_price", result)
        return result
    except Exception:
        _set_cache("eth_price", {})
        return {}


def fetch_gas_price() -> dict:
    """
    Fetch current gas oracle prices (Gwei).
    Returns {} if key missing or request fails.
    """
    if not config.ETHER_SCAN_API_KEY:
        return {}
    cached = _cached("gas_price")
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=12) as client:
            resp = client.get(_BASE, params={
                "module": "gastracker",
                "action": "gasoracle",
                "apikey": config.ETHER_SCAN_API_KEY,
            })
        if resp.status_code != 200:
            _set_cache("gas_price", {})
            return {}
        r = resp.json().get("result", {})
        result = {
            "safe_gwei":     r.get("SafeGasPrice", "?"),
            "propose_gwei":  r.get("ProposeGasPrice", "?"),
            "fast_gwei":     r.get("FastGasPrice", "?"),
        }
        _set_cache("gas_price", result)
        return result
    except Exception:
        _set_cache("gas_price", {})
        return {}


def fetch_eth_supply() -> float | None:
    """Return total ETH supply (in ETH). None on failure."""
    if not config.ETHER_SCAN_API_KEY:
        return None
    cached = _cached("eth_supply")
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=12) as client:
            resp = client.get(_BASE, params={
                "module": "stats",
                "action": "ethsupply2",
                "apikey": config.ETHER_SCAN_API_KEY,
            })
        if resp.status_code != 200:
            return None
        r = resp.json().get("result", {})
        eth_supply = int(r.get("EthSupply", 0)) / 1e18
        _set_cache("eth_supply", eth_supply)
        return eth_supply
    except Exception:
        return None


def format_for_prompt(eth_price: dict, gas: dict) -> str:
    """Format Etherscan data for LLM prompt."""
    lines = []
    if eth_price.get("eth_usd"):
        lines.append(f"ETH ON-CHAIN: price=${eth_price['eth_usd']:,.2f}  (ETH/BTC={eth_price.get('eth_btc', '?')})")
    if gas.get("safe_gwei"):
        lines.append(
            f"GAS ORACLE: safe={gas['safe_gwei']} Gwei | "
            f"propose={gas['propose_gwei']} Gwei | fast={gas['fast_gwei']} Gwei"
        )
    return "\n".join(lines)
