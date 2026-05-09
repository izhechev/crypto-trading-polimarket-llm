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
_VOL_PRE_MULT     = 5.0    # today vol > 5x 30d avg → PRE-PUMP (early accumulation)
_VOL_EARLY_MULT   = 2.0    # today vol > 2x 30d avg → EARLY signal (fear market = lower baseline)
_VOL_MID_MULT     = 8.0    # today vol > 8x 30d avg → MID (confirmed pump)
_PRICE_PRE_MIN    = 2.0    # 24h minimum for PRE stage
_PRICE_PRE_MAX    = 7.0    # PRE cap — above this qualifies for EARLY instead
_PRICE_EARLY_MIN  = 5.0    # 24h price change minimum for EARLY (lowered from 10%)
_PRICE_EARLY_MAX  = 30.0   # above this → MID territory
_PRICE_MID_MAX    = 100.0  # above this → LATE (too late to enter)

# LATE stage: price already >100% 7d → watchlist only, wait for crash
_7D_LATE_MIN      = 100.0

# Tokens that should never be whale ride candidates (confirmed scam/rugged only)
_BLACKLIST: frozenset[str] = frozenset({
    "FTT",    # FTX collapse token — fully worthless
    "RLC",    # delisted RLC/BTC on Binance Mar 2026, KuCoin margin ended Jan 2026
    "KNC",    # post-pump exhaustion, no organic protocol demand
    "ORDI",   # high BTC correlation + amplified downside
})

# Shared hard disqualifiers
_MIN_MCAP         = 20_000_000  # $20M
_MIN_CIRC_PCT     = 15.0        # circulating supply minimum
_MIN_ENTRY_AGE_H  = 1.0         # hours before exit can fire after entry (avoids same-scan exits)
_VOL_HISTORY_DAYS = 30          # days of volume history to keep per coin
_MIN_HISTORY_DAYS = 3           # need at least 3 days of data before firing alerts

# PRE-PUMP extra filters (tighter — compensates for earlier, riskier entry)
_PRE_MIN_HISTORY  = 14          # need 14 days vol history — no cold-start estimates
_PRE_MIN_MCAP     = 50_000_000  # $50M minimum (2.5x stricter than regular)
_PRE_COOLDOWN_HOURS = 4         # re-alert PRE stage every 4h (not 24h)

# ── Exit / post-crash thresholds ──────────────────────────────────────────────
_MAX_HOLD_HOURS   = 24.0   # whale rides are short-term — exit after 24h
_EXIT_24H_FLOOR   = 5.0    # 24h below this → momentum dying → exit signal
_EXIT_RSI_CEIL    = 85.0   # RSI above this → overbought → exit signal

_CRASH_DROP_PCT   = 30.0   # post-crash bounce: must have dropped ≥30% from peak
_CRASH_RSI_MAX    = 30.0   # RSI must be oversold (<30) for bounce entry
_CRASH_VOL_MAX    = 0.30   # volume must be dying (low vol = stable bottom)

