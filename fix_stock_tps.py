"""
One-time migration: cap take-profit on all OPEN STOCK positions to entry x 1.10.
Positions where current_price already exceeds the new TP are closed as WIN.

Run: python fix_stock_tps.py
"""
import csv
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))
import config

LOG_PATH    = config.DATA_DIR / "stock_recommendations.csv"
NEW_TP_MULT = 1.10   # new take-profit cap
NEW_SL_MULT = 0.92   # stop-loss floor


def _safe_float(val: str) -> float | None:
    try:
        return float(val) if val and str(val).strip() else None
    except (ValueError, AttributeError):
        return None


def main() -> None:
    if not LOG_PATH.exists():
        print("stock_recommendations.csv not found")
        return

    with open(LOG_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    fieldnames = rows[0].keys() if rows else []
    changed = wins = 0

    for row in rows:
        if row.get("status") != "OPEN":
            continue

        entry = _safe_float(row.get("entry_price", ""))
        tp    = _safe_float(row.get("take_profit", ""))
        if entry is None or tp is None or entry <= 0:
            continue

        old_ratio = tp / entry
        if old_ratio <= NEW_TP_MULT:
            continue  # already within new cap

        new_tp = round(entry * NEW_TP_MULT, 4)
        new_sl = round(entry * NEW_SL_MULT, 4)
        row["take_profit"] = str(new_tp)
        row["stop_loss"]   = str(new_sl)
        changed += 1

        # If current price already at or above new TP, close as WIN
        current = _safe_float(row.get("current_price", ""))
        if current is not None and current >= new_tp:
            pnl = round((new_tp - entry) / entry * 100, 2)
            row["status"]     = "WIN"
            row["exit_price"] = str(new_tp)
            row["pnl_pct"]    = str(pnl)
            wins += 1
            print(f"  WIN  {row['symbol']:8s}  entry={entry:.4g}  new_tp={new_tp:.4g}"
                  f"  current={current:.4g}  pnl=+{pnl:.1f}%")
        else:
            print(f"  OPEN {row['symbol']:8s}  entry={entry:.4g}"
                  f"  tp {tp:.4g} -> {new_tp:.4g}  (was {old_ratio:.2f}x -> {NEW_TP_MULT:.2f}x)")

    # Write back
    with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nDone: {changed} TP(s) updated, {wins} position(s) closed as WIN.")


if __name__ == "__main__":
    main()
