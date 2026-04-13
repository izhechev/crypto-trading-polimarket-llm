# 🧠 CryptoAdvisor LLM

Personal crypto analyzer with technical analysis, sentiment tracking, and Polymarket odds monitoring.

## Quick Start

```bash
# 1. Clone / enter directory
cd crypto-advisor

# 2. Create virtual environment
python -m venv .venv && source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
cp .env.example .env
# Edit .env and fill in your API keys (most are free)

# 5. Run it
python run.py
```

## What it does (Phase 1)

- Fetches live prices for BTC, ETH, INJ, RENDER, DOT
- Computes technical indicators (RSI, MACD, Bollinger, EMA)
- Shows Fear & Greed Index
- Tracks your portfolio P&L
- All free APIs, no paid subscriptions needed

## Budget: €9/month

| Item | Cost |
|------|------|
| Hetzner VPS (optional) | €8 |
| All APIs | Free |
| LLM (Groq free tier) | Free |
| **Total** | **€0-8** |

## Project Structure

```
crypto-advisor/
├── run.py                          # Main entry point
├── config.py                       # Configuration
├── portfolio.json                  # Your holdings
├── requirements.txt                # Python dependencies
├── .env.example                    # API keys template
└── src/
    ├── agents/
    │   └── technical_analyst.py    # TA with pandas-ta
    ├── connectors/
    │   └── coingecko.py            # CoinGecko + Fear&Greed
    ├── models/
    │   └── crypto.py               # All Pydantic models
    └── utils/                      # Rate limiter, budget tracker (TODO)
```

## Next Steps

- [ ] Phase 1: Add Telegram alerts
- [ ] Phase 1: Add Groq LLM analysis (free tier)
- [ ] Phase 2: Add Sentiment Agent + Polymarket tracking
- [ ] Phase 3: Add RAG + Bull/Bear debate
- [ ] Phase 4: Streamlit dashboard
- [ ] Phase 5: Paper trading
