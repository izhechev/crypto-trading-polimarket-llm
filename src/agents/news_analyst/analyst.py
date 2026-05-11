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

        headlines = "\n".join([f"- {n.get('title')}" for n in news_data])
        prompt = f"""
        Analyze these news headlines for {coin_symbol} and determine if they represent a real trade catalyst (partnerships, mainnet, etc.).
        Respond in JSON with: "verdict" (bullish/bearish/neutral), "score" (0-10), "catalyst_type", "summary".
        
        Headlines:
        {headlines}
        """
        
        return self.llm.call(prompt, system_prompt="You are a crypto news analyst.")

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
