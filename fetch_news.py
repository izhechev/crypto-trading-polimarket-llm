
import sys
import argparse
from pathlib import Path
from datetime import datetime

# Setup paths
root_path = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(root_path))

import config
from src.agents.news_analyst.analyst import NewsAnalyst
from src.connectors.web_research import fetch_news_for_coins

def calculate_score(sentiment_data, ta_data=None):
    """
    Score logic:
    Base: 50
    Catalyst: +20 (if bullish)
    Trend: +15
    TA: +10 per signal (max 30)
    Penalties: -20 to -30
    """
    score = 50
    reasons = []

    # Catalyst (News)
    if sentiment_data.get("verdict") == "bullish":
        news_score = sentiment_data.get("score", 0) * 2 # Scale 0-10 to max 20
        score += min(news_score, 20)
        reasons.append(f"Catalyst: {sentiment_data['catalyst_type']} (+{min(news_score, 20)})")

    # TODO: Integration with TA data here
    # Placeholder TA points
    if ta_data:
        # Example TA logic
        if ta_data.get("rsi_bullish"): score += 10
        if ta_data.get("macd_bullish"): score += 10
        if ta_data.get("bb_bullish"): score += 10
        reasons.append("TA Signals applied")

    # Penalty for bearish sentiment
    if sentiment_data.get("verdict") == "bearish":
        score -= 25
        reasons.append("Bearish News Penalty (-25)")

    return score, reasons

def main():
    parser = argparse.ArgumentParser(description="Fetch and score news for a coin.")
    parser.add_argument("symbol", help="Coin symbol (e.g., BTC)")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    print(f"Analyzing {symbol}...")

    # 1. Fetch News (skip Tavily since it's failing)
    # Force use of RSS fallback by passing an empty key or bypassing Tavily
    news = fetch_news_for_coins([{ "symbol": symbol }], limit_per_coin=5).get(symbol, [])
    if not news:
        from src.connectors.web_research import search_google_news
        news = search_google_news(f"{symbol} crypto news", limit=5)
    
    # 2. Analyze News
    analyst = NewsAnalyst(config.GROQ_API_KEY)
    sentiment = analyst.analyze_news(symbol, news)
    
    # 3. Score
    score, reasons = calculate_score(sentiment)
    
    print("\n--- RESULTS ---")
    print(f"Sentiment: {sentiment.get('verdict')}")
    print(f"Summary: {sentiment.get('summary')}")
    print(f"Final Score: {score}")
    print(f"Reasons: {reasons}")

if __name__ == "__main__":
    main()
