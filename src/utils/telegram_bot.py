"""
Telegram command bot — long-polls for commands and replies with live data.
Runs as a daemon thread; start with start_bot_thread() during --schedule mode.

Commands:
  /portfolio                         — show current holdings with live prices
  /update_portfolio COIN AMOUNT [ENTRY_USD]
                                     — update amount (and optionally entry price)
                                       for a coin in portfolio.json
                                       Example: /update_portfolio INJ 28 2.85
  /price SYMBOL                      — current price + basic TA for any coin
                                       Example: /price BTC
  /fear                              — Fear & Greed Index
  /polymarket                        — top Polymarket crypto prediction odds
  /analyze SYMBOL                    — full scanner-style analysis for one coin
                                       Example: /analyze ETH
"""
import json
import threading
import time
import httpx
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

_offset: int = 0
_running: bool = False


# ── HTTP helpers ──────────────────────────────────────────────────────────

def _get_updates(offset: int) -> list[dict]:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        with httpx.Client(timeout=35) as client:
            resp = client.get(url, params={"offset": offset, "timeout": 30})
            resp.raise_for_status()
            return resp.json().get("result", [])
    except Exception:
        return []


def _reply(chat_id: int, text: str) -> None:
    from src.utils.telegram import send_telegram_to
    send_telegram_to(chat_id, text)


# ── Symbol → coin_id resolver ─────────────────────────────────────────────

def _symbol_to_coin_id(symbol: str) -> str | None:
    """
    Resolve a ticker symbol (e.g. "BTC") to a CoinGecko coin_id.
    Checks watchlist first, then Kraken's known mapping.
    """
    symbol = symbol.upper()
    # Check watchlist
    for cid, sym in config.WATCHLIST_SYMBOLS.items():
        if sym.upper() == symbol:
            return cid
    # Check Kraken mapping (broadest source)
    try:
        from src.connectors.kraken import _COIN_IDS
        if symbol in _COIN_IDS:
            return _COIN_IDS[symbol]
    except Exception:
        pass
    # Fallback: lowercase symbol as coin_id (works for many CoinGecko IDs)
    return symbol.lower()


# ── Command handlers ──────────────────────────────────────────────────────

def _handle_portfolio(chat_id: int) -> None:
    """Reply with current portfolio holdings and live EUR prices."""
    try:
        from src.connectors.kraken import fetch_kraken_portfolio
        from src.connectors.coingecko import fetch_prices

        holdings, source = fetch_kraken_portfolio()
        if holdings is None:
            with open(config.PORTFOLIO_PATH) as f:
                pf = json.load(f)
            holdings = pf.get("holdings", [])
            source = "portfolio.json"

        coin_ids = [h["coin_id"] for h in holdings if h.get("coin_id")]
        if not coin_ids:
            _reply(chat_id, "No holdings found.")
            return

        prices = {p.coin_id: p for p in fetch_prices(coin_ids)}
        DUST = 0.10

        lines = [f"<b>PORTFOLIO</b> (source: {source})\n"]
        total_eur = 0.0
        for h in holdings:
            p = prices.get(h.get("coin_id", ""))
            if not p:
                continue
            amt = h["amount"]
            eur_value = amt * p.price_eur
            if eur_value < DUST:
                lines.append(f"[dust] {h['asset']}  €{eur_value:.4f}")
                continue
            entry = h.get("entry_price_usd")
            if entry:
                pnl_pct = (p.price_usd - entry) / entry * 100
                pnl_str = f"  P&amp;L: {pnl_pct:+.1f}%"
            else:
                pnl_str = ""
            decimals = 2 if p.price_eur >= 1 else 4 if p.price_eur >= 0.01 else 6 if p.price_eur >= 0.0001 else 8
            lines.append(
                f"<b>{h['asset']}</b>  {amt:.4f} × €{p.price_eur:.{decimals}f}"
                f" = €{eur_value:.2f}{pnl_str}"
            )
            total_eur += eur_value

        lines.append(f"\n<b>TOTAL: €{total_eur:.2f}</b>")
        _reply(chat_id, "\n".join(lines))

    except Exception as e:
        _reply(chat_id, f"Error fetching portfolio: {e}")


