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

def _pfmt(p: float) -> str:
    if p >= 1:       return f"${p:,.4f}"
    if p >= 0.01:    return f"${p:.5f}"
    if p >= 0.0001:  return f"${p:.7f}"
    return f"${p:.10f}"


# Whale ride milestones: pnl threshold → message template
# No trailing stops — positions hold until TP (+200%) or pnl <= -100%
_WHALE_MILESTONES = [
    (200.0, "🌙 {coin} +200% ({price}) — TP hit! Close position ✅"),
    (150.0, "🚀 {coin} +150% ({price}) — on the way to +200%!"),
    (100.0, "🚀 {coin} +100% ({price}) — 3× milestone hit!"),
    ( 50.0, "🚀 {coin}  +50% ({price}) — 2× milestone hit!"),
    ( 25.0, "🚀 {coin}  +25% ({price}) — 1× milestone hit! (principal recovered)"),
    ( 15.0, "🚀 {coin}  +15% ({price}) — Early milestone hit!"),
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

def _fetch_prices_usd(coin_ids: list[str], open_rows: list[dict] | None = None) -> dict[str, float]:
    """
    Fetch current USD prices keyed by coin_id.
    Tiers: CoinGecko → Binance → KuCoin → CoinCap.
    CP-format coin_ids (e.g. 'op-optimism') are translated to CG IDs before the CG call.
    Binance/KuCoin tiers use the coin symbol from open_rows for lookup.
    """
    if not coin_ids:
        return {}

    import httpx as _httpx
    from src.connectors.coingecko import _headers as _cg_headers
    from src.connectors.coinpaprika import SYMBOL_TO_CG_ID as _SYM_TO_CG

    result: dict[str, float] = {}

    # Build symbol map from open_rows (coin_id → symbol) for non-CG tiers
    _cid_sym: dict[str, str] = {}
    if open_rows:
        for _r in open_rows:
            _cid2 = _r.get("coin_id", "")
            _sym2 = _r.get("coin", "").upper()
            if _cid2 and _sym2:
                _cid_sym[_cid2] = _sym2

    # Translate CP-format coin_ids → CG IDs for tier 1
    _cid_to_cg: dict[str, str] = {}
    for _cid in coin_ids:
        _sym = _cid_sym.get(_cid, "")
        _first_seg = _cid.split("-")[0].upper() if "-" in _cid else ""
        _is_cp = bool(_first_seg and _first_seg == _sym)
        if _is_cp and _sym:
            _cg_id = _SYM_TO_CG.get(_sym)
            _cid_to_cg[_cid] = _cg_id if _cg_id else _cid
        else:
            _cid_to_cg[_cid] = _cid

    _cg_to_cids: dict[str, list[str]] = {}
    for _cid, _cgid in _cid_to_cg.items():
        _cg_to_cids.setdefault(_cgid, []).append(_cid)

    # Tier 1: CoinGecko /simple/price
    try:
        resp = _httpx.get(
            "https://pro-api.coingecko.com/api/v3/simple/price",
            params={"ids": ",".join(_cg_to_cids.keys()), "vs_currencies": "usd"},
            headers=_cg_headers(),
            timeout=15,
        )
        if resp.status_code == 200:
            for _cgid, _data in resp.json().items():
                _usd = _data.get("usd")
                if _usd:
                    for _cid in _cg_to_cids.get(_cgid, [_cgid]):
                        result[_cid] = float(_usd)
        else:
            print(f"  [alerts] CoinGecko simple/price HTTP {resp.status_code}")
    except Exception as e:
        print(f"  [alerts] CoinGecko price fetch failed: {e}")

    # Binance cross-validation: compare with CG; override if >10% divergence.
    # Only uses coins we already know the symbol for (from open_rows) to avoid
    # ticker reassignment risk on delisted coins.
    if _cid_sym:
        try:
            _bn_resp = _httpx.get(
                "https://api.binance.com/api/v3/ticker/price",
                timeout=15,
            )
            if _bn_resp.status_code == 200:
                _bn_map: dict[str, float] = {}
                for _tk in _bn_resp.json():
                    _s = _tk.get("symbol", "")
                    if _s.endswith("USDT"):
                        try:
                            _bn_map[_s[:-4]] = float(_tk["price"])
                        except (ValueError, TypeError):
                            pass
                for _cid, _sym in _cid_sym.items():
                    _bn_p = _bn_map.get(_sym)
                    if not _bn_p:
                        continue
                    _cg_p = result.get(_cid)
                    if _cg_p:
                        _diff = abs(_bn_p - _cg_p) / _cg_p
                        if _diff > 0.10:
                            print(f"  ⚠️  [alerts] {_sym} price mismatch: CG={_pfmt(_cg_p)} Binance={_pfmt(_bn_p)} ({_diff*100:.1f}%) — using Binance")
                            result[_cid] = _bn_p
                    elif _cid not in result:
                        result[_cid] = _bn_p
        except Exception:
            pass
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
    usd_map  = _fetch_prices_usd(coin_ids, open_rows=open_rows)
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

        # Sanity check: skip if fetched price is implausible vs entry.
        # WHALE_RIDE TP is at +200% (3x) — anything above 4x is a bad fetch.
        # SCANNER TP is at +20-40% — anything above 2.5x is a bad fetch.
        # Lower floor: >85% drop is suspicious (use 0.15 floor).
        _entry_check = entry_map.get(row["coin_id"], 0)
        if _entry_check > 0:
            _ratio = usd / _entry_check
            _is_wr = row.get("type") == "WHALE_RIDE"
            _max_ratio = 4.0 if _is_wr else 2.5
            if _ratio < 0.15 or _ratio > _max_ratio:
                print(
                    f"  [alerts] SANITY SKIP {row.get('coin','?')} "
                    f"fetched=${usd:.6f} vs entry=${_entry_check:.6f} "
                    f"(ratio {_ratio:.2f}x, max {_max_ratio}x) — likely bad price source"
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

        # Always update current price and P&L in the CSV to keep it fresh
        row["current_price"] = str(round(usd, 8))
        row["pnl_pct"]       = str(round(pnl_pct, 2))
        dirty_csv = True

        # ── Whale ride: house-money system ───────────────────────────────
        if row.get("type") == "WHALE_RIDE":
            _SCORES         = {25: 1, 50: 2, 100: 3, 150: 3, 200: 4}
            reasoning       = row.get("reasoning", "")
            is_pr           = "PRINCIPAL_RECOVERED" in reasoning
            _peak_key       = f"{key}_peak"

            # Time-based expiry: close when max_hold_hours elapsed.
            # Serial scams are force-closed regardless of milestone/principal state.
            import re as _re_ta
            _now_str_ta = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            _is_scam    = "SERIAL SCAM" in reasoning
            try:
                _entry_dt  = datetime.strptime(row["date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                _hrs_open  = (datetime.now(timezone.utc) - _entry_dt).total_seconds() / 3600
                _mh        = _re_ta.search(r"max_hold:\s*(\d+)h", reasoning)
                _max_hold  = int(_mh.group(1)) if _mh else 24
                _expired   = _hrs_open >= _max_hold
                # Serial scams: force close if expired, skip if not
                # Non-scams: close only if not already house-money managed
                if _expired and "time_expired" not in fired and (_is_scam or not is_pr):
                    _exp_status = "WIN" if pnl_pct > 0 else "LOSS"
                    _scam_tag   = " ⚠️ SERIAL SCAM" if _is_scam else ""
                    _alert(f"⏰ {coin} expired ({_hrs_open:.0f}h/{_max_hold}h) {pnl_pct:+.1f}% → {_exp_status}  ${usd:.4f}{_scam_tag}")
                    row["status"]     = _exp_status
                    row["exit_price"] = str(round(usd, 8))
                    row["close_date"] = _now_str_ta
                    row["pnl_pct"]    = str(round(pnl_pct, 2))
                    fired.add("time_expired")
                    dirty_csv = True
                    print(f"  [time-expire] {coin} closed {_exp_status} {pnl_pct:+.1f}% after {_hrs_open:.0f}h{_scam_tag}")
                    continue
            except Exception:
                pass

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
                    msg = msg_tmpl.format(coin=coin, price=_pfmt(usd), pnl=pnl_pct)
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

    # Always save state first — fired milestone flags must persist even if CSV write fails.
    # Prune state for closed positions (keep it lean)
    open_keys = {_position_key(r) for r in open_rows}
    for k in list(state.keys()):
        base = k[:-5] if k.endswith("_peak") else k
        if base not in open_keys:
            del state[k]
    _save_state(state)

    if dirty_csv:
        _write_csv(rows)


# ── Custom price targets (spam alert) ─────────────────────────────────────

# Each entry: (cp_coin_id, symbol, target_pct, baseline_price)
# baseline_price = price at time of setup; alert fires when current >= baseline * (1 + target_pct/100)
_SPAM_ALERTS: list[tuple[str, str, float, float]] = [
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
