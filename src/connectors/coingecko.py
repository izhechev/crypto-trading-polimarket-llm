"""CoinGecko API connector with rate limiting and caching."""
import httpx
import time
from datetime import datetime
from tenacity import retry, wait_exponential, stop_after_attempt
from src.models.crypto import CryptoPrice

# Simple in-memory cache — 60 min TTL for OHLCV/F&G; 5 min for live prices.
_cache: dict[str, tuple[float, any]] = {}
CACHE_TTL       = 3600  # 60 minutes — OHLCV, Fear & Greed, EUR rate
PRICE_CACHE_TTL = 300   # 5 minutes  — live prices (fetch_prices, fetch_simple_usd)

# Stale cache: stores the last successful fetch indefinitely.
# Used as fallback when live fetch fails (429 / network error).
_stale_cache: dict[str, any] = {}
_stale_cache_ts: dict[str, float] = {}  # tracks when each stale entry was written

# ── CoinGecko call counter (real HTTP requests only, cache hits excluded) ──
_CG_CALLS: int = 0

# Set to True when a 429 monthly-quota response is received so all subsequent
# OHLCV calls skip the CG request immediately instead of retrying.
_CG_OHLCV_QUOTA_EXHAUSTED: bool = False

# Session-level search cache: symbol → CoinGecko ID (empty string = confirmed not found)
_cg_search_cache: dict[str, str] = {}


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


def _get_cached(key: str, ttl: int | None = None):
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < (ttl or CACHE_TTL):
            return data
    return None


def _set_cache(key: str, data):
    now = time.time()
    _cache[key] = (now, data)
    _stale_cache[key] = data
    _stale_cache_ts[key] = now


def _headers() -> dict:
    try:
        import config
        if config.COINGECKO_API_KEY:
            # User confirmed they use PRO
            return {"x-cg-pro-api-key": config.COINGECKO_API_KEY}
    except Exception:
        pass
    return {}


_eur_rate_cache: tuple[float, float] | None = None