def _handle_update_portfolio(chat_id: int, args: list[str]) -> None:
    """
    /update_portfolio COIN AMOUNT [ENTRY_USD]
    Updates or adds a holding in portfolio.json.
    """
    if len(args) < 2:
        _reply(
            chat_id,
            "Usage: /update_portfolio COIN AMOUNT [ENTRY_USD]\n"
            "Example: /update_portfolio INJ 28 2.85\n\n"
            "To remove a coin set AMOUNT to 0."
        )
        return

    coin = args[0].upper()
    try:
        amount = float(args[1])
    except ValueError:
        _reply(chat_id, f"Invalid amount: {args[1]}")
        return

    entry: float | None = None
    if len(args) >= 3:
        try:
            entry = float(args[2])
        except ValueError:
            _reply(chat_id, f"Invalid entry price: {args[2]}")
            return

    try:
        with open(config.PORTFOLIO_PATH) as f:
            pf = json.load(f)

        holdings = pf.get("holdings", [])

        if amount == 0:
            pf["holdings"] = [h for h in holdings if h["asset"].upper() != coin]
            with open(config.PORTFOLIO_PATH, "w") as f:
                json.dump(pf, f, indent=4)
            _reply(chat_id, f"Removed {coin} from portfolio.")
            print(f"  Portfolio: removed {coin} via Telegram")
            return

        updated = False
        for h in holdings:
            if h["asset"].upper() == coin:
                h["amount"] = amount
                if entry is not None:
                    h["entry_price_usd"] = entry
                updated = True
                break

        if not updated:
            from src.connectors.kraken import _COIN_IDS
            coin_id = _COIN_IDS.get(coin, coin.lower())
            holdings.append({
                "asset":           coin,
                "coin_id":         coin_id,
                "amount":          amount,
                "entry_price_usd": entry,
                "entry_date":      date.today().isoformat(),
            })
            pf["holdings"] = holdings

        with open(config.PORTFOLIO_PATH, "w") as f:
            json.dump(pf, f, indent=4)

        msg = f"Updated {coin}: {amount} units"
        if entry is not None:
            msg += f" @ ${entry:.4f}"
        _reply(chat_id, msg)
        print(f"  Portfolio updated via Telegram: {coin} × {amount}"
              + (f" @ ${entry:.4f}" if entry else ""))

    except Exception as e:
        _reply(chat_id, f"Error updating portfolio: {e}")


def _handle_price(chat_id: int, args: list[str]) -> None:
    """
    /price SYMBOL — fetch live price + basic TA for any coin.
    Example: /price BTC
    """
    if not args:
        _reply(chat_id, "Usage: /price SYMBOL\nExample: /price BTC")
        return

    symbol = args[0].upper()
    coin_id = _symbol_to_coin_id(symbol)
    if not coin_id:
        _reply(chat_id, f"Unknown symbol: {symbol}")
        return

    try:
        from src.connectors.coingecko import fetch_prices, fetch_ohlcv
        from src.agents.technical_analyst import compute_ta

        prices = fetch_prices([coin_id])
        if not prices:
            _reply(chat_id, f"Could not fetch price for {symbol}. Check the symbol.")
            return

        p = prices[0]
        decimals = 2 if p.price_eur >= 1 else 4

        # Try to get TA
        ta_lines = []
        try:
            ohlcv = fetch_ohlcv(coin_id, days=30)
            if ohlcv and len(ohlcv) >= 20:
                ta = compute_ta(coin_id, symbol, ohlcv)
                trend_icon = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(ta.trend, "❓")
                ta_lines = [
                    f"\n<b>Technical Analysis</b>",
                    f"Trend: {trend_icon} {ta.trend} ({ta.confidence:.0%} confidence)",
                ]
                if ta.rsi_14:
                    rsi_label = "oversold" if ta.rsi_14 < 30 else "overbought" if ta.rsi_14 > 70 else "neutral"
                    ta_lines.append(f"RSI(14): {ta.rsi_14:.1f} ({rsi_label})")
                if ta.macd_signal:
                    ta_lines.append(f"MACD: {ta.macd_signal}")
                if ta.bollinger_position:
                    ta_lines.append(f"Bollinger: {ta.bollinger_position.replace('_', ' ').lower()}")
                if ta.support_levels:
                    ta_lines.append(f"Support: ${ta.support_levels[0]:.{decimals}f}")
                if ta.resistance_levels:
                    ta_lines.append(f"Resistance: ${ta.resistance_levels[0]:.{decimals}f}")
                ta_lines.append(f"\n<i>{ta.key_observation}</i>")
        except Exception:
            pass

        arrow = "↑" if p.change_24h > 0 else "↓" if p.change_24h < 0 else "→"
        msg = (
            f"<b>{p.symbol} — {p.name}</b>\n\n"
            f"Price:  €{p.price_eur:.{decimals}f}  (${p.price_usd:.{decimals}f})\n"
            f"24h:    {arrow} {p.change_24h:+.1f}%\n"
            f"7d:     {p.change_7d:+.1f}%\n"
            f"MCap:   €{p.market_cap / 1e6:.0f}M"
        )
        if ta_lines:
            msg += "\n" + "\n".join(ta_lines)

        _reply(chat_id, msg)

    except Exception as e:
        _reply(chat_id, f"Error fetching price for {symbol}: {e}")