# ── Alert deduplication ───────────────────────────────────────────────────────
_DUPLICATE_HOURS  = 2      # re-alert same coin every 2h (was 24h)

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
            raw.setdefault("exited_ts", {})
            raw.setdefault("pre_alerts", {})
            return raw
    except Exception:
        pass
    return {"active": {}, "late_watchlist": {}, "last_seen_24h": {}, "exited_ts": {}, "pre_alerts": {}}


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

        if sym in _BLACKLIST:
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

        if vol < 1_000_000:
            continue  # fake/zero volume — no real whale action possible

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
            # Cold-start fallback: no 3-day history yet.
            # Baseline estimated_avg = mcap * 0.05 (coins normally trade ~5% vol/mcap).
            # Fires when vol/mcap > 0.10 (2x the baseline × _VOL_EARLY_MULT).
            if mcap > 0 and ch24 >= _PRICE_EARLY_MIN:
                _est_avg = mcap * 0.05
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
        elif (
            not _cold_start
            and vol_ratio >= _VOL_PRE_MULT
            and _PRICE_PRE_MIN <= ch24 < _PRICE_PRE_MAX
            and mcap >= _PRE_MIN_MCAP
            and len(hist.get(sym, [])) >= _PRE_MIN_HISTORY
        ):
            stage = "PRE"
        else:
            continue   # no anomaly

        # 24h momentum check vs previous scan
        # NOTE: only PRE requires strictly accelerating momentum.
        # EARLY/MID only die if ch24 drops BELOW the stage's entry floor —
        # the 24h rolling window naturally decays as the pump ages, so a coin
        # at +34% peaking to +21% should still fire, not get silenced.
        prev_24h_entry = seen_24h.get(sym)
        if prev_24h_entry:
            prev_ts = prev_24h_entry.get("ts", 0)
            age_h   = (now_ts - prev_ts) / 3600
            if age_h < 6:
                if stage == "PRE":
                    prev_ch24 = prev_24h_entry.get("ch24", ch24)
                    if ch24 <= prev_ch24:
                        continue   # PRE requires strictly accelerating 24h%
                elif stage == "EARLY" and ch24 < _PRICE_EARLY_MIN:
                    continue   # pump unwound below entry floor
                elif stage == "MID" and ch24 < _PRICE_EARLY_MAX:
                    continue   # pump unwound below MID floor
        elif stage == "PRE":
            continue   # PRE with no prior reading = can't confirm acceleration

        vm = vol / max(mcap, 1)
        candidates.append({
            "symbol":       sym,
            "name":         coin.get("name", sym),
            "coin_id":      coin.get("_cg_id") or coin.get("id", ""),
            "price":        coin.get("current_price", 0),
            "change_24h":   ch24,
            "change_7d":    ch7d,
            "vol_mcap":     round(vm, 3),
            "vol_ratio":    round(vol_ratio, 1),
            "vol_usd":      round(vol, 0),
            "avg_vol_usd":  round(avg_vol, 0),
            "mcap":         mcap,
            "circ_pct":     round(circ_pct, 1),
            "ath_change":   coin.get("ath_change_percentage") or 0,
            "stage":        stage,
            "risk_cat":     cat,
            "cold_start":   _cold_start,
        })

    # Update last-seen 24h
    for coin in coins:
        sym  = coin.get("symbol", "").upper()
        ch24 = coin.get("price_change_percentage_24h") or 0
        seen_24h[sym] = {"ch24": ch24, "ts": now_ts}
    data["last_seen_24h"] = seen_24h
    _save(data)

    def _whale_score(c: dict) -> float:
        # vol_ratio × vol_mcap: rewards both anomaly strength AND price-impact power.
        # A coin with 15x multiplier but 0.01 vol/mcap moves less than one with
        # 3x multiplier and 4x vol/mcap — the latter is the actual whale setup.
        return c["vol_ratio"] * c["vol_mcap"]

    candidates.sort(key=lambda x: (
        {"PRE": 0, "EARLY": 1, "MID": 2}.get(x["stage"], 3),
        -_whale_score(x),
    ))
    return candidates


# ── Alert sending ─────────────────────────────────────────────────────────────

