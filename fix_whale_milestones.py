"""
One-time fix script:
  1. Add WHALE_MILESTONE WIN records for every milestone already hit
  2. Stamp [MILESTONE_XX] flags into reasoning so they don't re-trigger
  3. Set SL=0 (no stop loss) and TP=+200% on ALL open WHALE_RIDE positions
"""
import csv, sys, io
from pathlib import Path
from datetime import datetime, timezone

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
import config

CSV_PATH = config.DATA_DIR / "recommendations.csv"

FIELDNAMES = [
    "date", "type", "coin", "coin_id", "entry_price", "stop_loss", "take_profit",
    "status", "exit_price", "close_date", "pnl_pct", "current_price", "price_eur",
    "timeframe", "fear_greed", "reasoning", "groq_rank", "qualifier", "key_signal",
]

def _read():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def _write(rows):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

# ─── Milestones to add manually (from Telegram history) ──────────────────────
# (coin_id, flag, pct, exit_price_usd, close_date)
MILESTONES = [
    ("drift-protocol", "[MILESTONE_25]",  25,  0.0516,  "2026-04-13 09:05 UTC"),
    ("humidifi",       "[MILESTONE_25]",  25,  0.1294,  "2026-04-13 11:05 UTC"),
    ("humidifi",       "[MILESTONE_50]",  50,  0.1554,  "2026-04-13 12:05 UTC"),
    ("enjincoin",      "[MILESTONE_25]",  25,  0.0505,  "2026-04-13 16:36 UTC"),
    # 币安人生: +25% not shown explicitly — calculate from entry ($0.154836 * 1.25)
    ("bianrensheng",   "[MILESTONE_25]",  25,  None,    "2026-04-13 16:00 UTC"),
    ("bianrensheng",   "[MILESTONE_50]",  50,  0.2329,  "2026-04-13 17:21 UTC"),
    ("bless-2",        "[MILESTONE_25]",  25,  0.0142,  "2026-04-13 18:51 UTC"),
    ("tradoor",        "[MILESTONE_25]",  25,  7.1300,  "2026-04-13 19:21 UTC"),
    ("bless-2",        "[MILESTONE_50]",  50,  0.0169,  "2026-04-13 20:06 UTC"),
]

rows = _read()

# Find BLESS and TRADOOR coin_ids from open rows (in case coin_id differs)
_id_map = {}
for r in rows:
    if r.get("type") == "WHALE_RIDE" and r.get("status") == "OPEN":
        sym = r.get("coin", "").upper()
        cid = r.get("coin_id", "")
        _id_map[sym] = cid
print("Open whale rides found:", {k: v for k, v in _id_map.items() if k.isascii()})

# Fix BLESS and TRADOOR coin_ids if needed
for i, (cid, flag, pct, price, ts) in enumerate(MILESTONES):
    if cid == "bless-2" and "BLESS" in _id_map:
        MILESTONES[i] = (_id_map["BLESS"], flag, pct, price, ts)
    if cid == "tradoor" and "TRADOOR" in _id_map:
        MILESTONES[i] = (_id_map["TRADOOR"], flag, pct, price, ts)

# Already present WHALE_MILESTONE records — avoid duplicates
existing = set()
for r in rows:
    if r.get("type") == "WHALE_MILESTONE":
        existing.add((r.get("coin_id", ""), int(float(r.get("pnl_pct", 0) or 0))))

new_records = []

for row in rows:
    if row.get("type") != "WHALE_RIDE" or row.get("status") != "OPEN":
        continue

    coin_id = row.get("coin_id", "")
    coin    = row.get("coin", "")
    entry   = float(row.get("entry_price", 0) or 0)
    if entry <= 0:
        continue

    # ── Add missing milestone WIN records ────────────────────────────────
    for ms_cid, flag, pct, exit_usd, close_date in MILESTONES:
        if coin_id != ms_cid:
            continue
        if (coin_id, pct) in existing:
            print(f"  SKIP {coin} +{pct}% — already exists")
            continue

        # Stamp the flag into reasoning so it won't re-trigger
        if flag not in row.get("reasoning", ""):
            row["reasoning"] = (row.get("reasoning", "") + f" {flag}").strip()
            print(f"  Stamped {flag} into {coin} reasoning")

        # Use Telegram price if available, else calculate
        actual_exit = exit_usd if exit_usd else round(entry * (1 + pct / 100), 8)

        rec = {
            "date":          row["date"],
            "type":          "WHALE_MILESTONE",
            "coin":          coin,
            "coin_id":       coin_id,
            "entry_price":   row["entry_price"],
            "stop_loss":     "",
            "take_profit":   "",
            "status":        "WIN",
            "exit_price":    actual_exit,
            "close_date":    close_date,
            "pnl_pct":       float(pct),
            "current_price": "",
            "price_eur":     "",
            "timeframe":     "",
            "fear_greed":    row.get("fear_greed", ""),
            "reasoning":     f"[WHALE_MILESTONE +{pct}%] Partial win — position stays open",
            "groq_rank":     "",
            "qualifier":     "WHALE_RIDE",
            "key_signal":    "",
        }
        new_records.append(rec)
        existing.add((coin_id, pct))
        print(f"  ✅ WHALE_MILESTONE WIN: {coin} +{pct}%  exit=${actual_exit}  @ {close_date}")

    # ── Set SL=0, TP=+200% ───────────────────────────────────────────────
    row["stop_loss"]   = 0
    row["take_profit"] = round(entry * 3.00, 8)
    print(f"  Updated {coin}: SL=0 (no SL)  TP=${round(entry * 3.00, 8)} (+200%)")

rows.extend(new_records)
_write(rows)
print(f"\n✅ Done — added {len(new_records)} milestone WIN record(s), updated SL/TP on all open whale rides.")
