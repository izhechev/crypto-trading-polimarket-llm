"""Recommendation logger with live position tracking."""
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

LOG_PATH          = config.DATA_DIR / "recommendations.csv"
HISTORY_PATH      = config.DATA_DIR / "price_history.csv"
DUST_THRESHOLD_EUR = 0.10   # holdings below this are labelled "dust"

import re as _re


# ── WIN analysis helpers ──────────────────────────────────────────────────

def _parse_entry_signals(reasoning: str) -> dict:
    """
    Extract structured signals from the reasoning string stored in the CSV.
    Returns dict with boolean/numeric fields for pattern analysis.
    """
    r = reasoning.lower()

    # MACD bullish: "macd+rsi+vol confirmed", "macd bullish", etc.
    macd_bullish = bool(_re.search(r'macd\+rsi\+vol|macd.*bullish', r))
    if _re.search(r'macd bearish|full bearish alignment|momentum stall', r):
        macd_bullish = False

    # Vol/mcap ratio — first number after "vol/mcap"
    vol_mcap: float | None = None
    m = _re.search(r'vol/mcap\s*([\d.]+)x', r)
    if m:
        try:
            vol_mcap = float(m.group(1))
        except ValueError:
            pass

    # RSI — first number after "rsi"
    rsi: float | None = None
    m = _re.search(r'rsi\s*([\d.]+)', r)
    if m:
        try:
            rsi = float(m.group(1))
        except ValueError:
            pass

    return {
        "macd_bullish":  macd_bullish,
        "vol_mcap":      vol_mcap,
        "rsi":           rsi,
        "bb_below":      "below lower bb" in r,
        "bb_above":      "above upper bb" in r,
        "coiled_spring": "coiled spring" in r,
        "dip_7d":        bool(_re.search(r'7d dip', r)),
    }


