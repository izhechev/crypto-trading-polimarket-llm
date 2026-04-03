"""Send Telegram alerts via Bot API (no extra dependencies — plain httpx)."""
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config


def send_telegram_to(chat_id: int | str, message: str) -> bool:
    """Send a message to a specific chat_id. Used by the bot command handler."""
    if not config.TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
            })
            resp.raise_for_status()
        return True
    except Exception as e:
        print(f"  Telegram reply error: {e}")
        return False


def send_telegram(message: str) -> bool:
    """Send a message to the configured Telegram chat. Returns True on success."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("  Telegram: BOT_TOKEN or CHAT_ID not set in .env — skipping")
        return False
    ok = send_telegram_to(config.TELEGRAM_CHAT_ID, message)
    if ok:
        print("  Telegram alert sent")
    return ok


def format_recommendation(rec: dict, fear_greed: dict) -> str:
    """Format a recommendation dict as an HTML Telegram message."""
    fg_value = fear_greed.get("value", "?")
    fg_label = fear_greed.get("label", "?")

    def _fmt(val):
        return f"${val:,.4f}" if isinstance(val, (int, float)) else str(val)

    return (
        f"<b>CryptoAdvisor Signal</b>\n\n"
        f"<b>BUY:</b> {rec.get('coin', '?')}\n"
        f"Entry:      {_fmt(rec.get('entry_price'))}\n"
        f"Stop-Loss:  {_fmt(rec.get('stop_loss'))}\n"
        f"Take-Profit:{_fmt(rec.get('take_profit'))}\n"
        f"Timeframe:  {rec.get('timeframe', '?')}\n\n"
        f"Fear &amp; Greed: {fg_value}/100 ({fg_label})\n\n"
        f"<i>{rec.get('reasoning', '')}</i>"
    )
