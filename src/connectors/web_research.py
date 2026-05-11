"""
Web research connector — Reddit JSON, Google News RSS, CryptoCompare News.
Free sources, no extra API keys required (uses CRYPTOCOMPARE_API_KEY if set).
Rate-limited to respect server policies.
"""
import time
import xml.etree.ElementTree as ET
import sys
from pathlib import Path
from urllib.parse import quote_plus

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

_HEADERS = {"User-Agent": "CryptoAdvisor/1.0 (personal research tool)"}
# Reddit requires a browser-like User-Agent — generic bots get 429 or empty results
_REDDIT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
_REDDIT_DELAY = 2.0   # seconds between Reddit requests


def _safe_get(url: str, params: dict | None = None, timeout: int = 10,
              headers: dict | None = None) -> httpx.Response | None:
    try:
        h = headers if headers is not None else _HEADERS
        with httpx.Client(timeout=timeout, headers=h, follow_redirects=True) as client:
            resp = client.get(url, params=params)
            if resp.status_code == 200:
                return resp
    except Exception:
        pass
    return None


# ── Reddit (no API key — .json trick) ────────────────────────────────────

def search_reddit(query: str, subreddit: str = "CryptoCurrency", limit: int = 5) -> list[dict]:
    """
    Search Reddit for posts about a query using the public .json API.
    Uses global /search.json with subreddit filter in the query string —
    more reliable than /r/{sub}/search.json which gets blocked more aggressively.
    Returns [] silently on 429/403; caller should treat Reddit as optional.
    Rate-limited: 1 request per 2 seconds.
    """
    time.sleep(_REDDIT_DELAY)
    # Use global search with subreddit scoped in query — avoids subreddit-level blocks
    scoped_query = f"{query} subreddit:{subreddit.lower()}"
    params = {"q": scoped_query, "sort": "new", "limit": limit, "t": "week", "type": "link"}
    resp = _safe_get(
        "https://www.reddit.com/search.json",
        params=params,
        headers=_REDDIT_HEADERS,
    )
    if not resp:
        return []
    try:
        data  = resp.json()
        posts = data.get("data", {}).get("children", [])
        results = []
        for p in posts[:limit]:
            pd_ = p.get("data", {})
            title = pd_.get("title", "")
            if not title:
                continue
            results.append({
                "title":     title,
                "score":     pd_.get("score", 0),
                "url":       f"https://reddit.com{pd_.get('permalink','')}",
                "date":      pd_.get("created_utc", 0),
                "subreddit": pd_.get("subreddit", subreddit),
                "selftext":  (pd_.get("selftext","") or "")[:200],
            })
        return results
    except Exception:
        return []


# ── Google News RSS (free, no key) ────────────────────────────────────────

def search_google_news(query: str, limit: int = 5) -> list[dict]:
    """
    Fetch Google News RSS for a query.
    Returns list of {title, url, date}.
    """
    encoded = quote_plus(f"{query} crypto OR stock OR finance")
    url     = f"https://news.google.com/rss/search?q={encoded}&hl=en&gl=US&ceid=US:en"
    resp    = _safe_get(url, timeout=12)
    if not resp:
        return []
    try:
        root  = ET.fromstring(resp.text)
        items = root.findall(".//item")
        results = []
        for item in items[:limit]:
            title = item.findtext("title","").strip()
            link  = item.findtext("link","").strip()
            pub   = item.findtext("pubDate","").strip()
            if title:
                results.append({"title": title, "url": link, "date": pub})
        return results
    except Exception:
        return []


# ── CryptoCompare News (uses existing key if set) ─────────────────────────