def _handle_fear(chat_id: int) -> None:
    """
    /fear — show the current Fear & Greed Index.
    """
    try:
        from src.connectors.coingecko import fetch_fear_greed
        fg = fetch_fear_greed()
        value = fg["value"]
        label = fg["label"]

        # Build ASCII bar (50 chars wide)
        bar_len = value // 2
        bar = "█" * bar_len + "░" * (50 - bar_len)

        if value <= 20:
            emoji = "😱"
        elif value <= 40:
            emoji = "😨"
        elif value <= 60:
            emoji = "😐"
        elif value <= 80:
            emoji = "😄"
        else:
            emoji = "🤑"

        msg = (
            f"<b>Fear &amp; Greed Index</b>\n\n"
            f"{emoji} <b>{value}/100 — {label}</b>\n\n"
            f"<code>[{bar}]</code>"
        )
        _reply(chat_id, msg)
    except Exception as e:
        _reply(chat_id, f"Error fetching Fear &amp; Greed: {e}")


def _handle_polymarket(chat_id: int) -> None:
    """
    /polymarket — show top crypto prediction market odds.
    """
    try:
        from src.connectors.polymarket import fetch_crypto_markets
        markets = fetch_crypto_markets(limit=10)

        if not markets:
            _reply(chat_id, "No Polymarket data available right now.")
            return

        lines = ["<b>Polymarket — Crypto Prediction Odds</b>\n"]
        for m in markets:
            question = m.get("question", "")
            if not question:
                continue
            prob = m.get("probability")
            vol = m.get("volume_usd", 0)
            prob_str = f"{prob * 100:.0f}%" if prob is not None else "?"
            vol_str = f"${vol / 1000:.0f}k" if vol >= 1000 else f"${vol:.0f}"

            # Probability emoji
            if prob is not None:
                if prob >= 0.75:
                    icon = "🟢"
                elif prob >= 0.5:
                    icon = "🟡"
                elif prob >= 0.25:
                    icon = "🟠"
                else:
                    icon = "🔴"
            else:
                icon = "❓"

            lines.append(f"{icon} {prob_str}  {question}\n   Vol: {vol_str}")

        _reply(chat_id, "\n\n".join(lines))
    except Exception as e:
        _reply(chat_id, f"Error fetching Polymarket: {e}")


