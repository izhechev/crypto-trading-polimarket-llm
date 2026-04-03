"""
News connector — CryptoPanic (primary) + CryptoCompare (fallback).

Strategy per coin:
  1. CryptoPanic by symbol  (e.g. "UNI")
  2. CryptoPanic by name    (e.g. "Uniswap") if symbol returns nothing
  3. CryptoCompare by symbol                 if both CryptoPanic attempts fail
404s and unknown-coin responses are silently skipped.
"""
import time
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

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


# ── CryptoPanic ───────────────────────────────────────────────────────────

def _cp_fetch(query: str, by_name: bool = False) -> list[dict]:
    """
    One CryptoPanic request.  Returns [] on 404, empty results, or any error.
    `by_name=True` passes the query as a free-text filter instead of a currency code.
    """
    params = {
        "auth_token": config.CRYPTOPANIC_API_KEY,
        "public":     "true",
        "kind":       "news",
    }
    if by_name:
        params["filter"] = query          # free-text search
    else:
        params["currencies"] = query.upper()

    try:
        with httpx.Client(timeout=12) as client:
            resp = client.get("https://cryptopanic.com/api/v1/posts/", params=params)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception:
        return []


def _parse_cp(results: list[dict], limit: int) -> list[dict]:
    articles = []
    for item in results[:limit]:
        votes    = item.get("votes", {}) or {}
        positive = votes.get("positive", 0) or 0
        negative = votes.get("negative", 0) or 0
        sentiment = "positive" if positive > negative else "negative" if negative > positive else "neutral"
        articles.append({
            "title":     item.get("title", ""),
            "source":    item.get("source", {}).get("title", ""),
            "sentiment": sentiment,
        })
    return articles


# ── CryptoCompare ────────────────────────────────────────────────────────

def _cc_fetch(symbol: str, name: str, limit: int) -> list[dict]:
    """
    CryptoCompare /v2/news/ — delegates to the standalone cryptocompare connector.
    Returns [] if key not set or request fails.
    """
    from src.connectors.cryptocompare import fetch_news_for_coin
    return fetch_news_for_coin(symbol, name, limit)


# ── Public API ────────────────────────────────────────────────────────────

def fetch_news(
    symbols: list[str],
    names: list[str] | None = None,
    limit: int = 3,
) -> list[dict]:
    """
    Fetch recent news for the given symbols (up to 5 coins, `limit` articles each).
    `names` is an optional parallel list of full coin names for fallback queries.
    Returns a flat deduplicated list of {title, source, sentiment, coin} dicts.
    """
    if not config.CRYPTOPANIC_API_KEY and not config.CRYPTOCOMPARE_API_KEY:
        return []

    names = names or []
    all_articles: list[dict] = []
    seen_titles:  set[str]   = set()

    for i, sym in enumerate(symbols[:5]):
        coin_name = names[i] if i < len(names) else ""
        cache_key = f"news_{sym.upper()}"
        cached    = _cached(cache_key)
        if cached is not None:
            all_articles.extend(cached)
            continue

        articles: list[dict] = []

        if config.CRYPTOPANIC_API_KEY:
            # Attempt 1: CryptoPanic by symbol
            results = _cp_fetch(sym)
            if results:
                articles = _parse_cp(results, limit)

            # Attempt 2: CryptoPanic by full name (if symbol returned nothing)
            if not articles and coin_name:
                results = _cp_fetch(coin_name, by_name=True)
                if results:
                    articles = _parse_cp(results, limit)

        # Attempt 3: CryptoCompare fallback
        if not articles:
            articles = _cc_fetch(sym, coin_name, limit)

        # Tag each article with its coin and deduplicate across all coins
        tagged = []
        for a in articles:
            if a["title"] not in seen_titles:
                seen_titles.add(a["title"])
                tagged.append({**a, "coin": sym.upper()})
        _set_cache(cache_key, tagged)
        all_articles.extend(tagged)

    return all_articles


def format_for_prompt(articles: list[dict]) -> str:
    """Compact news summary for LLM prompts."""
    if not articles:
        return "No recent news found."
    return "\n".join(
        f"- [{a['sentiment'].upper()}] {a.get('coin', '')} — {a['title']} ({a['source']})"
        for a in articles
    )