def search_cryptocompare_news(coin_symbol: str, limit: int = 5) -> list[dict]:
    """
    Fetch latest news from CryptoCompare for a coin symbol.
    Returns list of {title, url, date, source}.
    """
    url    = "https://min-api.cryptocompare.com/data/v2/news/"
    params = {"categories": coin_symbol.upper(), "sortOrder": "latest"}
    if config.CRYPTOCOMPARE_API_KEY:
        params["api_key"] = config.CRYPTOCOMPARE_API_KEY
    resp = _safe_get(url, params=params)
    if not resp:
        return []
    try:
        data = resp.json().get("Data", [])
        results = []
        for item in data[:limit]:
            results.append({
                "title":  item.get("title",""),
                "url":    item.get("url",""),
                "date":   item.get("published_on",""),
                "source": item.get("source",""),
            })
        return results
    except Exception:
        return []


# Messari News removed — Enterprise-only API, returns 0 results on free tier.
# Dead endpoint: wastes 10 API calls per scan with nothing to show for it.
def search_messari_news(coin_symbol: str, limit: int = 5) -> list[dict]:
    """Removed — Messari News requires Enterprise key. Always returns []."""
    return []


# ── CoinGecko Status Updates (free, uses coin_id not symbol) ─────────────

def search_coingecko_status(coin_id: str, limit: int = 3) -> list[dict]:
    """
    Fetch team status updates from CoinGecko for a coin.
    These are official project announcements (launches, partnerships, upgrades).
    Returns list of {title, date, source}.
    """
    url     = f"https://api.coingecko.com/api/v3/coins/{coin_id}/status_updates"
    params  = {"per_page": limit}
    headers = {}
    if config.COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = config.COINGECKO_API_KEY
    try:
        with httpx.Client(timeout=10, headers=headers, follow_redirects=True) as client:
            resp = client.get(url, params=params)
            if resp.status_code != 200:
                return []
        updates = resp.json().get("status_updates", [])
        results = []
        for item in updates[:limit]:
            # CoinGecko status update: user.name + description
            desc = (item.get("description") or "").strip()
            user = item.get("user", "") or ""
            category = item.get("category", "") or ""
            title = f"[{category}] {desc[:120]}" if category else desc[:120]
            if title:
                results.append({
                    "title":  title,
                    "date":   item.get("created_at", ""),
                    "source": f"CoinGecko status ({user})",
                })
        return results
    except Exception:
        return []


# ── Aggregated research ───────────────────────────────────────────────────

def research_crypto(coin_symbol: str, coin_name: str = "") -> dict:
    """
    Run full web research for a crypto coin.
    Returns {reddit_posts, news_articles, cc_news, sentiment_summary}.

    Uses full coin name (not just symbol) for ambiguous short tickers like
    "AB", "GO", "LINK" to avoid returning news about unrelated companies.
    """
    symbol_upper = coin_symbol.upper()
    # For short/ambiguous symbols (<=3 chars), anchor searches on the full name
    # so we don't get Swedish companies, baseball teams, etc.
    if coin_name and len(symbol_upper) <= 3:
        reddit_query = f"{coin_name} cryptocurrency"
        news_query   = f"{coin_name} crypto token"
    else:
        reddit_query = f"{symbol_upper} {coin_name}".strip()
        news_query   = f"{reddit_query} cryptocurrency"

    reddit_crypto = search_reddit(reddit_query, subreddit="CryptoCurrency", limit=5)
    time.sleep(_REDDIT_DELAY)
    # For defi subreddit, always use full name if available to avoid confusion
    defi_query    = coin_name if coin_name else coin_symbol
    reddit_defi   = search_reddit(defi_query, subreddit="defi", limit=3)

    news = search_google_news(news_query, limit=5)

    sentiment = _classify_sentiment(
        [p["title"] for p in reddit_crypto + reddit_defi] +
        [n["title"] for n in news]
    )

    return {
        "reddit_posts":   reddit_crypto + reddit_defi,
        "news_articles":  news,
        "cc_news":        [],   # CryptoCompare + Messari removed (generic/dead endpoints)
        "sentiment":      sentiment,
    }


