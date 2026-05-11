import os
import subprocess
import threading
import json
import csv
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import sys
import ccxt

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config
from src.connectors.coingecko import fetch_prices
from src.connectors.kraken import fetch_kraken_portfolio

app = FastAPI(title="CryptoAdvisor API")

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print(f"CRITICAL ERROR: {exc}")
    import traceback
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "error": str(exc)},
    )

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global State
scan_state = {
    "is_running": False,
    "last_output": "",
    "last_scan_ts": None,
}

# Real-time Price Cache
realtime_prices = {}
# Persistent Logo Cache
LOGO_CACHE_PATH = PROJECT_ROOT / "data" / "logo_cache.json"
logo_cache = {}

def load_logo_cache():
    global logo_cache
    if LOGO_CACHE_PATH.exists():
        try:
            with open(LOGO_CACHE_PATH, 'r') as f:
                logo_cache = json.load(f)
        except Exception:
            logo_cache = {}

def save_logo_cache():
    try:
        with open(LOGO_CACHE_PATH, 'w') as f:
            json.dump(logo_cache, f)
    except Exception:
        pass

load_logo_cache()

def price_poller_task():
    """Background task to fetch prices from CoinGecko and logos."""
    print("Starting CoinGecko price poller...")
    
    last_logo_fetch = 0
    
    while True:
        # 1. Fetch Prices (every 60s to respect rate limits)
        try:
            # Gather all needed IDs
            to_fetch = set()
            rec_path = config.DATA_DIR / "recommendations.csv"
            if rec_path.exists():
                with open(rec_path, mode='r', encoding='utf-8') as f:
                    for row in csv.DictReader(f):
                        cid = row.get("coin_id")
                        if cid: to_fetch.add(cid)
            
            # Fetch prices
            from src.connectors.coingecko import fetch_prices
            prices = fetch_prices(list(to_fetch))
            if prices:
                for p in prices:
                    realtime_prices[p.symbol.upper()] = p.price
        except Exception as e:
            print(f"CoinGecko price poller error: {e}")

        # 2. Fetch Logos (every 10 minutes)
        if time.time() - last_logo_fetch > 600:
            # ... (logo fetching logic remains)

# Start poller in a daemon thread
poller_thread = threading.Thread(target=price_poller_task, daemon=True)
poller_thread.start()

class Position(BaseModel):
    coin: str
    symbol: str
    coin_id: Optional[str] = None
    amount: float
    entry_price: float
    current_price: float
    pnl_pct: float
    type: str 
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

class PositionsResponse(BaseModel):
    positions: List[Position]
    net_worth_eur: float
    total_pnl_pct: float

def get_logo_url(symbol: str, coin_id: Optional[str]) -> Optional[str]:
    """Get logo URL with high-reliability CDN fallbacks."""
    if coin_id and logo_cache.get(coin_id):
        return logo_cache[coin_id]

    # Primary Fallback: CoinGecko direct format (if coin_id is clean)
    if coin_id:
        return f"https://coin-images.coingecko.com/coins/images/{coin_id}/large/logo.png"

    # Secondary Fallback: spothq (for symbols only)
    s = symbol.lower()
    return f"https://raw.githubusercontent.com/spothq/cryptocurrency-icons/master/128/color/{s}.png"