def send_whale_ride_alerts(
    candidates: list[dict],
    fear_greed: dict | None = None,
) -> list[dict]:
    """
    Send one batch Telegram message: "WHALE RIDE DETECTED — TOP 3".
    Re-sends every 2h per coin. Returns candidates that were newly tracked.
    """
    from src.utils.telegram import send_telegram

    data     = _load()
    active   = data["active"]
    now_ts   = time.time()
    now_iso  = datetime.now(timezone.utc).isoformat()
    sent: list[dict] = []

    pre_alerts = data.setdefault("pre_alerts", {})
    fg_value   = (fear_greed or {}).get("value", 50)

    # ── High-Conviction Filter Logic ──────────────────────────────────────────
    high_conviction = []
    for c in candidates:
        mcap      = c["mcap"]
        vol_mult  = c["vol_ratio"]
        vol_mcap  = c["vol_mcap"]
        ch24      = c["change_24h"]
        ch7d      = c["change_7d"]
        ath_dist  = c["ath_change"]
        stage     = c["stage"]
        sym       = c["symbol"]

        # 1. HARD FILTERS
        if mcap > 400_000_000: continue
        
        # Vol multiplier filter
        vol_threshold = 2.0 if mcap < 30_000_000 else 3.0
        if vol_mult < vol_threshold: continue
        
        if vol_mcap > 0.60: continue
        if not (5.0 <= ch24 <= 30.0): continue
        if ath_dist > -20.0: continue
        
        # Stage filter: EARLY preferred, MID only if 7d > 100%
        if stage not in ("EARLY", "MID"): continue
        if stage == "MID" and ch7d <= 100.0: continue

        # 2. BONUS SCORE
        score = 0
        if mcap < 50_000_000: score += 2
        if ch7d > 25.0:       score += 2
        if vol_mult > 5.0:    score += 1
        if ath_dist < -80.0:  score += 1
        if 0.10 <= vol_mcap <= 0.35: score += 1
        
        # Persistence check (2+ times in 48h)
        sym_data = active.get(sym, {})
        hits = sym_data.get("hits", [])
        if len([h for h in hits if now_ts - h <= 48 * 3600]) >= 2:
            score += 1
        
        if score >= 3:
            c["hc_score"] = score
            high_conviction.append(c)

    high_conviction.sort(key=lambda x: -x["hc_score"])
    
    hc_lines = []
    for c in high_conviction:
        hc_lines.append(
            f"  {c['symbol']:6} | {_fmt_mcap(c['mcap']):>5} | {c['vol_ratio']:>4.1f}x | {c['vol_mcap']:>4.2f}x | "
            f"{c['change_24h']:>+5.1f}% | {c['change_7d']:>+5.1f}% | {c['ath_change']:>4.0f}% | {c['hc_score']}/8"
        )
    
    hc_msg = ""
    if hc_lines:
        hc_msg = "🔥 <b>HIGH-CONVICTION WHALE SIGNALS</b>\n" + \
                 "<pre>TICKER | mcap  | volM | v/mc | 24h%  | 7d%   | ATH% | score</pre>\n" + \
                 "\n".join(hc_lines) + "\n\n"
    elif candidates:
        hc_msg = "❌ No high-conviction signals this round.\n\n"

    # ── Original Batch Logic ──────────────────────────────────────────────────
    # Count total open positions — cap auto-opens at 50 total (Normal market)
    _open_count = 0
    try:
        from src.utils.logger import _read as _log_read
        _open_count = sum(1 for r in _log_read() if r.get("status") == "OPEN")
    except Exception:
        pass

    is_neutral = fg_value >= 40
    _MAX_TOTAL_POSITIONS = 50 if is_neutral else 30
    _MAX_WHALE_AUTO      = 20 if is_neutral else 10
    _auto_opened         = 0
    fg_line  = f"\n⚠️ F&amp;G = {fg_value} ({'Neutral' if is_neutral else 'Fear'}) — {'Aggressive' if is_neutral else 'Conservative'} mode"

    top10 = candidates[:10]
    lines = []
    for rank, c in enumerate(top10, 1):
        sym         = c["symbol"]
        name        = c.get("name", sym)
        stage       = c["stage"]
        price_usd   = c["price"]
        ch24        = c["change_24h"]
        ch7d        = c.get("change_7d", 0)
        vol_ratio   = c.get("vol_ratio", 0)
        vol_mcap    = c.get("vol_mcap", 0)
        vol_usd     = c.get("vol_usd", 0)
        avg_vol_usd = c.get("avg_vol_usd", 0)
        mcap        = c.get("mcap", 0)
        circ_pct    = c.get("circ_pct", 100)
        ath_chg     = c.get("ath_change", 0)
        cold        = " ❄️" if c.get("cold_start") else ""
        stage_icon  = {"PRE": "🔵", "EARLY": "🟢", "MID": "🟡"}.get(stage, "⚪")
        mcap_str    = _fmt_mcap(mcap) if mcap else "?"
        vol_str     = _fmt_mcap(vol_usd) if vol_usd else "?"
        avg_str     = _fmt_mcap(avg_vol_usd) if avg_vol_usd else "?"
        ath_str     = f"{ath_chg:+.0f}% from ATH" if ath_chg else ""
        circ_str    = f"circ {circ_pct:.0f}%" if circ_pct < 100 else ""
        extra       = "  |  ".join(x for x in (ath_str, circ_str) if x)
        _coin_id = c.get("coin_id", "")
        _link    = f' <a href="https://www.coingecko.com/en/coins/{_coin_id}">CG</a>' if _coin_id else ""
        lines.append(
            f"  #{rank} <b>{sym}</b> ({name}) {stage_icon} {stage}{cold}{_link}\n"
            f"     {_fmt_price(price_usd)}  24h {ch24:+.1f}%  7d {ch7d:+.0f}%\n"
            f"     vol {vol_str} ({vol_ratio:.1f}x avg {avg_str})  vol/mcap {vol_mcap:.2f}x\n"
            f"     mcap {mcap_str}" + (f"  |  {extra}" if extra else "")
        )

    if lines or hc_msg:
        batch_msg = (
            hc_msg +
            f"🐋 <b>WHALE RIDE DETECTED — TOP {len(top10)}</b>{fg_line}\n\n"
            + "\n\n".join(lines)
            + "\n\n  ⚠️ Manual trade only — invest $100 max per signal"
        )
        try:
            send_telegram(batch_msg)
            # print(f"  🐋 WHALE RIDE batch sent: {', '.join(c['symbol'] for c in top10)}")
        except Exception as e:
            print(f"  ⚠️  Whale ride batch alert failed: {e}")

    # Track every candidate in active dict (for exit signal monitoring)
    for c in candidates:
        sym   = c["symbol"]
        stage = c["stage"]
        ch24  = c["change_24h"]
        ch7d      = c["change_7d"]
        price_usd = c["price"]

        if stage == "PRE":
            pre_alerts[sym] = {"ts": now_ts, "price": price_usd}
            continue

        sym_data = active.setdefault(sym, {})
        hits = sym_data.setdefault("hits", [])
        hits.append(now_ts)
        # Keep only last 48h
        sym_data["hits"] = [h for h in hits if now_ts - h <= 48 * 3600]

        prev_age_h = (now_ts - sym_data.get("alert_ts", 0)) / 3600
        is_new = sym_data.get("alert_ts") is None or prev_age_h >= _DUPLICATE_HOURS

        # Bug fix: if alerted but failed to open in CSV (due to prior bug),
        # force a retry even if not "new" by age.
        _already_open = False
        try:
            from src.utils.logger import _read as _log_read
            _already_open = any(
                r.get("coin", "").upper() == sym.upper()
                and r.get("status") == "OPEN"
                and r.get("type") == "WHALE_RIDE"
                for r in _log_read()
            )
        except Exception:
            pass

        if not _already_open:
            is_new = True  # force retry if not yet in CSV

        sym_data.update({
            "alert_ts":        now_ts,
            "alert_time":      now_iso,
            "alert_price":     price_usd,
            "alert_price_usd": price_usd,
            "stage":           stage,
            "ch7d_at_alert":   ch7d,
            "ch24_at_alert":   ch24,
        })
        
        if is_new:
            sent.append(c)
            _whale_entry_alerts_sent.add(sym)
            # Auto-open position: top 3 EARLY only, skip if too many open
            _should_open = (
                stage == "EARLY"
                and _auto_opened < _MAX_WHALE_AUTO
                and _open_count < _MAX_TOTAL_POSITIONS
            )
            try:
                from src.utils.logger import log_whale_rider_alert
                _was_logged = log_whale_rider_alert(c, fg_value, open_position=_should_open)
                if _should_open and _was_logged:
                    _auto_opened += 1
                    _open_count   += 1
            except Exception:
                pass

    data["active"]     = active
    data["pre_alerts"] = pre_alerts
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

    now_ts     = time.time()
    rsi_map    = rsi_map or {}
    coin_map   = {c.get("symbol", "").upper(): c for c in coins}
    to_remove: list[str] = []
    triggered: list[str] = []

    for sym, alert in active.items():
        coin = coin_map.get(sym)
        if not coin:
            continue

        # Enforce minimum hold before exit can fire — prevents same-scan exits
        # caused by data source mismatch between detection and exit check feeds.
        alert_ts = alert.get("alert_ts", 0)
        if (now_ts - alert_ts) < _MIN_ENTRY_AGE_H * 3600:
            continue

        ch24        = coin.get("price_change_percentage_24h") or 0
        current     = coin.get("current_price", 0)
        alert_price = alert.get("alert_price_usd", alert.get("alert_price", 0))
        rsi         = rsi_map.get(sym)
        stage       = alert.get("stage", "?")

        exit_reason: str | None = None
        age_h = (now_ts - alert_ts) / 3600

        if age_h > _MAX_HOLD_HOURS:
            exit_reason = f"Max hold reached ({age_h:.0f}h &gt; {_MAX_HOLD_HOURS:.0f}h)"
        elif ch24 < _EXIT_24H_FLOOR:
            exit_reason = f"24h momentum slowing ({ch24:+.1f}% &lt; +{_EXIT_24H_FLOOR:.0f}%)"
        elif rsi is not None and rsi > _EXIT_RSI_CEIL:
            exit_reason = f"RSI {rsi:.1f} &gt; {_EXIT_RSI_CEIL:.0f} (overbought)"

        if not exit_reason:
            continue

        pnl_pct = (current - alert_price) / alert_price * 100 if alert_price > 0 and current > 0 else 0.0
        pnl_str = f" ({pnl_pct:+.1f}% from alert price)" if alert_price > 0 and current > 0 else ""

        # Failure diagnosis when position never reached +15%
        failure_note = ""
        if pnl_pct < 15.0:
            _ch24_entry = alert.get("ch24_at_alert", 0)
            _fail_notes = []
            if stage == "PRE":
                _fail_notes.append("PRE-stage (riskier early entry)")
            if _ch24_entry >= 20:
                _fail_notes.append(f"+{_ch24_entry:.0f}% already moved at entry")
            _plain_exit = exit_reason.replace("&lt;", "<").replace("&gt;", ">")
            if "momentum" in _plain_exit.lower():
                _cause = "momentum reversed"
            elif "Max hold" in _plain_exit:
                _cause = "24h hold expired"
            elif "RSI" in _plain_exit:
                _cause = "RSI overbought exit"
            else:
                _cause = "pump stalled"
            failure_note = (
                f"\n\n  ❌ <b>Never reached +15%</b> — {_cause}"
                + (f"\n  ℹ️  Signal at entry: {' | '.join(_fail_notes)}" if _fail_notes else "")
            )

        _cid_exit = alert.get("coin_id", "")
        _link_exit = f'\n  🔗 <a href="https://www.coingecko.com/en/coins/{_cid_exit}">CoinGecko</a>' if _cid_exit else ""
        msg = (
            f"🐋 <b>WHALE EXIT SIGNAL — {sym}</b>\n\n"
            f"  Current:   {_fmt_price(current)}{pnl_str}\n"
            f"  Signal:    {exit_reason}\n"
            f"  Stage was: {stage}{_link_exit}"
            f"{failure_note}\n\n"
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
        exited = data.setdefault("exited_ts", {})
        for sym in to_remove:
            active.pop(sym, None)
            exited[sym] = now_ts
        data["active"] = active
        _save(data)

    return triggered


# ── Sector rotation detection ────────────────────────────────────────────────

# Cooldown: once a sector alert fires, suppress it for this many hours
_SECTOR_COOLDOWN_HOURS = 6

# Minimum coins in the same sector to trigger a rotation alert
_SECTOR_MIN_COINS = 3

# Sector labels → emoji for Telegram display
_SECTOR_EMOJI: dict[str, str] = {
    "ai":      "🤖",
    "depin":   "📡",
    "layer1":  "⛓️",
    "layer2":  "🔗",
    "defi":    "🏦",
    "meme":    "🐸",
    "privacy": "🔒",
    "rwa":     "🏛️",
}


def detect_sector_rotation(candidates: list[dict]) -> list[dict]:
    """
    Group whale ride candidates by sector. Return sectors where ≥3 coins qualify
    simultaneously — a reliable signal of narrative-driven capital rotation.

    Returns list of dicts: {sector, coins (sorted by vol_ratio desc), avg_vol_ratio}
    Only fires for sectors not already alerted within _SECTOR_COOLDOWN_HOURS.
    """
    try:
        from src.agents.scanner import _SECTOR_MAP
    except Exception:
        return []

    if not candidates:
        return []

    data     = _load()
    now_ts   = time.time()
    cooldown = data.setdefault("sector_rotation_ts", {})

    from collections import defaultdict
    sector_hits: dict[str, list[dict]] = defaultdict(list)

    for c in candidates:
        sym = c["symbol"]
        for sector, syms in _SECTOR_MAP.items():
            if sym in syms:
                sector_hits[sector].append(c)

    rotations: list[dict] = []
    for sector, coins in sector_hits.items():
        if len(coins) < _SECTOR_MIN_COINS:
            continue
        last_fired = cooldown.get(sector, 0)
        if now_ts - last_fired < _SECTOR_COOLDOWN_HOURS * 3600:
            age_h = (now_ts - last_fired) / 3600
            print(f"  ℹ️  Sector {sector} — alerted {age_h:.1f}h ago (cooldown {_SECTOR_COOLDOWN_HOURS}h)")
            continue
        coins_sorted = sorted(coins, key=lambda x: -x["vol_ratio"])
        rotations.append({
            "sector":        sector,
            "coins":         coins_sorted,
            "avg_vol_ratio": round(sum(c["vol_ratio"] for c in coins_sorted) / len(coins_sorted), 1),
            "coin_count":    len(coins_sorted),
        })

    return rotations


def send_sector_rotation_alerts(
    rotations: list[dict],
    fear_greed: dict | None = None,
) -> None:
    """
    Send one Telegram alert per detected sector rotation.
    Records fire time to enforce cooldown between alerts.
    """
    from src.utils.telegram import send_telegram

    if not rotations:
        return

    data     = _load()
    now_ts   = time.time()
    cooldown = data.setdefault("sector_rotation_ts", {})

    for rot in rotations:
        sector    = rot["sector"]
        coins     = rot["coins"]
        emoji     = _SECTOR_EMOJI.get(sector, "📊")
        best      = coins[0]
        others    = coins[1:]

        fg_line = ""
        if fear_greed:
            fg_val = fear_greed.get("value", 50)
            if fg_val < 25:
                fg_line = f"\n  ⚠️  F&amp;G = {fg_val} (Extreme Fear) — rotation may be brief"

        best_price = _fmt_price(best["price"])
        coin_lines = (
            f"  🥇 <b>{best['symbol']}</b> ({best['name']})  "
            f"{best_price}  {best['change_24h']:+.1f}%  vol {best['vol_ratio']:.0f}x"
        )
        for c in others[:2]:
            coin_lines += (
                f"\n  ▪️  {c['symbol']:8s}  {c['change_24h']:+.1f}%  vol {c['vol_ratio']:.0f}x"
            )
        if len(coins) > 3:
            coin_lines += f"\n  +{len(coins) - 3} more in sector"

        msg = (
            f"{emoji} <b>SECTOR ROTATION — {sector.upper()}</b>\n\n"
            f"  {rot['coin_count']} coins spiking simultaneously  "
            f"(avg vol {rot['avg_vol_ratio']:.0f}x normal)\n\n"
            f"{coin_lines}"
            f"{fg_line}\n\n"
            f"  💡 Capital rotating into <b>{sector}</b> narrative.\n"
            f"  Best entry: <b>{best['symbol']}</b> (highest vol ratio)\n"
            f"  🛑 Exit: same rules as whale rides (24h &lt; +5% OR RSI &gt; 85)\n"
            f"  ⚠️ Manual trade only — sector rotation can reverse fast"
        )

        try:
            ok = send_telegram(msg)
        except Exception as e:
            print(f"  ⚠️  Sector rotation alert failed ({sector}): {e}")
            ok = False

        if ok:
            cooldown[sector] = now_ts
            print(f"  {emoji} SECTOR ROTATION alert: {sector.upper()} "
                  f"({len(coins)} coins, avg {rot['avg_vol_ratio']:.0f}x) — best: {best['symbol']}")

    data["sector_rotation_ts"] = cooldown
    _save(data)


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
        # print(f"\n  ⏳  LATE STAGE PUMPS (>+{_7D_LATE_MIN:.0f}% 7d — watchlist only)\n" + "-" * 60)
        for c in late:
            sym   = c.get("symbol", "?")
            ch7d  = c.get("price_change_percentage_7d_in_currency") or 0
            price = c.get("current_price", 0)
            mcap  = (c.get("market_cap") or 0) / 1e6
            # print(f"  ⏳ {sym:8s} {_fmt_price(price)}  7d: {ch7d:+.0f}%  MCap: ${mcap:.0f}M"
            #       f"  → Wait for -{_CRASH_DROP_PCT:.0f}% crash then RSI <{_CRASH_RSI_MAX:.0f}")
            pass

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