def research_stock(ticker: str, company_name: str = "") -> dict:
    """Run web research for a stock ticker."""
    query = f"{ticker} {company_name}".strip()

    time.sleep(_REDDIT_DELAY)
    wsb   = search_reddit(query, subreddit="wallstreetbets", limit=5)
    time.sleep(_REDDIT_DELAY)
    stocks= search_reddit(query, subreddit="stocks", limit=3)
    news  = search_google_news(f"{ticker} stock earnings", limit=5)

    sentiment = _classify_sentiment(
        [p["title"] for p in wsb + stocks] + [n["title"] for n in news]
    )

    return {
        "reddit_posts":  wsb + stocks,
        "news_articles": news,
        "cc_news":       [],
        "sentiment":     sentiment,
    }


def research_polymarket(question: str) -> dict:
    """Run web research for a Polymarket event question."""
    query = question[:80]

    time.sleep(_REDDIT_DELAY)
    poly = search_reddit(query, subreddit="polymarket", limit=5)
    time.sleep(_REDDIT_DELAY)
    news_sub = search_reddit(query, subreddit="news", limit=3)
    news     = search_google_news(query, limit=5)

    sentiment = _classify_sentiment(
        [p["title"] for p in poly + news_sub] + [n["title"] for n in news]
    )

    return {
        "reddit_posts":  poly + news_sub,
        "news_articles": news,
        "cc_news":       [],
        "sentiment":     sentiment,
    }


# ── Naive sentiment classifier ────────────────────────────────────────────

_BULLISH_WORDS = {
    "bullish","moon","pump","surge","rally","breakout","buy","long","gains","strong",
    "bull","up","rise","rising","soar","growth","positive","upside","record","launch",
    "upgrade","partnership","adoption","listing","win","approve","approved","green",
}
_BEARISH_WORDS = {
    "bearish","crash","dump","drop","fall","falling","sell","short","loss","weak",
    "bear","down","decline","plunge","scam","hack","exploit","lawsuit","sec","probe",
    "ban","warning","risk","concern","fear","bubble","rug","fraud","negative","red",
    "downside","delay","fail","reject","breach","vulnerability",
}


def _classify_sentiment(titles: list[str]) -> dict:
    """Simple word-count sentiment from titles. Returns {bullish, bearish, neutral, label}."""
    b = be = 0
    for title in titles:
        t = title.lower()
        for w in _BULLISH_WORDS:
            if w in t:
                b += 1
                break
        for w in _BEARISH_WORDS:
            if w in t:
                be += 1
                break

    n = len(titles) - b - be
    if b > be:
        label = "BULLISH"
    elif be > b:
        label = "BEARISH"
    else:
        label = "NEUTRAL"

    return {"bullish": b, "bearish": be, "neutral": n, "label": label}


# ── Format for prompt ─────────────────────────────────────────────────────

def format_research_for_prompt(research: dict, label: str = "") -> str:
    """Format research dict into a compact prompt string."""
    parts = []
    if label:
        parts.append(f"WEB RESEARCH — {label}")

    reddit = research.get("reddit_posts", [])
    if reddit:
        parts.append("Reddit posts:")
        for p in reddit[:5]:
            score = p.get("score", 0)
            parts.append(f"  [{score:+d}] {p['title'][:90]}")

    news = research.get("news_articles", []) + research.get("cc_news", [])
    if news:
        parts.append("News headlines:")
        for n in news[:5]:
            parts.append(f"  • {n['title'][:90]}")

    sent = research.get("sentiment", {})
    if sent:
        parts.append(
            f"Social sentiment: {sent.get('bullish',0)} bullish, "
            f"{sent.get('bearish',0)} bearish, {sent.get('neutral',0)} neutral "
            f"→ {sent.get('label','?')}"
        )

    return "\n".join(parts)


# ── Tavily AI search (free tier: 1,000/month, no CC) ─────────────────────

