"""Recommendation logger with live position tracking."""
import csv
import json
import time
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

LOG_PATH          = config.DATA_DIR / "recommendations.csv"
HISTORY_PATH      = config.DATA_DIR / "price_history.csv"
DUST_THRESHOLD_USD = 0.12

# Thread safety lock for CSV access
_FILE_LOCK = threading.Lock()

import re as _re


def _pfmt(p: float) -> str:
    """Format a price with enough decimal places."""
    if p >= 1:       return f"${p:,.4f}"
    if p >= 0.01:    return f"${p:.5f}"
    if p >= 0.0001:  return f"${p:.7f}"
    return f"${p:.10f}"


# ── WIN analysis helpers ──────────────────────────────────────────────────

def _parse_entry_signals(reasoning: str) -> dict:
    r = reasoning.lower()
    macd_bullish = bool(_re.search(r'macd\+rsi\+vol|macd.*bullish', r))
    if _re.search(r'macd bearish|full bearish alignment|momentum stall', r):
        macd_bullish = False

    vol_mcap: float | None = None
    m = _re.search(r'vol/mcap\s*([\d.]+)x', r)
    if m:
        try: vol_mcap = float(m.group(1))
        except ValueError: pass

    rsi: float | None = None
    m = _re.search(r'rsi\s*([\d.]+)', r)
    if m:
        try: rsi = float(m.group(1))
        except ValueError: pass

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
    except Exception: pass

    sigs   = _parse_entry_signals(row.get("reasoning", ""))
    fg_val: int | None = None
    try: fg_val = int(row.get("fear_greed", ""))
    except (ValueError, TypeError): pass

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
    "position_id", "entry_price", "stop_loss", "take_profit",
    "status", "exit_price", "close_date", "pnl_pct", "current_price",
    "price_eur", "timeframe", "fear_greed", "reasoning",
    "recommended_order", "groq_rank", "qualifier", "key_signal",
]

_HISTORY_HEADERS = ["timestamp", "coin", "coin_id", "price_eur", "price_usd"]


# ── Internal helpers ──────────────────────────────────────────────────────

def _open_with_retry(path: Path, mode: str = "r", retries: int = 6, delay: float = 0.5, **kwargs):
    for attempt in range(retries):
        try:
            return open(path, mode, **kwargs)
        except PermissionError:
            if attempt == retries - 1: raise
            time.sleep(delay)


def _read() -> list[dict]:
    with _FILE_LOCK:
        if not LOG_PATH.exists():
            return []
        with _open_with_retry(LOG_PATH, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))


def _write(rows: list[dict]) -> None:
    with _FILE_LOCK:
        with _open_with_retry(LOG_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_HEADERS, extrasaction="ignore", restval="")
            writer.writeheader()
            writer.writerows(rows)


def _usd_to_eur(usd: float) -> str:
    val = usd
    decimals = 2 if val >= 1 else 4 if val >= 0.01 else 6 if val >= 0.0001 else 8
    return f"${val:.{decimals}f}"


# ── Public API ────────────────────────────────────────────────────────────

_MAX_OPEN_SCANNER  = 50


def _make_position_id(coin: str) -> str:
    return f"{coin}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"


def log_recommendation(rec: dict, fear_greed_value: int) -> None:
    """Append a new recommendation. Strictly prevents duplicate OPEN positions."""
    rows = _read()
    coin = rec.get("coin", "").upper()

    # CRITICAL: Clean up existing duplicates before checking for a new one
    # If the user has duplicates, they were likely created by race conditions.
    open_for_coin = [
        r for r in rows
        if r.get("status") == "OPEN"
        and r.get("coin", "").upper() == coin
    ]
    if open_for_coin:
        return

    if sum(1 for r in rows if r.get("status") == "OPEN") >= _MAX_OPEN_SCANNER:
        return

    pid = _make_position_id(coin)
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
        "current_price": rec.get("entry_price", ""),
        "timeframe":     rec.get("timeframe", ""),
        "fear_greed":    fear_greed_value,
        "reasoning":     rec.get("reasoning", ""),
        "recommended_order": rec.get("recommended_order", "NONE"),
    })
    _write(rows)
    print(f"  Logged {pid} ({rec.get('recommended_order','SPOT')})")


