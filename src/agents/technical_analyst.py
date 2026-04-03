"""Technical Analysis using pandas-ta. Pure Python, no C deps."""
import pandas as pd
import pandas_ta as ta
from src.models.crypto import TechnicalAnalysis


def compute_ta(coin_id: str, symbol: str, ohlcv_data: list[dict]) -> TechnicalAnalysis:
    """Compute technical indicators from OHLCV data."""
    if len(ohlcv_data) < 20:
        return TechnicalAnalysis(
            asset=symbol,
            price=ohlcv_data[-1]["close"] if ohlcv_data else 0,
            trend="NEUTRAL",
            key_observation="Insufficient data for TA",
        )

    df = pd.DataFrame(ohlcv_data)
    df.columns = [c.lower() for c in df.columns]

    # Current price
    price = df["close"].iloc[-1]

    # RSI
    rsi_series = ta.rsi(df["close"], length=14)
    rsi = float(rsi_series.iloc[-1]) if rsi_series is not None and not rsi_series.empty else None

    # MACD
    macd_df = ta.macd(df["close"])
    macd_signal = "NEUTRAL"
    if macd_df is not None and not macd_df.empty:
        macd_line = macd_df.iloc[-1, 0]
        signal_line = macd_df.iloc[-1, 2]
        if macd_line > signal_line:
            macd_signal = "BULLISH"
        elif macd_line < signal_line:
            macd_signal = "BEARISH"

    # Bollinger Bands
    bbands = ta.bbands(df["close"], length=20, std=2)
    bb_position = "MIDDLE"
    if bbands is not None and not bbands.empty:
        upper = bbands.iloc[-1, 2]  # BBU
        lower = bbands.iloc[-1, 0]  # BBL
        if price > upper:
            bb_position = "ABOVE_UPPER"
        elif price < lower:
            bb_position = "BELOW_LOWER"

    # EMAs
    ema_20 = ta.ema(df["close"], length=20)
    ema_50 = ta.ema(df["close"], length=50) if len(df) >= 50 else None
    ema_200 = ta.ema(df["close"], length=200) if len(df) >= 200 else None

    ema_20_val = float(ema_20.iloc[-1]) if ema_20 is not None and not ema_20.empty else None
    ema_50_val = float(ema_50.iloc[-1]) if ema_50 is not None and not ema_50.empty else None
    ema_200_val = float(ema_200.iloc[-1]) if ema_200 is not None and not ema_200.empty else None

    # Support / Resistance (simple: recent lows/highs)
    recent = df.tail(20)
    supports = sorted(recent["low"].nsmallest(3).tolist())
    resistances = sorted(recent["high"].nlargest(3).tolist(), reverse=True)

    # Determine trend
    bullish_signals = 0
    bearish_signals = 0

    if rsi and rsi < 30:
        bullish_signals += 1  # oversold = potential reversal up
    elif rsi and rsi > 70:
        bearish_signals += 1

    if macd_signal == "BULLISH":
        bullish_signals += 1
    elif macd_signal == "BEARISH":
        bearish_signals += 1

    if bb_position == "BELOW_LOWER":
        bullish_signals += 1  # oversold
    elif bb_position == "ABOVE_UPPER":
        bearish_signals += 1

    if ema_20_val and price > ema_20_val:
        bullish_signals += 1
    elif ema_20_val and price < ema_20_val:
        bearish_signals += 1

    if bullish_signals > bearish_signals:
        trend = "BULLISH"
    elif bearish_signals > bullish_signals:
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"

    confidence = max(bullish_signals, bearish_signals) / 4.0

    # Key observation
    observations = []
    if rsi:
        if rsi < 30:
            observations.append(f"RSI oversold ({rsi:.0f})")
        elif rsi > 70:
            observations.append(f"RSI overbought ({rsi:.0f})")
        else:
            observations.append(f"RSI neutral ({rsi:.0f})")

    observations.append(f"MACD {macd_signal.lower()}")
    observations.append(f"Bollinger {bb_position.lower().replace('_', ' ')}")

    return TechnicalAnalysis(
        asset=symbol,
        price=price,
        trend=trend,
        rsi_14=round(rsi, 1) if rsi else None,
        macd_signal=macd_signal,
        bollinger_position=bb_position,
        ema_20=round(ema_20_val, 4) if ema_20_val else None,
        ema_50=round(ema_50_val, 4) if ema_50_val else None,
        ema_200=round(ema_200_val, 4) if ema_200_val else None,
        support_levels=[round(s, 4) for s in supports],
        resistance_levels=[round(r, 4) for r in resistances],
        key_observation=". ".join(observations),
        confidence=round(confidence, 2),
    )