_tavily_quota_exceeded: bool = False   # set True on 432 — skip for rest of session


def _tavily_search(query: str, max_results: int = 3) -> dict:
    """
    Call Tavily Search API and return the response dict.
    Sets _tavily_quota_exceeded=True on HTTP 432 (plan limit reached).
    Returns {} on error, missing key, or quota exceeded.
    """
    global _tavily_quota_exceeded
    if not config.TAVILY_API_KEY or _tavily_quota_exceeded:
        return {}
    try:
        import json as _json
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            r = client.post(
                "https://api.tavily.com/search",
                headers={"Content-Type": "application/json"},
                content=_json.dumps({
                    "api_key":       config.TAVILY_API_KEY,
                    "query":         query,
                    "search_depth":  "basic",   # basic = 1 credit, advanced = 2
                    "include_answer": True,
                    "max_results":   max_results,
                    "topic":         "news",
                }),
            )
        if r.status_code == 432:
            _tavily_quota_exceeded = True
            # print("  ⚠️  Tavily quota exceeded — switching to Brave/DuckDuckGo for this session")
            return {}
        if r.status_code == 200:
            try:
                from src.utils.budget_tracker import log_llm_call as _log
                _log("tavily", tokens_in=0, tokens_out=0, endpoint="search")
            except Exception:
                pass
            return r.json()
    except Exception:
        pass
    return {}


# ── NewsData.io (free tier: 6,000/month, no CC) ───────────────────────────

def _newsdata_search(query: str, coin_name: str = "", coin_sym: str = "",
                     max_results: int = 3) -> dict:
    """
    Call NewsData.io API — free tier 6,000 requests/month, no credit card.
    Filters results so the coin name or symbol actually appears in the headline.
    Returns Tavily-compatible dict: {"results": [...], "answer": ""}
    """
    if not config.NEWSDATA_API_KEY:
        return {}
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            r = client.get(
                "https://newsdata.io/api/1/news",
                params={
                    "apikey":    config.NEWSDATA_API_KEY,
                    "q":         query,
                    "language":  "en",
                    "category":  "business,technology",
                    "size":      max_results * 3,   # fetch extra so filter has headroom
                },
            )
        if r.status_code != 200:
            return {}
        hits = r.json().get("results", [])

        # Relevance filter: title must mention coin name or symbol
        name_l = coin_name.lower() if coin_name else ""
        sym_l  = coin_sym.lower()  if coin_sym  else ""
        relevant = []
        for h in hits:
            title = (h.get("title") or "").lower()
            desc  = (h.get("description") or "").lower()
            text  = title + " " + desc
            if (name_l and name_l in text) or (sym_l and len(sym_l) > 2 and sym_l in text):
                relevant.append(h)
            if len(relevant) >= max_results:
                break

        return {
            "answer":  "",
            "results": [
                {
                    "title":   h.get("title", ""),
                    "url":     h.get("link", ""),
                    "content": (h.get("description") or h.get("content") or "").strip(),
                }
                for h in relevant
            ],
        }
    except Exception:
        pass
    return {}


# ── DuckDuckGo fallback (zero key, zero cost) ─────────────────────────────

def _ddg_search(query: str, max_results: int = 3) -> list[dict]:
    """
    Search DuckDuckGo using the duckduckgo-search library (no API key needed).
    Returns list of {title, href, body} or [] if library not installed.
    """
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except ImportError:
        return []   # library not installed — caller falls back to Google News RSS
    except Exception:
        return []


