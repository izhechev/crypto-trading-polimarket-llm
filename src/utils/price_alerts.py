"""
Price alert monitor — runs every 15 minutes, no Groq needed.

Whale ride milestones (checked every run):
  +25%   → alert: first milestone hit
  +50%   → alert: double milestone
  +100%  → alert: triple milestone
  +150%  → alert: on the way to +200%
  +200%  → alert: TP hit — close position

No trailing stops. Positions only close at TP (+200%) or pnl <= -100%.

Normal scanner picks (checked every run):
  PnL >= +8%  → approaching TP alert  (2% before TP of +10%)
  PnL <= -8%  → approaching SL alert  (2% before SL of -10%)

Alerts are de-duplicated: each milestone fires once per position
using a sidecar file (alert_state.json).
"""
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

_ALERT_STATE_PATH = config.DATA_DIR / "alert_state.json"
_CSV_PATH         = config.DATA_DIR / "recommendations.csv"

# Whale ride milestones: pnl threshold → message template
# No trailing stops — positions hold until TP (+200%) or pnl <= -100%
_WHALE_MILESTONES = [
    (200.0, "🌙 {coin} +200% (${price:.4f}) — TP hit! Close position ✅"),
    (150.0, "🚀 {coin} +150% (${price:.4f}) — on the way to +200%!"),
    (100.0, "🚀 {coin} +100% (${price:.4f}) — 3× milestone hit!"),
    ( 50.0, "🚀 {coin}  +50% (${price:.4f}) — 2× milestone hit!"),
    ( 25.0, "🚀 {coin}  +25% (${price:.4f}) — 1× milestone hit!"),
]

_TP_ALERT_PNL = 8.0    # alert when pnl >= +8%  (2% before TP of +10%)
_SL_ALERT_PNL = -8.0   # alert when pnl <= -8%  (2% before SL of -10%)


# ── State helpers ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    """Load per-position alert state. Key: '<coin>|<date>' → set of fired milestones."""
    try:
        if _ALERT_STATE_PATH.exists():
            with open(_ALERT_STATE_PATH, encoding="utf-8") as f:
                raw = json.load(f)
            # Convert lists back to sets
            return {k: set(v) for k, v in raw.items()}
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        serialisable = {k: list(v) for k, v in state.items()}
        with open(_ALERT_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(serialisable, f, indent=2)
    except Exception:
        pass


def _position_key(row: dict) -> str:
    return f"{row.get('coin', '').upper()}|{row.get('date', '')}"


# ── CSV read/write ────────────────────────────────────────────────────────

def _read_csv() -> list[dict]:
    if not _CSV_PATH.exists():
        return []
    try:
        with open(_CSV_PATH, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _write_csv(rows: list[dict]) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ── Price fetch ───────────────────────────────────────────────────────────

def _fetch_prices_usd(coin_ids: list[str]) -> dict[str, float]:
    """Fetch current USD prices from CoinGecko free API."""
    if not coin_ids:
        return {}
    try:
        from src.connectors.coingecko import fetch_prices
        price_objs = fetch_prices(coin_ids)
        return {p.coin_id: p.price_usd for p in price_objs}
    except Exception as e:
        print(f"  [alerts] price fetch failed: {e}")
        return {}


# ── Telegram send ─────────────────────────────────────────────────────────

def _alert(msg: str) -> None:
    print(f"  [ALERT] {msg}")
    try:
        from src.utils.telegram import send_telegram
        send_telegram(msg)
    except Exception as e:
        print(f"  [alerts] telegram failed: {e}")


# ── Main check ────────────────────────────────────────────────────────────

def check_price_alerts() -> None:
    """
    Check all OPEN positions for milestone/proximity alerts.
    Updates trailing stop-losses in the CSV when milestones are hit.
    Call every 15 minutes.
    """
    rows      = _read_csv()
    state     = _load_state()
    dirty_csv = False  # tracks whether CSV needs rewriting

    open_rows = [
        r for r in rows
        if r.get("status") == "OPEN"
        and r.get("coin_id")
        and r.get("type", "SCANNER") in ("SCANNER", "", "WHALE_RIDE")
    ]
    if not open_rows:
        return

    coin_ids = list({r["coin_id"] for r in open_rows})
    usd_map  = _fetch_prices_usd(coin_ids)
    if not usd_map:
        return

    for row in rows:
        if row.get("status") != "OPEN" or not row.get("coin_id"):
            continue
        if row.get("type", "SCANNER") not in ("SCANNER", "", "WHALE_RIDE"):
            continue

        usd = usd_map.get(row["coin_id"])
        if usd is None:
            continue

        try:
            entry = float(row["entry_price"])
            sl    = float(row["stop_loss"])
            tp    = float(row["take_profit"])
        except (ValueError, KeyError):
            continue

        pnl_pct = (usd - entry) / entry * 100
        coin    = row.get("coin", "").upper()
        key     = _position_key(row)
        fired   = state.setdefault(key, set())

        # ── Whale ride: milestone alerts (no trailing stops) ─────────────
        if row.get("type") == "WHALE_RIDE":
            for threshold, msg_tmpl in _WHALE_MILESTONES:
                label = f"whale_{threshold:.0f}"
                if pnl_pct >= threshold and label not in fired:
                    msg = msg_tmpl.format(coin=coin, price=usd, pnl=pnl_pct)
                    _alert(msg)
                    fired.add(label)

        # ── Normal scanner picks: PnL-based alerts ───────────────────────
        else:
            # Approaching TP: pnl >= +8%  (2% before TP of +10%)
            if pnl_pct >= _TP_ALERT_PNL and "near_tp" not in fired:
                _alert(
                    f"⚠️ {coin} at ${usd:.4f} — approaching TP ${tp:.4f} "
                    f"(PnL {pnl_pct:+.1f}%)"
                )
                fired.add("near_tp")
            elif pnl_pct < _TP_ALERT_PNL and "near_tp" in fired:
                fired.discard("near_tp")  # reset if price pulled back

            # Approaching SL: pnl <= -8%  (2% before SL of -10%)
            if pnl_pct <= _SL_ALERT_PNL and "near_sl" not in fired:
                _alert(
                    f"⚠️ {coin} at ${usd:.4f} — approaching SL ${sl:.4f} "
                    f"(PnL {pnl_pct:+.1f}%)"
                )
                fired.add("near_sl")
            elif pnl_pct > _SL_ALERT_PNL and "near_sl" in fired:
                fired.discard("near_sl")  # reset if price recovered

    if dirty_csv:
        _write_csv(rows)

    # Prune state for closed positions (keep it lean)
    open_keys = {_position_key(r) for r in open_rows}
    for k in list(state.keys()):
        if k not in open_keys:
            del state[k]

    _save_state(state)


# ── Loop mode ─────────────────────────────────────────────────────────────

def run_alert_loop(interval_minutes: int = 15) -> None:
    """
    Run check_price_alerts() every `interval_minutes` minutes.
    Blocking — call in a background thread or standalone process.
    """
    print(f"  [alerts] Starting price alert loop (every {interval_minutes}min)...")
    while True:
        try:
            check_price_alerts()
        except Exception as e:
            print(f"  [alerts] check failed: {e}")
        time.sleep(interval_minutes * 60)


if __name__ == "__main__":
    # Run as standalone: python -m src.utils.price_alerts
    run_alert_loop(interval_minutes=15)
