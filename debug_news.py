"""
Standalone news debug script — tests NewsData.io, DuckDuckGo, and the full Groq pipeline.

Usage:
    python debug_news.py                     # default coins
    python debug_news.py BTC ETH SOL PEPE    # custom symbols
"""

import sys
import json
import time
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config

DEFAULT_COINS = [
    {"symbol": "BTC",  "name": "Bitcoin"},
    {"symbol": "ETH",  "name": "Ethereum"},
    {"symbol": "SOL",  "name": "Solana"},
    {"symbol": "PEPE", "name": "Pepe"},
    {"symbol": "INJ",  "name": "Injective"},
]

if len(sys.argv) > 1:
    COINS = [{"symbol": s.upper(), "name": s.upper()} for s in sys.argv[1:]]
else:
    COINS = DEFAULT_COINS

import httpx

print("=" * 60)
print("NEWS DEBUG SCRIPT")
print("=" * 60)
print(f"GROQ_API_KEY     : {'SET (' + config.GROQ_API_KEY[:8] + '...)' if config.GROQ_API_KEY else 'MISSING ❌'}")
print(f"NEWSDATA_API_KEY : {'SET (' + config.NEWSDATA_API_KEY[:8] + '...)' if config.NEWSDATA_API_KEY else 'MISSING ❌'}")
print(f"TAVILY_API_KEY   : {'SET (' + config.TAVILY_API_KEY[:8] + '...)' if config.TAVILY_API_KEY else 'MISSING ❌'}")
print(f"Coins            : {[c['symbol'] for c in COINS]}")
print()

# ─── STEP 0: Tavily quick check ───────────────────────────────────────────────
print("=" * 60)
print("STEP 0 — Tavily quota check (one call)")
print("=" * 60)
if not config.TAVILY_API_KEY:
    print("SKIPPED — no TAVILY_API_KEY")
else:
    try:
        with httpx.Client(timeout=10) as _c:
            _r = _c.post(
                "https://api.tavily.com/search",
                headers={"Content-Type": "application/json"},
                content=json.dumps({"api_key": config.TAVILY_API_KEY,
                                    "query": "Bitcoin coin news", "search_depth": "basic",
                                    "include_answer": True, "max_results": 1, "topic": "news"}),
            )
        if _r.status_code == 432:
            print("❌ QUOTA EXCEEDED — will be skipped this session")
        elif _r.status_code == 200:
            print(f"✅ Tavily OK — answer: {(_r.json().get('answer') or '')[:100]!r}")
        else:
            print(f"HTTP {_r.status_code}: {_r.text[:200]}")
    except Exception as _e:
        print(f"❌ {_e}")
print()

# ─── STEP 1: NewsData.io test ─────────────────────────────────────────────────
print("=" * 60)
print("STEP 1 — NewsData.io test (single coin: Bitcoin)")
print("=" * 60)
if not config.NEWSDATA_API_KEY:
    print("SKIPPED — no NEWSDATA_API_KEY")
else:
    _q = "Bitcoin"
    print(f"Query: {_q!r}  (+ category=business,technology + relevance filter)")
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as _c:
            _r = _c.get(
                "https://newsdata.io/api/1/news",
                params={"apikey": config.NEWSDATA_API_KEY, "q": _q, "language": "en",
                        "category": "business,technology", "size": 9},
            )
        print(f"HTTP status : {_r.status_code}")
        if _r.status_code != 200:
            print(f"Response    : {_r.text[:300]}")
        else:
            _data = _r.json()
            _all  = _data.get("results", [])
            # Apply relevance filter
            _hits = [h for h in _all
                     if "bitcoin" in (h.get("title","") + h.get("description","")).lower()][:3]
            print(f"raw results : {len(_all)}  |  relevant: {len(_hits)}  |  totalResults: {_data.get('totalResults','?')}")
            for i, h in enumerate(_hits):
                print(f"  [{i}] {h.get('title','')[:80]}")
                _desc = (h.get('description') or '').strip()
                if _desc:
                    print(f"       {_desc[:80]}")
    except Exception as _e:
        print(f"❌ EXCEPTION: {_e}")
print()

# ─── STEP 2: DuckDuckGo test ──────────────────────────────────────────────────
print("=" * 60)
print("STEP 2 — DuckDuckGo test (single coin: Bitcoin)")
print("=" * 60)
try:
    from duckduckgo_search import DDGS
    _has_ddg = True
except ImportError:
    _has_ddg = False
    print("SKIPPED — duckduckgo-search not installed")
    print("Install with: pip install duckduckgo-search")

if _has_ddg:
    _q = "Bitcoin coin news"
    print(f"Query: {_q!r}")
    try:
        with DDGS() as ddgs:
            _hits = list(ddgs.text(_q, max_results=3))
        print(f"results : {len(_hits)} hit(s)")
        for i, h in enumerate(_hits):
            print(f"  [{i}] {h.get('title','')[:80]}")
            print(f"       {(h.get('body') or '')[:80]}")
    except Exception as _e:
        print(f"❌ EXCEPTION: {_e}")
print()