def _web_search(query: str, coin_name: str = "", coin_sym: str = "",
                max_results: int = 3) -> dict:
    """
    Unified search with priority chain:
      1. NewsData.io (6,000/month free, key required)
      2. DuckDuckGo (unlimited, no key, library required)
    Returns Tavily-compatible dict: {"results": [...], "answer": ""}
    """
    # 1. NewsData.io
    if config.NEWSDATA_API_KEY:
        nd = _newsdata_search(query, coin_name=coin_name, coin_sym=coin_sym,
                              max_results=max_results)
        if nd.get("results"):
            return nd

    # 2. DuckDuckGo
    ddg = _ddg_search(query, max_results)
    if ddg:
        return {
            "answer":  "",
            "results": [
                {"title": h.get("title", ""), "url": h.get("href", ""),
                 "content": h.get("body", "")}
                for h in ddg
            ],
        }

    return {}


# ── Groq-assisted news researcher ────────────────────────────────────────

def _groq_agentic_news(coins: list[dict]) -> dict[str, str]:
    """
    Two-step news pipeline:
      1. Search each coin with a simple template query via _web_search()
         (Brave → DuckDuckGo — no Groq query generation needed)
      2. Groq reads all raw results and writes one-sentence summaries (single batch call → JSON)

    Returns {SYMBOL: "summary"} or {} on failure.
    """
    if not config.GROQ_API_KEY or not coins:
        return {}
    # Need at least one search backend
    _has_newsdata = bool(config.NEWSDATA_API_KEY)
    try:
        from duckduckgo_search import DDGS as _DDGS  # noqa: F401
        _has_ddg = True
    except ImportError:
        _has_ddg = False
    if not _has_newsdata and not _has_ddg:
        return {}

    import json as _json
    import re  as _re

    try:
        from groq import Groq as _Groq
    except ImportError:
        return {}

    client = _Groq(api_key=config.GROQ_API_KEY)

    # ── Step 1: search each coin with a simple query ──────────────────────
    _backend = "NewsData.io" if _has_newsdata else "DuckDuckGo"
    print(f"  🔍 {_backend} searching {len(coins)} coins...")

    raw_results: dict[str, str] = {}
    for c in coins:
        sym  = c.get("symbol", "").upper()
        name = c.get("name", "") or sym
        # Use just the coin name — short query gives better NewsData relevance
        query = name
        data  = _web_search(query, coin_name=name, coin_sym=sym, max_results=3)
        answer = (data.get("answer") or "").strip()
        hits   = data.get("results", [])
        if answer and len(answer) > 20:
            raw_results[sym] = f"Answer: {answer[:400]}"
        elif hits:
            snippets = []
            for h in hits[:3]:
                title   = h.get("title", "").strip()
                content = (h.get("content") or h.get("body") or "").strip()
                if content and len(content) > 20:
                    snippets.append(f"- {title}: {content[:150]}")
                elif title:
                    snippets.append(f"- {title}")
            if snippets:
                raw_results[sym] = "\n".join(snippets)
        time.sleep(0.2)

    print(f"  📰 Got results for {len(raw_results)}/{len(coins)} coins")
    # Debug: show raw news results
    for _sym, _content in raw_results.items():
        print(f"  🔍 News for {_sym}:\n{_content[:200].strip()}...")

    if not raw_results:
        return {}

    # ── Step 2: Groq synthesises one-sentence summaries ───────────────────
    results_block = "\n\n".join(
        f"COIN: {sym}\n{content}"
        for sym, content in raw_results.items()
    )
    s_prompt = (
        "You are a crypto news editor. Below are web search results for several cryptocurrencies.\n"
        "For each coin that has meaningful, specific news (not generic price predictions), "
        "write ONE concise sentence summarising the most important recent development.\n"
        "Omit coins with no real news. Never make up information not in the results.\n"
        "Return ONLY valid JSON: {\"SYMBOL\": \"one sentence summary\", ...}\n\n"
        f"Search results:\n{results_block}"
    )
    try:
        s_resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": s_prompt}],
            temperature=0.1,
            max_tokens=800,
        )
        s_raw = s_resp.choices[0].message.content or ""
        m = _re.search(r'\{.*\}', s_raw, _re.DOTALL)
        if m:
            result = {k.upper(): str(v) for k, v in _json.loads(m.group(0)).items() if v}
            print(f"  ✅ Groq summarised news for {len(result)}/{len(coins)} coins")
            return result
    except Exception as _e:
        print(f"  ⚠️  Groq summarisation failed: {_e}")

    return {}


