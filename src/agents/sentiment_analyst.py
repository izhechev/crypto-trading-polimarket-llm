"""
Sentiment Analyst agent — aggregates Fear & Greed, CryptoPanic, and Reddit
to produce a structured SentimentAnalysis for each coin.

Uses Groq (free) for classification.  No Groq call is made if F&G + news
alone give a clear signal — to preserve the daily call budget.
"""
import re
import sys
import json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config
from src.models.crypto import SentimentAnalysis


# ── Reddit helper (optional — graceful if praw not installed) ─────────────

def _fetch_reddit_posts(symbol: str, subreddits: list[str] | None = None) -> list[str]:
    """
    Fetch recent post titles from relevant crypto subreddits.
    Returns [] if PRAW is not configured or installed.
    """
    if not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
        return []
    try:
        import praw
        reddit = praw.Reddit(
            client_id=config.REDDIT_CLIENT_ID,
            client_secret=config.REDDIT_CLIENT_SECRET,
            user_agent=config.REDDIT_USER_AGENT,
        )
        subs = subreddits or ["CryptoCurrency", "CryptoMarkets"]
        posts = []
        for sub_name in subs:
            try:
                sub = reddit.subreddit(sub_name)
                for post in sub.search(symbol, limit=5, time_filter="week"):
                    posts.append(post.title)
            except Exception:
                continue
        return posts[:15]  # Cap to avoid huge prompts
    except ImportError:
        return []
    except Exception:
        return []


# ── Simple rule-based scorer (no LLM needed for basic cases) ─────────────

_BULLISH_WORDS = {
    "buy", "bullish", "moon", "pump", "breakout", "rally", "surge",
    "accumulate", "undervalued", "gem", "launch", "partnership", "upgrade",
    "adoption", "ATH", "bull", "long", "green",
}
_BEARISH_WORDS = {
    "sell", "bearish", "dump", "crash", "drop", "rug", "scam", "hack",
    "fear", "panic", "short", "red", "collapse", "dead", "rekt", "correction",
    "overvalued", "bubble", "lawsuit", "ban", "regulation",
}


def _score_text(texts: list[str]) -> float:
    """
    Keyword-based sentiment scorer using substring matching (handles conjugations).
    Returns a float from -1.0 (very bearish) to +1.0 (very bullish).
    """
    if not texts:
        return 0.0
    bull = bear = 0
    for text in texts:
        tl = text.lower()
        # Substring match so "surges"/"surged" hits "surge", "crashed" hits "crash", etc.
        bull += sum(1 for kw in _BULLISH_WORDS if kw in tl)
        bear += sum(1 for kw in _BEARISH_WORDS if kw in tl)
    total = bull + bear
    if total == 0:
        return 0.0
    return (bull - bear) / total


def _sentiment_label(score: float) -> str:
    if score >= 0.4:
        return "VERY_BULLISH"
    if score >= 0.15:
        return "BULLISH"
    if score <= -0.4:
        return "VERY_BEARISH"
    if score <= -0.15:
        return "BEARISH"
    return "NEUTRAL"


# ── Main analysis function ────────────────────────────────────────────────

def _relevance_terms(symbol: str, coin_name: str) -> set[str]:
    """Build the set of lowercase strings a headline must contain to be coin-relevant."""
    try:
        from src.agents.scanner import _COIN_ALIASES
        aliases = _COIN_ALIASES.get(symbol.upper(), [])
    except Exception:
        aliases = []
    terms = {symbol.lower()}
    if coin_name:
        terms.add(coin_name.lower())
    for a in aliases:
        terms.add(a.lower())
    return terms


def _matches_term(text_low: str, term: str) -> bool:
    """
    Word-boundary match: 'fil' must appear as a standalone word, not inside
    'filed', 'profile', 'profitable', etc.
    Multi-word terms (e.g. 'near protocol') are matched as a phrase.
    """
    return bool(re.search(r'\b' + re.escape(term) + r'\b', text_low))


