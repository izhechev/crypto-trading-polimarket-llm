"""
Standalone News Analyst module for debugging and validating news catalysts.
This isolates the Groq news processing logic from the main scanner.
"""

import json
import logging
from typing import Optional, List, Dict, Any

# Setup basic debug logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("news_analyst")

import os
import json
import logging
from typing import Optional, List, Dict, Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("news_analyst")

from src.utils.llm_client import LLMClient

class NewsAnalyst:
    def __init__(self):
        self.llm = LLMClient()

    def analyze_news(self, coin_symbol: str, news_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not news_data:
            return {"verdict": "neutral", "score": 0, "summary": "No news data"}

        # Include age in context so LLM knows recency
        context_lines = []
        for n in news_data:
            age_h = n.get("age_hours")
            age_str = f"{age_h:.0f}h ago" if age_h is not None else "age unknown"
            context_lines.append(
                f"- [{age_str}] Title: {n.get('title')}\n  Snippet: {n.get('snippet', '')}"
            )
        context = "\n".join(context_lines)

        prompt = f"""
        Analyze these crypto news items for {coin_symbol}. Score ONLY based on events that are NEW and RECENT (within the last 7 days).

        CRITICAL RULES:
        - If an article has "age unknown" or is describing a long-standing fact/architecture/integration that has existed for months or years, score it 1-3 (noise).
        - A fact being significant does NOT make it a catalyst. It must be a NEW event announced recently.
        - Examples of things that are NOT catalysts even if important: existing protocol integrations, well-known partnerships, established use cases, background information about how a project works.

        Score scale:
        0: Major Negative (Hack, Delisting, Exploit, Insolvency, Regulatory Ban) — must be a new event.
        1-3: Noise — price predictions, background info, old facts, SEO fluff, evergreen articles.
        4-7: Moderate NEW event — recent development update, new community activity, new non-major partnership.
        8-10: Major NEW Fundamental Catalyst — institutional funding announced THIS WEEK, brand-new Tier-1 exchange listing, new mainnet launch, major new protocol upgrade.

        Respond ONLY in JSON:
        - "verdict": "bullish" | "bearish" | "neutral"
        - "score": (integer 0-10)
        - "catalyst_type": (e.g., "listing", "mainnet", "none", "exploit", "background_info")
        - "summary": (one sentence — state WHY this is or isn't a new catalyst)

        Data:
        {context}
        """

        return self.llm.call(prompt, system_prompt="You are a strict, skeptical crypto fundamental analyst. You never confuse old established facts with new catalysts.")

if __name__ == "__main__":
    # Debug mode test
    import os
    from pathlib import Path
    import sys
    # Add root of project to path
    root_path = Path(__file__).resolve().parent.parent.parent.parent
    sys.path.insert(0, str(root_path))
    import config
    
    analyst = NewsAnalyst(config.GROQ_API_KEY)
    test_news = [{"title": "Bitcoin ETF approved in Hong Kong"}, {"title": "Random price prediction for BTC"}]
    result = analyst.analyze_news("BTC", test_news)
    print(json.dumps(result, indent=2))
