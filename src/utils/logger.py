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
    
    # 1. Deduplicate OPEN positions immediately
    seen_coins = set()
    rows_clean = []
    for r in rows:
        if r.get("status") == "OPEN":
            c = r.get("coin", "").upper()
            if c in seen_coins:
                r["status"] = "EXCLUDED"
                r["reasoning"] = r.get("reasoning", "") + " [DUPLICATE REMOVED]"
            else:
                seen_coins.add(c)
        rows_clean.append(r)
    rows = rows_clean

    open_rows = [r for r in rows if r.get("status") == "OPEN"]
    if not open_rows:
        _write(rows)
        return

    from src.connectors.coingecko import fetch_prices, fetch_coin_list
    
    # Self-healing missing coin_ids
    missing_ids = [r for r in open_rows if not r.get("coin_id")]
    if missing_ids:
        try:
            full_list = fetch_coin_list()
            cg_map = {c["symbol"].upper(): c["id"] for c in full_list}
            for r in rows:
                if r.get("status") == "OPEN" and not r.get("coin_id"):
                    sym = r.get("coin", "").upper()
                    if sym in cg_map:
                        r["coin_id"] = cg_map[sym]
                        print(f"  ✨ Self-healed coin_id for {sym} -> {r['coin_id']}")
            # Refresh open_rows after healing
            open_rows = [r for r in rows if r.get("status") == "OPEN"]
        except Exception: pass

    coin_ids = list({r["coin_id"] for r in open_rows if r.get("coin_id")})
    if not coin_ids:
        # print("  ℹ️ No coin_ids found for open positions — updates pending healing")
        return

    try:
        price_objs = fetch_prices(coin_ids)
        usd_map = {p.coin_id: p.price_usd for p in price_objs}
    except Exception as e:
        print(f"  Warning: CG price update failed: {e}")
        usd_map = {}

    # CoinPaprika scanner rows store IDs like "banana-banana-for-scale"; CoinGecko
    # doesn't know these IDs so we must resolve them. Strategy:
    #  1. Try CoinPaprika ticker directly (correct price for CP-format IDs)
    #  2. Fall back to resolve_cg_id → CoinGecko (only for CG-format IDs)
    missing_after_markets = [r for r in open_rows if r.get("coin_id") and r.get("coin_id") not in usd_map]
    if missing_after_markets:
        # ── Step 1: CoinPaprika direct ticker (handles CP-format coin_ids) ──
        import httpx as _httpx
        for r in missing_after_markets:
            cid = r.get("coin_id", "")
            if cid in usd_map:
                continue
            try:
                _resp = _httpx.get(
                    f"https://api.coinpaprika.com/v1/tickers/{cid}",
                    timeout=8,
                )
                if _resp.status_code == 200:
                    _price = _resp.json().get("quotes", {}).get("USD", {}).get("price")
                    if _price:
                        usd_map[cid] = float(_price)
            except Exception:
                pass

        # ── Step 2: resolve_cg_id for anything still missing ──
        still_missing = [r for r in missing_after_markets if r.get("coin_id") not in usd_map]
        if still_missing:
            try:
                from src.connectors.coingecko import fetch_simple_usd
                from src.connectors.coinpaprika import resolve_cg_id

                cg_to_original: dict[str, list[str]] = {}
                for r in still_missing:
                    cid = r.get("coin_id", "")
                    sym = r.get("coin", "").upper()
                    cg_id = resolve_cg_id(sym) or cid
                    if cg_id:
                        cg_to_original.setdefault(cg_id, []).append(cid)

                simple_map = fetch_simple_usd(list(cg_to_original))
                for cg_id, usd in simple_map.items():
                    for cid in cg_to_original.get(cg_id, []):
                        usd_map[cid] = usd
            except Exception:
                pass

    # ── Multi-Source Fallback (Binance/Kraken) ──
    # If a price is missing from CG, try other exchanges
    for r in open_rows:
        cid = r.get("coin_id")
        sym = r.get("coin", "").upper()
        if not usd_map.get(cid):
            try:
                # 1. Try Binance
                from src.connectors.binance import fetch_binance_ticker
                bn = fetch_binance_ticker(sym)
                if bn and bn.get("price"):
                    usd_map[cid] = bn["price"]
                    # print(f"  🔄 Fallback price for {sym}: ${bn['price']:.6f} (Binance)")
                    continue
                
                # 2. Try Kraken
                from src.connectors.kraken import fetch_kraken_ticker
                kr = fetch_kraken_ticker(sym)
                if kr and kr.get("price"):
                    usd_map[cid] = kr["price"]
                    # print(f"  🔄 Fallback price for {sym}: ${kr['price']:.6f} (Kraken)")
                    continue
            except Exception: pass

    if not usd_map:
        # print("  ⚠️  No prices available from any source.")
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

        order_type = row.get("recommended_order", "SPOT")
        is_short = order_type == "SHORT"
        
        base_pnl_pct = ((entry - usd) if is_short else (usd - entry)) / entry * 100
        
        pnl_pct = base_pnl_pct
            
        row["current_price"] = str(round(usd, 12))
        row["pnl_pct"] = str(round(pnl_pct, 2))

        # ── Fixed Stop Loss Logic ──
        if row.get("type") == "WHALE_RIDE":
            try:
                curr_sl = float(row.get("stop_loss") or 0)
                
                # Exit check: Hit Stop Loss
                if (not is_short and usd <= curr_sl) or (is_short and usd >= curr_sl):
                    row["status"]     = "LOSS"
                    row["exit_price"] = str(round(usd, 8))
                    row["close_date"] = _now_str
                    _reason = f"Hit Stop Loss ({curr_sl})"
                    row["reasoning"]  = row.get("reasoning", "") + f" | EXIT: {_reason}"
                    print(f"  🛑 {row['status']} ({row['coin']}): {pnl_pct:+.1f}% ({_reason})")
                    try:
                        from src.utils.telegram import send_telegram as _tg
                        _tg(f"🛑 <b>Position Closed (LOSS) — {row['coin']}</b>\n"
                            f"  PnL: {pnl_pct:+.1f}%\n"
                            f"  Reason: {_reason}")
                    except Exception: pass
                    continue
            except (ValueError, TypeError): pass

        try:
            entry_dt = datetime.strptime(row["date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            hours_open = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
        except Exception: hours_open = 0.0

        # Close condition 1: WIN (Only if not already handled by trail)
        # Note: In trailing mode, we let it ride past 10% until trail hits.
        # But if type is SCANNER (not Whale Ride), we keep the old fixed rules.
        if row.get("type") != "WHALE_RIDE" and pnl_pct >= 10.0:
            row["status"]     = "WIN"
            row["exit_price"] = str(round(usd, 8))
            row["close_date"] = _now_str
            new_wins.append(row)
            side = row.get("recommended_order", "LONG")
            print(f"  ✅ WIN ({side}): {row['coin']} {pnl_pct:+.1f}% within {hours_open:.1f}h")
            try:
                from src.utils.telegram import send_telegram as _tg
                _tg(f"✅ <b>WIN +10% ({side}) — {row['coin']}</b>\n"
                    f"  PnL: {pnl_pct:+.1f}% after {hours_open:.1f}h\n"
                    f"  ✅ Position closed as WIN.")
            except Exception: pass
            continue

        # Close condition 2: Stop Loss (Only for non-Whale Rides)
        if row.get("type") != "WHALE_RIDE" and pnl_pct <= -10.0:
            row["status"]     = "LOSS"
            row["exit_price"] = str(round(usd, 8))
            row["close_date"] = _now_str
            print(f"  🛑 LOSS (SL -10%): {row['coin']} {pnl_pct:+.1f}%")
            try:
                from src.utils.telegram import send_telegram as _tg
                _tg(f"🛑 <b>LOSS (SL -10%) — {row['coin']}</b>\n"
                    f"  PnL: {pnl_pct:+.1f}%\n"
                    f"  ❌ Position closed as LOSS.")
            except Exception: pass
            continue

        # Close condition 3: 24h timeout
        if hours_open >= 24.0:
            row["status"]     = "WIN" if pnl_pct > 0 else "LOSS"
            row["exit_price"] = str(round(usd, 8))
            row["close_date"] = _now_str
            print(f"  ⏰ {row['status']} (24h timeout): {row['coin']} {pnl_pct:+.1f}%")
            try:
                from src.utils.telegram import send_telegram as _tg
                _tg(f"⏰ <b>{row['status']} (24h Timeout) — {row['coin']}</b>\n"
                    f"  PnL: {pnl_pct:+.1f}% after {hours_open:.1f}h\n")
            except Exception: pass
            continue

    _write(rows)
    for win_row in new_wins: print_win_analysis(win_row)


def log_whale_ride(wr: dict, fear_greed_value: int) -> None:
    rows = _read()
    coin = wr.get("symbol", "").upper()
    if any(r.get("status") == "OPEN" and r.get("coin", "").upper() == coin for r in rows):
        return

    entry = round(wr.get("entry", 0), 12)
    sl    = round(entry * 0.90, 12) # Initial fixed -10% Stop Loss
    rows.append({
        "date":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "type":          "WHALE_RIDE",
        "coin":          coin,
        "coin_id":       wr.get("coin_id", ""),
        "entry_price":   entry,
        "stop_loss":     sl,
        "take_profit":   round(entry * 1.15, 12),
        "status":        "OPEN",
        "current_price": entry,
        "timeframe":     "24h Window",
        "fear_greed":    fear_greed_value,
        "reasoning":     f"Whale Ride. {wr.get('crash_reason','')}",
        "recommended_order": "LONG",
    })
    _write(rows)
    print(f"  Whale ride logged -> {coin}")


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
            row["exit_price"]    = round(current_price, 12)
            row["close_date"]    = _now_str
            row["pnl_pct"]       = round(pnl_pct, 2)
            row["current_price"] = round(current_price, 12)
            row["reasoning"]     = row.get("reasoning", "") + f" | EXIT: {exit_reason}"
            changed = True
            icon = "✅" if status == "WIN" else "❌"
            print(f"  {icon} Closed {status} -> {sym} {pnl_pct:+.1f}% ({exit_reason})")
            try:
                from src.utils.telegram import send_telegram as _tg
                _tg(f"{icon} <b>Position Closed ({status}) — {sym}</b>\n"
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
    
    # Split OPEN by type
    open_scanner = [r for r in rows if r.get("status") == "OPEN" and r.get("type", "SCANNER") != "WHALE_RIDE"]
    open_whale   = [r for r in rows if r.get("status") == "OPEN" and r.get("type") == "WHALE_RIDE"]

    print(f"\n  💼  OPEN SCANNER POSITIONS ({len(open_scanner)}):")
    if open_scanner:
        for r in sorted(open_scanner, key=lambda x: x.get("date", ""), reverse=True):
            try:
                entry = float(r.get("entry_price") or 0)
                curr = float(r.get("current_price") or entry)
                try:
                    pnl = float(r.get("pnl_pct") or 0)
                except ValueError:
                    pnl = 0.0
                try:
                    entry_dt = datetime.strptime(r["date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                    hrs = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
                    age_str = f"{hrs:.1f}h" if hrs < 24 else f"{hrs/24:.1f}d"
                except Exception: age_str = "?h"
                icon = "🟢" if pnl >= 0 else "🔴"
                side = r.get("recommended_order", "SPOT")
                print(f"    {icon} {r['coin']:8s}  {pnl:+.1f}% ({side})  [{age_str}]  entry {_pfmt(entry)}  now {_pfmt(curr)}")
            except Exception: pass
    else:
        print("    (none)")

    print(f"\n  🐋  OPEN WHALE RIDE POSITIONS ({len(open_whale)}):")
    if open_whale:
        for r in sorted(open_whale, key=lambda x: x.get("date", ""), reverse=True):
            try:
                entry = float(r.get("entry_price") or 0)
                curr = float(r.get("current_price") or entry)
                try:
                    pnl = float(r.get("pnl_pct") or 0)
                except ValueError:
                    pnl = 0.0
                try:
                    entry_dt = datetime.strptime(r["date"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                    hrs = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
                    age_str = f"{hrs:.1f}h" if hrs < 24 else f"{hrs/24:.1f}d"
                except Exception: age_str = "?h"
                icon = "🟢" if pnl >= 0 else "🔴"
                print(f"    {icon} {r['coin']:8s}  {pnl:+.1f}%  [{age_str}]  entry {_pfmt(entry)}  now {_pfmt(curr)}")
            except Exception: pass
    else:
        print("    (none)")

def print_track_record() -> None:
    """Print category-specific win/loss records."""
    rows = _read()
    
    def _stats(label, trades):
        wins = [r for r in trades if r.get("status") == "WIN"]
        losses = [r for r in trades if r.get("status") == "LOSS"]
        total = len(wins) + len(losses)
        rate = (len(wins) / total * 100) if total else 0
        return f"{label}: {len(wins)}W / {len(losses)}L ({rate:.1f}% win rate)"

    # Scanner stats
    scanner_trades = [r for r in rows if r.get("type", "SCANNER") != "WHALE_RIDE"]
    print(f"\n  {_stats('SCANNER TRACK RECORD', scanner_trades)}")
    
    # Whale Ride stats
    whale_trades = [r for r in rows if r.get("type") == "WHALE_RIDE"]
    print(f"  {_stats('WHALE RIDE TRACK RECORD', whale_trades)}")
    
    # Combined
    print(f"  {_stats('TOTAL COMBINED RECORD', rows)}")
