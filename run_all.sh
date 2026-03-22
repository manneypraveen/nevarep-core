#!/usr/bin/env bash

# ══════════════════════════════════════════════════════════════
#  NEVAREP — Full Pipeline Runner
#  Run this after cloning the repo to set up everything.
#
#  Usage:
#    chmod +x run_all.sh
#    ./run_all.sh
#
#  Or run individual steps:
#    ./run_all.sh 1      # Only step 1
#    ./run_all.sh 4      # Only step 4
#    ./run_all.sh 4-8    # Steps 4 through 8
# ══════════════════════════════════════════════════════════════

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

STEP_START=${1:-1}
STEP_END=${1:-11}

# Handle range input like "4-8"
if [[ "$1" == *-* ]]; then
    STEP_START="${1%-*}"
    STEP_END="${1#*-}"
fi

step_header() {
    echo ""
    echo -e "${CYAN}══════════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  STEP $1: $2${NC}"
    echo -e "${CYAN}══════════════════════════════════════════════════════════════${NC}"
    echo ""
}

step_done() {
    echo -e "${GREEN}  ✅ Step $1 complete.${NC}"
}

step_skip() {
    echo -e "${YELLOW}  ⏭  Step $1 skipped (outside range $STEP_START-$STEP_END).${NC}"
}

check_env() {
    if [ ! -f ".env" ]; then
        echo -e "${RED}ERROR: .env file not found.${NC}"
        echo "  Run: cp .env.example .env"
        echo "  Then edit .env with your FRED_API_KEY"
        exit 1
    fi
}

check_venv() {
    if [ -z "$VIRTUAL_ENV" ]; then
        if [ -d "venv" ]; then
            echo -e "${YELLOW}  Activating virtual environment...${NC}"
            source venv/bin/activate
        else
            echo -e "${RED}ERROR: No virtual environment found.${NC}"
            echo "  Run: python3 -m venv venv && source venv/bin/activate"
            exit 1
        fi
    fi
}

should_run() {
    [ "$1" -ge "$STEP_START" ] && [ "$1" -le "$STEP_END" ]
}

# ──────────────────────────────────────────────────────────
echo -e "${CYAN}"
echo "  ╔═══════════════════════════════════════════════════╗"
echo "  ║           NEVAREP — Full Pipeline Runner          ║"
echo "  ║   Indian Financial Intelligence Terminal          ║"
echo "  ╚═══════════════════════════════════════════════════╝"
echo -e "${NC}"

check_venv
check_env
mkdir -p logs data/raw_csv/fo_bhav data/raw_csv/participant_oi data/articles

# ──────────────────────────────────────────────────────────
# STEP 1: Install dependencies
# ──────────────────────────────────────────────────────────
if should_run 1; then
    step_header 1 "Install Python dependencies"

    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt
    pip install --quiet fastapi uvicorn

    step_done 1
else
    step_skip 1
fi

# ──────────────────────────────────────────────────────────
# STEP 2: Initialize database + trading calendar
# ──────────────────────────────────────────────────────────
if should_run 2; then
    step_header 2 "Initialize database + trading calendar"

    python setup.py

    echo ""
    python -c "
import duckdb
con = duckdb.connect('data/nevarep.duckdb', read_only=True)
days = con.execute('SELECT COUNT(*) FROM trading_day WHERE is_trading_day = TRUE').fetchone()[0]
holidays = con.execute('SELECT COUNT(*) FROM trading_day WHERE is_trading_day = FALSE').fetchone()[0]
print(f'  Trading days: {days}')
print(f'  Holidays:     {holidays}')
con.close()
"
    step_done 2
else
    step_skip 2
fi

# ──────────────────────────────────────────────────────────
# STEP 3: Sprint 1 — Global market data (Yahoo Finance + FRED)
# ──────────────────────────────────────────────────────────
if should_run 3; then
    step_header 3 "Sprint 1 — Global market data (17 tickers + FRED yields)"

    python -m scrapers.global_fetcher

    echo ""
    python -c "