def _filter_relevant(headlines: list[str], terms: set[str]) -> list[str]:
    """Return only headlines that mention the coin by ticker, name, or alias (word-boundary match)."""
    return [h for h in headlines if any(_matches_term(h.lower(), t) for t in terms)]


def analyze_sentiment(
    symbol: str,
    coin_name: str = "",
    fear_greed: dict | None = None,
    news_headlines: list[str] | None = None,
) -> SentimentAnalysis:
    """
    Aggregate sentiment for a coin from all available sources.

    Only headlines that mention the coin's ticker, full name, or a known alias
    are scored. Generic crypto headlines ("Bitcoin pares gains") are ignored.
    If no coin-specific headlines are found, sentiment is NEUTRAL — F&G alone
    does not determine a coin's sentiment.

    Parameters
    ----------
    symbol : str
        Coin ticker (e.g. "BTC", "INJ")
    coin_name : str
        CoinGecko full name (e.g. "Bittensor") — used for relevance filtering.
    fear_greed : dict | None
        Pre-fetched F&G dict (value, label). Fetched if not provided.
    news_headlines : list[str] | None
        Pre-fetched headlines from CryptoPanic. Fetched if not provided.

    Returns
    -------
    SentimentAnalysis (Pydantic model)
    """
    # 1. Fear & Greed
    if fear_greed is None:
        try:
            from src.connectors.coingecko import fetch_fear_greed
            fear_greed = fetch_fear_greed()
        except Exception:
            fear_greed = {"value": 50, "label": "Neutral"}

    fg_value: int = fear_greed.get("value", 50)

    # 2. News headlines
    if news_headlines is None:
        try:
            from src.connectors.cryptopanic import fetch_news
            news = fetch_news([symbol])
            news_headlines = [n.get("title", "") for n in news if n.get("title")]
        except Exception:
            news_headlines = []

    # 3. Reddit posts (optional)
    reddit_posts = _fetch_reddit_posts(symbol)

    # 4. Relevance filter — only score headlines about THIS coin
    rel_terms       = _relevance_terms(symbol, coin_name)
    relevant_news   = _filter_relevant(news_headlines or [], rel_terms)
    relevant_reddit = _filter_relevant(reddit_posts, rel_terms)
    has_relevant    = bool(relevant_news or relevant_reddit)

    # 5. Score only relevant text
    all_relevant = relevant_news + relevant_reddit
    text_score   = _score_text(all_relevant)

    # 5b. Headline-level cross-check (temporary fix).
    #     _score_text uses broad sentiment words ("bearish", "decline", "risk")
    #     that appear in price-prediction articles even when no real signal exists.
    #     Cross-check with the scanner's headline keyword sets: if NEITHER a catalyst
    #     action word NOR a bearish signal word appears in any relevant headline,
    #     the text_score is noise — force it to 0.0 (NEUTRAL).
    if text_score < 0 and all_relevant:
        try:
            from src.agents.scanner import _NEWS_CATALYST_ACTIONS, _NEWS_BEARISH
            hl_catalyst = sum(
                1 for h in all_relevant
                if any(w in h.lower() for w in _NEWS_CATALYST_ACTIONS)
                and not any(w in h.lower() for w in _NEWS_BEARISH)
            )
            hl_bearish = sum(
                1 for h in all_relevant
                if any(w in h.lower() for w in _NEWS_BEARISH)
            )
            if hl_catalyst == 0 and hl_bearish == 0:
                text_score = 0.0
        except Exception:
            pass

    # 6. Combine scores.
    #    If NO coin-specific headlines exist, F&G is a market-wide indicator and
    #    should NOT override the coin's sentiment — return NEUTRAL.
    #    Only when we have real coin news do we blend F&G (40%) + news (60%).
    if has_relevant:
        fg_norm  = (fg_value - 50) / 50  # -1.0 to +1.0
        combined = 0.4 * fg_norm + 0.6 * text_score
    else:
        combined = 0.0  # no coin-specific data → NEUTRAL

    # 7. Top narratives (from relevant text only)
    narratives: list[str] = []
    rel_lower = " ".join(all_relevant).lower()
    themes = {
        "ETF": ["etf", "spot etf", "bitcoin etf"],
        "Regulation": ["sec", "regulation", "ban", "law", "compliance"],
        "Partnership": ["partnership", "integration", "collaboration"],
        "Upgrade": ["upgrade", "mainnet", "v2", "update"],
        "Adoption": ["adoption", "institutional", "enterprise"],
        "DeFi": ["defi", "liquidity", "yield", "tvl"],
        "Sell-off": ["dump", "selling", "sell-off", "correction"],
        "Whale": ["whale", "large transfer", "accumulation"],
    }
    for theme, keywords in themes.items():
        if any(kw in rel_lower for kw in keywords):
            narratives.append(theme)

    # 8. Build key insight
    fg_label = fear_greed.get("label", "Neutral")
    if has_relevant:
        insight = (
            f"F&G: {fg_value}/100 ({fg_label}). "
            f"{len(relevant_news)} relevant news + {len(relevant_reddit)} Reddit posts "
            f"(filtered from {len(news_headlines or [])} total). "
            f"Text sentiment: {text_score:+.2f}. "
            f"Combined: {combined:+.2f} ({_sentiment_label(combined)})."
        )
    else:
        insight = (
            f"F&G: {fg_value}/100 ({fg_label}). "
            f"No coin-specific headlines found ({len(news_headlines or [])} general headlines filtered out). "
            f"Defaulting to NEUTRAL — F&G alone does not determine coin sentiment."
        )
    if narratives:
        insight += f" Key themes: {', '.join(narratives)}."

    return SentimentAnalysis(
        asset=symbol,
        fear_greed=fg_value,
        social_sentiment=_sentiment_label(combined),
        news_sentiment=round(text_score, 3),
        top_narratives=narratives[:5],
        key_insight=insight,
    )