def update_open_positions() -> None:
    """Fetch prices and update all OPEN positions. Applies 24h strict strategy."""
    rows = _read()
    
    # 1. Deduplicate OPEN positions immediately (keep only the FIRST one)
    seen_coins = set()
    rows_clean = []
    for r in rows:
        if r.get("status") == "OPEN":
            c = r.get("coin", "").upper()
            if c in seen_coins:
                # Duplicate! Mark as EXCLUDED (Category Fix)
                r["status"] = "EXCLUDED"
                r["reasoning"] = r.get("reasoning", "") + " [DUPLICATE REMOVED]"
            else:
                seen_coins.add(c)
        rows_clean.append(r)
    rows = rows_clean

    open_rows = [r for r in rows if r.get("status") == "OPEN"]
    if not open_rows:
        _write(rows) # Save the deduplicated state
        return

    from src.connectors.coingecko import fetch_prices
    coin_ids = list({r["coin_id"] for r in open_rows if r.get("coin_id")})
    try:
        price_objs = fetch_prices(coin_ids)
        usd_map = {p.coin_id: p.price_usd for p in price_objs}
    except Exception as e:
        print(f"  Warning: price update failed: {e}")
        return

    _now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    new_wins = []

    for row in rows:
        if row.get("status") != "OPEN": continue
        
        usd = usd_map.get(row.get("coin_id"))
        if usd is None: continue

        try: entry = float(row.get("entry_price") or 0)
        except (ValueError, TypeError): continue
        if entry <= 0: continue

        is_short = row.get("recommended_order") == "SHORT"
        pnl_pct = ((entry - usd) if is_short else (usd - entry)) / entry * 100
            
        row["current_price"] = round(usd, 8)
        row["pnl_pct"] = round(pnl_pct, 2)

        try:
            entry_dt = datetime.strptime(row["date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            hours_open = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
        except Exception: hours_open = 0.0

        # Close condition 1: WIN (+10%)
        if pnl_pct >= 10.0:
            row["status"]     = "WIN"
            row["exit_price"] = round(usd, 8)
            row["close_date"] = _now_str
            new_wins.append(row)
            side = row.get("recommended_order", "LONG")
            print(f"  ✅ WIN ({side}): {row['coin']} {pnl_pct:+.1f}% within {hours_open:.1f}h")
            try:
                from src.utils.telegram import send_telegram as _tg
                _tg(f"✅ <b>WIN +10% ({side}) â€” {row['coin']}</b>\n"
                    f"  PnL: {pnl_pct:+.1f}% after {hours_open:.1f}h\n"
                    f"  âœ… Position closed as WIN.")
            except Exception: pass
            continue

        # Close condition 2: Stop Loss (-10%)
        if pnl_pct <= -10.0:
            row["status"]     = "LOSS"
            row["exit_price"] = round(usd, 8)
            row["close_date"] = _now_str
            print(f"  🛑 LOSS (SL -10%): {row['coin']} {pnl_pct:+.1f}%")
            try:
                from src.utils.telegram import send_telegram as _tg
                _tg(f"🛑 <b>LOSS (SL -10%) â€” {row['coin']}</b>\n"
                    f"  PnL: {pnl_pct:+.1f}%\n"
                    f"  âŒ Position closed as LOSS.")
            except Exception: pass
            continue

        # Close condition 3: 24h timeout
        if hours_open >= 24.0:
            row["status"]     = "LOSS"
            row["exit_price"] = round(usd, 8)
            row["close_date"] = _now_str
            print(f"  â° LOSS (24h timeout): {row['coin']} {pnl_pct:+.1f}%")
            try:
                from src.utils.telegram import send_telegram as _tg
                _tg(f"â° <b>LOSS (24h Timeout) â€” {row['coin']}</b>\n"
                    f"  PnL: {pnl_pct:+.1f}% after {hours_open:.1f}h\n"
                    f"  âŒ Position closed as LOSS.")
            except Exception: pass
            continue

    _write(rows)
    for win_row in new_wins: print_win_analysis(win_row)


def log_whale_ride(wr: dict, fear_greed_value: int) -> None:
    rows = _read()
    coin = wr.get("symbol", "").upper()
    if any(r.get("status") == "OPEN" and r.get("coin", "").upper() == coin for r in rows):
        return

    entry = round(wr.get("entry", 0), 8)
    rows.append({
        "date":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "type":          "WHALE_RIDE",
        "coin":          coin,
        "coin_id":       wr.get("coin_id", ""),
        "entry_price":   entry,
        "status":        "OPEN",
        "current_price": entry,
        "timeframe":     "24h Window",
        "fear_greed":    fear_greed_value,
        "reasoning":     f"Whale Ride. {wr.get('crash_reason','')}",
        "recommended_order": "LONG",
    })
    _write(rows)
    print(f"  Whale ride logged â†’ {coin}")


def close_whale_rider_position(sym: str, current_price: float, exit_reason: str = "") -> None:
    rows = _read()
    changed = False
    _now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for row in rows:
        if (row.get("status") == "OPEN" and row.get("coin", "").upper() == sym.upper()):
            try:
                entry = float(row.get("entry_price") or 0)
                is_short = row.get("recommended_order") == "SHORT"
                pnl_pct = ((entry - current_price) if is_short else (current_price - entry)) / entry * 100
            except (ValueError, TypeError): pnl_pct = 0

            status = "WIN" if pnl_pct >= 10.0 else "LOSS"
            row["status"]        = status
            row["exit_price"]    = round(current_price, 8)
            row["close_date"]    = _now_str
            row["pnl_pct"]       = round(pnl_pct, 2)
            row["current_price"] = round(current_price, 8)
            row["reasoning"]     = row.get("reasoning", "") + f" | EXIT: {exit_reason}"
            changed = True
            icon = "âœ…" if status == "WIN" else "âŒ"
            print(f"  {icon} Closed {status} â†’ {sym} {pnl_pct:+.1f}% ({exit_reason})")
            try:
                from src.utils.telegram import send_telegram as _tg
                _tg(f"{icon} <b>Position Closed ({status}) â€” {sym}</b>\n"
                    f"  PnL: {pnl_pct:+.1f}%\n"
                    f"  Reason: {exit_reason or 'Strategy Exit'}")
            except Exception: pass
            break
    if changed: _write(rows)

def log_scanner_results(*a, **kw): pass
def update_scanner_sltp(*a, **kw): pass
def log_portfolio_positions(*a, **kw): pass
def log_watchlist_prices(*a, **kw): pass
def log_portfolio_results(*a, **kw): pass
def update_groq_rank(*a, **kw): pass
def update_whale_rank(*a, **kw): pass

def log_price_history() -> None:
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
                writer.writerow({"timestamp": now, "coin": p.symbol, "coin_id": p.coin_id, 
                                 "price_eur": round(p.price_eur, 6), "price_usd": round(p.price_usd, 6)})
    except Exception: pass

def print_daily_activity() -> None:
    rows = _read()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    wins = [r for r in rows if r.get("status") == "WIN" and r.get("close_date", "").startswith(today)]
    losses = [r for r in rows if r.get("status") == "LOSS" and r.get("close_date", "").startswith(today)]
    print("\n  📊  TODAY'S ACTIVITY")
    print(f"  Closed WIN:  {', '.join(f'{r['coin']} {float(r['pnl_pct']):+.1f}%' for r in wins) or 'none'}")
    print(f"  Closed LOSS: {', '.join(f'{r['coin']} {float(r['pnl_pct']):+.1f}%' for r in losses) or 'none'}")
def print_scan_summary(top10: list[dict] | None = None, whale_rides: list[dict] | None = None, fear_greed: dict | None = None) -> None:
    rows = _read()
    open_rows = [r for r in rows if r.get("status") == "OPEN"]
    print(f"\n  OPEN POSITIONS ({len(open_rows)}/{_MAX_OPEN_SCANNER} slots used):")
    for r in sorted(open_rows, key=lambda x: x.get("date", ""), reverse=True):
        try:
            entry = float(r.get("entry_price") or 0)
            curr = float(r.get("current_price") or entry)
            
            # Age calculation
            try:
                entry_dt = datetime.strptime(r["date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                hrs = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
                age_str = f"{hrs:.1f}h" if hrs < 24 else f"{hrs/24:.1f}d"
            except Exception: age_str = "?h"

            is_short = r.get("recommended_order") == "SHORT"
            pnl = ((entry - curr) if is_short else (curr - entry)) / entry * 100 if entry > 0 else 0
            
            icon = "+" if pnl >= 0 else "-"
            side = r.get("recommended_order", "SPOT")
            print(f"    [{icon}] {r['coin']:8s}  {pnl:+.1f}% ({side})  [{age_str}]  entry {entry:.4f}  now {curr:.4f}")
        except Exception: pass

    # ── Most Valuable Picks (Scanner) ──
    if top10:
        longs  = [r for r in top10 if r.get("recommended_order") == "LONG"]
        shorts = [r for r in top10 if r.get("recommended_order") == "SHORT"]
        spots  = [r for r in top10 if r.get("recommended_order") == "SPOT"]
        
        if longs or shorts or spots:
            print(f"\n  MOST VALUABLE SCANNER PICKS:")
            if longs:
                print("    🚀 LONGS:")
                for i, r in enumerate(longs[:5], 1):
                    print(f"      {i}. {r['symbol']:8s} score={r['score']}  {_pfmt(r['price'])}")
            if shorts:
                print("    📉 SHORTS:")
                for i, r in enumerate(shorts[:5], 1):
                    print(f"      {i}. {r['symbol']:8s} score={r['score']}  {_pfmt(r['price'])}")
            if spots:
                print("    💰 SPOTS:")
                for i, r in enumerate(spots[:5], 1):
                    print(f"      {i}. {r['symbol']:8s} score={r['score']}  {_pfmt(r['price'])}")
        else:
            print("\n  ℹ️  No high-conviction scanner picks this round.")

    # ── Valuable Whale Rides ──
    if whale_rides:
        print(f"\n  🐋  VALUABLE WHALE RIDE CANDIDATES:")
        for i, wr in enumerate(whale_rides[:5], 1):
            sym = wr.get("symbol", "?")
            cyc = wr.get("cycle_number", "?")
            score = wr.get("hc_score", "?")
            print(f"    {i:2}. {sym:8s} (Cycle #{cyc} | Score {score}/8)")
    else:
        print("\n  🐋  No high-conviction whale rides this round.")


def print_track_record() -> None:
    rows = _read()
    wins = [r for r in rows if r.get("status") == "WIN"]
    losses = [r for r in rows if r.get("status") == "LOSS"]
    total = len(wins) + len(losses)
    rate = (len(wins) / total * 100) if total else 0
    print(f"\n  TRACK RECORD: {len(wins)}W / {len(losses)}L ({rate:.1f}% win rate)")
