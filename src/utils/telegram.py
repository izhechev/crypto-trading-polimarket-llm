"""Send Telegram alerts via Bot API (no extra dependencies — plain httpx)."""
import httpx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config


_MAX_CHARS = 4000  # Telegram hard limit is 4096; stay safely under


def _split_message(message: str) -> list[str]:
    """
    Split a message into chunks of at most _MAX_CHARS characters.
    Splits on newlines where possible to avoid cutting mid-line.
    """
    if len(message) <= _MAX_CHARS:
        return [message]

    chunks: list[str] = []
    while message:
        if len(message) <= _MAX_CHARS:
            chunks.append(message)
            break
        # Find last newline within the limit
        cut = message.rfind("\n", 0, _MAX_CHARS)
        if cut <= 0:
            cut = _MAX_CHARS  # no newline found, hard cut
        chunks.append(message[:cut])
        message = message[cut:].lstrip("\n")

    return chunks


def _send_one(chat_id: int | str, text: str) -> bool:
    """Send a single pre-sized chunk to Telegram."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json={
                "chat_id":    chat_id,
                "text":       text,
                "parse_mode": "HTML",
            })
            resp.raise_for_status()
        return True
    except Exception as e:
        print(f"  Telegram send error: {e}")
        return False


def send_telegram_to(chat_id: int | str, message: str) -> bool:
    """Send a message to a specific chat_id, splitting if over 4000 chars."""
    if not config.TELEGRAM_BOT_TOKEN:
        return False
    chunks = _split_message(message)
    ok = True
    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            chunk = f"({i}/{len(chunks)})\n" + chunk
        ok = _send_one(chat_id, chunk) and ok
    return ok


def send_telegram(message: str) -> bool:
    """Send a message to the configured Telegram chat, splitting if over 4000 chars."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("  Telegram: BOT_TOKEN or CHAT_ID not set in .env — skipping")
        return False
    chunks = _split_message(message)
    ok = True
    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            chunk = f"({i}/{len(chunks)})\n" + chunk
        ok = _send_one(config.TELEGRAM_CHAT_ID, chunk) and ok
    if ok:
        n = len(chunks)
        print(f"  Telegram alert sent ({n} message{'s' if n > 1 else ''})")
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
