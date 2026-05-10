"""Recommendation logger with live position tracking."""
import csv
import json
import time
import re
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

LOG_PATH          = config.DATA_DIR / "recommendations.csv"
HISTORY_PATH      = config.DATA_DIR / "price_history.csv"
DUST_THRESHOLD_USD = 0.12   # holdings below this are labelled "dust"

import re as _re


def _pfmt(p: float) -> str:
    """Format a price with enough decimal places to show significance."""
    if p >= 1:       return f"${p:,.4f}"
    if p >= 0.01:    return f"${p:.5f}"
    if p >= 0.0001:  return f"${p:.7f}"
    return f"${p:.10f}"


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

    # LESSON generation logic remains
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


_HEADERS = [
    "date", "type", "coin", "coin_id",
    "position_id",    # unique per DCA entry, e.g. BTC_20250505_143022
    "entry_price",    # USD
    "stop_loss",      # USD
    "take_profit",    # USD
    "status",         # OPEN / WIN / LOSS / EXCLUDED / "" (watchlist)
    "exit_price",     # USD, filled when closed
    "close_date",     # UTC timestamp when WIN/LOSS/EXCLUDED was set
    "pnl_pct",        # % — currency-neutral
    "current_price",  # USD
    "price_eur",      # EUR (kept for reference; all display uses USD)
    "timeframe", "fear_greed", "reasoning",
    "recommended_order",  # LONG / SHORT / SPOT / NONE
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
    """Format as '$X.XXXX' choosing decimal places by magnitude."""
    val = usd
    decimals = 2 if val >= 1 else 4 if val >= 0.01 else 6 if val >= 0.0001 else 8
    return f"${val:.{decimals}f}"


def _usd_to_eur(usd: float) -> str:
    """Convert a stored USD price to a formatted string."""
    val = usd
    decimals = 2 if val >= 1 else 4 if val >= 0.01 else 6 if val >= 0.0001 else 8
    return f"${val:.{decimals}f}"


# ── Public API ────────────────────────────────────────────────────────────

_MAX_OPEN_SCANNER  = 50   # Hard cap


def _make_position_id(coin: str) -> str:
    return f"{coin}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"


def log_recommendation(rec: dict, fear_greed_value: int) -> None:
    """
    Append a new recommendation with status=OPEN and type=SCANNER.
    Strictly prevents duplicate OPEN positions for the same coin.
    """
    rows = _read()
    coin = rec.get("coin", "").upper()

    # Strict check: only one open position per coin.
    open_for_coin = [
        r for r in rows
        if r.get("status") == "OPEN"
        and r.get("coin", "").upper() == coin
    ]
    if open_for_coin:
        return

    # Max concurrent positions cap
    open_count = sum(
        1 for r in rows
        if r.get("status") == "OPEN"
        and r.get("type", "SCANNER") in ("SCANNER", "")
    )
    if open_count >= _MAX_OPEN_SCANNER:
        return

    pid = _make_position_id(coin)
    base_reasoning = rec.get("reasoning", "").replace("\n", " ")
    rows.append({
        "date":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "type":          "SCANNER",
        "coin":          coin,
        "coin_id":       rec.get("coin_id", ""),
        "position_id":   pid,
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
        "reasoning":     base_reasoning,
        "recommended_order": rec.get("recommended_order", "NONE"),
    })
    _write(rows)
    print(f"  Logged {pid} ({rec.get('recommended_order','SPOT')}) → {LOG_PATH}")


def update_open_positions() -> None:
    """
    Fetch current prices and update P&L for all OPEN positions.
    Applies strict 24h Window Strategy:
      - WIN:  Price hits +10% within 24h of entry.
      - LOSS: 24h passes and +10% was never hit.
    Handles SHORT positions by inverting PnL calculation.
    """
    rows = _read()
    open_rows = [r for r in rows if r.get("status") == "OPEN"]
    if not open_rows:
        return

    from src.connectors.coingecko import fetch_prices
    coin_ids = list({r["coin_id"] for r in open_rows if r.get("coin_id")})
    if not coin_ids:
        return

    try:
        price_objs = fetch_prices(coin_ids)
        usd_map = {p.coin_id: p.price_usd for p in price_objs}
    except Exception as e:
        print(f"  Warning: price update failed: {e}")
        return

    _now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    closed = 0
    new_wins = []

    for row in rows:
        if row.get("status") != "OPEN" or not row.get("coin_id"):
            continue
        
        usd = usd_map.get(row["coin_id"])
        if usd is None:
            continue

        try:
            entry = float(row.get("entry_price") or 0)
        except (ValueError, TypeError):
            continue
        if entry <= 0: continue

        # ── Correct PnL calculation for SHORTs ──
        is_short = row.get("recommended_order") == "SHORT"
        if is_short:
            pnl_pct = (entry - usd) / entry * 100
        else:
            pnl_pct = (usd - entry) / entry * 100
            
        row["current_price"] = round(usd, 6)
        row["pnl_pct"] = round(pnl_pct, 2)

        # ── 24h Window Strategy ──
        try:
            entry_dt = datetime.strptime(row["date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            hours_open = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
        except Exception:
            hours_open = 0.0

        # Condition 1: WIN (+10%)
        if pnl_pct >= 10.0:
            row["status"]     = "WIN"
            row["exit_price"] = round(usd, 6)
            row["close_date"] = _now_str
            closed += 1
            new_wins.append(row)
            side = "SHORT" if is_short else "LONG"
            print(f"  ✅ WIN ({side}): {row['coin']} {pnl_pct:+.1f}% within {hours_open:.1f}h")
            try:
                from src.utils.telegram import send_telegram as _tg
                _tg(f"✅ <b>WIN +10% ({side}) — {row['coin']}</b>\n"
                    f"  PnL: {pnl_pct:+.1f}% after {hours_open:.1f}h\n"
                    f"  ✅ Position closed as WIN.")
            except Exception: pass
            continue

        # Condition 2: 24h timeout (LOSS unless WIN hit)
        if hours_open >= 24.0:
            row["status"]     = "LOSS"
            row["exit_price"] = round(usd, 6)
            row["close_date"] = _now_str
            closed += 1
            side = "SHORT" if is_short else "LONG/SPOT"
            print(f"  ⏰ LOSS ({side} 24h timeout): {row['coin']} {pnl_pct:+.1f}% after {hours_open:.1f}h")
            try:
                from src.utils.telegram import send_telegram as _tg
                _tg(f"⏰ <b>LOSS (24h Timeout) — {row['coin']}</b>\n"
                    f"  PnL: {pnl_pct:+.1f}% after {hours_open:.1f}h\n"
                    f"  ❌ Position closed as LOSS.")
            except Exception: pass
            continue

    _write(rows)
    for win_row in new_wins:
        print_win_analysis(win_row)


def log_whale_ride(wr: dict, fear_greed_value: int) -> None:
    """Log a whale ride candidate as type=WHALE_RIDE with 24h tracking."""
    rows = _read()
    coin = wr.get("symbol", "").upper()
    
    # Dedup
    if any(r.get("status") == "OPEN" and r.get("coin", "").upper() == coin for r in rows):
        return

    entry = round(wr.get("entry", 0), 8)
    rows.append({
        "date":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "type":          "WHALE_RIDE",
        "coin":          coin,
        "coin_id":       wr.get("coin_id", ""),
        "entry_price":   entry,
        "stop_loss":     round(entry * 0.90, 8),
        "take_profit":   round(entry * 1.10, 8),
        "status":        "OPEN",
        "exit_price":    "",
        "pnl_pct":       "",
        "current_price": entry,
        "price_eur":     "",
        "timeframe":     "24h Window",
        "fear_greed":    fear_greed_value,
        "reasoning":     f"Whale Ride. {wr.get('crash_reason','')}",
        "recommended_order": "LONG", # Whale rides are always bullish bounces
    })
    _write(rows)
    print(f"  Whale ride logged → {coin}")


def close_whale_rider_position(sym: str, current_price: float, exit_reason: str = "") -> None:
    """Force close a whale rider position (momentum reverse, etc)."""
    rows = _read()
    changed = False
    _now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for row in rows:
        if (row.get("status") == "OPEN" 
                and row.get("type") == "WHALE_RIDE" 
                and row.get("coin", "").upper() == sym.upper()):
            
            try:
                entry = float(row.get("entry_price") or 0)
                pnl_pct = (current_price - entry) / entry * 100 if entry > 0 else 0
            except (ValueError, TypeError):
                pnl_pct = 0

            # Strict 24h strategy: WIN only if +10% or more.
            status = "WIN" if pnl_pct >= 10.0 else "LOSS"
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
            
            try:
                from src.utils.telegram import send_telegram as _tg
                _tg(f"{icon} <b>Whale Rider Early Exit — {sym}</b>\n"
                    f"  Status: {status}\n"
                    f"  PnL: {pnl_pct:+.1f}%\n"
                    f"  Reason: {exit_reason or 'Momentum Slowing'}")
            except Exception: pass
            break

    if changed:
        _write(rows)


def log_scanner_results(top10: list[dict], fear_greed_value: int) -> None:
    """Backwards compat alias — not used in the new strict strategy."""
    pass


def log_portfolio_results(holdings: list[dict]) -> None:
    """Backwards compat alias."""
    pass


def log_price_history() -> None:
    """Append current prices to history file."""
    from src.connectors.coingecko import fetch_prices
    rows = _read()
    coin_ids = {r["coin_id"] for r in rows if r.get("status") == "OPEN" and r.get("coin_id")}
    if not coin_ids: return

    try:
        prices = fetch_prices(list(coin_ids))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(HISTORY_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_HISTORY_HEADERS)
            for p in prices:
                writer.writerow({
                    "timestamp": now,
                    "coin": p.symbol,
                    "coin_id": p.coin_id,
                    "price_eur": round(p.price_eur, 6),
                    "price_usd": round(p.price_usd, 6),
                })
    except Exception: pass


def print_daily_activity() -> None:
    """Print consolidated daily summary."""
    rows = _read()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    wins = [r for r in rows if r.get("status") == "WIN" and r.get("close_date", "").startswith(today)]
    losses = [r for r in rows if r.get("status") == "LOSS" and r.get("close_date", "").startswith(today)]
    open_rows = [r for r in rows if r.get("status") == "OPEN"]

    print("\n  📊  TODAY'S ACTIVITY")
    print(f"  Closed WIN:  {', '.join(f'{r['coin']} {float(r['pnl_pct']):+.1f}%' for r in wins) or 'none'}")
    print(f"  Closed LOSS: {', '.join(f'{r['coin']} {float(r['pnl_pct']):+.1f}%' for r in losses) or 'none'}")
    print(f"  Still open:  {len(open_rows)} position(s)")


def print_scan_summary(top10: list[dict] | None = None, whale_rides: list[dict] | None = None, fear_greed: dict | None = None) -> None:
    """Display final scan results."""
    rows = _read()
    open_rows = [r for r in rows if r.get("status") == "OPEN"]
    
    print(f"\n  OPEN POSITIONS ({len(open_rows)}/{_MAX_OPEN_SCANNER} slots used):")
    for r in sorted(open_rows, key=lambda x: x.get("date", ""), reverse=True):
        try:
            entry = float(r.get("entry_price") or 0)
            curr = float(r.get("current_price") or entry)
            is_short = r.get("recommended_order") == "SHORT"
            if is_short:
                pnl = (entry - curr) / entry * 100
            else:
                pnl = (curr - entry) / entry * 100
            icon = "+" if pnl >= 0 else "-"
            side = "SHORT" if is_short else "LONG"
            print(f"    [{icon}] {r['coin']:8s}  {pnl:+.1f}% ({side})  entry {entry:.4f}  now {curr:.4f}")
        except Exception: pass

    if top10:
        valuable = [r for r in top10 if int(r.get("score", 0)) >= 8]
        longs = [r for r in valuable if r.get("recommended_order") == "LONG"]
        shorts = [r for r in valuable if r.get("recommended_order") == "SHORT"]
        spots = [r for r in valuable if r.get("recommended_order") == "SPOT"]
        
        if longs:
            print("\n    🚀 LONGS:")
            for i, r in enumerate(longs[:5], 1): print(f"      {i}. {r['symbol']:8s} score={r['score']}")
        if shorts:
            print("\n    📉 SHORTS:")
            for i, r in enumerate(shorts[:5], 1): print(f"      {i}. {r['symbol']:8s} score={r['score']}")
        if spots:
            print("\n    💰 SPOTS:")
            for i, r in enumerate(spots[:5], 1): print(f"      {i}. {r['symbol']:8s} score={r['score']}")
    
    if whale_rides:
        print("\n  PROVEN WHALE RIDES:")
        for i, wr in enumerate([w for w in whale_rides if int(w.get("cycle_number", 0)) >= 2][:5], 1):
            print(f"    {i}. {wr['symbol']} (Cycle #{wr['cycle_number']})")


def print_track_record() -> None:
    """Print simplified win/loss record."""
    rows = _read()
    wins = [r for r in rows if r.get("status") == "WIN"]
    losses = [r for r in rows if r.get("status") == "LOSS"]
    total = len(wins) + len(losses)
    rate = (len(wins) / total * 100) if total else 0
    print(f"\n  TRACK RECORD: {len(wins)}W / {len(losses)}L ({rate:.1f}% win rate)")
