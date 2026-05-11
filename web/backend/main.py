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
    """Background task to fetch prices from Binance and logos from CoinGecko."""
    print("Starting real-time price poller...")
    exchange = ccxt.binance({'enableRateLimit': True})
    
    # Initial market load
    active_symbols = set()
    try:
        markets = exchange.load_markets()
        active_symbols = {
            s for s, m in markets.items() 
            if m.get('active') and m.get('info', {}).get('status') == 'TRADING'
        }
        print(f"Tracking {len(active_symbols)} active Binance markets.")
    except Exception as e:
        print(f"Initial market load error: {e}")

    last_logo_fetch = 0
    
    while True:
        # 1. Fetch Prices (every 1s)
        try:
            tickers = exchange.fetch_tickers()
            new_prices = {}
            for pair, data in tickers.items():
                if pair not in active_symbols: continue
                base = pair.split('/')[0]
                if pair.endswith('/USDT'):
                    new_prices[base] = data['last']
                elif pair.endswith('/USDC'):
                    if base not in new_prices: new_prices[base] = data['last']
            realtime_prices.update(new_prices)
        except Exception as e:
            print(f"Price poller error: {e}")

        # 2. Fetch Logos (every 10 minutes or if cache is empty)
        if time.time() - last_logo_fetch > 600:
            try:
                # Find all coin_ids that need logos
                needs_logo = set()
                # From Recs
                rec_path = config.DATA_DIR / "recommendations.csv"
                if rec_path.exists():
                    with open(rec_path, mode='r', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            cid = row.get("coin_id")
                            if cid and cid not in logo_cache: needs_logo.add(cid)
                # From Portfolio
                try:
                    holdings, _ = fetch_kraken_portfolio()
                    for h in holdings:
                        cid = h.get("coin_id")
                        if cid and cid not in logo_cache: needs_logo.add(cid)
                except Exception: pass

                if needs_logo:
                    print(f"Fetching logos for: {needs_logo}")
                    prices = fetch_prices(list(needs_logo))
                    if prices:
                        for p in prices:
                            logo_cache[p.coin_id] = p.image_url
                        save_logo_cache()
                
                last_logo_fetch = time.time()
            except Exception as e:
                print(f"Logo fetcher error: {e}")

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

@app.get("/api/positions", response_model=PositionsResponse)
async def get_positions():
    positions = []
    fixed_allocation_eur = 100.0
    
    # 1. Load data
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
                logo_url=logo_cache.get(cid),
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

def run_scan_task():
    global scan_state
    print(f"[{datetime.now()}] Background scan task started.")
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
        print(f"[{datetime.now()}] Background scan task completed. Exit code: {process.returncode}")
    except Exception as e:
        print(f"[{datetime.now()}] Background scan task failed: {e}")
        scan_state["last_output"] += f"\nERROR: {e}"
    finally:
        scan_state["is_running"] = False

@app.post("/api/scan")
async def start_scan(background_tasks: BackgroundTasks):
    print(f"[{datetime.now()}] POST /api/scan")
    if scan_state["is_running"]:
        print("Scan already in progress.")
        raise HTTPException(status_code=400, detail="Scan already in progress")
    
    background_tasks.add_task(run_scan_task)
    return {"message": "Scan started"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
