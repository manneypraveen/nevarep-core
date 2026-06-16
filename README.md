# NEVAREP Core

**Voice-Operated Indian Financial Intelligence Terminal**

The brain of NEVAREP — data collection, processing, pattern analysis, and prediction engine for Indian derivatives markets (Nifty 50, Bank Nifty).

## Quick Start

```bash
# 1. Clone
git clone https://github.com/manneypraveen/nevarep-core.git
cd nevarep-core

# 2. Virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env — add your FRED_API_KEY (free from fred.stlouisfed.org)

# 5. Initial setup (creates DB + trading calendar)
python setup.py

# 6. Sprint 1: Download global market data
python run.py backfill --sprint 1
```

## Project Structure

```
nevarep-core/
├── scrapers/              # Data collection (NSE, Yahoo Finance, FRED, MoneyControl)
│   ├── nse_fo_downloader.py    # NSE F&O bhav copy (old + UDiFF)
│   ├── global_fetcher.py       # Yahoo Finance + FRED (US markets, yields, crude)
│   ├── participant_oi.py       # NSE participant-wise OI (FII/DII/Client/Pro)
│   ├── fii_dii_scraper.py      # MoneyControl FII/DII cash flows
│   └── news_scraper.py         # Market wrap articles + GDELT
├── llm/                   # LLM intelligence
│   ├── news_structurer.py      # Article → structured news items (Claude API)
│   ├── narrative_generator.py  # Daily narrative + pattern tags
│   └── prompts.py              # All LLM prompt templates
├── db/                    # Database
│   ├── loader.py               # Parse + load data into DuckDB
│   └── derived.py              # Compute PCR, max pain, DMAs, candle types
├── engine/                # Prediction
│   ├── similarity.py           # 5-dimension weighted matching
│   ├── predictor.py            # Daily prediction engine
│   └── strategy.py             # Strike selection, position sizing
├── pipeline/              # Automation
│   ├── orchestrator.py         # Daily job scheduler (4 jobs)
│   └── quality_monitor.py      # Data quality checks
├── backtest/              # Validation
│   ├── backtester.py           # Walk-forward backtest harness
│   └── scorecard.py            # Performance metrics
├── config/
│   └── settings.py             # All config, URLs, symbols, thresholds
├── utils/
│   ├── nse_session.py          # NSE session manager (cookies, UA rotation)
│   ├── date_utils.py           # Trading day helpers
│   └── constants.py            # Enums, categories, pattern tags
├── data/                  # Local storage (gitignored)
│   ├── raw_csv/                # Downloaded CSVs
│   ├── articles/               # Scraped news HTML
│   └── nevarep.duckdb          # DuckDB database file
├── logs/
├── tests/
├── .env.example
├── .gitignore
├── requirements.txt
├── setup.py                    # Initial setup: DB + trading calendar
└── run.py                      # CLI entry point
```

## Scope

- **Trading instruments:** Nifty 50 F&O + Bank Nifty F&O (NSE)
- **Context data:** US markets, crude, gold, DXY, USD/INR, yields, VIX, FII/DII flows
- **Index OHLC:** Nifty + Bank Nifty + Sensex (via Yahoo Finance)
- **Historical range:** Jan 2022 — present (~800 trading days)

## Data Sources (15 sources, all free)

| # | Source | Method | Sprint |
|---|--------|--------|--------|
| 1 | Yahoo Finance (15 tickers) | Python API | Sprint 1 |
| 2 | FRED API (US yields) | Python API | Sprint 1 |
| 3 | NSE F&O bhav copy (old format) | HTTP scrape | Sprint 2 |
| 4 | NSE F&O bhav copy (UDiFF) | HTTP scrape | Sprint 2 |
| 5 | NSE participant-wise OI | HTTP download | Sprint 3 |
| 6 | NSE participant-wise volumes | HTTP download | Sprint 3 |
| 7 | MoneyControl FII/DII | Web scrape | Sprint 4 |
| 8 | MoneyControl market wraps | Web scrape | Sprint 5 |
| 9 | Economic Times articles | Web scrape | Sprint 5 |
| 10 | GDELT Project | REST API | Sprint 5 |
| 11 | RBI press releases | Web scrape | Sprint 5 |
| 12 | FOMC statements | Web scrape | Sprint 5 |
| 13 | Trading Economics calendar | Web scrape | Sprint 5 |
| 14 | Google News RSS | RSS feed | Sprint 5 |
| 15 | NSE equity bhav copy | HTTP scrape | Sprint 6 |

## Database

DuckDB (embedded, file-based) with 13 tables. The DDL lives inline in
`setup.py` (no separate `db/schema.sql` file) — run `python setup.py` to
create or recreate the schema.

| Table | Rows (3yr est.) | Description |
|-------|-----------------|-------------|
| trading_day | ~1,100 | Calendar with holidays, expiry flags |
| raw_fo_bhav | ~3.5M | All Nifty + BN F&O contracts |
| daily_index_ohlc | ~2,400 | Index OHLC + derived (gaps, candles, DMAs) |
| daily_options_snapshot | ~1,600 | PCR, max pain, VIX, IV, mismatch scores |
| daily_participant_oi | ~3,200 | FII/DII/Client/Pro positions |
| daily_global_context | ~800 | US markets, yields, crude, DXY, currencies |
| daily_institutional_flows | ~800 | FII/DII cash market buy/sell |
| daily_news_events | ~5,000 | LLM-structured news items |
| daily_scheduled_events | ~1,000 | Expiry, RBI, FOMC, budget dates |
| daily_narrative | ~800 | LLM-generated daily summary + patterns |
| condition_rules | ~200 | IF/THEN rules with reliability scores |
| predictions_log | ~300+ | Backtest + live predictions |
| pipeline_runs | ~3,000+ | Job execution tracking |

## Status

**Phase 1: Data Infrastructure — IN PROGRESS**

---

*NEVAREP: Never repeat. Always remember. Always learn.*