import duckdb
con = duckdb.connect('data/nevarep.duckdb', read_only=True)
ohlc = con.execute('SELECT COUNT(*) FROM daily_index_ohlc').fetchone()[0]
ctx = con.execute('SELECT COUNT(*) FROM daily_global_context').fetchone()[0]
print(f'  daily_index_ohlc:    {ohlc:,} rows')
print(f'  daily_global_context: {ctx:,} rows')
con.close()
"
    step_done 3
else
    step_skip 3
fi

# ──────────────────────────────────────────────────────────
# STEP 4: Sprint 2 — NSE F&O bhav copies (LONG — 4-8 hours)
# ──────────────────────────────────────────────────────────
if should_run 4; then
    step_header 4 "Sprint 2 — NSE F&O bhav copy download (4-8 hours)"
    echo -e "${YELLOW}  This is the longest step. Downloads ~1,780 files from NSE.${NC}"
    echo -e "${YELLOW}  Safe to Ctrl+C and restart — skips already-downloaded files.${NC}"
    echo ""

    python -m scrapers.nse_fo_downloader --download-only

    step_done 4
else
    step_skip 4
fi

# ──────────────────────────────────────────────────────────
# STEP 5: Sprint 2 — Parse bhav copies into database
# ──────────────────────────────────────────────────────────
if should_run 5; then
    step_header 5 "Sprint 2 — Parse bhav copies into database (~5 min)"

    python -m scrapers.nse_fo_downloader --parse-only

    echo ""
    python -c "
import duckdb
con = duckdb.connect('data/nevarep.duckdb', read_only=True)
count = con.execute('SELECT COUNT(*) FROM raw_fo_bhav').fetchone()[0]
print(f'  raw_fo_bhav: {count:,} rows')
con.close()
"
    step_done 5
else
    step_skip 5
fi

# ──────────────────────────────────────────────────────────
# STEP 6: Sprint 3 — Participant-wise OI (FII/DII positioning)
# ──────────────────────────────────────────────────────────
if should_run 6; then
    step_header 6 "Sprint 3 — Participant-wise OI download + parse (~30 min)"

    python -m scrapers.participant_oi

    echo ""
    python -c "
import duckdb
con = duckdb.connect('data/nevarep.duckdb', read_only=True)
count = con.execute('SELECT COUNT(*) FROM daily_participant_oi').fetchone()[0]
print(f'  daily_participant_oi: {count:,} rows')
con.close()
"
    step_done 6
else
    step_skip 6
fi

# ──────────────────────────────────────────────────────────
# STEP 7: Sprint 4 — Institutional flows (FII/DII streaks)
# ──────────────────────────────────────────────────────────
if should_run 7; then
    step_header 7 "Sprint 4 — Institutional flows (10 seconds)"

    python -m scrapers.fii_dii_scraper

    echo ""
    python -c "
import duckdb
con = duckdb.connect('data/nevarep.duckdb', read_only=True)
count = con.execute('SELECT COUNT(*) FROM daily_institutional_flows').fetchone()[0]
print(f'  daily_institutional_flows: {count:,} rows')
con.close()
"
    step_done 7
else
    step_skip 7
fi

# ──────────────────────────────────────────────────────────
# STEP 8: Sprint 5 — News collection (LONG — 30+ hours)
# ──────────────────────────────────────────────────────────
if should_run 8; then
    step_header 8 "Sprint 5 — News collection from GDELT (30+ hours)"
    echo -e "${YELLOW}  Starting news download in background...${NC}"
    echo -e "${YELLOW}  Check progress: ls data/articles/news_*.json | wc -l${NC}"
    echo ""

    nohup python -m scrapers.news_scraper --download-only > logs/news_download.log 2>&1 &
    NEWS_PID=$!
    echo -e "  Background PID: ${GREEN}$NEWS_PID${NC}"
    echo "  View log: tail -f logs/news_download.log"

    # Wait a few seconds to confirm it started
    sleep 3
    if ps -p $NEWS_PID > /dev/null 2>&1; then
        echo -e "  ${GREEN}News download started successfully.${NC}"
    else
        echo -e "  ${RED}News download failed to start. Check logs/news_download.log${NC}"
    fi

    step_done 8