def print_win_analysis(row: dict) -> None:
    """
    Print a post-WIN breakdown for a just-closed scanner pick.
    Shows why the trade worked, which signals predicted it, and a lesson.
    """
    coin = row.get("coin", "?").upper()
    try:
        pnl    = float(row["pnl_pct"])
        entry  = float(row["entry_price"])
        exit_p = float(row["exit_price"])
    except (ValueError, KeyError, TypeError):
        return

    days_str = "?"
    try:
        open_dt  = datetime.strptime(row["date"],       "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
        close_dt = datetime.strptime(row["close_date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
        d = max(0, (close_dt - open_dt).days)
        days_str = f"{d} day{'s' if d != 1 else ''}"
    except Exception:
        pass

    sigs   = _parse_entry_signals(row.get("reasoning", ""))
    fg_val: int | None = None
    try:
        fg_val = int(row.get("fear_greed", ""))
    except (ValueError, TypeError):
        pass

    print(f"\n  ✅ WIN: {coin} {pnl:+.1f}% ({days_str})")
    print(f"     WHY IT WORKED:")

    if fg_val is not None:
        zone = (
            "extreme fear buy zone" if fg_val < 20 else
            "fear zone — contrarian entry" if fg_val < 40 else
            "neutral market" if fg_val < 60 else
            "greed zone"
        )
        print(f"     - Entry during F&G={fg_val} ({zone})")

    if sigs["vol_mcap"] is not None:
        vm = sigs["vol_mcap"]
        lbl = ("strong buying pressure" if vm > 0.5
               else "elevated buying activity" if vm > 0.2
               else "moderate volume")
        print(f"     - Vol/mcap {vm:.2f}x at entry ({lbl})")

    if sigs["macd_bullish"]:
        ctx = ("contrarian — MACD bullish while market was fearful"
               if fg_val is not None and fg_val < 40
               else "momentum confirmed by MACD")
        print(f"     - {ctx}")

    if sigs["rsi"] is not None:
        r_val = sigs["rsi"]
        lbl   = ("deeply oversold" if r_val < 30
                 else "oversold" if r_val < 40
                 else f"RSI {r_val:.0f}")
        print(f"     - RSI {r_val:.1f} at entry ({lbl} — bounce setup)")

    if sigs["bb_below"]:
        print(f"     - Price below lower Bollinger Band (mean-reversion setup)")
    if sigs["coiled_spring"]:
        print(f"     - Coiled spring: deep ATH discount + exhausted sellers")

    print(f"     - Entered ${entry:.4f} → exited ${exit_p:.4f}")

    # Build 1-line lesson from dominant signals
    lessons: list[str] = []
    if fg_val is not None and fg_val < 25 and sigs["macd_bullish"]:
        lessons.append("MACD bullish + extreme fear = high-probability bounce")
    elif sigs["macd_bullish"]:
        lessons.append("bullish MACD at entry is a reliable leading signal")
    if sigs["vol_mcap"] and sigs["vol_mcap"] > 0.30:
        lessons.append("high vol/mcap confirms genuine buying interest")
    if sigs["bb_below"]:
        lessons.append("below lower BB + volume = clean mean-reversion trade")
    if sigs["coiled_spring"]:
        lessons.append("coiled spring + catalyst = explosive bounce potential")
    if lessons:
        print(f"     LESSON: {' | '.join(lessons[:2])}")


def print_win_patterns() -> None:
    """
    Surface the most predictive entry signals across all WIN trades.
    Shows signal hit-rates and the best single-combo win rate.
    Only prints when there are ≥2 wins (avoids misleading stats on tiny samples).
    """
    _excluded = ("PORTFOLIO", "WATCHLIST", "WHALE_RIDE", "WHALE_MILESTONE")
    rows  = _read()
    wins  = [
        r for r in rows
        if r.get("status") == "WIN"
        and r.get("type", "SCANNER") not in _excluded
    ]
    n = len(wins)
    if n < 2:
        return

    closed = [
        r for r in rows
        if r.get("status") in ("WIN", "LOSS")
        and r.get("type", "SCANNER") not in _excluded
    ]

    counts: dict[str, int] = {
        "macd_bullish":  0,
        "fg_fear":       0,
        "high_vol":      0,
        "bb_below":      0,
        "coiled_spring": 0,
    }
    days_list: list[float] = []
    pnl_list:  list[float] = []

    for r in wins:
        sigs = _parse_entry_signals(r.get("reasoning", ""))
        if sigs["macd_bullish"]:
            counts["macd_bullish"] += 1
        if sigs["vol_mcap"] is not None and sigs["vol_mcap"] > 0.20:
            counts["high_vol"] += 1
        if sigs["bb_below"]:
            counts["bb_below"] += 1
        if sigs["coiled_spring"]:
            counts["coiled_spring"] += 1
        try:
            if int(r.get("fear_greed", 100)) < 25:
                counts["fg_fear"] += 1
        except (ValueError, TypeError):
            pass
        try:
            open_dt  = datetime.strptime(r["date"],       "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            close_dt = datetime.strptime(r["close_date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            days_list.append((close_dt - open_dt).total_seconds() / 86400)
        except Exception:
            pass
        try:
            pnl_list.append(float(r["pnl_pct"]))
        except (ValueError, TypeError):
            pass

    avg_days = sum(days_list) / len(days_list) if days_list else 0
    avg_pnl  = sum(pnl_list)  / len(pnl_list)  if pnl_list  else 0

    print(f"\n  📊 WIN PATTERNS ({n} win{'s' if n != 1 else ''}):")
    # Only surface signals present in ≥40% of wins
    threshold = max(2, n * 0.40)
    for key, label in [
        ("macd_bullish",  "had MACD bullish at entry"),
        ("fg_fear",       "entered during F&G < 25 (extreme fear)"),
        ("high_vol",      "had vol/mcap > 0.20x"),
        ("bb_below",      "were below lower Bollinger Band"),
        ("coiled_spring", "were coiled spring setups"),
    ]:
        c = counts[key]
        if c >= threshold:
            pct = c / n * 100
            print(f"     - {c}/{n} ({pct:.0f}%) {label}")

    print(f"     - Avg days to close:  {avg_days:.1f}")
    print(f"     - Avg P&L per WIN:    {avg_pnl:+.1f}%")

    # Best combo: MACD bullish + F&G < 25 — compute win rate from ALL closed trades
    combo_wins = combo_total = 0
    for r in closed:
        sigs = _parse_entry_signals(r.get("reasoning", ""))
        fg_ok = False
        try:
            fg_ok = int(r.get("fear_greed", 100)) < 25
        except (ValueError, TypeError):
            pass
        if sigs["macd_bullish"] and fg_ok:
            combo_total += 1
            if r.get("status") == "WIN":
                combo_wins += 1
    if combo_total >= 2:
        rate = combo_wins / combo_total * 100
        print(f"     BEST SIGNAL: MACD bullish + F&G < 25 = {rate:.0f}% win rate ({combo_wins}/{combo_total} closed)")

    # Qualifier win-rate breakdown (only when enough data exists)
    qualifier_stats: dict[str, dict] = {}
    for r in closed:
        q = r.get("qualifier", "")
        if not q:
            continue
        s = qualifier_stats.setdefault(q, {"wins": 0, "total": 0})
        s["total"] += 1
        if r.get("status") == "WIN":
            s["wins"] += 1
    if qualifier_stats:
        print(f"     QUALIFIER WIN RATES:")
        for q, s in sorted(qualifier_stats.items(), key=lambda x: -x[1]["wins"] / max(x[1]["total"], 1)):
            if s["total"] >= 2:
                qrate = s["wins"] / s["total"] * 100
                print(f"       {q:22s} {qrate:.0f}%  ({s['wins']}/{s['total']})")


def print_lose_patterns() -> None:
    """
    Surface the most common entry signals across all LOSS trades.
    Shows signal hit-rates, avg days held, avg P&L, and the worst-performing
    signal combo to help the system avoid repeating the same mistakes.
    Only prints when there is ≥1 loss.
    """
    rows   = _read()
    _excluded = ("PORTFOLIO", "WATCHLIST", "WHALE_RIDE", "WHALE_MILESTONE")
    losses = [
        r for r in rows
        if r.get("status") == "LOSS"
        and r.get("type", "SCANNER") not in _excluded
    ]
    n = len(losses)
    if n < 1:
        return

    closed = [
        r for r in rows
        if r.get("status") in ("WIN", "LOSS")
        and r.get("type", "SCANNER") not in _excluded
    ]

    counts: dict[str, int] = {
        "macd_bullish":  0,
        "macd_bearish":  0,
        "fg_fear":       0,
        "fg_greed":      0,
        "high_vol":      0,
        "bb_above":      0,
        "rsi_high":      0,
    }
    days_list: list[float] = []
    pnl_list:  list[float] = []

    for r in losses:
        sigs = _parse_entry_signals(r.get("reasoning", ""))
        if sigs["macd_bullish"]:
            counts["macd_bullish"] += 1
        else:
            counts["macd_bearish"] += 1
        if sigs["vol_mcap"] is not None and sigs["vol_mcap"] > 0.20:
            counts["high_vol"] += 1
        if sigs["bb_above"]:
            counts["bb_above"] += 1
        if sigs["rsi"] is not None and sigs["rsi"] > 65:
            counts["rsi_high"] += 1
        try:
            fg = int(r.get("fear_greed", 100))
            if fg < 25:
                counts["fg_fear"] += 1
            elif fg > 60:
                counts["fg_greed"] += 1
        except (ValueError, TypeError):
            pass
        try:
            open_dt  = datetime.strptime(r["date"],       "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            close_dt = datetime.strptime(r["close_date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            days_list.append((close_dt - open_dt).total_seconds() / 86400)
        except Exception:
            pass
        try:
            pnl_list.append(float(r["pnl_pct"]))
        except (ValueError, TypeError):
            pass

    avg_days = sum(days_list) / len(days_list) if days_list else 0
    avg_pnl  = sum(pnl_list)  / len(pnl_list)  if pnl_list  else 0

    print(f"\n  📉 LOSE PATTERNS ({n} loss{'es' if n != 1 else ''}):")
    threshold = 1 if n <= 3 else max(2, n * 0.40)
    for key, label in [
        ("macd_bearish",  "had NO MACD bullish at entry (weak momentum)"),
        ("macd_bullish",  "had MACD bullish but still lost (false signal)"),
        ("fg_greed",      "entered during F&G > 60 (greed zone)"),
        ("bb_above",      "were above upper Bollinger Band (overbought entry)"),
        ("rsi_high",      "had RSI > 65 at entry (buying top)"),
        ("high_vol",      "had high vol/mcap but still lost"),
        ("fg_fear",       "entered during F&G < 25 (fear didn't save them)"),
    ]:
        c = counts[key]
        if c >= threshold:
            pct = c / n * 100
            print(f"     - {c}/{n} ({pct:.0f}%) {label}")

    print(f"     - Avg days to stop-loss:  {avg_days:.1f}")
    print(f"     - Avg P&L per LOSS:       {avg_pnl:+.1f}%")

    # Worst combos: check multiple avoid-signal patterns, show any with >= 1 sample
    _avoid_combos = [
        (
            "no MACD + F&G > 60 (greed entry, no momentum)",
            lambda r, s: not s["macd_bullish"] and _fg_above(r, 60),
        ),
        (
            "MACD bullish + F&G < 25 (extreme fear, momentum faded)",
            lambda r, s: s["macd_bullish"] and _fg_below(r, 25),
        ),
        (
            "no MACD + F&G < 30 (no momentum, fear zone)",
            lambda r, s: not s["macd_bullish"] and _fg_below(r, 30),
        ),
    ]

    def _fg_above(r: dict, val: int) -> bool:
        try:
            return int(r.get("fear_greed", 0)) > val
        except (ValueError, TypeError):
            return False

    def _fg_below(r: dict, val: int) -> bool:
        try:
            fg = r.get("fear_greed")
            return fg is not None and int(fg) < val
        except (ValueError, TypeError):
            return False

    for _combo_label, _combo_fn in _avoid_combos:
        _c_losses = _c_total = 0
        for r in closed:
            sigs = _parse_entry_signals(r.get("reasoning", ""))
            if _combo_fn(r, sigs):
                _c_total += 1
                if r.get("status") == "LOSS":
                    _c_losses += 1
        if _c_total >= 1 and _c_losses >= 1:
            rate = _c_losses / _c_total * 100
            print(f"     AVOID SIGNAL: {_combo_label} = {rate:.0f}% lose rate ({_c_losses}/{_c_total} closed)")

_HEADERS = [
    "date", "type", "coin", "coin_id",
    "entry_price",    # USD
    "stop_loss",      # USD
    "take_profit",    # USD
    "status",         # OPEN / WIN / LOSS / EXCLUDED / "" (watchlist)
    "exit_price",     # USD, filled when closed
    "close_date",     # UTC timestamp when WIN/LOSS/EXCLUDED was set
    "pnl_pct",        # % — currency-neutral
    "current_price",  # USD
    "price_eur",      # EUR
    "timeframe", "fear_greed", "reasoning",
    "groq_rank",      # 1/2/3 — Groq's ranking among top picks (empty if not a Groq pick)
    "qualifier",      # INSTANT_QUALIFIER | NEWS_BOOST | OVERSOLD_VOL | BASE_SCORE
    "key_signal",     # the one signal that pushed it into Groq's top 3
]

_HISTORY_HEADERS = ["timestamp", "coin", "coin_id", "price_eur", "price_usd"]

_W = 48  # section divider width


# ── Internal helpers ──────────────────────────────────────────────────────

def _open_with_retry(path: Path, mode: str = "r", retries: int = 6, delay: float = 0.5, **kwargs):
    """Open a file, retrying on PermissionError (e.g. OneDrive sync lock)."""
    for attempt in range(retries):
        try:
            return open(path, mode, **kwargs)
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(delay)


def _read() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    with _open_with_retry(LOG_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write(rows: list[dict]) -> None:
    # SAFETY: never silently drop OPEN scanner positions.
    # Allow status to change OPEN → WIN/LOSS (legitimate close) — only error if the
    # row is completely absent from rows (that would be data loss).
    existing_open  = [r for r in _read() if r.get("type", "") == "SCANNER" and r.get("status") == "OPEN"]
    existing_coins = {r["coin"].upper() for r in existing_open}
    # Any coin that appears in rows as a SCANNER row (any status) is still tracked
    all_scanner_coins = {r["coin"].upper() for r in rows if r.get("type", "") == "SCANNER"}
    dropped = existing_coins - all_scanner_coins
    if dropped:
        raise RuntimeError(
            f"BUG: _write() would delete OPEN scanner positions entirely: {dropped}. "
            "Aborting write to protect trade history."
        )
    with _open_with_retry(LOG_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_HEADERS, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)


def _latest_per_coin(rows: list[dict]) -> dict[str, dict]:
    """Return the most-recent row keyed by coin symbol."""
    latest: dict[str, dict] = {}
    for r in rows:
        coin = r["coin"]
        if coin not in latest or r["date"] > latest[coin]["date"]:
            latest[coin] = r
    return latest


def _fmt(eur: float, usd: float) -> str:
    """Format as '€X.XXXX ($Y.YYYY)' choosing decimal places by magnitude."""
    decimals = 2 if eur >= 1 else 4 if eur >= 0.01 else 6 if eur >= 0.0001 else 8
    return f"€{eur:.{decimals}f} (${usd:.{decimals}f})"


# ── Public API ────────────────────────────────────────────────────────────

def log_recommendation(rec: dict, fear_greed_value: int) -> None:
    """
    Append a new scanner recommendation with status=OPEN and type=SCANNER.
    Skips if the same coin already has an OPEN scanner position.
    If the coin previously closed as WIN/LOSS, re-opens a new position and notes it.
    """
    rows = _read()
    coin = rec.get("coin", "").upper()

    # Duplicate check — avoid logging the same pick while it's still open.
    already_open = any(
        r.get("type", "") in ("SCANNER", "")
        and r.get("status") == "OPEN"
        and r.get("coin", "").upper() == coin
        for r in rows
    )
    if already_open:
        print(f"  Skipped — {coin} already has an OPEN scanner position")
        return

    # Category guard: if this coin has an OPEN WHALE_RIDE entry, close it as EXCLUDED
    # before opening a SCANNER position. Scanner pick takes precedence — never mix categories.
    for r in rows:
        if (r.get("type") == "WHALE_RIDE"
                and r.get("status") == "OPEN"
                and r.get("coin", "").upper() == coin):
            r["status"] = "EXCLUDED"
            r["reasoning"] = (
                "[SCANNER PICK SUPERSEDES WHALE_RIDE — category corrected] "
                + r.get("reasoning", "")
            )
            print(f"  Closed WHALE_RIDE for {coin} — superseded by scanner pick (category fix)")

    # Find most-recent closed trade for this coin to add re-entry note
    prev_note = ""
    closed_trades = [
        r for r in rows
        if r.get("coin", "").upper() == coin
        and r.get("type", "") in ("SCANNER", "")
        and r.get("status") in ("WIN", "LOSS", "TIME EXIT", "EXCLUDED")
    ]
    if closed_trades:
        last = max(closed_trades, key=lambda r: r.get("date", ""))
        prev_status = last.get("status", "")

        # 48h cooldown after LOSS — let the coin stabilize before re-entering
        if prev_status == "LOSS":
            try:
                last_dt = datetime.strptime(last["date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if hours_since < 48:
                    print(f"  Cooldown — {coin} closed as LOSS {hours_since:.0f}h ago, skipping for 48h")
                    return
            except Exception:
                pass

        # 7-day cooldown after EXCLUDED — fundamental crisis (web validation reject),
        # not just a price dip. Give the situation time to resolve.
        # Exception: if the CURRENT scan's web validation returned CONFIRM, the
        # issue has resolved → lift the exclusion and allow re-entry.
        if prev_status == "EXCLUDED":
            try:
                last_dt = datetime.strptime(last["date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if hours_since < 168:  # 7 days
                    if rec.get("web_research_verdict") == "CONFIRM":
                        # Web validation re-confirmed → clear the exclusion flag so
                        # _build_excluded_cooldown_set won't suppress it from display either.
                        last["status"] = "EXCLUDED_CLEARED"
                        _write(rows)
                        print(f"  ✅ {coin} exclusion lifted — web validation CONFIRM overrides {hours_since:.0f}h cooldown")
                    else:
                        days_left = (168 - hours_since) / 24
                        print(f"  Cooldown — {coin} EXCLUDED {hours_since:.0f}h ago (fundamental issue), {days_left:.1f}d remaining")
                        return
            except Exception:
                pass

        try:
            prev_pnl = float(last.get("pnl_pct", 0))
            prev_note = f" | new position (prev: {prev_status} {prev_pnl:+.1f}%)"
        except (ValueError, TypeError):
            prev_note = f" | new position (prev: {prev_status})"
        print(f"  Re-opening {coin} — {prev_note.strip(' | ')}")

    base_reasoning = rec.get("reasoning", "").replace("\n", " ")
    rows.append({
        "date":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "type":          "SCANNER",
        "coin":          coin,
        "coin_id":       rec.get("coin_id", ""),
        "entry_price":   rec.get("entry_price", ""),
        "stop_loss":     rec.get("stop_loss", ""),
        "take_profit":   rec.get("take_profit", ""),
        "status":        "OPEN",
        "exit_price":    "",
        "pnl_pct":       "",
        "current_price": rec.get("entry_price", ""),
        "price_eur":     "",
        "timeframe":     rec.get("timeframe", ""),
        "fear_greed":    fear_greed_value,
        "reasoning":     base_reasoning + prev_note,
    })
    _write(rows)
    print(f"  Logged → {LOG_PATH}")


def _tp_sl_for_fg(entry: float, fear_greed_value: int) -> tuple[float, float]:
    """
    Return (take_profit, stop_loss) based on Fear & Greed index.

      F&G <  20  → TP +10%  | SL -10%  (extreme fear — tight targets)
      F&G 20-40  → TP +15%  | SL -12%  (fear — moderate targets)
      F&G >  40  → TP +20%  | SL -15%  (neutral/greed — wider targets)
    """
    if fear_greed_value < 20:
        tp_mult, sl_mult = 1.10, 0.90
    elif fear_greed_value <= 40:
        tp_mult, sl_mult = 1.15, 0.88
    else:
        tp_mult, sl_mult = 1.20, 0.85
    return round(entry * tp_mult, 8), round(entry * sl_mult, 8)


def log_scanner_results(top10: list[dict], fear_greed_value: int) -> None:
    """
    Log every coin in the top-10 scanner results as an OPEN scanner pick.
    Auto-calculates TP/SL based on Fear & Greed:
      F&G < 20  → TP +10%  | SL -10%
      F&G 20-40 → TP +15%  | SL -12%
      F&G > 40  → TP +20%  | SL -15%
    Groq can later sharpen SL/TP via update_scanner_sltp().
    """
    logged = 0
    for r in top10:
        entry = r.get("price", 0)
        if not entry:
            continue
        take_profit, stop_loss = _tp_sl_for_fg(entry, fear_greed_value)
        reasons = r.get("reasons", [])
        rec = {
            "coin":        r["symbol"],
            "coin_id":     r["coin_id"],
            "entry_price": round(entry, 8),
            "stop_loss":   stop_loss,
            "take_profit": take_profit,
            "timeframe":   "3-7 days",
            "reasoning":   f"Score {r['score']}. " + ", ".join(reasons),
        }
        rows_before = len(_read())
        log_recommendation(rec, fear_greed_value)
        if len(_read()) > rows_before:
            logged += 1
    if logged:
        print(f"  {logged} new scanner picks logged")


def log_whale_ride(wr: dict, fear_greed_value: int) -> None:
    """
    Log a whale ride candidate as type=WHALE_RIDE.
    SL = -15%, TP = +50%, max hold 24h (serial scam) or 48h.
    Skips if this coin already has an OPEN WHALE_RIDE position.
    Skips if this coin is already logged as SCANNER (never mix categories).
    """
    rows = _read()
    coin   = wr.get("symbol", "").upper()
    cp_id  = wr.get("coin_id", "")
    mcap   = wr.get("market_cap", 0) or 0
    entry  = wr.get("entry", 0) or 0

    # Bug 5a: skip generic/ambiguous symbols (too short = price collision risk)
    if len(coin) <= 2:
        print(f"  Skipped WHALE_RIDE — {coin}: symbol too short (collision risk)")
        return

    # Bug 5b: skip micro-cap coins (< $10M mcap)
    if 0 < mcap < 10_000_000:
        print(f"  Skipped WHALE_RIDE — {coin}: mcap ${mcap/1e6:.1f}M < $10M")
        return

    # Bug 5c: skip zero or sub-satoshi entries (untrackable precision)
    if entry <= 0:
        print(f"  Skipped WHALE_RIDE — {coin}: entry price is zero")
        return
    if entry < 0.000001:
        print(f"  Skipped WHALE_RIDE — {coin}: entry ${entry:.2e} too small to track reliably")
        return

    from datetime import timedelta
    _7d_ago = datetime.now(timezone.utc) - timedelta(days=7)

    def _row_date(r: dict):
        try:
            raw = r.get("date", "")
            return datetime.fromisoformat(raw.replace(" UTC", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    # Bug 4: dedup by symbol OR coin_id (prevents same coin logged twice)
    already_open = any(
        r.get("type") == "WHALE_RIDE"
        and r.get("status") in ("OPEN", "EXCLUDED")
        and (r.get("coin", "").upper() == coin
             or (cp_id and r.get("coin_id", "") == cp_id))
        and _row_date(r) >= _7d_ago
        for r in rows
    )
    if already_open:
        print(f"  Skipped WHALE_RIDE — {coin} already has an open or recently excluded whale ride (within 7d)")
        return

    # Never log as WHALE_RIDE if coin already has an OPEN SCANNER position
    open_as_scanner = any(
        r.get("type", "SCANNER") in ("SCANNER", "")
        and r.get("status") == "OPEN"
        and r.get("coin", "").upper() == coin
        for r in rows
    )
    if open_as_scanner:
        print(f"  Skipped WHALE_RIDE — {coin} is already open as SCANNER (category guard)")
        return

    # Note previous cycles in reasoning
    cycles_str = " → ".join(wr.get("known_cycles", [])) or "no prior cycles"
    scam_note  = " | SERIAL SCAM" if wr.get("is_serial_scam") else ""
    reasoning  = (
        f"Cycle #{wr.get('cycle_number',1)} | {cycles_str}"
        f" | crash: {wr.get('crash_reason','')}"
        f" | max_hold: {wr.get('max_hold_hours',48)}h{scam_note}"
    )

    rows.append({
        "date":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "type":          "WHALE_RIDE",
        "coin":          coin,
        "coin_id":       wr.get("coin_id", ""),
        "entry_price":   round(wr.get("entry", 0), 8),
        "stop_loss":     round(wr.get("stop_loss", 0), 8),
        "take_profit":   round(wr.get("take_profit", 0), 8),
        "status":        "OPEN",
        "exit_price":    "",
        "pnl_pct":       "",
        "current_price": round(wr.get("entry", 0), 8),
        "price_eur":     "",
        "timeframe":     f"{wr.get('max_hold_hours', 48)}h max",
        "fear_greed":    fear_greed_value,
        "reasoning":     reasoning,
    })
    _write(rows)
    print(f"  Whale ride logged → {coin} (cycle #{wr.get('cycle_number',1)})")


def log_whale_rider_alert(c: dict, fear_greed_value: int) -> None:
    """
    Log a whale_rider module pump alert (EARLY/MID stage) to recommendations.csv.
    Only called after a successful Telegram send.
    SL/TP = 0 — manual trade, no automated stop/target.
    Closed by close_whale_rider_position() when exit signal fires.
    """
    rows = _read()
    coin = c.get("symbol", "").upper()

    # Dedup: skip if already OPEN or EXCLUDED WHALE_RIDE within 7d
    from datetime import timedelta
    _7d_ago = datetime.now(timezone.utc) - timedelta(days=7)

    def _row_date_wr(r: dict):
        try:
            return datetime.fromisoformat(r.get("date", "").replace(" UTC", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    if any(
        r.get("type") == "WHALE_RIDE"
        and r.get("status") in ("OPEN", "EXCLUDED")
        and r.get("coin", "").upper() == coin
        and _row_date_wr(r) >= _7d_ago
        for r in rows
    ):
        return

    stage = c.get("stage", "?")
    ch7d  = c.get("change_7d", 0)
    ch24  = c.get("change_24h", 0)
    vm    = c.get("vol_mcap", 0)
    price = c.get("price", 0)

    reasoning = (
        f"[WHALE_RIDER] {stage} | 7d {ch7d:+.0f}% | 24h {ch24:+.1f}% | "
        f"vol/mcap {vm:.2f}x | Manual trade only | max_hold: 720h"
    )

    rows.append({
        "date":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "type":          "WHALE_RIDE",
        "coin":          coin,
        "coin_id":       c.get("coin_id", ""),
        "entry_price":   round(price, 8),
        "stop_loss":     0,                         # no SL — ride until +200% or expiry
        "take_profit":   round(price * 3.00, 8),   # +200%
        "status":        "OPEN",
        "exit_price":    "",
        "pnl_pct":       "",
        "current_price": round(price, 8),
        "price_eur":     "",
        "timeframe":     "manual",
        "fear_greed":    fear_greed_value,
        "reasoning":     reasoning,
    })
    _write(rows)
    print(f"  Whale rider logged → {coin} [{stage}] SL=-10% TP=+200%")


def close_whale_rider_position(sym: str, current_price: float, exit_reason: str = "") -> None:
    """
    Whale rider exit signal — closes the position immediately as WIN or LOSS.
    Called when momentum slows, RSI overbought, or other exit conditions fire.
    """
    rows    = _read()
    changed = False
    _now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for row in rows:
        if (row.get("type") == "WHALE_RIDE"
                and row.get("status") == "OPEN"
                and row.get("coin", "").upper() == sym.upper()
                and "[WHALE_RIDER]" in row.get("reasoning", "")):
            try:
                entry   = float(row["entry_price"])
                pnl_pct = (current_price - entry) / entry * 100 if entry > 0 else 0.0
            except (ValueError, KeyError):
                pnl_pct = 0.0

            status = "WIN" if pnl_pct >= 0 else "LOSS"
            note   = f" | EXIT_SIGNAL: {exit_reason}" if exit_reason else " | EXIT_SIGNAL"
            row["status"]        = status
            row["exit_price"]    = round(current_price, 8)
            row["close_date"]    = _now_str
            row["pnl_pct"]       = round(pnl_pct, 2)
            row["current_price"] = round(current_price, 8)
            row["reasoning"]     = row.get("reasoning", "") + note
            changed = True
            icon = "✅" if status == "WIN" else "❌"
            print(f"  {icon} Whale rider CLOSED {status} → {sym} {pnl_pct:+.1f}%"
                  + (f" ({exit_reason})" if exit_reason else ""))
            break

    if changed:
        _write(rows)


def update_scanner_sltp(
    coin: str,
    stop_loss: float,
    take_profit: float,
    reasoning: str = "",
) -> None:
    """
    Update SL/TP (and optionally reasoning) for an existing OPEN scanner position.
    Called after Groq analysis to sharpen the auto-calculated levels.
    """
    rows = _read()
    updated = False
    for row in rows:
        if (row.get("type", "") in ("SCANNER", "")
                and row.get("status") == "OPEN"
                and row.get("coin", "").upper() == coin.upper()):
            try:
                entry_f = float(row.get("entry_price") or 0)
                orig_sl = float(row.get("stop_loss") or 0)
                orig_tp = float(row.get("take_profit") or 0)

                if entry_f > 0:
                    # TP cap: never exceed entry × 1.25
                    max_tp = round(entry_f * 1.25, 8)
                    if take_profit > max_tp:
                        print(f"  TP capped at entry×1.25 for {coin.upper()}: ${take_profit:.6f} → ${max_tp:.6f}")
                        take_profit = max_tp

                    # SL floor: never widen (move further from entry) beyond the F&G-based original.
                    # Groq can only tighten the SL (move it closer to entry), not loosen it.
                    if orig_sl > 0 and stop_loss < orig_sl:
                        print(f"  SL floor kept for {coin.upper()}: Groq ${stop_loss:.6f} < original ${orig_sl:.6f} — keeping original")
                        stop_loss = orig_sl
            except (ValueError, TypeError):
                pass
            row["stop_loss"]   = stop_loss
            row["take_profit"] = take_profit
            if reasoning:
                row["reasoning"] = reasoning.replace("\n", " ")
            updated = True
            break
    if not updated:
        print(f"  Skipped SL/TP update for {coin.upper()} — no OPEN position (excluded/cooldown?)")
        return
    _write(rows)
    print(f"  SL/TP updated for {coin.upper()} → SL ${stop_loss:.6f}, TP ${take_profit:.6f}")


def update_groq_rank(coin: str, groq_rank: int, qualifier: str, key_signal: str) -> None:
    """
    Stamp Groq's rank, qualifier, and key_signal onto an existing OPEN scanner row.
    Called after Groq returns its top picks so the data is available for win-rate analysis.
    """
    rows   = _read()
    coin_u = coin.upper()
    for row in rows:
        if (row.get("type", "") in ("SCANNER", "")
                and row.get("status") == "OPEN"
                and row.get("coin", "").upper() == coin_u):
            row["groq_rank"]  = groq_rank
            row["qualifier"]  = qualifier
            row["key_signal"] = (key_signal or "")[:120]
            _write(rows)
            return


def update_open_positions() -> None:
    """
    Refresh prices for all OPEN SCANNER and WHALE_RIDE positions.
    - current >= take_profit → WIN
    - current <= stop_loss   → LOSS
    - WHALE_RIDE expired (max_hold_hours elapsed) → WIN or LOSS based on P&L
    - otherwise → stays OPEN, updates current_price + price_eur + pnl_pct
    All price comparisons use USD.
    """
    rows = _read()
    scanner_open = [
        r for r in rows
        if r.get("status") == "OPEN"
        and r.get("coin_id")
        and r.get("type", "SCANNER") in ("SCANNER", "WHALE_RIDE")
    ]
    if not scanner_open:
        return

    # Build lookup maps: coin_id → (usd, eur)
    coin_ids = list({r["coin_id"] for r in scanner_open})
    usd_map: dict[str, float] = {}
    eur_map: dict[str, float] = {}

    # Try CoinGecko first (handles legacy CG IDs)
    try:
        from src.connectors.coingecko import fetch_prices
        price_objs = fetch_prices(coin_ids)
        usd_map = {p.coin_id: p.price_usd for p in price_objs}
        eur_map = {p.coin_id: p.price_eur for p in price_objs}
    except Exception as e:
        print(f"  Warning: CoinGecko price fetch failed: {e}")

    # CoinPaprika fallback for any positions not resolved by CoinGecko
    missing_ids = [cid for cid in coin_ids if cid not in usd_map]
    if missing_ids:
        try:
            from src.connectors.coinpaprika import fetch_tickers_for_scanner
            tickers = fetch_tickers_for_scanner(limit=1000)
            # Build symbol→price map and cp_id→price map from CP tickers
            _cp_sym_usd: dict[str, float] = {}
            _cp_id_usd:  dict[str, float] = {}
            for t in tickers:
                sym = t.get("symbol", "").upper()
                cid = t.get("_cp_id", "")
                usd = t.get("current_price") or 0
                if usd > 0:
                    _cp_sym_usd[sym] = usd
                    if cid:
                        _cp_id_usd[cid] = usd
            # Match missing positions by coin_id (CP format) or symbol
            EUR_RATE = 0.92
            for row in scanner_open:
                cid = row.get("coin_id", "")
                if cid in usd_map:
                    continue  # already resolved
                sym = row.get("coin", "").upper()
                usd = _cp_id_usd.get(cid) or _cp_sym_usd.get(sym)
                if usd:
                    usd_map[cid] = usd
                    eur_map[cid] = usd * EUR_RATE
        except Exception as e2:
            print(f"  Warning: CoinPaprika fallback failed: {e2}")

    if not usd_map:
        print("  Warning: could not fetch prices for tracking: all sources failed")
        return

    closed    = 0
    new_wins: list[dict] = []
    for row in rows:
        row_type = row.get("type", "SCANNER")
        if (row.get("status") != "OPEN"
                or not row.get("coin_id")
                or row_type not in ("SCANNER", "", "WHALE_RIDE")):
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

        # Bug 1 — price sanity: if entry is a "real" priced coin (>$1) and fetched
        # price is <$0.01, the coin_id resolved to a different coin → skip this update.
        # Also guard against >100× price spike within first 2 hours (bad ID match).
        if entry > 0:
            ratio = usd / entry
            try:
                _age_hrs = (datetime.now(timezone.utc) -
                            datetime.strptime(row["date"], "%Y-%m-%d %H:%M UTC")
                            .replace(tzinfo=timezone.utc)).total_seconds() / 3600
            except Exception:
                _age_hrs = 999
            _price_collision = (
                (entry > 0 and (ratio < 0.1 or ratio > 10))  # price deviates >90% from entry
                or (_age_hrs < 2 and (ratio > 50 or ratio < 0.02))  # impossible move in <2h
            )
            if _price_collision:
                print(f"  ⚠️  PRICE COLLISION skipped: {row.get('coin')} "
                      f"entry=${entry:.6f} fetched=${usd:.6f} (ratio {ratio:.1f}×)")
                continue

        pnl_pct = (usd - entry) / entry * 100
        row["current_price"] = round(usd, 6)
        row["price_eur"]     = round(eur_map.get(row["coin_id"], 0), 6)
        row["pnl_pct"]       = round(pnl_pct, 2)

        # Whale ride: milestone logging + SL/TP/time-expiry close
        if row_type == "WHALE_RIDE":
            reasoning = row.get("reasoning", "")
            import re as _re
            _now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            # Time-based expiry (Bug 6): close when max_hold_hours elapsed
            _wr_expired = False
            _hrs_open   = 0.0
            try:
                _entry_dt = datetime.strptime(row["date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                _hrs_open = (datetime.now(timezone.utc) - _entry_dt).total_seconds() / 3600
                _m = _re.search(r"max_hold:\s*(\d+)h", reasoning)
                _max_hold = int(_m.group(1)) if _m else 48
                _wr_expired = _hrs_open >= _max_hold
            except Exception:
                pass

            # Milestone logging — each level creates its own WIN record; position stays open
            _milestone_flags = {
                25:  ("[MILESTONE_25]",  1),
                50:  ("[MILESTONE_50]",  2),
                100: ("[MILESTONE_100]", 3),
                150: ("[MILESTONE_150]", 3),
                200: ("[MILESTONE_200]", 4),
            }
            for _pct, (_flag, _score) in sorted(_milestone_flags.items()):
                if pnl_pct >= _pct and _flag not in reasoning:
                    row["reasoning"] = reasoning + f" {_flag}"
                    reasoning = row["reasoning"]
                    _icon = "🌙" if _pct >= 200 else "🚀"
                    print(f"  {_icon} WHALE_RIDE MILESTONE: {row['coin']} hit +{_pct}% ({_score}pt) (current: {pnl_pct:+.1f}%)")
                    new_wins.append({**row, "_milestone": _pct, "_milestone_only": True})
                    _ms_record = {
                        "date":          row.get("date", ""),
                        "type":          "WHALE_MILESTONE",
                        "coin":          row.get("coin", ""),
                        "coin_id":       row.get("coin_id", ""),
                        "entry_price":   row.get("entry_price", ""),
                        "stop_loss":     "",
                        "take_profit":   "",
                        "status":        "WIN",
                        "exit_price":    round(entry * (1 + _pct / 100), 8),
                        "close_date":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                        "pnl_pct":       float(_pct),
                        "current_price": row.get("current_price", ""),
                        "price_eur":     row.get("price_eur", ""),
                        "timeframe":     "",
                        "fear_greed":    row.get("fear_greed", ""),
                        "reasoning":     f"[WHALE_MILESTONE +{_pct}% / {_score}pt] Partial win — position stays open",
                        "groq_rank":     _score,
                        "qualifier":     "WHALE_RIDE",
                        "key_signal":    "",
                    }
                    _already = any(
                        r.get("type") == "WHALE_MILESTONE"
                        and r.get("coin", "").upper() == _ms_record["coin"].upper()
                        and r.get("date", "") == _ms_record["date"]
                        and str(r.get("pnl_pct", "")) == str(_ms_record["pnl_pct"])
                        for r in rows
                    )
                    if not _already:
                        rows.append(_ms_record)

            # Pre-milestone hard SL at -15%: if +25% was never hit, cap the loss
            _is_principal_recovered = "PRINCIPAL_RECOVERED" in reasoning
            if not _is_principal_recovered and pnl_pct <= -15.0:
                row["status"]     = "LOSS"
                row["exit_price"] = round(usd, 6)
                row["close_date"] = _now_str
                closed += 1
                print(f"  🛑 WHALE_RIDE -15% SL: {row['coin']} {pnl_pct:+.1f}% → LOSS (pre-milestone)")
                continue

            # Close conditions (in priority order)
            if tp > 0 and usd >= tp:
                row["status"]     = "WIN"
                row["exit_price"] = round(usd, 6)
                row["close_date"] = _now_str
                closed += 1
                new_wins.append(row)
                print(f"  🌙 WHALE_RIDE TP HIT: {row['coin']} +200% → closed WIN")
            elif sl > 0 and usd <= sl:
                # Bug 2: honour SL for whale rides
                row["status"]     = "LOSS"
                row["exit_price"] = round(usd, 6)
                row["close_date"] = _now_str
                closed += 1
                print(f"  🛑 WHALE_RIDE SL HIT: {row['coin']} {pnl_pct:+.1f}% (entry ${entry:.6f} → ${usd:.6f} ≤ SL ${sl:.6f})")
            elif pnl_pct <= -99.9:
                row["status"]     = "LOSS"
                row["exit_price"] = round(usd, 6)
                row["close_date"] = _now_str
                closed += 1
                print(f"  WHALE_RIDE {row['coin']} worthless → LOSS {pnl_pct:+.1f}%")
            elif _wr_expired:
                # Bug 6: time limit expired → close WIN if profitable, LOSS otherwise
                _expire_status = "WIN" if pnl_pct > 0 else "LOSS"
                row["status"]     = _expire_status
                row["exit_price"] = round(usd, 6)
                row["close_date"] = _now_str
                closed += 1
                print(f"  ⏰ WHALE_RIDE EXPIRED: {row['coin']} {pnl_pct:+.1f}% after {_hrs_open:.0f}h → {_expire_status}")
                if _expire_status == "WIN":
                    new_wins.append(row)
            elif _hrs_open > 72 and pnl_pct < 10.0:
                # Stale position: open >72h with less than +10% gain — no longer worth holding
                _stale_status = "WIN" if pnl_pct > 0 else "LOSS"
                row["status"]     = _stale_status
                row["exit_price"] = round(usd, 6)
                row["close_date"] = _now_str
                closed += 1
                print(f"  ⏰ WHALE_RIDE STALE: {row['coin']} {pnl_pct:+.1f}% after {_hrs_open:.0f}h → {_stale_status}")
                if _stale_status == "WIN":
                    new_wins.append(row)
            continue

        _now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # TIME-BASED FORCE CLOSE: stale position → log as TIME EXIT or LOSS
        # Tier 1: age >= 7 days AND pnl < +5%  (includes losses — position is going nowhere)
        # Tier 2: age > 10 days AND pnl < +10% (keep winners running up to 10d)
        try:
            entry_dt  = datetime.strptime(row["date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            days_open = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 86400
            _tier1 = days_open >= 7  and pnl_pct < 5.0
            _tier2 = days_open > 10 and pnl_pct < 10.0
            if _tier1 or _tier2:
                _te_status = "LOSS" if pnl_pct < 0 else "TIME EXIT"
                row["status"]     = _te_status
                row["exit_price"] = round(usd, 6)
                row["close_date"] = _now_str
                closed += 1
                _reason = "≥7d <+5%" if _tier1 else ">10d <+10%"
                print(f"  [TIME EXIT] {row['coin']} {pnl_pct:+.1f}% ({days_open:.0f}d) — {_reason}")
                continue
        except Exception:
            pass

        if tp > 0 and usd >= tp:
            row["status"]     = "WIN"
            row["exit_price"] = round(usd, 6)
            row["close_date"] = _now_str
            closed += 1
            new_wins.append(row)
        elif sl > 0 and usd <= sl:
            row["status"]     = "LOSS"
            row["exit_price"] = round(usd, 6)
            row["close_date"] = _now_str
            closed += 1

    _write(rows)
    if closed:
        print(f"  {closed} position(s) closed (WIN/LOSS/TIME EXIT)")
    for win_row in new_wins:
        if win_row.get("_milestone_only"):
            _pct = win_row.get("_milestone", 0)
            _cur = win_row.get("pnl_pct", 0)
            _icon = "🌙" if _pct >= 200 else "🚀"
            print(f"\n  {_icon} WHALE_RIDE MILESTONE +{_pct}%: {win_row.get('coin', '?')} "
                  f"currently at {_cur:+.1f}% — HOLD, monitoring to 200%+")
        else:
            print_win_analysis(win_row)


def log_portfolio_positions() -> None:
    """
    Fetch holdings (Kraken live → portfolio.json fallback) and log
    a snapshot row per holding with type=PORTFOLIO.
    reasoning: "amount:16|src:Kraken"
    """
    from src.connectors.kraken import fetch_kraken_portfolio
    from src.connectors.coingecko import fetch_prices

    holdings, source = fetch_kraken_portfolio()
    if holdings is None:
        try:
            with open(config.PORTFOLIO_PATH) as f:
                pf = json.load(f)
            holdings = pf.get("holdings", [])
            source = "portfolio.json"
        except Exception as e:
            print(f"  Warning: could not load portfolio: {e}")
            return

    if not holdings:
        return

    coin_ids = [h["coin_id"] for h in holdings if h.get("coin_id")]
    if not coin_ids:
        print("  Warning: no coin IDs resolved for portfolio holdings")
        return

    try:
        price_objs = fetch_prices(coin_ids)
        usd_map = {p.coin_id: p.price_usd for p in price_objs}
        eur_map = {p.coin_id: p.price_eur for p in price_objs}
    except Exception as e:
        print(f"  Warning: could not fetch portfolio prices: {e}")
        return

    rows = _read()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    logged = 0

    for h in holdings:
        coin_id = h.get("coin_id")
        if not coin_id:
            continue
        usd = usd_map.get(coin_id)
        eur = eur_map.get(coin_id)
        if usd is None:
            continue

        entry = h.get("entry_price_usd")
        pnl_pct = round((usd - entry) / entry * 100, 2) if entry else ""

        rows.append({
            "date":          now,
            "type":          "PORTFOLIO",
            "coin":          h["asset"],
            "coin_id":       coin_id,
            "entry_price":   entry if entry is not None else "",
            "stop_loss":     "",
            "take_profit":   "",
            "status":        "OPEN",
            "exit_price":    "",
            "pnl_pct":       pnl_pct,
            "current_price": round(usd, 6),
            "price_eur":     round(eur, 6) if eur else "",
            "timeframe":     "",
            "fear_greed":    "",
            "reasoning":     f"amount:{h['amount']}|src:{source}",
        })
        logged += 1

    _write(rows)
    print(f"  Portfolio positions logged ({logged} holdings, source: {source})")


def log_watchlist_prices() -> None:
    """
    Fetch current prices for WATCHLIST_TRACK coins and log with type=WATCHLIST.
    reasoning: "7d:-4.52"
    """
    if not getattr(config, "WATCHLIST_TRACK", None):
        return

    from src.connectors.coingecko import fetch_prices
    try:
        price_list = fetch_prices(config.WATCHLIST_TRACK)
    except Exception as e:
        print(f"  Warning: could not fetch watchlist prices: {e}")
        return

    rows = _read()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for p in price_list:
        rows.append({
            "date":          now,
            "type":          "WATCHLIST",
            "coin":          p.symbol,
            "coin_id":       p.coin_id,
            "entry_price":   "",
            "stop_loss":     "",
            "take_profit":   "",
            "status":        "",
            "exit_price":    "",
            "pnl_pct":       "",
            "current_price": round(p.price_usd, 6),
            "price_eur":     round(p.price_eur, 6),
            "timeframe":     "",
            "fear_greed":    "",
            "reasoning":     f"7d:{p.change_7d:.2f}",
        })

    _write(rows)
    print(f"  Watchlist prices logged ({len(price_list)} coins)")


def log_price_history() -> None:
    """
    Append EUR+USD prices for all tracked coins to price_history.csv.
    Tracked = portfolio holdings + watchlist + open scanner positions.
    """
    from src.connectors.coingecko import fetch_prices

    coin_ids: set[str] = set()

    # Portfolio holdings
    try:
        with open(config.PORTFOLIO_PATH) as f:
            pf = json.load(f)
        for h in pf.get("holdings", []):
            if h.get("coin_id"):
                coin_ids.add(h["coin_id"])
    except Exception:
        pass

    # Watchlist
    for cid in getattr(config, "WATCHLIST_TRACK", []):
        coin_ids.add(cid)

    # Open scanner positions
    for r in _read():
        if (r.get("type", "SCANNER") == "SCANNER"
                and r.get("status") == "OPEN"
                and r.get("coin_id")):
            coin_ids.add(r["coin_id"])

    if not coin_ids:
        return

    try:
        prices = fetch_prices(list(coin_ids))
    except Exception as e:
        print(f"  Warning: price history fetch failed: {e}")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    write_header = not HISTORY_PATH.exists()
    try:
        fh = _open_with_retry(HISTORY_PATH, "a", newline="", encoding="utf-8")
    except PermissionError:
        print(f"  Warning: {HISTORY_PATH.name} still locked after retries — price history skipped")
        return
    with fh as f:
        writer = csv.DictWriter(f, fieldnames=_HISTORY_HEADERS)
        if write_header:
            writer.writeheader()
        for p in prices:
            writer.writerow({
                "timestamp": now,
                "coin":      p.symbol,
                "coin_id":   p.coin_id,
                "price_eur": round(p.price_eur, 6),
                "price_usd": round(p.price_usd, 6),
            })

    print(f"  Price history logged ({len(prices)} coins -> {HISTORY_PATH.name})")


def print_daily_activity() -> None:
    """
    Pure CSV calculation — no API calls, no Groq.
    Shows what happened today: opens, closes, P&L, best/worst open position.
    Assumes €100 allocation per scanner position.
    """
    rows = _read()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ALLOC = 100.0  # € per position

    # Opened today — SCANNER rows with date = today
    opened = [
        r for r in rows
        if r.get("type", "SCANNER") in ("SCANNER", "", "WHALE_RIDE")
        and r.get("date", "").startswith(today)
        and r.get("status") in ("OPEN", "WIN", "LOSS", "EXCLUDED")
    ]

    # Closed WIN today
    wins_today = [
        r for r in rows
        if r.get("status") == "WIN"
        and r.get("close_date", "").startswith(today)
    ]

    # Closed LOSS today
    losses_today = [
        r for r in rows
        if r.get("status") == "LOSS"
        and r.get("close_date", "").startswith(today)
    ]

    # Stale exits today (TIME EXIT = 7d/10d timeout, no TP/SL hit; EXPIRED = legacy name)
    expired_today = [
        r for r in rows
        if r.get("status") in ("TIME EXIT", "EXPIRED")
        and r.get("close_date", "").startswith(today)
    ]

    # All OPEN scanner positions
    open_rows = [
        r for r in rows
        if r.get("status") == "OPEN"
        and r.get("type", "SCANNER") in ("SCANNER", "", "WHALE_RIDE")
    ]

    # Best / worst open (by pnl_pct)
    best = worst = None
    best_pnl = worst_pnl = None
    for r in open_rows:
        try:
            pnl = float(r["pnl_pct"])
        except (ValueError, TypeError, KeyError):
            continue
        if best_pnl is None or pnl > best_pnl:
            best_pnl, best = pnl, r["coin"]
        if worst_pnl is None or pnl < worst_pnl:
            worst_pnl, worst = pnl, r["coin"]

    # Today's net P&L in EUR from closed positions (EXPIRED counts toward net)
    net_eur = 0.0
    for r in wins_today + losses_today + expired_today:
        try:
            net_eur += ALLOC * float(r["pnl_pct"]) / 100
        except (ValueError, TypeError, KeyError):
            pass

    print("\n  📊  TODAY'S ACTIVITY")
    print(f"  {'─'*46}")

    # Opened
    if opened:
        names = ", ".join(r["coin"] for r in opened)
    else:
        names = "none"
    print(f"  Opened:      {names}")

    # Closed WIN
    if wins_today:
        parts = [f"{r['coin']} {float(r['pnl_pct']):+.1f}%" for r in wins_today]
        print(f"  Closed WIN:  {', '.join(parts)}")
    else:
        print("  Closed WIN:  none")

    # Closed LOSS
    if losses_today:
        parts = [f"{r['coin']} {float(r['pnl_pct']):+.1f}%" for r in losses_today]
        print(f"  Closed LOSS: {', '.join(parts)}")
    else:
        print("  Closed LOSS: none")

    # Expired (10d timeout)
    if expired_today:
        parts = [f"{r['coin']} {float(r['pnl_pct']):+.1f}%" for r in expired_today]
        print(f"  Expired:     {', '.join(parts)}")

    print(f"  Still open:  {len(open_rows)} position(s)")

    if best:
        print(f"  Best open:   {best} {best_pnl:+.1f}%")
    if worst and worst != best:
        print(f"  Worst open:  {worst} {worst_pnl:+.1f}%")

    sign = "+" if net_eur >= 0 else ""
    print(f"  Today's net: {sign}€{net_eur:.0f}  (€{ALLOC:.0f}/position × closed P&L)")
    print(f"  {'─'*46}")


def print_track_record() -> None:
    """Print a P&L summary: PORTFOLIO · WATCHLIST · SCANNER PICKS."""
    rows = _read()
    if not rows:
        print("\n  No data logged yet.")
        return

    # ── 1. PORTFOLIO ──────────────────────────────────────────────────────
    portfolio_rows = [r for r in rows if r.get("type") == "PORTFOLIO"]
    print(f"\n  {'─'*_W}")
    print(f"  PORTFOLIO  (portfolio.json)")
    print(f"  {'─'*_W}")

    if portfolio_rows:
        latest = _latest_per_coin(portfolio_rows)

        # Parse amounts from reasoning "amount:16|src:Kraken"
        amounts: dict[str, float] = {}
        for coin, r in latest.items():
            part = r.get("reasoning", "").split("|")[0]
            if part.startswith("amount:"):
                try:
                    amounts[coin] = float(part[7:])
                except ValueError:
                    pass
        if not amounts:
            try:
                with open(config.PORTFOLIO_PATH) as f:
                    pf = json.load(f)
                for h in pf.get("holdings", []):
                    amounts[h["asset"]] = h["amount"]
            except Exception:
                pass

        # Try to get trade history from Kraken for precise entry prices + fees
        trade_history: dict[str, dict] = {}
        try:
            from src.connectors.kraken import fetch_trade_history
            trade_history = fetch_trade_history()
        except Exception:
            pass

        # Build high/low per coin since first buy date from price_history.csv
        price_highs: dict[str, float] = {}
        price_lows:  dict[str, float] = {}
        if HISTORY_PATH.exists():
            try:
                with _open_with_retry(HISTORY_PATH, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        c = row.get("coin", "")
                        try:
                            eur_p = float(row.get("price_eur") or 0)
                            if eur_p <= 0:
                                continue
                            price_highs[c] = max(price_highs.get(c, 0), eur_p)
                            price_lows[c]  = min(price_lows.get(c, eur_p), eur_p)
                        except ValueError:
                            pass
            except Exception:
                pass

        total_value_eur    = 0.0
        total_cost_eur_est = 0.0

        for coin, r in sorted(latest.items()):
            try:
                usd = float(r["current_price"])
                eur_raw = r.get("price_eur", "")
                eur = float(eur_raw) if eur_raw else usd * 0.92
                amt = amounts.get(coin, 0.0)
                eur_value = amt * eur

                if eur_value < DUST_THRESHOLD_EUR:
                    print(f"    [dust] {coin:8s}  {_fmt(eur, usd)}  value €{eur_value:.2f}")
                    continue

                rate = eur / usd if usd else 0.92  # current EUR/USD ratio

                # Use Kraken trade history entry if available, else fall back to CSV entry_price
                trade = trade_history.get(coin)
                if trade:
                    entry_usd = trade["avg_entry_usd"]
                    first_buy = trade["first_buy"]
                    fees_usd  = trade["total_fees_usd"]
                    fees_eur  = fees_usd * rate
                    entry_eur = entry_usd * rate
                    source_tag = "Kraken trades"
                else:
                    entry_raw = r.get("entry_price", "")
                    entry_usd = float(entry_raw) if entry_raw else None
                    entry_eur = entry_usd * rate if entry_usd else None
                    first_buy = ""
                    fees_eur  = None
                    source_tag = "portfolio.json"

                if entry_eur:
                    cost_eur       = amt * entry_eur
                    pnl_eur        = eur_value - cost_eur - (fees_eur or 0)
                    pnl_pct        = pnl_eur / cost_eur * 100 if cost_eur else 0
                    total_cost_eur_est += cost_eur
                    total_value_eur    += eur_value
                    icon = "+" if pnl_eur >= 0 else "-"
                    entry_str = f"entry €{entry_eur:.4f}" + (f" on {first_buy}" if first_buy else "")
                    fee_str   = f"  fee €{fees_eur:.2f}" if fees_eur else ""
                    pnl_str   = f"P&L: €{pnl_eur:+.2f} ({pnl_pct:+.1f}%)"
                    high_str  = f"  High: €{price_highs[coin]:.4f}" if coin in price_highs else ""
                    low_str   = f"  Low: €{price_lows[coin]:.4f}"   if coin in price_lows  else ""
                    print(
                        f"    [{icon}] {coin:8s}  {amt:.4f} × {entry_str}{fee_str}\n"
                        f"           now {_fmt(eur, usd)}  value €{eur_value:.2f}"
                        f"  {pnl_str}{high_str}{low_str}"
                    )
                else:
                    total_value_eur += eur_value
                    print(
                        f"    [ ] {coin:8s}  now {_fmt(eur, usd)}"
                        f"  value €{eur_value:.2f}  (no entry price)"
                    )
            except (ValueError, KeyError):
                pass

        total_pnl_eur = total_value_eur - total_cost_eur_est
        total_pnl_pct = (total_pnl_eur / total_cost_eur_est * 100) if total_cost_eur_est else 0
        icon = "+" if total_pnl_eur >= 0 else "-"
        print(
            f"\n    [{icon}] TOTAL  invested ≈€{total_cost_eur_est:.2f}"
            f"  now €{total_value_eur:.2f}  ({total_pnl_pct:+.1f}%)"
        )
    else:
        print("    No portfolio data yet — run with --scan to populate.")

    # ── 2. WATCHLIST ──────────────────────────────────────────────────────
    # Coins already shown in PORTFOLIO are excluded — portfolio takes priority.
    portfolio_coins = set(_latest_per_coin(portfolio_rows).keys()) if portfolio_rows else set()

    watchlist_rows = [r for r in rows if r.get("type") == "WATCHLIST"]
    print(f"\n  {'─'*_W}")
    print(f"  WATCHLIST  (monitored, not owned)")
    print(f"  {'─'*_W}")

    if watchlist_rows:
        latest_wl = {
            coin: r
            for coin, r in _latest_per_coin(watchlist_rows).items()
            if coin not in portfolio_coins
        }
        for coin, r in sorted(latest_wl.items()):
            try:
                usd = float(r["current_price"])
                eur_raw = r.get("price_eur", "")
                eur = float(eur_raw) if eur_raw else usd * 0.92
                reasoning = r.get("reasoning", "")
                change_7d: float | None = None
                if reasoning.startswith("7d:"):
                    try:
                        change_7d = float(reasoning[3:])
                    except ValueError:
                        pass
                suffix = f"  (7d: {change_7d:+.1f}%)" if change_7d is not None else ""
                print(f"    {coin:8s}  {_fmt(eur, usd)}{suffix}")
            except (ValueError, KeyError):
                pass
    else:
        print("    No watchlist data yet — run with --scan to populate.")

    # ── 3. SCANNER PICKS ─────────────────────────────────────────────────
    scanner_rows = [
        r for r in rows
        if r.get("type", "SCANNER") not in ("PORTFOLIO", "WATCHLIST", "WHALE_RIDE", "WHALE_MILESTONE")
    ]
    total     = len(scanner_rows)
    n_open    = sum(1 for r in scanner_rows if r.get("status") == "OPEN")
    n_win     = sum(1 for r in scanner_rows if r.get("status") == "WIN")
    n_loss    = sum(1 for r in scanner_rows if r.get("status") == "LOSS")
    n_expired = sum(1 for r in scanner_rows if r.get("status") in ("TIME EXIT", "EXPIRED"))
    closed    = n_win + n_loss
    win_rate  = (n_win / closed * 100) if closed else 0

    pnls = []
    for r in scanner_rows:
        if r.get("status") in ("WIN", "LOSS"):
            try:
                pnls.append(float(r["pnl_pct"]))
            except (ValueError, KeyError):
                pass
    avg_pnl = sum(pnls) / len(pnls) if pnls else 0

    print(f"\n  {'─'*_W}")
    print(f"  SCANNER PICKS  ({total} recommendations)")
    print(f"  {'─'*_W}")
    expired_str = f"  Time Exit: {n_expired}" if n_expired else ""
    print(f"  Open: {n_open}  Win: {n_win}  Loss: {n_loss}{expired_str}")
    print(f"  Win Rate: {win_rate:.0f}%  (of {closed} closed)  Avg P&L: {avg_pnl:+.1f}%")

    open_scanner = [r for r in scanner_rows if r.get("status") == "OPEN"]
    if open_scanner:
        print(f"\n  OPEN POSITIONS:")
        for r in sorted(open_scanner, key=lambda x: x.get("date", ""), reverse=True):
            try:
                pnl     = float(r.get("pnl_pct") or 0)
                usd     = float(r.get("current_price") or 0)
                eur_raw = r.get("price_eur", "")
                eur     = float(eur_raw) if eur_raw else usd * 0.92
                icon    = "+" if pnl >= 0 else "-"
                entry_dt  = datetime.strptime(r["date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                days_open = (datetime.now(timezone.utc) - entry_dt).days
                print(
                    f"    [{icon}] {r['coin']:8s}  {pnl:+.1f}%"
                    f"  ({days_open}d)"
                    f"  entry ${float(r['entry_price']):.4f}  now {_fmt(eur, usd)}"
                )
            except (ValueError, KeyError):
                pass

    # ── Closed trades with individual P&L ────────────────────────────────
    closed_scanner = [r for r in scanner_rows if r.get("status") in ("WIN", "LOSS", "TIME EXIT", "EXPIRED")]
    if closed_scanner:
        # Sort by date descending, show last 20
        closed_sorted = sorted(closed_scanner, key=lambda x: x.get("date", ""), reverse=True)[:20]
        wins_shown  = [r for r in closed_sorted if r.get("status") == "WIN"]
        losses_shown = [r for r in closed_sorted if r.get("status") == "LOSS"]

        # Build re-open map: coin → list of prior closed statuses (ordered by date)
        # Used to label re-opened positions in display
        _coin_history: dict[str, list[dict]] = {}
        for r in sorted(closed_scanner, key=lambda x: x.get("date", "")):
            _coin_history.setdefault(r["coin"].upper(), []).append(r)

        print(f"\n  CLOSED TRADES  (last {len(closed_sorted)} of {len(closed_scanner)}):")
        for r in closed_sorted:
            try:
                pnl    = float(r["pnl_pct"])
                entry  = float(r["entry_price"])
                exit_p = float(r["exit_price"])
                status = r["status"]
                icon   = "WIN " if status == "WIN" else ("TIME" if status == "TIME EXIT" else "LOSS")
                date   = r["date"][:10]   # YYYY-MM-DD
                coin   = r["coin"].upper()
                # Detect re-opens: this row is not the first closed trade for this coin
                history = _coin_history.get(coin, [])
                this_idx = next((i for i, h in enumerate(history) if h is r), -1)
                reopen_tag = ""
                if this_idx > 0:
                    prev = history[this_idx - 1]
                    try:
                        prev_pnl = float(prev.get("pnl_pct", 0))
                        reopen_tag = f"  (re-open after {prev['status']} {prev_pnl:+.1f}%)"
                    except (ValueError, TypeError):
                        reopen_tag = "  (re-open)"
                print(
                    f"    [{icon}] {coin:8s}  {pnl:+.1f}%"
                    f"  entry ${entry:.4f} -> exit ${exit_p:.4f}  ({date}){reopen_tag}"
                )
            except (ValueError, KeyError):
                pass

    print_win_patterns()
    print_lose_patterns()
    print(f"  {'─'*_W}")

    # ── 4. WHALE RIDES ────────────────────────────────────────────────────
    whale_rows      = [r for r in rows if r.get("type") == "WHALE_RIDE"]
    milestone_rows  = [r for r in rows if r.get("type") == "WHALE_MILESTONE"]
    if whale_rows or milestone_rows:
        wr_open   = sum(1 for r in whale_rows if r.get("status") == "OPEN")
        wr_win    = sum(1 for r in whale_rows if r.get("status") == "WIN")
        wr_loss   = sum(1 for r in whale_rows if r.get("status") == "LOSS")
        wr_closed = wr_win + wr_loss
        wr_rate   = (wr_win / wr_closed * 100) if wr_closed else 0
        wr_pnls   = []
        for r in whale_rows:
            if r.get("status") in ("WIN", "LOSS"):
                try:
                    wr_pnls.append(float(r["pnl_pct"]))
                except (ValueError, KeyError):
                    pass
        wr_avg = sum(wr_pnls) / len(wr_pnls) if wr_pnls else 0

        # Milestone-adjusted win rate: a closed LOSS that hit a milestone
        # counts as effective WIN (principal was recovered before the drawdown)
        _ms_coin_dates = {
            (r.get("coin", "").upper(), r.get("date", ""))
            for r in milestone_rows
        }
        wr_eff_win  = sum(
            1 for r in whale_rows
            if r.get("status") in ("WIN", "LOSS")
            and (
                r.get("status") == "WIN"
                or (r.get("coin", "").upper(), r.get("date", "")) in _ms_coin_dates
            )
        )
        wr_eff_loss = wr_closed - wr_eff_win
        wr_eff_rate = (wr_eff_win / wr_closed * 100) if wr_closed else 0

        # Milestone stats (partial wins — separate from full ride closes)
        ms_wins = len(milestone_rows)
        ms_pnls = []
        for r in milestone_rows:
            try:
                ms_pnls.append(float(r["pnl_pct"]))
            except (ValueError, KeyError):
                pass
        ms_avg = sum(ms_pnls) / len(ms_pnls) if ms_pnls else 0

        print(f"\n  {'─'*_W}")
        print(f"  WHALE RIDES  ({len(whale_rows)} positions  |  {ms_wins} milestone wins)")
        print(f"  {'─'*_W}")
        print(f"  Open: {wr_open}  Closed: {wr_closed}  (Win: {wr_win}  Loss: {wr_loss})")
        if wr_closed:
            print(f"  Raw close rate:  {wr_rate:.0f}%  Avg P&L: {wr_avg:+.1f}%")
            if wr_eff_win != wr_win:
                print(f"  Milestone-adj rate: {wr_eff_rate:.0f}%  ({wr_eff_win} wins — principal recovered before close)")
        if ms_wins:
            print(f"  Milestones hit: {ms_wins}  Avg milestone P&L: {ms_avg:+.1f}%  (position stays open)")

        open_wr = [r for r in whale_rows if r.get("status") == "OPEN"]
        if open_wr:
            print(f"\n  OPEN WHALE RIDES:")
            import re as _re2
            for r in open_wr:
                try:
                    pnl       = float(r.get("pnl_pct") or 0)
                    usd       = float(r.get("current_price") or 0)
                    eur_raw   = r.get("price_eur", "")
                    eur       = float(eur_raw) if eur_raw else usd * 0.92
                    icon      = "+" if pnl >= 0 else "-"
                    reasoning = r.get("reasoning", "")
                    entry_dt  = datetime.strptime(r["date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                    hrs_open  = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600

                    if "[WHALE_RIDER]" in reasoning:
                        # Pump-follower alert — no fixed SL/TP, manual trade
                        sm = _re2.search(r"\[WHALE_RIDER\]\s+(\w+)", reasoning)
                        stage_tag = f"  [{sm.group(1)}]" if sm else ""
                        print(
                            f"    [{icon}] {r['coin']:8s}  {pnl:+.1f}%"
                            f"  ({hrs_open:.0f}h)  entry ${float(r['entry_price']):.6f}"
                            f"  now {_fmt(eur, usd)}{stage_tag}  Manual trade"
                        )
                    else:
                        scam     = "  ⚠️ SERIAL SCAM" if "SERIAL SCAM" in reasoning else ""
                        m2       = _re2.search(r"max_hold:\s*(\d+)h", reasoning)
                        max_hold = int(m2.group(1)) if m2 else 48
                        hrs_left = max(0, max_hold - hrs_open)
                        print(
                            f"    [{icon}] {r['coin']:8s}  {pnl:+.1f}%"
                            f"  ({hrs_open:.0f}/{max_hold}h, {hrs_left:.0f}h left)"
                            f"  entry ${float(r['entry_price']):.6f}  now {_fmt(eur, usd)}{scam}"
                        )
                except (ValueError, KeyError):
                    pass

        closed_wr = [r for r in whale_rows if r.get("status") in ("WIN", "LOSS")]
        if closed_wr:
            closed_wr_sorted = sorted(closed_wr, key=lambda x: x.get("close_date") or x.get("date", ""), reverse=True)[:20]
            print(f"\n  CLOSED WHALE RIDES  (last {len(closed_wr_sorted)} of {len(closed_wr)}):")
            for r in closed_wr_sorted:
                try:
                    pnl    = float(r["pnl_pct"])
                    entry  = float(r["entry_price"])
                    exit_p = float(r["exit_price"])
                    status = r["status"]
                    icon   = "WIN " if status == "WIN" else "LOSS"
                    date   = (r.get("close_date") or r["date"])[:10]
                    reasoning = r.get("reasoning", "")
                    scam   = " ⚠️" if "SERIAL SCAM" in reasoning else ""
                    print(
                        f"    [{icon}] {r['coin']:8s}  {pnl:+.1f}%"
                        f"  entry ${entry:.6f} → exit ${exit_p:.6f}  ({date}){scam}"
                    )
                except (ValueError, KeyError):
                    pass

        if milestone_rows:
            ms_sorted = sorted(milestone_rows, key=lambda x: x.get("close_date") or x.get("date",""), reverse=True)
            print(f"\n  MILESTONE WINS  ({len(milestone_rows)} partial wins — positions still open):")
            for r in ms_sorted:
                try:
                    pnl    = float(r["pnl_pct"])
                    entry  = float(r["entry_price"])
                    exit_p = float(r["exit_price"])
                    date   = (r.get("close_date") or r["date"])[:10]
                    print(f"    [+{pnl:.0f}%] {r['coin']:8s}  entry ${entry:.6f} → milestone ${exit_p:.6f}  ({date})")
                except (ValueError, KeyError):
                    pass

        print(f"  {'─'*_W}")