# ── Top-10 catalyst summaries ─────────────────────────────────────────────

def get_top10_catalysts(coins: list[dict]) -> dict[str, str]:
    """
    Return {symbol: "1-sentence news summary"} for each coin in the list.

    Priority:
      1. Groq + web search (Brave or DuckDuckGo) — Groq synthesises from real results.
      2. Tavily template fallback — only if key set AND quota not exceeded.
      3. DuckDuckGo direct — raw headline/snippet, no Groq.
      4. Google News RSS — always-available last resort.

    Empty string = no news found for that coin.
    """
    if not coins:
        return {}

    symbols  = [c.get("symbol", "").upper() for c in coins]
    names    = {c.get("symbol", "").upper(): c.get("name", "") for c in coins}
    result: dict[str, str] = {s: "" for s in symbols}

    # ── 1. Groq + web search (NewsData.io → DDG) ─────────────────────────
    _has_newsdata2 = bool(config.NEWSDATA_API_KEY)
    try:
        from duckduckgo_search import DDGS as _DDGS2  # noqa: F401
        _has_ddg2 = True
    except ImportError:
        _has_ddg2 = False

    if config.GROQ_API_KEY and (_has_newsdata2 or _has_ddg2):
        _backend = "NewsData.io" if _has_newsdata2 else "DuckDuckGo"
        print(f"  🤖 Groq + {_backend} researching news for {len(coins)} coins...")
        agentic = _groq_agentic_news(coins)
        if agentic:
            for sym in symbols:
                result[sym] = agentic.get(sym, "")
            return result
        print(f"  ⚠️  Groq+{_backend} returned nothing — falling back")

    # ── 2. Tavily template fallback (if quota not exceeded) ───────────────
    if config.TAVILY_API_KEY and not _tavily_quota_exceeded:
        import re as _re

        _no_data_phrases = (
            "not directly mentioned", "no information", "not available",
            "no specific", "not mentioned", "as of today", "as of april",
            "as of march", "as of february", "as of january", "as of december",
            "as of november", "as of october", "as of september", "as of august",
            "as of july", "as of june", "as of may",
            "provided data sources", "no recent news", "latest data",
            "no news", "cannot provide", "i don't have", "i do not have",
            "not been mentioned", "latest news on", "latest news about",
            "not directly available", "based on the latest", "fluctuating",
            "price prediction", "has not been mentioned",
        )

        def _is_relevant(text: str, sym: str, name: str) -> bool:
            tl = text.lower()
            sym_l  = sym.lower()
            name_l = name.lower() if name else ""
            if len(sym_l) <= 3:
                return bool(name_l and name_l in tl)
            return sym_l in tl or bool(name_l and name_l in tl)

        for sym in symbols:
            if _tavily_quota_exceeded:
                break
            name  = names.get(sym, "")
            query = f"{name} coin news" if name else f"{sym} crypto news"
            data  = _tavily_search(query, max_results=5)
            answer = (data.get("answer") or "").strip()
            hits   = data.get("results", [])
            sentence = ""

            if answer and len(answer) > 10:
                m = _re.search(r'^(.{20,220}?[.!?])(?:\s|$)', answer)
                candidate = m.group(1) if m else answer[:200]
                if (not any(nd in candidate.lower() for nd in _no_data_phrases)
                        and _is_relevant(candidate, sym, name)):
                    sentence = candidate

            if not sentence:
                for hit in hits:
                    for _text in (hit.get("title",""), hit.get("content",""), hit.get("snippet","")):
                        _text = _text.strip()
                        if (len(_text) >= 25 and " " in _text
                                and not any(nd in _text.lower() for nd in _no_data_phrases)
                                and not _text.lower().startswith(("news explorer","explore","home"))
                                and _is_relevant(_text, sym, name)):
                            sentence = _text[:120]
                            break
                    if sentence:
                        break

            result[sym] = sentence
            time.sleep(0.2)

        if not _tavily_quota_exceeded:
            return result
        # quota hit mid-loop — fall through to DDG for remaining coins

    # ── 3. DuckDuckGo direct ─────────────────────────────────────────────
    if _has_ddg2:
        for sym in symbols:
            if result.get(sym):
                continue  # already filled by partial Tavily run
            name  = names.get(sym, "")
            query = f"{name} coin news" if name else f"{sym} crypto news"
            hits  = _ddg_search(query, max_results=3)
            if hits:
                body = (hits[0].get("body") or hits[0].get("title", ""))[:120]
                result[sym] = body
            time.sleep(0.3)
        return result

    # ── 4. Google News RSS fallback (always works, no deps) ───────────────
    for sym in symbols:
        name  = names.get(sym, "")
        query = f"{name} crypto" if name else f"{sym} cryptocurrency"
        headlines = search_google_news(query, limit=3)
        if headlines:
            result[sym] = headlines[0]["title"][:100]
        time.sleep(0.3)

    return result