def get_eur_usd_rate() -> float:
    """Return current EUR per 1 USD rate. Cached 60 min. Fallback: 0.88."""
    global _eur_rate_cache
    if _eur_rate_cache:
        ts, rate = _eur_rate_cache
        if time.time() - ts < CACHE_TTL:
            return rate
    try:
        url = f"{_base_url()}/simple/price"
        params = {"ids": "tether", "vs_currencies": "eur"}

        with httpx.Client(timeout=10) as client:
            resp = client.get(
                url,
                params=params,
                headers=_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                rate = float(resp.json().get("tether", {}).get("eur", 0.88))
                _eur_rate_cache = (time.time(), rate)
                return rate
    except Exception:
        pass
    # Return stale cached rate if available, else fallback
    if _eur_rate_cache:
        return _eur_rate_cache[1]
    return 0.88


def _fetch_eur_prices(coin_ids: list[str]) -> dict[str, float]:
    """
    Lightweight /simple/price call for EUR prices only.
    Returns {coin_id: eur_price}. Falls back to empty dict on failure.
    """
    cache_key = f"eur_simple_{'_'.join(sorted(coin_ids))}"
    cached = _get_cached(cache_key, ttl=PRICE_CACHE_TTL)
    if cached is not None:
        return cached

    try:
        url = f"{_base_url()}/simple/price"
        params = {"ids": ",".join(coin_ids), "vs_currencies": "eur"}

        with httpx.Client(timeout=15) as client:
            resp = _cg_get(client, url, params=params, headers=_headers())
            resp.raise_for_status()
            raw = resp.json()
        result = {cid: data.get("eur", 0.0) for cid, data in raw.items()}
        _set_cache(cache_key, result)
        return result
    except Exception:
        return _stale_cache.get(cache_key, {})


def fetch_simple_usd(coin_ids: list[str]) -> dict[str, float]:
    """
    Lightweight /simple/price call for USD prices.
    Works for any valid CoinGecko ID including small-cap coins not in /coins/markets.
    Returns {coin_id: usd_price}. Falls back to empty dict on failure.
    """
    if not coin_ids:
        return {}
    cache_key = f"usd_simple_{'_'.join(sorted(coin_ids))}"
    cached = _get_cached(cache_key, ttl=PRICE_CACHE_TTL)
    if cached is not None:
        return cached
    try:
        url = f"{_base_url()}/simple/price"
        params = {"ids": ",".join(coin_ids), "vs_currencies": "usd"}

        with httpx.Client(timeout=15) as client:
            resp = _cg_get(client, url, params=params, headers=_headers())
            resp.raise_for_status()
            raw = resp.json()
        result = {cid: data.get("usd", 0.0) for cid, data in raw.items() if data.get("usd", 0.0) > 0}
        _set_cache(cache_key, result)
        return result
    except Exception:
        return _stale_cache.get(cache_key, {})


def fetch_prices(coin_ids: list[str]) -> list[CryptoPrice]:
    """Fetch USD market data + real EUR prices for multiple coins."""
    cache_key = f"prices_usd_{'_'.join(sorted(coin_ids))}"
    cached = _get_cached(cache_key, ttl=PRICE_CACHE_TTL)
    if cached:
        return cached

    try:
        return _fetch_prices_live(coin_ids, cache_key)
    except Exception as e:
        stale = _stale_cache.get(cache_key)
        if stale:
            stale_age_h = (time.time() - _stale_cache_ts.get(cache_key, time.time())) / 3600
            print(f"  [CG cache] using stale prices ({len(stale)} coins, {stale_age_h:.1f}h old) — live fetch failed: {type(e).__name__}")
            return stale
        raise


def _base_url() -> str:
    try:
        import config
        if config.COINGECKO_API_KEY:
            # User confirmed they use PRO
            return "https://pro-api.coingecko.com/api/v3"
    except Exception: pass
    return "https://api.coingecko.com/api/v3"

@retry(wait=wait_exponential(min=4, max=60), stop=stop_after_attempt(4))
def _fetch_prices_live(coin_ids: list[str], cache_key: str) -> list[CryptoPrice]:
    ids_str = ",".join(coin_ids)
    url = f"{_base_url()}/coins/markets"
    params = {
        "vs_currency": "usd",
        "ids": ids_str,
        "order": "market_cap_desc",
        "sparkline": "false",
        "price_change_percentage": "24h,7d,30d",
    }
    
    # Add key to params for Demo API, or headers for Pro
    headers = _headers()
    try:
        import config
        if config.COINGECKO_API_KEY:
            if "pro-api" in _base_url():
                pass # Already in headers via _headers()
            else:
                params["x_cg_demo_api_key"] = config.COINGECKO_API_KEY
    except Exception: pass

    with httpx.Client(timeout=30) as client:
        resp = _cg_get(client, url, params=params, headers=headers)
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


def search_cg_id(symbol: str) -> str:
    """
    Find the CoinGecko coin ID for a symbol using the /search endpoint.
    Returns empty string if not found. Results are cached for the session.
    """
    sym = symbol.upper()
    if sym in _cg_search_cache:
        return _cg_search_cache[sym]
    try:
        with httpx.Client(timeout=10) as client:
            resp = _cg_get(client,
                f"{_base_url()}/search",
                params={"query": sym},
                headers=_headers(),
            )
        coins = resp.json().get("coins", [])
        match = next((c for c in coins if (c.get("symbol") or "").upper() == sym), None)
        cg_id = match["id"] if match else ""
    except Exception:
        cg_id = ""
    _cg_search_cache[sym] = cg_id
    return cg_id


def fetch_platform_info(coin_id: str) -> dict:
    """
    Fetch contract address and platform (blockchain) for a coin.
    Used for on-chain security audits (GoPlus/DexScreener).
    """
    cache_key = f"platform_{coin_id}"
    cached = _get_cached(cache_key)
    if cached: return cached

    try:
        url = f"{_base_url()}/coins/{coin_id}"
        params = {"localization": "false", "tickers": "false", "market_data": "false", "community_data": "false", "developer_data": "false"}
        with httpx.Client(timeout=10) as client:
            resp = _cg_get(client, url, params=params, headers=_headers())
            if resp.status_code == 200:
                data = resp.json()
                platforms = data.get("platforms", {})
                # Pick the primary platform (first one usually)
                if not platforms: return {}
                
                chain = list(platforms.keys())[0]
                address = platforms[chain]
                
                result = {"chain": chain, "address": address, "all_platforms": platforms}
                _set_cache(cache_key, result)
                return result
    except Exception: pass
    return {}


def fetch_ohlcv(coin_id: str, days: int = 30) -> list[dict]:
    """Fetch OHLCV data for technical analysis."""
    global _CG_OHLCV_QUOTA_EXHAUSTED
    if _CG_OHLCV_QUOTA_EXHAUSTED:
        return []
    cache_key = f"ohlcv_{coin_id}_{days}"
    cached = _get_cached(cache_key)
    if cached:
        return cached
    try:
        return _fetch_ohlcv_live(coin_id, days, cache_key)
    except Exception:
        stale = _stale_cache.get(cache_key)
        return stale if stale else []


_OHLC_VALID_DAYS = (1, 7, 14, 30, 90, 180, 365)


def _snap_days(days: int) -> int:
    """Round up to the nearest value accepted by the pro /ohlc endpoint."""
    for v in _OHLC_VALID_DAYS:
        if days <= v:
            return v
    return 365


@retry(wait=wait_exponential(min=4, max=60), stop=stop_after_attempt(4))
def _fetch_ohlcv_live(coin_id: str, days: int, cache_key: str) -> list[dict]:
    global _CG_OHLCV_QUOTA_EXHAUSTED
    url = f"{_base_url()}/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": str(_snap_days(days))}

    # Auth for Demo tier
    try:
        import config
        if config.COINGECKO_API_KEY and "pro-api" not in _base_url():
            params["x_cg_demo_api_key"] = config.COINGECKO_API_KEY
    except Exception: pass

    with httpx.Client(timeout=30) as client:
        resp = _cg_get(client, url, params=params, headers=_headers())

    if resp.status_code == 429:
        _CG_OHLCV_QUOTA_EXHAUSTED = True
        return []

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
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()["data"][0]
    except (httpx.RequestError, httpx.HTTPStatusError, Exception) as e:
        print(f"  ⚠️  Fear & Greed fetch failed ({type(e).__name__}): {e} — using neutral fallback")
        return {"value": 50, "label": "Neutral", "timestamp": datetime.utcnow()}

    result = {
        "value": int(data["value"]),
        "label": data["value_classification"],
        "timestamp": datetime.fromtimestamp(int(data["timestamp"])),
    }

    _set_cache(cache_key, result)
    return result


