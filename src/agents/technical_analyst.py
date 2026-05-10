"""Technical Analysis with pandas-ta (with pure-pandas fallback)."""
import pandas as pd
import numpy as np
try:
    import pandas_ta as ta
    HAS_PTA = True
except ImportError:
    HAS_PTA = False
from src.models.crypto import TechnicalAnalysis


# ── Pure-pandas fallback helpers (no C deps) ────────────────────────
def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()

def _rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/length, min_periods=length).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/length, min_periods=length).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _macd(series: pd.Series):
    fast = _ema(series, 12)
    slow = _ema(series, 26)
    macd_line = fast - slow
    signal = _ema(macd_line, 9)
    hist = macd_line - signal
    return pd.DataFrame({"MACD": macd_line, "MACDh": hist, "MACDs": signal})

def _bbands(series: pd.Series, length: int = 20, std: float = 2):
    mid = series.rolling(length).mean()
    s = series.rolling(length).std()
    return pd.DataFrame({"BBL": mid - std * s, "BBM": mid, "BBU": mid + std * s})


def compute_ta(coin_id: str, symbol: str, ohlcv_data: list[dict]) -> TechnicalAnalysis:
    """Compute technical indicators from OHLCV data with Long/Short signals."""
    if len(ohlcv_data) < 20:
        return TechnicalAnalysis(
            asset=symbol,
            price=ohlcv_data[-1]["close"] if ohlcv_data else 0,
            trend="NEUTRAL",
            recommended_order="NONE",
            key_observation="Insufficient data for TA",
        )

    df = pd.DataFrame(ohlcv_data)
    df.columns = [c.lower() for c in df.columns]

    # Current price
    price = df["close"].iloc[-1]

    # RSI
    rsi_fn = ta.rsi if HAS_PTA else _rsi
    rsi_series = rsi_fn(df["close"], length=14)
    rsi = float(rsi_series.iloc[-1]) if rsi_series is not None and not rsi_series.empty else None

    # MACD
    macd_fn = ta.macd if HAS_PTA else _macd
    macd_df = macd_fn(df["close"])
    macd_signal = "NEUTRAL"
    macd_line_val = 0
    if macd_df is not None and not macd_df.empty:
        macd_line = macd_df.iloc[-1, 0]
        signal_line = macd_df.iloc[-1, 2]
        macd_line_val = macd_line
        if macd_line > signal_line:
            macd_signal = "BULLISH"
        elif macd_line < signal_line:
            macd_signal = "BEARISH"

    # Bollinger Bands
    bbands_fn = ta.bbands if HAS_PTA else _bbands
    bbands = bbands_fn(df["close"], length=20, std=2)
    bb_position = "MIDDLE"
    if bbands is not None and not bbands.empty:
        upper = bbands.iloc[-1, 2]  # BBU
        lower = bbands.iloc[-1, 0]  # BBL
        if price > upper:
            bb_position = "ABOVE_UPPER"
        elif price < lower:
            bb_position = "BELOW_LOWER"

    # EMAs
    ema_fn = ta.ema if HAS_PTA else _ema
    ema_20 = ema_fn(df["close"], length=20)
    ema_50 = ema_fn(df["close"], length=50) if len(df) >= 50 else None
    ema_200 = ema_fn(df["close"], length=200) if len(df) >= 200 else None

    ema_20_val = float(ema_20.iloc[-1]) if ema_20 is not None and not ema_20.empty else None
    ema_50_val = float(ema_50.iloc[-1]) if ema_50 is not None and not ema_50.empty else None
    ema_200_val = float(ema_200.iloc[-1]) if ema_200 is not None and not ema_200.empty else None

    # ADX (Trend Strength)
    adx_val = None
    if HAS_PTA and len(df) >= 27:
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        if adx_df is not None and not adx_df.empty:
            adx_val = float(adx_df.iloc[-1, 0]) # ADX_14

    # Ichimoku Cloud (simple check)
    ichimoku_verdict = "NEUTRAL"
    if HAS_PTA and len(df) >= 52:
        ichimoku_df, _ = ta.ichimoku(df["high"], df["low"], df["close"])
        if ichimoku_df is not None and not ichimoku_df.empty:
            span_a = ichimoku_df.iloc[-1, 0] # ISA_9
            span_b = ichimoku_df.iloc[-1, 1] # ISB_26
            if price > span_a and price > span_b: ichimoku_verdict = "BULLISH"
            elif price < span_a and price < span_b: ichimoku_verdict = "BEARISH"

    # RSI Divergence check (simple: last 10 candles)
    rsi_div = "NONE"
    if rsi_series is not None and len(df) >= 10:
        recent_prices = df["close"].tail(10).tolist()
        recent_rsi = rsi_series.tail(10).tolist()
        
        # Bullish Divergence: Price Lower Low, RSI Higher Low
        if recent_prices[-1] < min(recent_prices[:-1]) and recent_rsi[-1] > min(recent_rsi[:-1]):
            rsi_div = "BULLISH"
        # Bearish Divergence: Price Higher High, RSI Lower High
        elif recent_prices[-1] > max(recent_prices[:-1]) and recent_rsi[-1] < max(recent_rsi[:-1]):
            rsi_div = "BEARISH"

    # Determine trend and order
    bullish_score = 0
    bearish_score = 0

    # Trend (EMA 200 is strong bias)
    if ema_200_val:
        if price > ema_200_val: bullish_score += 2
        else: bearish_score += 2
    
    # Ichimoku confirmation
    if ichimoku_verdict == "BULLISH": bullish_score += 2
    elif ichimoku_verdict == "BEARISH": bearish_score += 2

    # Momentum
    if rsi:
        if rsi < 35: bullish_score += 2 # Oversold
        elif rsi > 65: bearish_score += 2 # Overbought
        
        if 45 < rsi < 55: pass # Neutral zone
        elif rsi > 50: bullish_score += 1
        else: bearish_score += 1

    if macd_signal == "BULLISH":
        bullish_score += 2
        if macd_line_val < 0: bullish_score += 1 # Cross below zero is stronger
    elif macd_signal == "BEARISH":
        bearish_score += 2
        if macd_line_val > 0: bearish_score += 1 # Cross above zero is stronger

    # Trend Strength (ADX)
    if adx_val and adx_val > 25:
        if bullish_score > bearish_score: bullish_score += 2 # Stronger bullish
        elif bearish_score > bullish_score: bearish_score += 2 # Stronger bearish

    if rsi_div == "BULLISH": bullish_score += 3
    elif rsi_div == "BEARISH": bearish_score += 3

    if bb_position == "BELOW_LOWER": bullish_score += 2
    elif bb_position == "ABOVE_UPPER": bearish_score += 2

    # Final Verdict
    recommended_order = "NONE"
    if bullish_score >= 10: recommended_order = "LONG"
    elif bearish_score >= 10: recommended_order = "SHORT"
    elif bullish_score >= 5: recommended_order = "SPOT"

    if bullish_score > bearish_score:
        trend = "BULLISH"
    elif bearish_score > bullish_score:
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"

    confidence = max(bullish_score, bearish_score) / 20.0 # Max possible ~20

    # Key observation
    observations = []
    if rsi_div != "NONE": observations.append(f"{rsi_div} RSI divergence")
    if adx_val and adx_val > 25: observations.append(f"strong trend (ADX {adx_val:.0f})")
    if ichimoku_verdict != "NEUTRAL": observations.append(f"Ichimoku {ichimoku_verdict.lower()}")
    if ema_200_val: observations.append("above 200 EMA" if price > ema_200_val else "below 200 EMA")
    observations.append(f"MACD {macd_signal.lower()}")
    observations.append(f"Bollinger {bb_position.lower().replace('_', ' ')}")

    return TechnicalAnalysis(
        asset=symbol,
        price=price,
        trend=trend,
        recommended_order=recommended_order,
        rsi_14=round(rsi, 1) if rsi else None,
        macd_signal=macd_signal,
        bollinger_position=bb_position,
        ema_20=round(ema_20_val, 4) if ema_20_val else None,
        ema_50=round(ema_50_val, 4) if ema_50_val else None,
        ema_200=round(ema_200_val, 4) if ema_200_val else None,
        support_levels=[], # Simplified for now
        resistance_levels=[],
        key_observation=". ".join(observations),
        confidence=round(confidence, 2),
    )
