# NEVAREP Core

**Voice-Operated Indian Financial Intelligence Terminal**

The brain of NEVAREP — data collection, processing, pattern analysis, and prediction engine for Indian derivatives markets (Nifty 50, Bank Nifty, Sensex).

## Quick Start

```bash
# Clone and setup
git clone https://github.com/manneypraveen/nevarep-core.git
cd nevarep-core
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API keys (FRED, Anthropic)

# Run backfill
python run.py backfill --start 2022-01-01 --end 2024-12-31
```

## Architecture

```
scrapers/    → Data collection (NSE, Yahoo Finance, FRED, MoneyControl)
llm/         → LLM intelligence (news structuring, narratives, patterns)
db/          → Database schema, loading, derived calculations
engine/      → Similarity matching + prediction engine
pipeline/    → Daily automation orchestrator
backtest/    → Walk-forward backtesting harness
```

## Data Sources (18 sources, all free)

- NSE F&O bhav copies (old + UDiFF format)
- Yahoo Finance (US markets, commodities, FX, VIX)
- FRED API (US Treasury yields)
- NSE participant-wise OI (FII/DII/Client/Pro)
- MoneyControl (FII/DII cash flows, market wrap articles)
- GDELT (historical news archive)
- RBI (policy statements)

## Status

Phase 1: Data Infrastructure — IN PROGRESS
