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
    """
    Fetch current USD prices keyed by coin_id.
    1. CoinGecko /simple/price  (lightweight, handles CG-format IDs like 'solana')
    2. For IDs not resolved: individual CoinPaprika /tickers/{id} calls (free tier, no bulk)
    """
    if not coin_ids:
        return {}

    result: dict[str, float] = {}

    # 1. CoinGecko /simple/price — much lighter than /coins/markets, higher rate limit
    try:
        import httpx as _httpx
        from src.connectors.coingecko import _headers as _cg_headers
        resp = _httpx.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ",".join(coin_ids), "vs_currencies": "usd"},
            headers=_cg_headers(),
            timeout=15,
        )
        if resp.status_code == 200:
            for cid, data in resp.json().items():
                usd = data.get("usd")
                if usd:
                    result[cid] = float(usd)
        else:
            print(f"  [alerts] CoinGecko simple/price HTTP {resp.status_code}")
    except Exception as e:
        print(f"  [alerts] CoinGecko price fetch failed: {e}")

    missing = [cid for cid in coin_ids if cid not in result]
    if not missing:
        return result

    # 2. Individual CoinPaprika /tickers/{id} calls (free on all plans, no bulk needed)
    import httpx as _httpx
    import time as _time
    for cid in missing:
        try:
            r = _httpx.get(
                f"https://api.coinpaprika.com/v1/tickers/{cid}",
                params={"quotes": "USD"},
                timeout=10,
            )
            if r.status_code == 200:
                price = r.json().get("quotes", {}).get("USD", {}).get("price")
                if price:
                    result[cid] = float(price)
            _time.sleep(0.12)  # stay within 10 req/sec
        except Exception as e2:
            print(f"  [alerts] CP ticker {cid} failed: {e2}")

    return result


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

    # entry_price map for sanity checks (coin_id → entry_price)
    entry_map: dict[str, float] = {}
    for r in open_rows:
        try:
            entry_map[r["coin_id"]] = float(r["entry_price"])
        except (ValueError, KeyError, TypeError):
            pass

    for row in rows:
        if row.get("status") != "OPEN" or not row.get("coin_id"):
            continue
        if row.get("type", "SCANNER") not in ("SCANNER", "", "WHALE_RIDE"):
            continue

        usd = usd_map.get(row["coin_id"])
        if usd is None:
            continue

        # Sanity check: skip if fetched price deviates >90% from entry
        _entry_check = entry_map.get(row["coin_id"], 0)
        if _entry_check > 0:
            _ratio = usd / _entry_check
            if _ratio < 0.1 or _ratio > 10:
                print(
                    f"  [alerts] SANITY SKIP {row.get('coin','?')} "
                    f"fetched=${usd:.6f} vs entry=${_entry_check:.6f} "
                    f"(ratio {_ratio:.2f}) — likely wrong coin_id"
                )
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

        # ── Whale ride: house-money system ───────────────────────────────
        if row.get("type") == "WHALE_RIDE":
            _SCORES         = {25: 1, 50: 2, 100: 3, 150: 3, 200: 4}
            reasoning       = row.get("reasoning", "")
            is_pr           = "PRINCIPAL_RECOVERED" in reasoning
            _peak_key       = f"{key}_peak"

            # Pre-milestone: hard SL at -15%
            if not is_pr and pnl_pct <= -15.0 and "pre_sl_fired" not in fired:
                _alert(f"🛑 {coin} -15% SL at ${usd:.4f} — WHALE_RIDE closed LOSS (pre-milestone)")
                row["status"]     = "LOSS"
                row["exit_price"] = str(round(usd, 8))
                row["close_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                row["pnl_pct"]    = round(pnl_pct, 2)
                fired.add("pre_sl_fired")
                dirty_csv = True
                continue

            # Post-milestone: trailing stop 25% below peak, hard floor at entry
            if is_pr and "house_closed" not in fired:
                peak_price  = state.get(_peak_key, entry)
                if usd > peak_price:
                    state[_peak_key] = usd
                    peak_price = usd
                trailing_sl = max(entry, round(peak_price * 0.75, 8))
                row["stop_loss"] = str(trailing_sl)
                dirty_csv = True

                if usd <= trailing_sl:
                    _close_pct = (trailing_sl - entry) / entry * 100
                    _alert(
                        f"🔔 {coin} HOUSE MONEY closed at ${usd:.4f} "
                        f"(trail SL ${trailing_sl:.4f} = +{_close_pct:.1f}% from entry)"
                    )
                    row["status"]     = "WIN"
                    row["exit_price"] = str(round(usd, 8))
                    row["close_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    row["pnl_pct"]    = round(pnl_pct, 2)
                    fired.add("house_closed")
                    dirty_csv = True
                    continue

            # Milestone firing LOW→HIGH so +25% fires first and sets PRINCIPAL_RECOVERED
            for threshold, msg_tmpl in sorted(_WHALE_MILESTONES, key=lambda x: x[0]):
                label = f"whale_{threshold:.0f}"
                if pnl_pct >= threshold and label not in fired:
                    msg = msg_tmpl.format(coin=coin, price=usd, pnl=pnl_pct)
                    _alert(msg)
                    fired.add(label)
                    dirty_csv = True

                    # +25%: recover principal → set hard-floor SL + tag reasoning
                    if threshold == 25.0 and not is_pr:
                        row["reasoning"] = (reasoning + " PRINCIPAL_RECOVERED").strip()
                        row["stop_loss"] = str(entry)
                        state[_peak_key] = usd
                        reasoning = row["reasoning"]
                        is_pr = True

                    pct   = int(threshold)
                    score = _SCORES.get(pct, "")
                    flag  = f"[MILESTONE_{pct}]"
                    if flag not in row.get("reasoning", ""):
                        row["reasoning"] = (row.get("reasoning", "") + f" {flag}").strip()

                    # Log WIN record for this milestone
                    try:
                        _entry_f  = float(row.get("entry_price") or 0)
                        _ms_price = round(_entry_f * (1 + pct / 100), 8) if _entry_f > 0 else round(usd, 8)
                    except (ValueError, TypeError):
                        _ms_price = round(usd, 8)
                    _ms = {
                        "date":          row.get("date", ""),
                        "type":          "WHALE_MILESTONE",
                        "coin":          row.get("coin", ""),
                        "coin_id":       row.get("coin_id", ""),
                        "entry_price":   row.get("entry_price", ""),
                        "stop_loss":     "",
                        "take_profit":   "",
                        "status":        "WIN",
                        "exit_price":    _ms_price,
                        "close_date":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                        "pnl_pct":       float(pct),
                        "current_price": round(usd, 8),
                        "price_eur":     "",
                        "timeframe":     "",
                        "fear_greed":    row.get("fear_greed", ""),
                        "reasoning":     f"[WHALE_MILESTONE +{pct}% / {score}pt] Partial win — position stays open",
                        "groq_rank":     score,
                        "qualifier":     "WHALE_RIDE",
                        "key_signal":    "",
                    }
                    _already = any(
                        r.get("type") == "WHALE_MILESTONE"
                        and r.get("coin", "").upper() == _ms["coin"].upper()
                        and r.get("date", "") == _ms["date"]
                        and str(r.get("pnl_pct", "")) == str(_ms["pnl_pct"])
                        for r in rows
                    )
                    if not _already:
                        rows.append(_ms)
                        print(f"  [milestone] {coin} +{pct}% ({score}pt) WIN record logged immediately")

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
    # Keys take two forms: "<coin>|<date>" (fired set) and "<coin>|<date>_peak" (peak tracker)
    open_keys = {_position_key(r) for r in open_rows}
    for k in list(state.keys()):
        base = k[:-5] if k.endswith("_peak") else k  # strip _peak suffix
        if base not in open_keys:
            del state[k]

    _save_state(state)


# ── Custom price targets (spam alert) ─────────────────────────────────────

# Each entry: (cp_coin_id, symbol, target_pct, baseline_price)
# baseline_price = price at time of setup; alert fires when current >= baseline * (1 + target_pct/100)
_SPAM_ALERTS: list[tuple[str, str, float, float]] = [
    ("dydx-dydx", "dYdX", 30.0, 0.135908),  # baseline $0.135908 on 2026-04-17
]
_SPAM_COUNT = 50  # number of messages to send


def check_spam_alerts(state: dict) -> None:
    """
    Check custom price targets and spam `_SPAM_COUNT` Telegram messages if hit.
    Fires once per alert (stored in state to prevent re-firing).
    """
    for cp_id, symbol, target_pct, baseline in _SPAM_ALERTS:
        key = f"spam_{cp_id}_{target_pct:.0f}"
        if key in state:
            continue  # already fired

        try:
            import httpx as _httpx
            _resp = _httpx.get(
                f"https://api.coinpaprika.com/v1/tickers/{cp_id}",
                params={"quotes": "USD"},
                timeout=12,
            )
            _resp.raise_for_status()
            _data = _resp.json()
            price = _data["quotes"]["USD"]["price"]
        except Exception as e:
            print(f"  [alerts] {symbol} price fetch failed: {e}")
            continue

        target_price = baseline * (1 + target_pct / 100)
        pnl = (price - baseline) / baseline * 100
        print(f"  [alerts] {symbol}: ${price:.6f} (baseline ${baseline:.6f}, target ${target_price:.6f}, now {pnl:+.1f}%)")

        if price >= target_price:
            print(f"  [alerts] 🚨 {symbol} +{target_pct:.0f}% HIT — spamming {_SPAM_COUNT} messages...")
            from src.utils.telegram import send_telegram
            for i in range(1, _SPAM_COUNT + 1):
                try:
                    send_telegram(
                        f"🚨🚨🚨 <b>dYdX +{target_pct:.0f}% HIT!</b> 🚨🚨🚨\n"
                        f"Price: ${price:.6f}  (was ${baseline:.6f})\n"
                        f"[{i}/{_SPAM_COUNT}] WAKE UP!!!"
                    )
                except Exception:
                    pass
            state[key] = True
            print(f"  [alerts] spam done — {_SPAM_COUNT} messages sent for {symbol}")


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
        try:
            _state = _load_state()
            check_spam_alerts(_state)
            _save_state(_state)
        except Exception as e:
            print(f"  [alerts] spam check failed: {e}")
        time.sleep(interval_minutes * 60)


if __name__ == "__main__":
    # Run as standalone: python -m src.utils.price_alerts
    run_alert_loop(interval_minutes=15)
