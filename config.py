"""Configuration for CryptoAdvisor LLM."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# === Project paths ===
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "crypto_advisor.db"
PORTFOLIO_PATH = BASE_DIR / "portfolio.json"

# === API Keys ===
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")
CRYPTOPANIC_API_KEY = os.getenv("CRYPTOPANIC_API_KEY", "")
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "CryptoAdvisor/1.0")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
CRYPTOCOMPARE_API_KEY = os.getenv("CRYPTOCOMPARE_API_KEY") or os.getenv("CRYPTO_COMPARE_API_KEY", "")
TAVILY_API_KEY        = os.getenv("TAVILY_API_KEY", "")
NEWSDATA_API_KEY      = os.getenv("NEWSDATA_API_KEY", "")
KRAKEN_API_KEY = os.getenv("KRAKEN_API_KEY", "")
KRAKEN_PRIVATE_KEY = os.getenv("KRAKEN_PRIVATE_KEY", "")
DUNE_API_KEY = os.getenv("DUNE_API_KEY", "")
COIN_MARKET_CAP_API_KEY = os.getenv("COIN_MARKET_CAP_API_KEY", "")
MESSARI_API_KEY = os.getenv("MESSARI_API_KEY", "")
ETHER_SCAN_API_KEY = os.getenv("ETHER_SCAN_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Serial scam / manipulation tracking is now fully automatic.
# See src/agents/coin_risk_assessor.py — real-time detection via news + on-chain signals.
# Cache stored in data/coin_risk_cache.json (24 h TTL).

# === LLM Config (Groq free tier) ===
LLM_MODEL = "llama-3.3-70b-versatile"  # Free on Groq
LLM_MAX_TOKENS = 2000
LLM_TEMPERATURE = 0.1  # Low for consistency

# === Daily Budget Caps (even on free tier, track usage) ===
DAILY_BUDGET_LIMITS = {
    "groq": {"max_calls": 100, "max_tokens": 500_000},  # Free tier limits
}

# === Watchlist ===
WATCHLIST = ["bitcoin", "injective-protocol", "render-token", "polkadot", "ethereum"]
WATCHLIST_SYMBOLS = {"bitcoin": "BTC", "injective-protocol": "INJ", "render-token": "RENDER", "polkadot": "DOT", "ethereum": "ETH"}
# Coins to monitor on every scan (not owned — tracked for buying interest)
WATCHLIST_TRACK = ["render-token", "polkadot"]

# === Revolut X tradeable coins (no public API — maintained manually) ===
REVOLUT_X_COINS = {
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOT", "LINK", "AVAX", "MATIC", "ATOM",
    "UNI", "AAVE", "FIL", "RENDER", "INJ", "NEAR", "APT", "SUI", "SEI", "OP",
    "ARB", "IMX", "PEPE", "DOGE", "SHIB", "LTC", "BCH", "ETC", "XLM", "ALGO",
    "FTM", "MANA", "SAND", "AXS", "ENJ", "CRV", "MKR", "SNX", "COMP", "GRT",
    "BAT", "ZEC", "DASH", "ENS", "LDO", "RPL", "SSV", "PENDLE", "TIA", "JUP",
    "PYTH", "WIF", "BONK", "FLOKI", "FET", "RNDR", "AGIX", "OCEAN",
}

# === Rate Limits (calls per minute) ===
RATE_LIMITS = {
    "coingecko": 20,    # Stay under 30/min limit
    "cryptopanic": 10,
    "reddit": 20,
    "groq": 30,         # Free tier ~30 req/min
}

# === Scheduler Intervals (seconds) ===
SCHEDULE = {
    "price_update": 300,       # 5 min
    "ta_refresh": 900,         # 15 min
    "sentiment_update": 1800,  # 30 min
    "full_analysis": 3600,     # 1 hour
    "polymarket_scan": 14400,  # 4 hours
}
