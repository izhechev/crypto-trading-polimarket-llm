"""CoinGecko API connector with rate limiting and caching."""
import httpx
import time
from datetime import datetime
from tenacity import retry, wait_exponential, stop_after_attempt
from src.models.crypto import CryptoPrice

# Simple in-memory cache — 60 min TTL matches the hourly scan cycle.
# Whale check runs every 15 min but fetches its own coin list; these
# caches cover fetch_prices / fetch_ohlcv / fetch_fear_greed calls.
_cache: dict[str, tuple[float, any]] = {}
CACHE_TTL = 3600  # 60 minutes

# ── CoinGecko call counter (real HTTP requests only, cache hits excluded) ──
_CG_CALLS: int = 0


def _cg_get(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    """Thin wrapper around client.get that increments the call counter."""
    global _CG_CALLS
    _CG_CALLS += 1
    return client.get(url, **kwargs)


def get_cg_call_count() -> int:
    return _CG_CALLS


def reset_cg_call_count() -> None:
    global _CG_CALLS
    _CG_CALLS = 0


def _get_cached(key: str):
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None


def _set_cache(key: str, data):
    _cache[key] = (time.time(), data)


def _headers() -> dict:
    try:
        import config
        if config.COINGECKO_API_KEY:
            return {"x-cg-demo-api-key": config.COINGECKO_API_KEY}
    except Exception:
        pass
    return {}


def _fetch_eur_prices(coin_ids: list[str]) -> dict[str, float]:
    """
    Lightweight /simple/price call for EUR prices only.
    Returns {coin_id: eur_price}. Falls back to empty dict on failure.
    """
    cache_key = f"eur_simple_{'_'.join(sorted(coin_ids))}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": ",".join(coin_ids), "vs_currencies": "eur"}
        with httpx.Client(timeout=15) as client:
            resp = _cg_get(client, url, params=params, headers=_headers())
            resp.raise_for_status()
            raw = resp.json()
        result = {cid: data.get("eur", 0.0) for cid, data in raw.items()}
        _set_cache(cache_key, result)
        return result
    except Exception:
        return {}


@retry(wait=wait_exponential(min=2, max=30), stop=stop_after_attempt(3))
def fetch_prices(coin_ids: list[str]) -> list[CryptoPrice]:
    """Fetch USD market data + real EUR prices for multiple coins."""
    cache_key = f"prices_usd_{'_'.join(sorted(coin_ids))}"
    cached = _get_cached(cache_key)
    if cached:
        return cached

    ids_str = ",".join(coin_ids)
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "ids": ids_str,
        "order": "market_cap_desc",
        "sparkline": "false",
        "price_change_percentage": "24h,7d,30d",
    }

    with httpx.Client(timeout=30) as client:
        resp = _cg_get(client, url, params=params, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    # Fetch real EUR prices via the lightweight /simple/price endpoint
    eur_map = _fetch_eur_prices(coin_ids)

    prices = []
    for coin in data:
        cid = coin["id"]
        usd = coin.get("current_price", 0) or 0
        eur = eur_map.get(cid) or usd * 0.92  # fallback estimate
        prices.append(CryptoPrice(
            coin_id=cid,
            symbol=coin["symbol"].upper(),
            name=coin["name"],
            price_usd=usd,
            price_eur=eur,
            market_cap=coin.get("market_cap", 0) or 0,
            volume_24h=coin.get("total_volume", 0) or 0,
            change_24h=coin.get("price_change_percentage_24h", 0) or 0,
            change_7d=coin.get("price_change_percentage_7d_in_currency", 0) or 0,
            change_30d=coin.get("price_change_percentage_30d_in_currency"),
            ath=coin.get("ath"),
            ath_change_pct=coin.get("ath_change_percentage"),
        ))

    _set_cache(cache_key, prices)
    return prices


@retry(wait=wait_exponential(min=2, max=30), stop=stop_after_attempt(3))
def fetch_ohlcv(coin_id: str, days: int = 30) -> list[dict]:
    """Fetch OHLCV data for technical analysis."""
    cache_key = f"ohlcv_{coin_id}_{days}"
    cached = _get_cached(cache_key)
    if cached:
        return cached

    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": str(days)}

    with httpx.Client(timeout=30) as client:
        resp = _cg_get(client, url, params=params, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    ohlcv = []
    for candle in data:
        ohlcv.append({
            "timestamp": datetime.fromtimestamp(candle[0] / 1000),
            "open": candle[1],
            "high": candle[2],
            "low": candle[3],
            "close": candle[4],
        })

    _set_cache(cache_key, ohlcv)
    return ohlcv


def fetch_fear_greed() -> dict:
    """Fetch Fear & Greed Index (no API key needed)."""
    cache_key = "fear_greed"
    cached = _get_cached(cache_key)
    if cached:
        return cached

    url = "https://api.alternative.me/fng/?limit=1"
    with httpx.Client(timeout=15) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()["data"][0]

    result = {
        "value": int(data["value"]),
        "label": data["value_classification"],
        "timestamp": datetime.fromtimestamp(int(data["timestamp"])),
    }

    _set_cache(cache_key, result)
    return result


if __name__ == "__main__":
    print("=== Fetching prices ===")
    prices = fetch_prices(["bitcoin", "injective-protocol", "render-token", "polkadot", "ethereum"])
    for p in prices:
        print(f"  {p.symbol}: €{p.price_eur:.2f} (${p.price_usd:.2f})  {p.change_24h:+.1f}% 24h")

    print("\n=== Fear & Greed ===")
    fg = fetch_fear_greed()
    print(f"  {fg['value']} — {fg['label']}")