@app.get("/api/positions", response_model=PositionsResponse)
async def get_positions():
    positions = []
    fixed_allocation_eur = 100.0
    
    # 1. Load data
    rec_path = config.DATA_DIR / "recommendations.csv"
    open_recs = []
    if rec_path.exists():
        try:
            with open(rec_path, mode='r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("status") == "OPEN":
                        open_recs.append(row)
        except Exception as e:
            print(f"CSV read error: {e}")
    
    try:
        holdings, _ = fetch_kraken_portfolio()
    except Exception:
        if config.PORTFOLIO_PATH.exists():
            with open(config.PORTFOLIO_PATH, 'r') as f:
                holdings = json.load(f).get("holdings", [])
        else:
            holdings = []

    total_current_value_eur = 0.0
    total_investment_eur = 0.0

    # 2. Map
    for rec in open_recs:
        cid = rec.get("coin_id")
        symbol = (rec.get("coin") or cid or "?").upper()
        
        entry = float(rec.get("entry_price") or 0)
        curr = realtime_prices.get(symbol, float(rec.get("current_price") or entry))
        
        pnl = ((curr - entry) / entry * 100) if entry > 0 else 0
        reasoning = rec.get("reasoning")
        if pnl > 500: reasoning = f"[⚠️ Stale Entry?] {reasoning}"

        total_current_value_eur += fixed_allocation_eur * (1 + pnl/100)
        total_investment_eur += fixed_allocation_eur

        positions.append(Position(
            coin=rec.get("coin") or cid or "Unknown",
            symbol=symbol,
            coin_id=cid,
            amount=0, 
            entry_price=entry,
            current_price=curr,
            pnl_pct=pnl,
            type=rec.get("type") or "SCANNER",
            status="OPEN",
            logo_url=get_logo_url(symbol, cid),
            stop_loss=float(rec.get("stop_loss")) if rec.get("stop_loss") else None,
            take_profit=float(rec.get("take_profit")) if rec.get("take_profit") else None,
            reasoning=reasoning,
            date=rec.get("date")
        ))

    for h in holdings:
        cid = h.get("coin_id")
        symbol = (h.get("asset") or cid or "?").upper()
        existing = next((pos for pos in positions if cid and pos.coin_id == cid), None)
        
        amt = float(h.get("amount") or 0)
        entry = float(h.get("entry_price_usd") or 0)
        curr = realtime_prices.get(symbol, entry)
        pnl = ((curr - entry) / entry * 100) if entry > 0 else 0
        
        if existing:
            existing.amount = amt
            existing.type = "PORTFOLIO"
        else:
            if amt > 0:
                total_current_value_eur += amt * curr * 0.92
                total_investment_eur += amt * entry * 0.92
            else:
                total_current_value_eur += fixed_allocation_eur * (1 + pnl/100)
                total_investment_eur += fixed_allocation_eur

            positions.append(Position(
                coin=h.get("asset") or cid or "Unknown",
                symbol=symbol,
                coin_id=cid,
                amount=amt,
                entry_price=entry,
                current_price=curr,
                pnl_pct=pnl,
                type="PORTFOLIO",
                status="OPEN",
                logo_url=get_logo_url(symbol, cid),
            ))

    total_pnl_pct = ((total_current_value_eur - total_investment_eur) / total_investment_eur * 100) if total_investment_eur > 0 else 0

    return PositionsResponse(
        positions=positions,
        net_worth_eur=total_current_value_eur,
        total_pnl_pct=total_pnl_pct
    )

@app.get("/api/scan/status", response_model=ScanStatus)
async def get_scan_status():
    return ScanStatus(**scan_state)

@app.post("/api/scan/reset")
async def reset_scan():
    global scan_state
    scan_state["is_running"] = False
    scan_state["last_output"] += "\n--- SCAN STATE MANUALLY RESET ---\n"
    return {"message": "Scan state reset"}

def run_scan_task():
    global scan_state
    print(f"[{datetime.now()}] Starting background scan subprocess...")
    scan_state["is_running"] = True
    scan_state["last_output"] = "--- SCAN INITIATED ---\n"
    
    # Use explicit python command and shell=True for Windows compatibility
    cmd = f'"{sys.executable}" run.py --scan'
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(PROJECT_ROOT),
            encoding="utf-8",
            errors="replace",
            shell=True
        )
        
        output = scan_state["last_output"]
        for line in iter(process.stdout.readline, ""):
            output += line
            lines = output.splitlines()[-200:]
            output = "\n".join(lines) + "\n"
            scan_state["last_output"] = output
            
        process.wait()
        scan_state["last_scan_ts"] = datetime.now()
        scan_state["last_output"] += f"\n--- SCAN COMPLETED [Exit: {process.returncode}] ---"
        print(f"[{datetime.now()}] Scan subprocess finished.")
    except Exception as e:
        print(f"[{datetime.now()}] Scan subprocess CRASHED: {e}")
        scan_state["last_output"] += f"\nFATAL ERROR: {e}"
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