# ─── STEP 3: Full web search per coin ────────────────────────────────────────
print("=" * 60)
print("STEP 3 — Full search per coin (NewsData.io → DDG)")
print("=" * 60)

if not config.NEWSDATA_API_KEY and not _has_ddg:
    print("❌ No search backend available")
    sys.exit(1)

raw_results: dict[str, str] = {}

for c in COINS:
    sym   = c["symbol"]
    name  = c["name"]
    query = name   # just the coin name — relevance filter handles the rest
    print(f"\n--- {sym} | query: {query!r} ---")
    found = False

    # NewsData.io
    if config.NEWSDATA_API_KEY:
        try:
            name_l = name.lower()
            sym_l  = sym.lower()
            with httpx.Client(timeout=15, follow_redirects=True) as _c:
                _r = _c.get(
                    "https://newsdata.io/api/1/news",
                    params={"apikey": config.NEWSDATA_API_KEY,
                            "q": query, "language": "en",
                            "category": "business,technology", "size": 9},
                )
            if _r.status_code == 200:
                _all  = _r.json().get("results", [])
                # Relevance filter
                _hits = []
                for h in _all:
                    t = (h.get("title") or "").lower()
                    d = (h.get("description") or "").lower()
                    txt = t + " " + d
                    if (name_l and name_l in txt) or (len(sym_l) > 2 and sym_l in txt):
                        _hits.append(h)
                    if len(_hits) >= 3:
                        break
                print(f"  NewsData: {len(_hits)}/{len(_all)} relevant hit(s)")
                snippets = []
                for h in _hits:
                    title = h.get("title", "")
                    desc  = (h.get("description") or h.get("content") or "").strip()
                    print(f"    • {title[:70]}")
                    if desc:
                        print(f"      {desc[:80]}")
                    if desc:
                        snippets.append(f"- {title}: {desc[:150]}")
                    elif title:
                        snippets.append(f"- {title}")
                if snippets:
                    raw_results[sym] = "\n".join(snippets)
                    found = True
            else:
                print(f"  NewsData HTTP {_r.status_code}: {_r.text[:100]}")
        except Exception as _e:
            print(f"  NewsData EXCEPTION: {_e}")

    # DuckDuckGo fallback
    if not found and _has_ddg:
        try:
            with DDGS() as ddgs:
                _hits = list(ddgs.text(query, max_results=3))
            print(f"  DDG: {len(_hits)} hit(s)")
            snippets = []
            for h in _hits[:3]:
                title = h.get("title", "")
                body  = (h.get("body") or "").strip()
                print(f"    • {title[:70]}")
                if body:
                    print(f"      {body[:80]}")
                if body:
                    snippets.append(f"- {title}: {body[:150]}")
                elif title:
                    snippets.append(f"- {title}")
            if snippets:
                raw_results[sym] = "\n".join(snippets)
                found = True
        except Exception as _e:
            print(f"  DDG EXCEPTION: {_e}")

    if not found:
        print("  ⚠️  No results from any backend")

    time.sleep(0.3)

print(f"\n✅ raw_results for: {list(raw_results.keys())}")

# ─── STEP 4: Groq summarisation ──────────────────────────────────────────────
print()
print("=" * 60)
print("STEP 4 — Groq summarises from search results")
print("=" * 60)

if not config.GROQ_API_KEY:
    print("SKIPPED — no GROQ_API_KEY")
elif not raw_results:
    print("SKIPPED — no raw_results from Step 3")
else:
    try:
        from groq import Groq as _Groq
    except ImportError:
        print("❌ groq not installed — pip install groq")
        sys.exit(1)

    client = _Groq(api_key=config.GROQ_API_KEY)
    results_block = "\n\n".join(
        f"COIN: {sym}\n{content}" for sym, content in raw_results.items()
    )
    s_prompt = (
        "You are a crypto news editor. Below are web search results for several cryptocurrencies.\n"
        "For each coin that has meaningful, specific news (not generic price predictions), "
        "write ONE concise sentence summarising the most important recent development.\n"
        "Omit coins with no real news. Never make up information not in the results.\n"
        "Return ONLY valid JSON: {\"SYMBOL\": \"one sentence summary\", ...}\n\n"
        f"Search results:\n{results_block}"
    )
    print(f"Sending {len(raw_results)} coins to Groq...")
    try:
        s_resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": s_prompt}],
            temperature=0.1,
            max_tokens=800,
        )
        s_raw = s_resp.choices[0].message.content or ""
        print(f"Groq raw:\n{s_raw}\n")
        m = re.search(r'\{.*\}', s_raw, re.DOTALL)
        if m:
            summaries = {k.upper(): str(v) for k, v in json.loads(m.group(0)).items() if v}
            print(f"✅ Final summaries ({len(summaries)} coins):")
            for sym, summary in summaries.items():
                print(f"   {sym}: {summary}")
        else:
            print("❌ No JSON found in Groq response")
    except Exception as _e:
        print(f"❌ Groq failed: {_e}")

print()
print("=" * 60)
print("DEBUG COMPLETE")
print("=" * 60)
