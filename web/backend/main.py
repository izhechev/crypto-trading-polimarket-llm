import os
import subprocess
import threading
import json
import csv
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import sys

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config
from src.connectors.coingecko import fetch_prices
from src.connectors.kraken import fetch_kraken_portfolio

app = FastAPI(title="CryptoAdvisor API")

# Enable CORS for frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state for scan
scan_state = {
    "is_running": False,
    "last_output": "",
    "last_scan_ts": None,
}

class Position(BaseModel):
    coin: str
    symbol: str
    coin_id: Optional[str] = None
    amount: float
    entry_price: float
    current_price: float
    pnl_pct: float
    type: str  # "SCANNER" or "PORTFOLIO"
    status: str
    logo_url: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reasoning: Optional[str] = None
    date: Optional[str] = None

class ScanStatus(BaseModel):
    is_running: bool
    last_output: str
    last_scan_ts: Optional[datetime] = None

@app.get("/api/positions", response_model=List[Position])
async def get_positions():
    positions = []
    
    # 1. Load from recommendations.csv (OPEN positions)
    rec_path = config.DATA_DIR / "recommendations.csv"
    open_recs = []
    if rec_path.exists():
        with open(rec_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("status") == "OPEN":
                    open_recs.append(row)
    
    # 2. Load from portfolio.json or Kraken
    try:
        holdings, _ = fetch_kraken_portfolio()
    except Exception:
        if config.PORTFOLIO_PATH.exists():
            with open(config.PORTFOLIO_PATH, 'r') as f:
                holdings = json.load(f).get("holdings", [])
        else:
            holdings = []

    # Collect all unique coin_ids to fetch live prices/logos
    coin_ids = set()
    for rec in open_recs:
        if rec.get("coin_id"):
            coin_ids.add(rec["coin_id"])
    for h in holdings:
        if h.get("coin_id"):
            coin_ids.add(h["coin_id"])
    
    # Fetch live prices and logos
    price_map = {}
    if coin_ids:
        try:
            prices = fetch_prices(list(coin_ids))
            price_map = {p.coin_id: p for p in prices}
        except Exception as e:
            print(f"Error fetching prices: {e}")

    # Process open recommendations
    for rec in open_recs:
        cid = rec.get("coin_id")
        p = price_map.get(cid) if cid else None
        
        entry = float(rec.get("entry_price") or 0)
        curr = p.price_usd if p else float(rec.get("current_price") or entry)
        pnl = ((curr - entry) / entry * 100) if entry > 0 else 0
        
        positions.append(Position(
            coin=rec.get("coin", cid or "Unknown"),
            symbol=p.symbol if p else rec.get("coin", "").upper(),
            coin_id=cid,
            amount=0, # Not specified in recs usually
            entry_price=entry,
            current_price=curr,
            pnl_pct=pnl,
            type=rec.get("type") or "SCANNER",
            status="OPEN",
            logo_url=p.image_url if p else None,
            stop_loss=float(rec.get("stop_loss")) if rec.get("stop_loss") else None,
            take_profit=float(rec.get("take_profit")) if rec.get("take_profit") else None,
            reasoning=rec.get("reasoning"),
            date=rec.get("date")
        ))

    # Process manual holdings
    for h in holdings:
        cid = h.get("coin_id")
        p = price_map.get(cid) if cid else None
        
        # Avoid duplicates if already in recs
        existing = next((pos for pos in positions if cid and pos.coin_id == cid), None)
        
        amt = float(h.get("amount") or 0)
        entry = float(h.get("entry_price_usd") or 0)
        curr = p.price_usd if p else entry
        pnl = ((curr - entry) / entry * 100) if entry > 0 else 0
        
        if existing:
            existing.amount = amt
            existing.type = "PORTFOLIO"
        else:
            positions.append(Position(
                coin=h.get("asset", cid or "Unknown"),
                symbol=p.symbol if p else h.get("asset", "").upper(),
                coin_id=cid,
                amount=amt,
                entry_price=entry,
                current_price=curr,
                pnl_pct=pnl,
                type="PORTFOLIO",
                status="OPEN",
                logo_url=p.image_url if p else None,
            ))


    return positions

@app.get("/api/scan/status", response_model=ScanStatus)
async def get_scan_status():
    return ScanStatus(**scan_state)

def run_scan_task():
    global scan_state
    scan_state["is_running"] = True
    scan_state["last_output"] = "Scan started...\n"
    
    cmd = [sys.executable, str(PROJECT_ROOT / "run.py"), "--scan"]
    try:
        # We use Popen to potentially stream output later, 
        # but for now let's just capture the whole thing.
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(PROJECT_ROOT),
            encoding="utf-8",
            errors="replace"
        )
        
        output = ""
        for line in iter(process.stdout.readline, ""):
            output += line
            scan_state["last_output"] = output # Update live-ish
            
        process.wait()
        scan_state["last_scan_ts"] = datetime.now()
    except Exception as e:
        scan_state["last_output"] += f"\nERROR: {e}"
    finally:
        scan_state["is_running"] = False

@app.post("/api/scan")
async def start_scan(background_tasks: BackgroundTasks):
    if scan_state["is_running"]:
        raise HTTPException(status_code=400, detail="Scan already in progress")
    
    background_tasks.add_task(run_scan_task)
    return {"message": "Scan started"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
