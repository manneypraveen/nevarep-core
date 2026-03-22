# NEVAREP — Complete Setup & Run Guide

> If you've cloned this repo from GitHub, follow these steps in order.
> Each step builds on the previous one. Do not skip steps.

---

## Prerequisites

- **Python 3.9+** (tested on 3.9, works on 3.10+)
- **Git**
- **Internet connection** (for downloading market data from NSE, Yahoo Finance, FRED)
- **~5 GB free disk space** (for downloaded CSVs + DuckDB database)
- **FRED API key** (free, get at https://fred.stlouisfed.org/docs/api/api_key.html)
- **Anthropic API key** (optional, for LLM news structuring, ~$5 cost, get at https://console.anthropic.com)

---

## Step 1: Clone and Setup Environment

```bash
git clone https://github.com/manneypraveen/nevarep-core.git
cd nevarep-core

# Create virtual environment
python3 -m venv venv
source venv/bin/activate    # On Mac/Linux
# venv\Scripts\activate     # On Windows

# Install dependencies
pip install -r requirements.txt

# Copy environment file
cp .env.example .env
```

Edit `.env` with your settings:
```bash
nano .env   # or open in any editor
```

Change these values:
```
FRED_API_KEY=your_actual_fred_key_here
ANTHROPIC_API_KEY=sk-ant-your-key-here   # optional, for LLM features
HIST_START=2019-01-01
HIST_END=2026-03-20
```

---

## Step 2: Initialize Database + Trading Calendar

```bash
python setup.py
```

**What it does:**
- Creates DuckDB database at `data/nevarep.duckdb`
- Creates all 13 tables with proper schemas
- Downloads Nifty 50 OHLC from Yahoo Finance to auto-detect trading days vs holidays
- Populates `trading_day` table with every calendar date, flagging holidays, weekly/monthly expiry days
- Marks holiday-adjacent trading days

**Expected output:**
```
Trading days:      ~1,780
Holidays detected: ~100
Weekly expiries:   ~370
Monthly expiries:  ~84
```

**Time:** ~30 seconds

---

## Step 3: Sprint 1 — Global Market Data (Yahoo Finance + FRED)

```bash
python -m scrapers.global_fetcher
```

**What it does:**
- Downloads 17 Yahoo Finance tickers: Nifty, Bank Nifty, Sensex, India VIX, S&P 500, Nasdaq, Dow, US VIX, DXY, USD/INR, Brent, Gold, Copper, Nikkei, Hang Seng, DAX, FTSE
- Downloads US 10Y and 2Y Treasury yields from FRED API
- Computes 30+ derived fields per index per day: gap analysis, candle classification, 20/50/200 DMAs, 52-week position, volume ratio
- Classifies global regimes: US market tone, yield regime, DXY regime, crude regime, Asia tone
- Loads into `daily_index_ohlc` (5,339 rows) and `daily_global_context` (1,781 rows)

**Expected output:**
```
daily_index_ohlc:    5,339 rows (3 indices x ~1,780 days)
daily_global_context: 1,781 rows
```

**Time:** ~30 seconds

---

## Step 4: Sprint 2 — NSE F&O Bhav Copies

This is the biggest download — every Nifty and Bank Nifty options/futures contract for 7 years.

```bash
# Download all bhav copies (takes 4-8 hours due to NSE rate limiting)
python -m scrapers.nse_fo_downloader --download-only
```

**Safe to interrupt** with Ctrl+C — it skips already-downloaded files on restart.

```bash
# After download completes, parse and load into database
python -m scrapers.nse_fo_downloader --parse-only
```

**What it does:**
- Downloads ZIP files from NSE archives for every trading day
- Handles two formats: old format (pre July 8, 2024) and UDiFF format (post July 8, 2024)
- Extracts CSV, filters for NIFTY + BANKNIFTY only (discards ~80% of rows)
- Maps both formats to unified column schema
- Loads into `raw_fo_bhav` table

**If you get errors during parse:**
- `NOT NULL constraint failed: raw_fo_bhav.strike_price` → Add `df["strike_price"] = df["strike_price"].fillna(0)` in parse function
- `Could not cast value to DECIMAL(15,2)` → Run: `python -c "import duckdb; con=duckdb.connect('data/nevarep.duckdb'); con.execute('ALTER TABLE raw_fo_bhav ALTER COLUMN value_lakhs TYPE DECIMAL(20,2)'); con.close()"`

**Expected output:**
```
raw_fo_bhav: 6,461,233 rows
  NIFTY:     3,790,805 rows (1,780 days)
  BANKNIFTY: 2,670,428 rows (1,780 days)
  Old format: 1,356 days
  UDiFF:      424 days
```

**Time:** 4-8 hours (download) + 5 minutes (parse)

---

## Step 5: Sprint 3 — Participant-wise OI (FII/DII Positioning)

```bash
python -m scrapers.participant_oi
```

**What it does:**
- Downloads daily participant OI CSVs from NSE archives
- 4 rows per day: Client, DII, FII, Pro
- Captures futures long/short, options call/put positions per participant
- Computes net index futures position (the most predictive signal)
- Loads into `daily_participant_oi`

**Expected output:**
```
daily_participant_oi: 7,116 rows (1,779 days x 4 participants)
```

**Time:** ~20-30 minutes (download) + 30 seconds (parse)

---

## Step 6: Sprint 4 — Institutional Flows (FII/DII Direction + Streaks)

```bash
python -m scrapers.fii_dii_scraper
```

**What it does:**
- Builds `daily_institutional_flows` from Sprint 3's participant OI data
- Computes daily change in FII/DII futures positioning as directional proxy
- Calculates: buying/selling streaks, intensity classification, month-to-date flows, dominance
- Loads into `daily_institutional_flows`

**Expected output:**
```
daily_institutional_flows: 1,779 rows
  Heavy sell days: ~619
  Heavy buy days:  ~545
  Max sell streak: -15 days
  Max buy streak:  +18 days
```

**Time:** ~10 seconds

---

## Step 7: Sprint 5 — News Collection (GDELT)

```bash
# Download news for all trading days (runs 30+ hours — use background)
nohup python -m scrapers.news_scraper --download-only > logs/news_download.log 2>&1 &

# Check progress
ls data/articles/news_*.json | wc -l

# After download completes, load into database
# Without LLM (free, keyword-based categorization):
python -m scrapers.news_scraper --load-only

# With LLM (requires ANTHROPIC_API_KEY, ~$5 cost, much better quality):
python -m scrapers.news_scraper --load-only --structure
```

**What it does:**
- Searches GDELT Project API with 10 market-related queries per trading day
- Collects 10-50 headlines per day from Indian financial media
- Categorizes into 15 categories: rbi_policy, crude_commodity, fii_dii_activity, etc.
- Rates severity (low/medium/high/critical) and expected impact (bullish/bearish/neutral)
- Loads into `daily_news_events`

**Expected output:**
```
daily_news_events: ~15,000-25,000 rows
```

**Time:** ~30 hours (download) + 5 minutes (load) or 30 minutes (with LLM)

---

## Step 8: Compute Options Analytics

```bash
python -m db.derived
```

**What it does:**
- Processes 6.4M raw F&O rows
- For each trading day × index, computes:
  - PCR (put-call ratio) with classification
  - Max pain strike
  - Highest call OI strike (resistance) and highest put OI strike (support)
  - OI-implied range and whether price stayed in it
  - FII long-short ratio
- Loads into `daily_options_snapshot`

**Expected output:**
```
daily_options_snapshot: 3,560 rows (1,780 days x 2 indices)
Key insight: 88% of days, price stayed within OI-implied range
  Nifty:     92.4% accuracy
  Bank Nifty: 83.7% accuracy
```

**Time:** ~60 seconds

---

## Step 9: Test Similarity Engine

```bash
# Find days similar to the latest day in database
python -m engine.similarity

# Find days similar to a specific date
python -m engine.similarity --date 2024-10-24

# Lower threshold for more results
python -m engine.similarity --min-sim 0.4
```

**What it does:**
- Builds a context profile for the target date (global macro, FII flow, options positioning, news, technicals)
- Compares against every prior day across 5 weighted dimensions
- Returns top 10 most similar days with outcomes (same day, next day, 3-day forward)
- Analyzes direction, confidence, expected range

**Expected output:**
```
Direction:     BEARISH
Confidence:    90%
Sample size:   10 similar days
Same day:      avg -1.22%
Next day:      avg -1.62%
3-day forward: avg -2.71%
```

**Time:** ~30-60 seconds per query

---

## Step 10: Run Walk-Forward Backtest

```bash
# Quick test (3 months, ~60 predictions)
python -m backtest.backtester --start 2025-01-01 --end 2025-03-20

# Full backtest (all available data, ~1,500 predictions, takes 4-5 hours)
nohup python -m backtest.backtester > logs/backtest_full.log 2>&1 &

# Check progress
tail -5 logs/backtest_full.log
```

**What it does:**
- For each test day T, predicts using only data before T (no look-ahead bias)
- Scores: direction correct? range correct? strategy P&L?
- Generates full scorecard with targets

**Performance targets:**
```
Direction accuracy > 60%   → achieved 73.2%
Win rate > 55%             → achieved 76.2%
Reward:Risk > 1.5:1        → achieved 3.59:1
Sharpe ratio > 1.2         → achieved 10.57
Max drawdown > -15%        → achieved -1.0%
```

**Time:** ~10 minutes (quick test) or 4-5 hours (full)

---

## Step 11: Start API Server

```bash
pip install fastapi uvicorn
python -m api.server
```

**Endpoints:**
| URL | Description |
|-----|-------------|
| http://localhost:8000/docs | Interactive API documentation |
| http://localhost:8000/api/health | Database health check |
| http://localhost:8000/api/dashboard | Full dashboard data (single call) |
| http://localhost:8000/api/premarket | Pre-market morning brief |
| http://localhost:8000/api/predict/2026-03-20 | Run prediction for a date |
| http://localhost:8000/api/snapshot/2026-03-20 | Options snapshot |
| http://localhost:8000/api/flows/2026-03-20 | FII/DII flows |
| http://localhost:8000/api/ohlc/range?start=2025-01-01&end=2025-03-20&index=NIFTY50 | OHLC range for charts |
| http://localhost:8000/api/fii-trend?days=30 | FII positioning trend |
| http://localhost:8000/api/backtest/summary | Backtest scorecard |
| http://localhost:8000/api/stats | Database statistics |

**Note:** API server cannot run while backtester is running (DuckDB file lock). Kill backtester first if needed: `kill <PID>`

**Time:** Instant startup

---

## Quick Verification After Setup

Run this to verify all tables are populated:

```bash
python -c "
import duckdb
con = duckdb.connect('data/nevarep.duckdb', read_only=True)
tables = ['trading_day', 'daily_index_ohlc', 'daily_global_context',
          'raw_fo_bhav', 'daily_participant_oi', 'daily_institutional_flows',
          'daily_options_snapshot', 'daily_news_events', 'predictions_log']
for t in tables:
    try:
        count = con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        print(f'  {t:30s} {count:>12,} rows')
    except:
        print(f'  {t:30s}       EMPTY')
con.close()
"
```

**Expected output (fully populated):**
```
  trading_day                          2,600 rows
  daily_index_ohlc                     5,339 rows
  daily_global_context                 1,781 rows
  raw_fo_bhav                      6,461,233 rows
  daily_participant_oi                 7,116 rows
  daily_institutional_flows            1,779 rows
  daily_options_snapshot               3,560 rows
  daily_news_events                   15,000+ rows
  predictions_log                      1,500+ rows
```

---

## Troubleshooting

**`TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`**
→ Python 3.9 issue. The files already have `from __future__ import annotations` at the top. If you see this error in any new file, add that import.

**`duckdb.IOException: Could not set lock on file`**
→ Another process (backtester, scraper) has the database open for writing. Only one write connection allowed at a time. Check: `ps aux | grep python` and kill the other process.

**`Conversion Error: Could not cast value to DECIMAL`**
→ UDiFF format has larger numbers. Run: `python -c "import duckdb; con=duckdb.connect('data/nevarep.duckdb'); con.execute('ALTER TABLE raw_fo_bhav ALTER COLUMN value_lakhs TYPE DECIMAL(20,2)'); con.close()"`

**NSE download blocked (403/HTML responses)**
→ NSE rate-limits scrapers. The downloader auto-refreshes sessions with progressive backoff. If persistently blocked, wait 30 minutes and retry. Try a different network/VPN.

**FRED API returns empty data**
→ Check your FRED_API_KEY in `.env`. Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html

**Yahoo Finance returns no data**
→ Occasionally Yahoo Finance has outages. Wait and retry. The data is cached — re-running only refetches, doesn't lose existing data.

---

## File Structure After Full Setup

```
nevarep-core/
├── data/
│   ├── nevarep.duckdb          # ~625 MB database (6.5M+ rows)
│   ├── raw_csv/
│   │   ├── fo_bhav/            # ~1,780 CSV files (~4 GB)
│   │   ├── participant_oi/     # ~1,780 CSV files (~10 MB)
│   │   └── fii_dii/            # JSON files
│   └── articles/               # ~1,780 JSON news files
├── logs/
│   ├── global_fetcher.log
│   ├── nse_fo_downloader.log
│   ├── participant_oi.log
│   ├── news_scraper.log
│   ├── derived.log
│   ├── similarity.log
│   └── backtester.log
├── scrapers/                   # Data collection
├── db/                         # Database + derived computations
├── engine/                     # Similarity + prediction
├── backtest/                   # Walk-forward validation
├── api/                        # FastAPI REST server
├── config/                     # Settings, URLs, thresholds
├── utils/                      # NSE session, date helpers
├── setup.py                    # Initial DB + calendar setup
├── run.py                      # CLI entry point
├── .env                        # API keys (gitignored)
├── .env.example                # Template for .env
└── requirements.txt            # Python dependencies
```

---

## Total Time Estimate

| Step | Time | Can Background? |
|------|------|-----------------|
| Steps 1-3 (setup + global data) | 5 minutes | No |
| Step 4 (NSE bhav copies) | 4-8 hours | Yes |
| Step 5 (participant OI) | 30 minutes | Yes |
| Step 6 (institutional flows) | 10 seconds | No |
| Step 7 (news download) | 30 hours | Yes |
| Step 8 (options analytics) | 60 seconds | No |
| Step 9 (similarity test) | 60 seconds | No |
| Step 10 (full backtest) | 4-5 hours | Yes |
| **Total** | **~40 hours** | Most runs in background |

The interactive steps (1-3, 6, 8-9) take under 10 minutes.
The long downloads (4, 5, 7, 10) run in background.

---

*NEVAREP: Never repeat. Always remember. Always learn.*