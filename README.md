# CryptoAdvisor LLM

AI-powered crypto scanner and trading advisor. Scans top 1000 coins, runs technical analysis, detects whale pumps, tracks Polymarket odds, and sends Telegram alerts — all on free APIs.

## Features

- **Smart Scanner** — scans top 1000 CoinGecko coins, scores by TA (RSI, MACD, Bollinger, EMA), Fear & Greed, volume/mcap. Groq LLM picks top 10 with entry/SL/TP
- **Whale Rider** — detects coins pumping +10% in 24h with high vol/mcap ratio. Tracks milestone profits (+25%, +50%) without closing the position. Closes at +200% only
- **Risk Assessor** — flags scam patterns (serial pump & dump, rug pulls, dilution). Groq + NewsData.io news verification for serious flags
- **News Pipeline** — NewsData.io (6,000 free/month) → DuckDuckGo fallback. Groq summarises per-coin headlines into one sentence
- **Polymarket Advisor** — tracks prediction market odds and surfaces tradeable events
- **Telegram Bot** — all alerts, scanner picks, whale rides, and milestone notifications
- **Track Record** — logs all scanner picks and whale rides. Separate win/loss stats for scanner vs whale rides. Milestone wins tracked independently

## Quick Start

```bash
git clone https://github.com/izhechev/crypto-trading-polimarket-llm.git
cd crypto-trading-polimarket-llm
python -m venv .venv && .venv\Scripts\activate  # Windows
pip install -r requirements.txt
cp .env.example .env   # fill in API keys
python run.py
```

## API Keys

| Key | Where to get | Cost |
|-----|-------------|------|
| `COINGECKO_API_KEY` | coingecko.com/api | Free |
| `GROQ_API_KEY` | console.groq.com | Free |
| `NEWSDATA_API_KEY` | newsdata.io | Free (6K/month) |
| `TELEGRAM_BOT_TOKEN` | @BotFather on Telegram | Free |
| `TELEGRAM_CHAT_ID` | your chat ID | Free |
| `TAVILY_API_KEY` | tavily.com | Free (1K/month) |

All others in `.env.example` are optional.

## Project Structure

```
├── run.py                          # Main entry point
├── config.py                       # All config & API keys
├── debug_news.py                   # Debug news pipeline standalone
├── src/
│   ├── agents/
│   │   ├── scanner.py              # Top 1000 coin scanner + scoring
│   │   ├── coin_risk_assessor.py   # Scam/risk detection
│   │   ├── whale_rider.py          # Whale pump detector
│   │   ├── groq_analyst.py         # Groq LLM analysis
│   │   ├── technical_analyst.py    # RSI, MACD, Bollinger, EMA
│   │   └── sentiment_analyst.py    # Fear & Greed + sentiment
│   ├── connectors/
│   │   ├── coingecko.py            # Prices, market data
│   │   ├── web_research.py         # NewsData.io + DuckDuckGo + RSS
│   │   └── polymarket.py           # Prediction market odds
│   └── utils/
│       ├── logger.py               # Trade logging, stats, win/loss tracking
│       ├── telegram.py             # Alert sender
│       └── budget_tracker.py       # API usage tracking
```

## Cost

Everything runs free. Optional VPS ~€8/month.