def fetch_coin_list() -> list[dict]:
    """Fetch the full list of supported coins from CoinGecko."""
    cache_key = "full_coin_list"
    cached = _get_cached(cache_key, ttl=3600 * 24) # Cache for 24h as it doesn't change often
    if cached:
        return cached

    url = f"{_base_url()}/coins/list"
    params = {}
    
    # Auth for Demo tier
    headers = _headers()
    try:
        import config
        if config.COINGECKO_API_KEY and "pro-api" not in _base_url():
            params["x_cg_demo_api_key"] = config.COINGECKO_API_KEY
    except Exception: pass

    try:
        with httpx.Client(timeout=30) as client:
            resp = _cg_get(client, url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            _set_cache(cache_key, data)
            return data
    except Exception as e:
        print(f"  ⚠️  CoinGecko /coins/list fetch failed: {e}")
        return []


if __name__ == "__main__":
    print("=== Fetching prices ===")
    prices = fetch_prices(["bitcoin", "injective-protocol", "render-token", "polkadot", "ethereum"])
    for p in prices:
        print(f"  {p.symbol}: ${p.price_usd:.2f}  {p.change_24h:+.1f}% 24h")

    print("\n=== Fear & Greed ===")
    fg = fetch_fear_greed()
    print(f"  {fg['value']} — {fg['label']}")