else
    step_skip 8
fi

# ──────────────────────────────────────────────────────────
# STEP 9: Compute options analytics (PCR, max pain, OI range)
# ──────────────────────────────────────────────────────────
if should_run 9; then
    step_header 9 "Compute options analytics (~60 seconds)"

    python -m db.derived

    echo ""
    python -c "
import duckdb
con = duckdb.connect('data/nevarep.duckdb', read_only=True)
count = con.execute('SELECT COUNT(*) FROM daily_options_snapshot').fetchone()[0]
pct = con.execute(\"\"\"
    SELECT ROUND(AVG(CASE WHEN price_in_oi_range THEN 100.0 ELSE 0 END), 1)
    FROM daily_options_snapshot WHERE index_name = 'NIFTY50'
\"\"\").fetchone()[0]
print(f'  daily_options_snapshot: {count:,} rows')
print(f'  OI range accuracy (Nifty): {pct}%')
con.close()
"
    step_done 9
else
    step_skip 9
fi

# ──────────────────────────────────────────────────────────
# STEP 10: Test similarity engine
# ──────────────────────────────────────────────────────────
if should_run 10; then
    step_header 10 "Test similarity engine (first prediction)"

    python -m engine.similarity

    step_done 10
else
    step_skip 10
fi

# ──────────────────────────────────────────────────────────
# STEP 11: Run backtest (LONG — 4-5 hours for full)
# ──────────────────────────────────────────────────────────
if should_run 11; then
    step_header 11 "Walk-forward backtest"
    echo -e "${YELLOW}  Running quick test (3 months) first...${NC}"
    echo ""

    python -m backtest.backtester --start 2025-01-01 --end 2025-03-20

    echo ""
    echo -e "${YELLOW}  Starting full backtest in background (4-5 hours)...${NC}"

    nohup python -m backtest.backtester > logs/backtest_full.log 2>&1 &
    BT_PID=$!
    echo -e "  Background PID: ${GREEN}$BT_PID${NC}"
    echo "  View results when done: cat logs/backtest_full.log"

    step_done 11
else
    step_skip 11
fi

# ──────────────────────────────────────────────────────────
# FINAL SUMMARY
# ──────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}══════════════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  PIPELINE COMPLETE${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════════════${NC}"
echo ""

python -c "
import duckdb, os
db_path = 'data/nevarep.duckdb'
if not os.path.exists(db_path):
    print('  Database not found.')
    exit()
con = duckdb.connect(db_path, read_only=True)
tables = [
    'trading_day', 'daily_index_ohlc', 'daily_global_context',
    'raw_fo_bhav', 'daily_participant_oi', 'daily_institutional_flows',
    'daily_options_snapshot', 'daily_news_events', 'predictions_log'
]
total = 0
for t in tables:
    try:
        count = con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        total += count
        status = '✅' if count > 0 else '⬜'
        print(f'  {status} {t:30s} {count:>12,} rows')
    except:
        print(f'  ❌ {t:30s}       MISSING')
con.close()
size = os.path.getsize(db_path) / 1024 / 1024
print(f'')
print(f'  Total rows: {total:,}')
print(f'  DB size:    {size:.1f} MB')
"

echo ""
echo -e "  ${GREEN}To start the API server:${NC}"
echo "    python -m api.server"
echo ""
echo -e "  ${GREEN}Then open:${NC}"
echo "    http://localhost:8000/docs         — API documentation"
echo "    http://localhost:8000/api/dashboard — Full dashboard data"
echo "    http://localhost:8000/api/premarket — Pre-market brief"
echo ""
echo -e "  ${GREEN}Background processes:${NC}"
echo "    Check: ps aux | grep python"
echo "    News download log: tail -f logs/news_download.log"
echo "    Backtest log: tail -f logs/backtest_full.log"
echo ""
echo -e "${CYAN}══════════════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  NEVAREP: Never repeat. Always remember. Always learn.${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════════════${NC}"