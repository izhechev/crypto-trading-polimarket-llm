"""
Whale Ride Alert Module — manual notification ONLY, zero auto-trading.

Detects coins showing VOLUME ANOMALIES (early pump signals) and sends Telegram
alerts for human decision. Never logs positions, never touches recommendations.csv.

Stage definitions (volume-anomaly based):
  EARLY  vol > 5x 30d avg AND price +10% → +30%   ← alert: early pump signal
  MID    vol > 10x 30d avg AND price +30% → +100%  ← alert: confirmed pump
  LATE   price > +100% 7d                           ← watchlist only; wait for crash

Alert lifecycle:
  1. Volume anomaly detected → Telegram alert sent, stored in whale_ride_alerts.json
     AND symbol added to _whale_entry_alerts_sent (session set)
  2. Every scan: check stored alerts for exit signals (24h < +5% OR RSI > 85)
     EXIT only fires if symbol is in _whale_entry_alerts_sent (prevents phantom exits
     for stale active entries that were never sent in this session)
  3. LATE coins tracked for post-crash bounce (crash >30% from peak + RSI <30)
  4. No cap on simultaneous active watches — alert every qualifying coin
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, date as _date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

ALERTS_PATH      = config.DATA_DIR / "whale_ride_alerts.json"
VOL_HISTORY_PATH = config.DATA_DIR / "whale_volume_history.json"

# ── Volume anomaly thresholds ─────────────────────────────────────────────────
_VOL_EARLY_MULT   = 5.0    # today vol > 5x 30d avg → EARLY signal
_VOL_MID_MULT     = 10.0   # today vol > 10x 30d avg → MID (confirmed pump)
_PRICE_EARLY_MIN  = 10.0   # 24h price change minimum for EARLY
_PRICE_EARLY_MAX  = 30.0   # above this → MID territory
_PRICE_MID_MAX    = 100.0  # above this → LATE (too late to enter)

# LATE stage: price already >100% 7d → watchlist only, wait for crash
_7D_LATE_MIN      = 100.0

# Shared hard disqualifiers
_MIN_MCAP         = 20_000_000  # $20M
_MIN_CIRC_PCT     = 15.0        # circulating supply minimum
_VOL_HISTORY_DAYS = 30          # days of volume history to keep per coin
_MIN_HISTORY_DAYS = 7           # need at least 7 days of data before firing alerts

# ── Exit / post-crash thresholds ──────────────────────────────────────────────
_EXIT_24H_FLOOR   = 5.0    # 24h below this → momentum dying → exit signal
_EXIT_RSI_CEIL    = 85.0   # RSI above this → overbought → exit signal

_CRASH_DROP_PCT   = 30.0   # post-crash bounce: must have dropped ≥30% from peak
_CRASH_RSI_MAX    = 30.0   # RSI must be oversold (<30) for bounce entry
_CRASH_VOL_MAX    = 0.30   # volume must be dying (low vol = stable bottom)

# ── Alert deduplication ───────────────────────────────────────────────────────
_DUPLICATE_HOURS  = 24     # suppress re-alert for same coin within N hours

# ── Session-level entry tracking ─────────────────────────────────────────────
# Populated when an entry alert is sent. Exit signals are ONLY sent for coins
# present in this set — prevents phantom exits for stale active[] entries
# that were never alerted in the current session.
_whale_entry_alerts_sent: set[str] = set()


# ── Persistence ───────────────────────────────────────────────────────────────

def _load() -> dict:
    """Load alerts JSON.  Migrates legacy flat format to new nested format."""
    try:
        if ALERTS_PATH.exists():
            raw = json.loads(ALERTS_PATH.read_text(encoding="utf-8"))
            # Migration: old format was a flat dict keyed by symbol at top level
            # (no "active", "late_watchlist", "last_seen_24h" keys).
            # Detect by checking for new-format sentinel keys.
            if "active" not in raw and "late_watchlist" not in raw:
                # Old format — migrate active entries across
                old_active = {k: v for k, v in raw.items() if isinstance(v, dict)}
                raw = {"active": old_active, "late_watchlist": {}, "last_seen_24h": {}}
            raw.setdefault("active", {})
            raw.setdefault("late_watchlist", {})
            raw.setdefault("last_seen_24h", {})
            return raw
    except Exception:
        pass
    return {"active": {}, "late_watchlist": {}, "last_seen_24h": {}}


def _save(data: dict) -> None:
    try:
        ALERTS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Volume history ────────────────────────────────────────────────────────────

def _load_vol_history() -> dict:
    """Load {symbol: [{"date": "YYYY-MM-DD", "volume": float}, ...]} (newest last)."""
    try:
        if VOL_HISTORY_PATH.exists():
            return json.loads(VOL_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_vol_history(hist: dict) -> None:
    try:
        VOL_HISTORY_PATH.write_text(json.dumps(hist, indent=2), encoding="utf-8")
    except Exception:
        pass


def update_volume_history(coins: list[dict]) -> dict:
    """
    Append today's 24h volume for each coin (one entry per calendar day).
    Keeps the last _VOL_HISTORY_DAYS entries per coin.
    Returns the updated history dict.
    """
    hist    = _load_vol_history()
    today   = str(_date.today())

    for coin in coins:
        sym = coin.get("symbol", "").upper()
        vol = coin.get("total_volume") or 0
        if vol <= 0:
            continue
        entries = hist.get(sym, [])
        # Skip if we already have an entry for today
        if entries and entries[-1].get("date") == today:
            continue
        entries.append({"date": today, "volume": vol})
        # Keep only the last _VOL_HISTORY_DAYS days
        if len(entries) > _VOL_HISTORY_DAYS:
            entries = entries[-_VOL_HISTORY_DAYS:]
        hist[sym] = entries

    _save_vol_history(hist)
    return hist


def _avg_volume(hist: dict, sym: str) -> float | None:
    """
    Return 30-day average daily volume for sym, or None if insufficient data.
    Requires at least _MIN_HISTORY_DAYS entries.
    """
    entries = hist.get(sym, [])
    if len(entries) < _MIN_HISTORY_DAYS:
        return None
    vols = [e["volume"] for e in entries if e.get("volume", 0) > 0]
    return sum(vols) / len(vols) if vols else None


# ── Formatters ────────────────────────────────────────────────────────────────

def _fmt_price(p: float) -> str:
    if p >= 1:       return f"${p:,.2f}"
    if p >= 0.01:    return f"${p:.4f}"
    if p >= 0.0001:  return f"${p:.6f}"
    return f"${p:.8f}"


def _fmt_mcap(m: float) -> str:
    if m >= 1e9: return f"${m/1e9:.1f}B"
    return f"${m/1e6:.0f}M"


# ── Detection ─────────────────────────────────────────────────────────────────

def detect_whale_rides(
    coins: list[dict],
    risk_map: dict,
    vol_history: dict | None = None,
) -> list[dict]:
    """
    Detect VOLUME ANOMALY whale ride signals.

    EARLY stage: today vol > 5x 30d avg AND 24h price +10% → +30%
    MID stage:   today vol > 10x 30d avg AND 24h price +30% → +100%

    These catch pumps at +10-20% instead of waiting for +100% 7d.
    Coins already open as SCANNER positions are skipped.
    Requires ≥7 days of volume history before firing (avoids cold-start false alerts).
    """
    hist      = vol_history if vol_history is not None else _load_vol_history()
    data      = _load()
    now_ts    = time.time()
    seen_24h  = data["last_seen_24h"]
    candidates: list[dict] = []

    # Load open SCANNER positions — never label a scanner pick as whale ride
    _open_scanner_syms: set[str] = set()
    try:
        import csv as _csv_wr
        _rec_path = config.DATA_DIR / "recommendations.csv"
        if _rec_path.exists():
            with open(_rec_path, newline="", encoding="utf-8") as _rf:
                _open_scanner_syms = {
                    r.get("coin", "").upper()
                    for r in _csv_wr.DictReader(_rf)
                    if r.get("status") == "OPEN"
                    and r.get("type", "SCANNER") in ("SCANNER", "")
                }
    except Exception:
        pass

    for coin in coins:
        sym   = coin.get("symbol", "").upper()

        if sym in _open_scanner_syms:
            continue

        ch24  = coin.get("price_change_percentage_24h") or 0
        ch7d  = coin.get("price_change_percentage_7d_in_currency") or 0
        vol   = coin.get("total_volume") or 0
        mcap  = coin.get("market_cap") or 0
        circ  = coin.get("circulating_supply") or 0
        total = coin.get("total_supply") or 0

        # Skip LATE stage coins — they go to watchlist, not pump alerts.
        # EXCEPTION: if a coin is surging 30%+ TODAY but we only first see it when
        # 7d is already high (fast mover), still run the volume check — it may be
        # the start of a new leg, not a stale pump.
        _new_leg_burst = ch24 >= 30.0 and ch7d >= _7D_LATE_MIN
        if ch7d >= _7D_LATE_MIN and not _new_leg_burst:
            continue

        # ── Hard disqualifiers ──────────────────────────────────────────
        if mcap < _MIN_MCAP:
            continue

        circ_pct = (circ / total * 100) if (total > 0 and circ > 0) else 100.0
        if circ_pct < _MIN_CIRC_PCT:
            continue

        risk = risk_map.get(sym)
        cat  = risk.category if risk else "NORMAL"
        if cat in ("ACTIVE_SCAM", "MANIPULATED_REAL"):
            continue

        # ── Volume anomaly check ────────────────────────────────────────
        avg_vol = _avg_volume(hist, sym)
        _cold_start = False
        if avg_vol is None or avg_vol <= 0:
            # Cold-start fallback: no 7-day history yet.
            # Use vol/mcap ratio as proxy — most coins trade at ~5-15% vol/mcap normally.
            # If today's vol/mcap > 0.50 (5x the expected baseline of 0.10), treat as
            # a potential surge. Baseline estimated_avg = mcap * 0.10.
            if mcap > 0 and ch24 >= _PRICE_EARLY_MIN:
                _est_avg = mcap * 0.10
                if vol > _est_avg * _VOL_EARLY_MULT:
                    avg_vol   = _est_avg
                    _cold_start = True   # flag so alert includes cold-start warning
                else:
                    continue   # not enough volume to be interesting even without history
            else:
                continue   # no history, no signal

        vol_ratio = vol / avg_vol

        # Classify stage by vol multiplier + price range
        if _new_leg_burst and ch24 >= _PRICE_MID_MAX and vol_ratio >= _VOL_MID_MULT:
            # Extreme single-day burst (>100% 24h) — very high risk, mark clearly
            stage = "MID"
        elif vol_ratio >= _VOL_MID_MULT and _PRICE_EARLY_MAX <= ch24 < _PRICE_MID_MAX:
            stage = "MID"
        elif vol_ratio >= _VOL_EARLY_MULT and _PRICE_EARLY_MIN <= ch24 < _PRICE_EARLY_MAX:
            stage = "EARLY"
        else:
            continue   # no anomaly

        # 24h momentum declining vs previous scan → skip
        prev_24h_entry = seen_24h.get(sym)
        if prev_24h_entry:
            prev_ch24 = prev_24h_entry.get("ch24", ch24)
            prev_ts   = prev_24h_entry.get("ts", 0)
            age_h     = (now_ts - prev_ts) / 3600
            if age_h < 6 and ch24 < prev_ch24:
                continue   # momentum declining

        vm = vol / max(mcap, 1)
        candidates.append({
            "symbol":     sym,
            "name":       coin.get("name", sym),
            "coin_id":    coin.get("id", ""),
            "price":      coin.get("current_price", 0),
            "change_24h": ch24,
            "change_7d":  ch7d,
            "vol_mcap":   round(vm, 3),
            "vol_ratio":  round(vol_ratio, 1),
            "avg_vol":    round(avg_vol, 0),
            "mcap":       mcap,
            "circ_pct":   round(circ_pct, 1),
            "stage":      stage,
            "risk_cat":   cat,
            "cold_start": _cold_start,   # True = no volume history, estimate-based
        })

    # Update last-seen 24h
    for coin in coins:
        sym  = coin.get("symbol", "").upper()
        ch24 = coin.get("price_change_percentage_24h") or 0
        seen_24h[sym] = {"ch24": ch24, "ts": now_ts}
    data["last_seen_24h"] = seen_24h
    _save(data)

    candidates.sort(key=lambda x: (0 if x["stage"] == "EARLY" else 1, -x["vol_ratio"]))
    return candidates


# ── Alert sending ─────────────────────────────────────────────────────────────

def send_whale_ride_alerts(
    candidates: list[dict],
    fear_greed: dict | None = None,
) -> list[dict]:
    """
    Send Telegram alert for each new candidate.
    Enforces: duplicate suppression (24h) only — no cap on active watches.
    Returns candidates for which alerts were actually sent.
    """
    from src.utils.telegram import send_telegram

    data     = _load()
    active   = data["active"]
    now_ts   = time.time()
    now_iso  = datetime.now(timezone.utc).isoformat()
    sent: list[dict] = []

    for c in candidates:
        sym = c["symbol"]

        # Duplicate suppression: 24h cooldown
        prev = active.get(sym)
        if prev and now_ts - prev.get("alert_ts", 0) < _DUPLICATE_HOURS * 3600:
            age_h = (now_ts - prev.get("alert_ts", 0)) / 3600
            print(f"  ℹ️  {sym} — already alerted {age_h:.1f}h ago (24h cooldown, re-sends at {_DUPLICATE_HOURS}h)")
            continue

        stage     = c["stage"]
        price     = c["price"]
        ch24      = c["change_24h"]
        ch7d      = c["change_7d"]
        vm        = c["vol_mcap"]
        mcap_str  = _fmt_mcap(c["mcap"])
        stage_icon  = "🟢" if stage == "EARLY" else "🟡"
        vol_ratio   = c.get("vol_ratio", 0)
        avg_vol_fmt = _fmt_mcap(c.get("avg_vol", 0))

        risk_line   = (f"\n  ⚠️  Risk flag: {c['risk_cat']}"
                       if c["risk_cat"] not in ("NORMAL", "SUSPICIOUS") else "")
        supply_line = (f"\n  ⚠️  Low float: {c['circ_pct']:.0f}% circ supply"
                       if c["circ_pct"] < 30 else "")
        cold_line   = (f"\n  ⚠️  No vol history — estimate-based signal (treat as higher risk)"
                       if c.get("cold_start") else "")
        fg_line     = ""
        if fear_greed:
            fg_val = fear_greed.get("value", 50)
            if fg_val < 25:
                fg_line = f"\n  ⚠️  F&amp;G = {fg_val} (Extreme Fear) — extra caution"

        name_str = c.get("name", sym)
        msg = (
            f"🐋 <b>Potential whale ride — invest €100 and hope for the best</b>\n\n"
            f"  <b>{sym}</b> ({name_str})\n\n"
            f"  Price:      {_fmt_price(price)}\n"
            f"  24h:        {ch24:+.1f}%\n"
            f"  7d:         {ch7d:+.0f}%\n"
            f"  Vol spike:  {vol_ratio:.0f}x normal (avg {avg_vol_fmt}/day)\n"
            f"  vol/mcap:   {vm:.3f}x\n"
            f"  MCap:       {mcap_str}\n"
            f"  Stage:      {stage_icon} <b>{stage}</b>"
            f"{risk_line}{supply_line}{cold_line}{fg_line}\n\n"
            f"  🎯 Target: +50% to +200% (no fixed TP)\n"
            f"  🛑 Exit: 24h momentum &lt; +5% OR RSI &gt; 85\n"
            f"  ⏱️ Watch hourly — not a 3-7 day hold\n"
            f"  ⚠️ Manual trade only"
        )

        try:
            ok = send_telegram(msg)
        except Exception as e:
            print(f"  ⚠️  Whale ride alert failed for {sym}: {e}")
            ok = False

        if ok:
            sent.append(c)
            active[sym] = {
                "alert_ts":      now_ts,
                "alert_time":    now_iso,
                "alert_price":   price,
                "stage":         stage,
                "ch7d_at_alert": ch7d,
                "ch24_at_alert": ch24,
            }
            # Track in session set — exit signals are gated on this
            _whale_entry_alerts_sent.add(sym)
            print(f"  🐋 WHALE RIDE ALERT sent: {sym} [{stage}] "
                  f"{ch24:+.1f}% 24h | vol {c.get('vol_ratio', 0):.0f}x normal")
            # Log to recommendations.csv so it appears in the track record
            try:
                from src.utils.logger import log_whale_rider_alert
                fg_val = (fear_greed or {}).get("value", 50)
                log_whale_rider_alert(c, fg_val)
            except Exception as _le:
                print(f"  ⚠️  Could not log whale rider to CSV: {_le}")
        else:
            print(f"  ⚠️  Whale ride alert NOT delivered for {sym} — skipping active tracking")

    if sent:
        data["active"] = active
        _save(data)

    return sent


# ── Exit signal check ─────────────────────────────────────────────────────────

def check_exit_signals(
    coins: list[dict],
    rsi_map: dict[str, float | None] | None = None,
) -> list[str]:
    """
    For each tracked active alert: send exit Telegram if 24h < +5% OR RSI > 85.
    Removes triggered coins from active tracking.
    """
    from src.utils.telegram import send_telegram

    data   = _load()
    active = data["active"]
    if not active:
        return []

    rsi_map    = rsi_map or {}
    coin_map   = {c.get("symbol", "").upper(): c for c in coins}
    to_remove: list[str] = []
    triggered: list[str] = []

    for sym, alert in active.items():
        coin = coin_map.get(sym)
        if not coin:
            continue

        # Only send exit alerts for coins that had an entry alert in this session.
        # Prevents phantom exits for stale active[] entries from previous runs
        # that were added under old/different criteria.
        if sym not in _whale_entry_alerts_sent:
            continue

        ch24        = coin.get("price_change_percentage_24h") or 0
        current     = coin.get("current_price", 0)
        alert_price = alert.get("alert_price", 0)
        rsi         = rsi_map.get(sym)
        stage       = alert.get("stage", "?")

        exit_reason: str | None = None
        if ch24 < _EXIT_24H_FLOOR:
            exit_reason = f"24h momentum slowing ({ch24:+.1f}% &lt; +{_EXIT_24H_FLOOR:.0f}%)"
        elif rsi is not None and rsi > _EXIT_RSI_CEIL:
            exit_reason = f"RSI {rsi:.1f} &gt; {_EXIT_RSI_CEIL:.0f} (overbought)"

        if not exit_reason:
            continue

        pnl_str = ""
        if alert_price > 0 and current > 0:
            pnl_pct = (current - alert_price) / alert_price * 100
            pnl_str = f" ({pnl_pct:+.1f}% from alert price)"

        msg = (
            f"🐋 <b>WHALE EXIT SIGNAL — {sym}</b>\n\n"
            f"  Current:   {_fmt_price(current)}{pnl_str}\n"
            f"  Signal:    {exit_reason}\n"
            f"  Stage was: {stage}\n\n"
            f"  💬 Consider taking profit <b>NOW</b>.\n"
            f"  This is not automatic — your decision."
        )
        try:
            send_telegram(msg)
            triggered.append(sym)
            to_remove.append(sym)
            plain_reason = exit_reason.replace("&lt;", "<").replace("&gt;", ">")
            print(f"  🛑 WHALE EXIT sent: {sym} — {plain_reason}")
            # Close the CSV row for this whale_rider position
            try:
                from src.utils.logger import close_whale_rider_position
                close_whale_rider_position(sym, current, exit_reason=plain_reason)
            except Exception as _ce:
                print(f"  ⚠️  Could not close whale rider CSV row: {_ce}")
        except Exception:
            pass

    if to_remove:
        for sym in to_remove:
            active.pop(sym, None)
        data["active"] = active
        _save(data)

    return triggered


# ── Late-stage watchlist + post-crash bounce ──────────────────────────────────

def display_late_stage(coins: list[dict]) -> None:
    """
    Track LATE stage coins (7d > +100%) in watchlist.
    No alert sent — just console print and peak tracking.
    Post-crash bounce detection is handled by check_post_crash_bounces().
    """
    data      = _load()
    watchlist = data["late_watchlist"]
    now_ts    = time.time()
    late      = []

    for coin in coins:
        sym   = coin.get("symbol", "").upper()
        ch7d  = coin.get("price_change_percentage_7d_in_currency") or 0
        price = coin.get("current_price", 0)

        if ch7d < _7D_LATE_MIN:
            continue

        late.append(coin)

        # Track peak price for post-crash entry detection
        entry = watchlist.get(sym, {})
        peak  = entry.get("peak_price", 0)
        if price > peak:
            watchlist[sym] = {
                "peak_price": price,
                "peak_ts":    now_ts,
                "added_ts":   entry.get("added_ts", now_ts),
                "ch7d_peak":  ch7d,
            }

    if late:
        print(f"\n  ⏳  LATE STAGE PUMPS (>+{_7D_LATE_MIN:.0f}% 7d — watchlist only)\n" + "-" * 60)
        for c in late:
            sym   = c.get("symbol", "?")
            ch7d  = c.get("price_change_percentage_7d_in_currency") or 0
            price = c.get("current_price", 0)
            mcap  = (c.get("market_cap") or 0) / 1e6
            print(f"  ⏳ {sym:8s} {_fmt_price(price)}  7d: {ch7d:+.0f}%  MCap: ${mcap:.0f}M"
                  f"  → Wait for -{_CRASH_DROP_PCT:.0f}% crash then RSI <{_CRASH_RSI_MAX:.0f}")

    data["late_watchlist"] = watchlist
    _save(data)


def check_post_crash_bounces(
    coins: list[dict],
    rsi_map: dict[str, float | None] | None = None,
) -> list[str]:
    """
    For coins in the late-stage watchlist: if price has dropped ≥40% from the
    tracked peak AND RSI < 40 AND vol/mcap < 0.30x (volume dying) → send
    POST-CRASH ENTRY alert.

    Returns list of symbols that triggered the alert.
    """
    from src.utils.telegram import send_telegram

    data      = _load()
    watchlist = data["late_watchlist"]
    if not watchlist:
        return []

    rsi_map   = rsi_map or {}
    coin_map  = {c.get("symbol", "").upper(): c for c in coins}
    triggered: list[str] = []

    for sym, entry in list(watchlist.items()):
        coin = coin_map.get(sym)
        if not coin:
            continue

        peak    = entry.get("peak_price", 0)
        current = coin.get("current_price", 0)
        if peak <= 0 or current <= 0:
            continue

        drop_pct = (peak - current) / peak * 100
        rsi      = rsi_map.get(sym)
        vol      = coin.get("total_volume") or 0
        mcap     = coin.get("market_cap") or 1
        vm       = vol / max(mcap, 1)

        if drop_pct < _CRASH_DROP_PCT:
            continue
        if rsi is None or rsi >= _CRASH_RSI_MAX:
            continue
        if vm >= _CRASH_VOL_MAX:
            continue   # volume still too high — not a stable bottom yet

        pct_from_peak_str = f"{drop_pct:.0f}"
        msg = (
            f"🐋 <b>POST-CRASH ENTRY — {sym}</b>\n\n"
            f"  Dropped {pct_from_peak_str}% from peak {_fmt_price(peak)}\n"
            f"  Current:   {_fmt_price(current)}\n"
            f"  RSI:       {rsi:.1f} (oversold)\n"
            f"  vol/mcap:  {vm:.2f}x (volume dying — stable bottom)\n\n"
            f"  🎯 TP: +25% | SL: -15% | Max hold: 48h\n"
            f"  💬 Whale bounce opportunity — manual entry.\n"
            f"  ⚠️ Max 5% portfolio — high risk."
        )
        try:
            send_telegram(msg)
            triggered.append(sym)
            print(f"  🐋 POST-CRASH ENTRY alert sent: {sym} "
                  f"(dropped {drop_pct:.0f}% from peak, RSI {rsi:.1f})")
            # Remove from watchlist after firing (one alert per crash)
            watchlist.pop(sym, None)
        except Exception as e:
            print(f"  ⚠️  Post-crash alert failed for {sym}: {e}")

    if triggered:
        data["late_watchlist"] = watchlist
        _save(data)

    return triggered