def analyze_sentiment_batch(
    symbols: list[str],
    coin_names: list[str] | None = None,
    fear_greed: dict | None = None,
) -> dict[str, SentimentAnalysis]:
    """
    Run sentiment analysis for multiple coins, sharing a single F&G fetch.
    Returns {symbol: SentimentAnalysis}.

    Parameters
    ----------
    symbols : list[str]
        Coin tickers in the same order as coin_names.
    coin_names : list[str] | None
        CoinGecko full names (parallel to symbols). Used for relevance filtering.
    fear_greed : dict | None
        Pre-fetched F&G. Fetched once and shared if not provided.
    """
    # Share one F&G fetch across all coins
    if fear_greed is None:
        try:
            from src.connectors.coingecko import fetch_fear_greed
            fear_greed = fetch_fear_greed()
        except Exception:
            fear_greed = {"value": 50, "label": "Neutral"}

    names_map = {}
    if coin_names:
        names_map = {sym.upper(): name for sym, name in zip(symbols, coin_names)}

    results = {}
    for symbol in symbols:
        try:
            results[symbol] = analyze_sentiment(
                symbol,
                coin_name=names_map.get(symbol.upper(), ""),
                fear_greed=fear_greed,
            )
        except Exception as e:
            print(f"  [sentiment] {symbol} failed: {e}")
    return results


def format_for_prompt(sentiments: dict[str, SentimentAnalysis]) -> str:
    """Format batch sentiment data for inclusion in an LLM prompt."""
    if not sentiments:
        return ""
    lines = ["SENTIMENT ANALYSIS:"]
    for sym, s in sentiments.items():
        lines.append(
            f"  {sym}: {s.social_sentiment} | F&G {s.fear_greed}/100 "
            f"| news_score {s.news_sentiment:+.2f}"
        )
        if s.top_narratives:
            lines.append(f"    themes: {', '.join(s.top_narratives)}")
    return "\n".join(lines)


if __name__ == "__main__":
    # Quick test
    from src.connectors.coingecko import fetch_fear_greed
    fg = fetch_fear_greed()
    for sym in ["BTC", "ETH", "INJ"]:
        s = analyze_sentiment(sym, fear_greed=fg)
        print(f"\n{sym}: {s.social_sentiment} | F&G {s.fear_greed} | {s.key_insight}")
