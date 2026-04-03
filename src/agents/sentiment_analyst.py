"""
Sentiment Analyst agent — aggregates Fear & Greed, CryptoPanic, and Reddit
to produce a structured SentimentAnalysis for each coin.

Uses Groq (free) for classification.  No Groq call is made if F&G + news
alone give a clear signal — to preserve the daily call budget.
"""
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
    Simple keyword-based sentiment scorer.
    Returns a float from -1.0 (very bearish) to +1.0 (very bullish).
    """
    if not texts:
        return 0.0
    bull = bear = 0
    for text in texts:
        words = text.lower().split()
        bull += sum(1 for w in words if w in _BULLISH_WORDS)
        bear += sum(1 for w in words if w in _BEARISH_WORDS)
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

def analyze_sentiment(
    symbol: str,
    fear_greed: dict | None = None,
    news_headlines: list[str] | None = None,
) -> SentimentAnalysis:
    """
    Aggregate sentiment for a coin from all available sources.

    Parameters
    ----------
    symbol : str
        Coin ticker (e.g. "BTC", "INJ")
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

    # 4. Score text from news + reddit
    all_texts = (news_headlines or []) + reddit_posts
    text_score = _score_text(all_texts)

    # 5. Combine F&G + text score (weighted)
    #    F&G maps 0-100 → -1.0 to +1.0; weight 40%
    #    Text score: weight 60%
    fg_norm = (fg_value - 50) / 50  # -1.0 to +1.0
    combined = 0.4 * fg_norm + 0.6 * text_score

    # 6. Top narratives (most-mentioned themes)
    narratives: list[str] = []
    all_lower = " ".join(all_texts).lower()
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
        if any(kw in all_lower for kw in keywords):
            narratives.append(theme)

    # 7. Build key insight
    fg_label = fear_greed.get("label", "Neutral")
    n_headlines = len(news_headlines or [])
    n_reddit = len(reddit_posts)
    insight = (
        f"F&G: {fg_value}/100 ({fg_label}). "
        f"{n_headlines} news + {n_reddit} Reddit posts. "
        f"Text sentiment: {text_score:+.2f}. "
        f"Combined: {combined:+.2f} ({_sentiment_label(combined)})."
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
    fear_greed: dict | None = None,
) -> dict[str, SentimentAnalysis]:
    """
    Run sentiment analysis for multiple coins, sharing a single F&G fetch.
    Returns {symbol: SentimentAnalysis}.
    """
    # Share one F&G fetch across all coins
    if fear_greed is None:
        try:
            from src.connectors.coingecko import fetch_fear_greed
            fear_greed = fetch_fear_greed()
        except Exception:
            fear_greed = {"value": 50, "label": "Neutral"}

    results = {}
    for symbol in symbols:
        try:
            results[symbol] = analyze_sentiment(symbol, fear_greed=fear_greed)
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