def _handle_analyze(chat_id: int, args: list[str]) -> None:
    """
    /analyze SYMBOL — run full TA + sentiment analysis for a single coin.
    Example: /analyze ETH
    """
    if not args:
        _reply(chat_id, "Usage: /analyze SYMBOL\nExample: /analyze ETH")
        return

    symbol = args[0].upper()
    coin_id = _symbol_to_coin_id(symbol)

    _reply(chat_id, f"⏳ Analyzing {symbol}... (this may take ~30s)")

    try:
        from src.connectors.coingecko import fetch_prices, fetch_ohlcv, fetch_fear_greed
        from src.connectors.cryptopanic import fetch_news, format_for_prompt
        from src.agents.technical_analyst import compute_ta

        # Price
        prices = fetch_prices([coin_id])
        if not prices:
            _reply(chat_id, f"Could not fetch data for {symbol}.")
            return
        p = prices[0]
        decimals = 2 if p.price_eur >= 1 else 4

        # TA
        ohlcv = fetch_ohlcv(coin_id, days=30)
        ta = compute_ta(coin_id, symbol, ohlcv)
        trend_icon = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(ta.trend, "❓")

        # Fear & Greed
        fg = fetch_fear_greed()

        # News
        news = fetch_news([symbol])
        news_text = format_for_prompt(news) if news else "No recent news."

        # Build Groq prompt for this specific coin
        try:
            from groq import Groq
            import json as _json

            client = Groq(api_key=config.GROQ_API_KEY)
            coin_summary = (
                f"{symbol} | price=${p.price_usd:.{decimals}f} "
                f"| 24h={p.change_24h:+.1f}% | 7d={p.change_7d:+.1f}% "
                f"| RSI={ta.rsi_14:.1f if ta.rsi_14 else 'N/A'} "
                f"| MACD={ta.macd_signal} | BB={ta.bollinger_position} "
                f"| trend={ta.trend}"
            )
            prompt = (
                f"Analyze this coin in depth:\n{coin_summary}\n\n"
                f"Fear & Greed: {fg['value']}/100 ({fg['label']})\n\n"
                f"Recent News:\n{news_text}\n\n"
                f"Give: bull case, bear case, recommended action (BUY/HOLD/SELL), "
                f"entry price, stop-loss, take-profit, timeframe, and confidence (0-1).\n\n"
                f"Return JSON: {{\"action\":\"BUY|HOLD|SELL\",\"entry_price\":N,\"stop_loss\":N,"
                f"\"take_profit\":N,\"timeframe\":\"...\",\"confidence\":N,\"bull_case\":\"...\","
                f"\"bear_case\":\"...\",\"reasoning\":\"...\"}}"
            )
            response = client.chat.completions.create(
                model=config.LLM_MODEL,
                messages=[
                    {"role": "system", "content": "You are a crypto analyst. Respond with valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=1000,
                temperature=config.LLM_TEMPERATURE,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content.strip()
            start = content.find("{")
            end = content.rfind("}") + 1
            rec = _json.loads(content[start:end]) if start >= 0 else {}

            def _fmt(val):
                return f"${val:,.{decimals}f}" if isinstance(val, (int, float)) else str(val)

            action = rec.get("action", "?")
            action_icon = {"BUY": "🟢", "HOLD": "🟡", "SELL": "🔴"}.get(action, "❓")
            conf = rec.get("confidence", 0)

            msg = (
                f"<b>{symbol} — Full Analysis</b>\n\n"
                f"Price: €{p.price_eur:.{decimals}f}  (${p.price_usd:.{decimals}f})\n"
                f"24h: {p.change_24h:+.1f}%  |  7d: {p.change_7d:+.1f}%\n\n"
                f"<b>TA:</b> {trend_icon} {ta.trend}"
            )
            if ta.rsi_14:
                msg += f"  |  RSI {ta.rsi_14:.1f}"
            if ta.macd_signal:
                msg += f"  |  MACD {ta.macd_signal}"
            msg += (
                f"\n\n<b>Signal:</b> {action_icon} {action}  (confidence: {conf:.0%})\n"
                f"Entry:       {_fmt(rec.get('entry_price'))}\n"
                f"Stop-Loss:   {_fmt(rec.get('stop_loss'))}\n"
                f"Take-Profit: {_fmt(rec.get('take_profit'))}\n"
                f"Timeframe:   {rec.get('timeframe', '?')}\n\n"
                f"<b>Bull Case:</b> {rec.get('bull_case', '?')}\n\n"
                f"<b>Bear Case:</b> {rec.get('bear_case', '?')}\n\n"
                f"<i>{rec.get('reasoning', '')}</i>\n\n"
                f"F&amp;G: {fg['value']}/100 ({fg['label']})"
            )
            _reply(chat_id, msg)

        except Exception as e:
            # Fallback: TA-only summary if Groq fails
            msg = (
                f"<b>{symbol} — TA Summary</b> (LLM unavailable: {e})\n\n"
                f"Price: €{p.price_eur:.{decimals}f}  (${p.price_usd:.{decimals}f})\n"
                f"24h: {p.change_24h:+.1f}%  |  7d: {p.change_7d:+.1f}%\n\n"
                f"Trend: {trend_icon} {ta.trend} ({ta.confidence:.0%})\n"
            )
            if ta.rsi_14:
                msg += f"RSI(14): {ta.rsi_14:.1f}\n"
            if ta.macd_signal:
                msg += f"MACD: {ta.macd_signal}\n"
            msg += f"\n<i>{ta.key_observation}</i>"
            _reply(chat_id, msg)

    except Exception as e:
        _reply(chat_id, f"Error analyzing {symbol}: {e}")


def _handle_help(chat_id: int) -> None:
    """
    /help — list all available commands.
    """
    msg = (
        "<b>CryptoAdvisor Commands</b>\n\n"
        "/portfolio — live portfolio with P&amp;L\n"
        "/update_portfolio COIN AMOUNT [ENTRY] — update holding\n\n"
        "/price SYMBOL — price + TA (e.g. /price BTC)\n"
        "/fear — Fear &amp; Greed Index\n"
        "/polymarket — crypto prediction market odds\n"
        "/analyze SYMBOL — deep analysis + LLM signal (e.g. /analyze ETH)\n\n"
        "/help — this message"
    )
    _reply(chat_id, msg)


# ── Polling loop ──────────────────────────────────────────────────────────

_ALLOWED_CHAT_ID = str(config.TELEGRAM_CHAT_ID) if config.TELEGRAM_CHAT_ID else None


def _process_update(update: dict) -> None:
    global _offset
    _offset = update["update_id"] + 1

    msg = update.get("message", {})
    text = (msg.get("text") or "").strip()
    chat_id = msg.get("chat", {}).get("id")

    if not text or not chat_id:
        return

    # Only respond to the configured chat (security)
    if _ALLOWED_CHAT_ID and str(chat_id) != _ALLOWED_CHAT_ID:
        return

    parts = text.split()
    command = parts[0].lower().split("@")[0]  # strip @BotName suffix
    args = parts[1:]

    if command == "/portfolio":
        _handle_portfolio(chat_id)
    elif command == "/update_portfolio":
        _handle_update_portfolio(chat_id, args)
    elif command == "/price":
        _handle_price(chat_id, args)
    elif command == "/fear":
        _handle_fear(chat_id)
    elif command == "/polymarket":
        _handle_polymarket(chat_id)
    elif command == "/analyze":
        _handle_analyze(chat_id, args)
    elif command in ("/help", "/start"):
        _handle_help(chat_id)


def _poll_loop() -> None:
    global _offset, _running
    print("  Telegram bot: polling for commands (/portfolio, /price, /fear, /polymarket, /analyze)")
    while _running:
        try:
            updates = _get_updates(_offset)
            for update in updates:
                try:
                    _process_update(update)
                except Exception as e:
                    print(f"  Telegram bot: error processing update: {e}")
        except Exception:
            pass
        time.sleep(1)


# ── Public API ────────────────────────────────────────────────────────────

def start_bot_thread() -> threading.Thread | None:
    """Start the command polling bot in a daemon thread. Returns the thread or None."""
    global _running
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("  Telegram bot: BOT_TOKEN or CHAT_ID not configured — commands disabled")
        return None
    _running = True
    t = threading.Thread(target=_poll_loop, daemon=True, name="telegram-bot")
    t.start()
    return t


def stop_bot() -> None:
    global _running
    _running = False
