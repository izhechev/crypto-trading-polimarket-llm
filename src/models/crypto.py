"""Data models for CryptoAdvisor."""
from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, Field


# === Price & Market Data ===

class CryptoPrice(BaseModel):
    coin_id: str
    symbol: str
    name: str
    price_usd: float
    price_eur: float
    market_cap: float
    volume_24h: float
    change_24h: float
    change_7d: float
    change_30d: Optional[float] = None
    ath: Optional[float] = None
    ath_change_pct: Optional[float] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class OHLCVData(BaseModel):
    coin_id: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


# === Technical Analysis ===

class TechnicalAnalysis(BaseModel):
    asset: str
    price: float
    trend: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    recommended_order: Literal["LONG", "SHORT", "NONE"] = "NONE"
    rsi_14: Optional[float] = None
    macd_signal: Optional[Literal["BULLISH", "BEARISH", "NEUTRAL"]] = None
    bollinger_position: Optional[Literal["ABOVE_UPPER", "MIDDLE", "BELOW_LOWER"]] = None
    ema_20: Optional[float] = None
    ema_50: Optional[float] = None
    ema_200: Optional[float] = None
    support_levels: list[float] = Field(default_factory=list)
    resistance_levels: list[float] = Field(default_factory=list)
    key_observation: str = ""
    confidence: float = 0.5


# === Sentiment ===

class FearGreedIndex(BaseModel):
    value: int  # 0-100
    label: str  # "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SentimentAnalysis(BaseModel):
    asset: str
    fear_greed: Optional[int] = None
    social_sentiment: Literal["VERY_BEARISH", "BEARISH", "NEUTRAL", "BULLISH", "VERY_BULLISH"] = "NEUTRAL"
    news_sentiment: float = 0.0  # -1.0 to 1.0
    top_narratives: list[str] = Field(default_factory=list)
    key_insight: str = ""


# === Polymarket ===

class PolymarketEvent(BaseModel):
    event_id: str
    title: str
    description: str
    outcomes: list[str]
    odds: dict[str, float]  # outcome -> probability
    volume: float
    liquidity: float
    end_date: Optional[datetime] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class PolymarketShift(BaseModel):
    event_id: str
    title: str
    outcome: str
    old_odds: float
    new_odds: float
    shift_pct: float  # absolute change
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# === Trading Signals ===

class TradingSignal(BaseModel):
    asset: str
    action: Literal["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"]
    confidence: float = Field(ge=0.0, le=1.0)
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    timeframe: str = "1-7 days"
    reasoning: str = ""
    bull_case: str = ""
    bear_case: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# === LLM Call Logging ===

class LLMCallLog(BaseModel):
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float  # 0 for free tier
    endpoint: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