def _parse_age_hours(date_val) -> float | None:
    """
    Parse a date value and return how many hours ago it was published.
    Handles:
      - Unix timestamp (int/float)
      - RFC-2822 string  "Mon, 07 Apr 2026 12:00:00 GMT"  (Google News RSS pubDate)
      - ISO 8601 string  "2026-04-10T14:23:00Z" or "2026-04-10T14:23:00+00:00"
    Returns None if unparseable.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if not date_val:
        return None
    try:
        # Unix timestamp
        if isinstance(date_val, (int, float)) and date_val > 0:
            published = datetime.fromtimestamp(float(date_val), tz=timezone.utc)
            return max(0, (now - published).total_seconds() / 3600)
        if isinstance(date_val, str):
            # ISO 8601: "2026-04-10T14:23:00Z" or "2026-04-10T14:23:00+00:00"
            if "T" in date_val:
                published = datetime.fromisoformat(date_val.replace("Z", "+00:00"))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                return max(0, (now - published).total_seconds() / 3600)
            # RFC-2822: "Mon, 07 Apr 2026 12:00:00 GMT" — Google News pubDate
            # Try email.utils first (most reliable for strict RFC-2822)
            try:
                from email.utils import parsedate_to_datetime
                published = parsedate_to_datetime(date_val)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                return max(0, (now - published).total_seconds() / 3600)
            except Exception:
                pass
            # Fallback: strptime for common variations
            for fmt in (
                "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S GMT",
                "%d %b %Y %H:%M:%S %z",
                "%Y-%m-%d",
            ):
                try:
                    published = datetime.strptime(date_val, fmt)
                    if published.tzinfo is None:
                        published = published.replace(tzinfo=timezone.utc)
                    return max(0, (now - published).total_seconds() / 3600)
                except ValueError:
                    continue
    except Exception:
        pass
    return None


def fetch_news_for_coins(
    coins: list[dict],
    limit_per_coin: int = 10,
) -> dict[str, list[dict]]:
    """
    Batch-fetch per-coin news.  Priority:
      1. Tavily AI (TAVILY_API_KEY set) — AI answer + fresh web results, 1 credit/call
      2. Google News RSS  — free, no key, always returns results
      3. CryptoPanic      — by ticker then by name (requires CRYPTOPANIC_API_KEY)

    Returns {symbol: [{"title": str, "age_hours": float|None, "is_recent": bool, "source": str}, ...]}
    age_hours=0.5 means published 30 mins ago; None means date unknown.
    """
    import re as _re
    per_coin: dict[str, list[dict]] = {}
    _cp_enabled  = bool(config.CRYPTOPANIC_API_KEY)
    _use_tavily  = bool(config.TAVILY_API_KEY)
    _no_data_phrases = (
        "not directly mentioned", "no information", "not available",
        "no specific", "not mentioned",
    )

    for coin in coins:
        sym  = coin.get("symbol", "").upper()
        name = coin.get("name", "")
        items: list[dict] = []
        seen:  set[str]   = set()

        def _add(title: str, date_val, source: str = "") -> None:
            if title and title not in seen:
                seen.add(title)
                age_h = _parse_age_hours(date_val)
                items.append({
                    "title":     title,
                    "age_hours": age_h,
                    "is_recent": (age_h is not None and age_h <= 2.0),
                    "source":    source,
                })

        if _use_tavily:
            # ── 1. Tavily — AI-powered news (1 credit per coin) ──────────────
            query = f"{name} cryptocurrency latest news" if name else f"{sym} crypto latest news"
            data  = _tavily_search(query, max_results=4)
            answer = (data.get("answer") or "").strip()
            if answer and len(answer) > 10:
                if not any(nd in answer.lower() for nd in _no_data_phrases):
                    m = _re.search(r'^(.{20,220}?[.!?])(?:\s|$)', answer)
                    sentence = m.group(1) if m else answer[:200]
                    _add(sentence, None, "Tavily")
            for res in data.get("results", [])[:limit_per_coin]:
                _add((res.get("title") or "").strip(), None, "Tavily")
            time.sleep(0.2)
        else:
            # ── 2. Google News RSS — free fallback (fetch more for better coverage) ──
            if len(sym) <= 2:
                gn_query = f"{name} cryptocurrency" if name else f"{sym} crypto"
            elif len(sym) <= 4:
                gn_query = f"{name} crypto" if name else f"{sym} cryptocurrency"
            else:
                gn_query = f"{sym} {name} crypto".strip() if name else f"{sym} cryptocurrency"
            for gn in search_google_news(gn_query, limit=10):
                _add(gn.get("title", ""), gn.get("date"), "GoogleNews")

            # ── 2b. Google News RSS second pass — risk/bearish angle ─────────
            risk_query = f"{sym} crypto hack scam risk news" if len(sym) > 2 else f"{name} crypto risk"
            for gn in search_google_news(risk_query, limit=5):
                _add(gn.get("title", ""), gn.get("date"), "GoogleNews")

            # ── 3. CryptoCompare — always try (no key required for basic use) ─
            try:
                cc_news = search_cryptocompare_news(sym, limit=5)
                for item in cc_news:
                    _add(item.get("title", ""), item.get("date"), "CryptoCompare")
            except Exception:
                pass

            # ── 4. CryptoPanic (optional key) ────────────────────────────────
            if _cp_enabled:
                try:
                    from src.connectors.cryptopanic import _cp_fetch, _parse_cp
                    cp_results = _cp_fetch(sym) or _cp_fetch(name, by_name=True)
                    for article in _parse_cp(cp_results, limit=5):
                        _add(article.get("title", ""), None, "CryptoPanic")
                except Exception:
                    pass

        if items:
            per_coin[sym] = items[:limit_per_coin + 5]   # keep generously for scoring

    return per_coin


def print_research(research: dict, label: str = "") -> None:
    """Print research summary to console."""
    print(f"\n  WEB RESEARCH{' — ' + label if label else ''}")
    sent = research.get("sentiment", {})
    b  = sent.get("bullish", 0)
    be = sent.get("bearish", 0)
    n  = sent.get("neutral", 0)
    print(f"  Reddit: {b} bullish, {be} bearish, {n} neutral — {sent.get('label','?')}")

    news = research.get("news_articles", []) + research.get("cc_news", [])
    if news:
        print("  News:")
        for item in news[:4]:
            print(f"    • {item['title'][:80]}")
