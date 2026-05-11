import os
import subprocess
import threading
import json
import csv
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

from fastapi import FastAPI, BackgroundTasks, HTTPException
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
# symbol -> usd_price
realtime_prices = {}
# coin_id -> logo_url
logo_cache = {}

def price_poller_task():
    """Background task to fetch prices from Binance every second."""
    print("Starting real-time price poller...")
    exchange = ccxt.binance({'enableRateLimit': True})
    while True:
        try:
            # Fetch all USDT tickers at once - very efficient
            tickers = exchange.fetch_tickers()
            for pair, data in tickers.items():
                if pair.endswith('/USDT'):
                    symbol = pair.split('/')[0]
                    realtime_prices[symbol] = data['last']
                elif pair.endswith('/USDC'):
                    symbol = pair.split('/')[0]
                    if symbol not in realtime_prices:
                        realtime_prices[symbol] = data['last']
        except Exception as e:
            print(f"Price poller error: {e}")
        time.sleep(1)

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

class PositionsResponse(BaseModel):
    positions: List[Position]
    net_worth_eur: float
    total_pnl_pct: float

@app.get("/api/positions", response_model=PositionsResponse)
async def get_positions():
    print(f"[{datetime.now()}] GET /api/positions")
    positions = []
    fixed_allocation_eur = 100.0
    
    # 1. Load data sources
    rec_path = config.DATA_DIR / "recommendations.csv"
    open_recs = []
    if rec_path.exists():
        with open(rec_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("status") == "OPEN":
                    open_recs.append(row)
    
    try:
        holdings, _ = fetch_kraken_portfolio()
    except Exception as e:
        print(f"Kraken error: {e}")
        if config.PORTFOLIO_PATH.exists():
            with open(config.PORTFOLIO_PATH, 'r') as f:
                holdings = json.load(f).get("holdings", [])
        else:
            holdings = []

    # 2. Collect unique IDs for logo fetching (only if not cached)
    needs_logo = set()
    for rec in open_recs:
        cid = rec.get("coin_id")
        if cid and cid not in logo_cache: needs_logo.add(cid)
    for h in holdings:
        cid = h.get("coin_id")
        if cid and cid not in logo_cache: needs_logo.add(cid)
    
    if needs_logo:
        try:
            print(f"Fetching logos for: {needs_logo}")
            prices = fetch_prices(list(needs_logo))
            for p in prices:
                logo_cache[p.coin_id] = p.image_url
                # Map coin_id -> symbol for better Binance lookup
                # Some coins have different symbols on CG vs Binance
                if p.coin_id and p.symbol:
                    # e.g. "snt-status" -> "SNT"
                    pass 
                if p.symbol not in realtime_prices:
                    realtime_prices[p.symbol] = p.price_usd
        except Exception as e:
            print(f"Logo fetch error: {e}")

    total_current_value_eur = 0.0
    total_investment_eur = 0.0

    # 3. Process positions with real-time prices
    for rec in open_recs:
        cid = rec.get("coin_id")
        symbol = (rec.get("coin") or cid or "?").upper()
        
        entry = float(rec.get("entry_price") or 0)
        # Try exact symbol, then common variations
        curr = realtime_prices.get(symbol)
        if curr is None:
            curr = float(rec.get("current_price") or entry)
        
        pnl = ((curr - entry) / entry * 100) if entry > 0 else 0
        
        # Suspicious data check (e.g. 10x in a day might be wrong entry)
        reasoning = rec.get("reasoning")
        if pnl > 500: # Flag extremely high PnL as potentially stale entry
            reasoning = f"[⚠️ Stale Entry?] {reasoning}"

        curr_val_eur = fixed_allocation_eur * (1 + pnl/100)
        total_current_value_eur += curr_val_eur
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
            logo_url=logo_cache.get(cid),
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
                curr_val_eur = amt * curr * 0.92 
                total_current_value_eur += curr_val_eur
                total_investment_eur += (amt * entry * 0.92)
            else:
                curr_val_eur = fixed_allocation_eur * (1 + pnl/100)
                total_current_value_eur += curr_val_eur
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
                logo_url=logo_cache.get(cid),
            ))

    total_pnl_pct = ((total_current_value_eur - total_investment_eur) / total_investment_eur * 100) if total_investment_eur > 0 else 0
    print(f"Response: {len(positions)} pos, Net Worth: {total_current_value_eur:.2f} EUR")

    return PositionsResponse(
        positions=positions,
        net_worth_eur=total_current_value_eur,
        total_pnl_pct=total_pnl_pct
    )

@app.get("/api/scan/status", response_model=ScanStatus)
async def get_scan_status():
    return ScanStatus(**scan_state)

def run_scan_task():
    global scan_state
    scan_state["is_running"] = True
    scan_state["last_output"] = "Scan started...\n"
    
    cmd = [sys.executable, str(PROJECT_ROOT / "run.py"), "--scan"]
    try:
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
            scan_state["last_output"] = output
            
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
