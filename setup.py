"""
NEVAREP — Initial Setup (Steps 1-5)
Run this once to:
  1. Create the DuckDB database with all tables
  2. Auto-detect trading days and holidays using Yahoo Finance
  3. Mark weekly and monthly expiries
  4. Verify the setup

Usage:
    python setup.py
"""

import os
import sys
from datetime import date, timedelta, datetime
from pathlib import Path

# Ensure we can import from project root
sys.path.insert(0, str(Path(__file__).parent))

# ── Step 1: Check dependencies ──
print("=" * 60)
print("  NEVAREP — Initial Setup")
print("=" * 60)

print("\n[Step 1] Checking dependencies...")
missing = []
try:
    import yfinance as yf
except ImportError:
    missing.append("yfinance")
try:
    import duckdb
except ImportError:
    missing.append("duckdb")
try:
    import pandas as pd
except ImportError:
    missing.append("pandas")
try:
    from dotenv import load_dotenv
except ImportError:
    missing.append("python-dotenv")
try:
    from loguru import logger
except ImportError:
    missing.append("loguru")

if missing:
    print(f"  Missing packages: {', '.join(missing)}")
    print(f"  Run: pip install {' '.join(missing)}")
    sys.exit(1)

print("  All dependencies OK")

# ── Load config ──
load_dotenv()
from config.settings import (
    DB_PATH, HIST_START, HIST_END, DATA_DIR, 
    BHAV_DIR, PARTICIPANT_OI_DIR, ARTICLES_DIR,
)

# ── Step 2: Create database and tables ──
print(f"\n[Step 2] Creating DuckDB database at {DB_PATH}...")

con = duckdb.connect(str(DB_PATH))

# ── Trading day calendar ──
con.execute("""
    CREATE TABLE IF NOT EXISTS trading_day (
        trade_date       DATE PRIMARY KEY,
        is_trading_day   BOOLEAN NOT NULL DEFAULT TRUE,
        day_of_week      VARCHAR(10),
        is_expiry_weekly BOOLEAN DEFAULT FALSE,
        is_expiry_monthly BOOLEAN DEFAULT FALSE,
        is_holiday_adjacent BOOLEAN DEFAULT FALSE,
        holiday_name     VARCHAR(100)
    )
""")

# ── Raw F&O bhav copy ──
con.execute("""
    CREATE TABLE IF NOT EXISTS raw_fo_bhav (
        trade_date       DATE NOT NULL,
        instrument       VARCHAR(20) NOT NULL,
        symbol           VARCHAR(30) NOT NULL,
        expiry_date      DATE NOT NULL,
        strike_price     DECIMAL(12,2),
        option_type      VARCHAR(5),
        open_price       DECIMAL(12,2),
        high_price       DECIMAL(12,2),
        low_price        DECIMAL(12,2),
        close_price      DECIMAL(12,2),
        settle_price     DECIMAL(12,2),
        last_price       DECIMAL(12,2),
        prev_close       DECIMAL(12,2),
        underlying_price DECIMAL(12,2),
        contracts        BIGINT,
        value_lakhs      DECIMAL(15,2),
        open_interest    BIGINT,
        change_in_oi     BIGINT,
        num_trades       BIGINT,
        lot_size         INTEGER,
        source_format    VARCHAR(10) NOT NULL,
        PRIMARY KEY (trade_date, symbol, expiry_date, strike_price, option_type)
    )
""")

# ── Daily index OHLC ──
con.execute("""
    CREATE TABLE IF NOT EXISTS daily_index_ohlc (
        trade_date       DATE NOT NULL,
        index_name       VARCHAR(20) NOT NULL,
        open_price       DECIMAL(12,2),
        high_price       DECIMAL(12,2),
        low_price        DECIMAL(12,2),
        close_price      DECIMAL(12,2),
        prev_close       DECIMAL(12,2),
        change_points    DECIMAL(10,2),
        change_pct       DECIMAL(8,4),
        volume           BIGINT,
        gap_open_points  DECIMAL(10,2),
        gap_open_pct     DECIMAL(8,4),
        gap_type         VARCHAR(10),
        gap_filled       BOOLEAN,
        day_range        DECIMAL(10,2),
        close_position   VARCHAR(15),
        candle_body      DECIMAL(10,2),
        upper_wick       DECIMAL(10,2),
        lower_wick       DECIMAL(10,2),
        candle_type      VARCHAR(20),
        dma_20           DECIMAL(12,2),
        dma_50           DECIMAL(12,2),
        dma_200          DECIMAL(12,2),
        vs_20dma_pct     DECIMAL(8,4),
        vs_50dma_pct     DECIMAL(8,4),
        vs_200dma_pct    DECIMAL(8,4),
        above_200dma     BOOLEAN,
        dist_52w_high_pct DECIMAL(8,4),
        dist_52w_low_pct DECIMAL(8,4),
        vol_vs_20d_avg   DECIMAL(8,4),
        day_shape        VARCHAR(30),
        PRIMARY KEY (trade_date, index_name)
    )
""")

# ── Daily options snapshot ──
con.execute("""
    CREATE TABLE IF NOT EXISTS daily_options_snapshot (
        trade_date              DATE NOT NULL,
        index_name              VARCHAR(20) NOT NULL,
        total_call_oi           BIGINT,
        total_put_oi            BIGINT,
        pcr_oi                  DECIMAL(8,4),
        pcr_classification      VARCHAR(20),
        pcr_change              DECIMAL(8,4),
        max_pain_strike         DECIMAL(12,2),
        highest_call_oi_strike  DECIMAL(12,2),
        highest_put_oi_strike   DECIMAL(12,2),
        oi_range_low            DECIMAL(12,2),
        oi_range_high           DECIMAL(12,2),
        price_in_oi_range       BOOLEAN,
        oi_breach_side          VARCHAR(15),
        net_oi_interpretation   VARCHAR(20),
        india_vix_close         DECIMAL(8,2),
        india_vix_change_pct    DECIMAL(8,4),
        vix_regime              VARCHAR(15),
        atm_iv                  DECIMAL(8,2),
        iv_percentile           DECIMAL(8,2),
        iv_skew                 DECIMAL(8,4),
        fii_index_futures_net   BIGINT,
        fii_long_short_ratio    DECIMAL(8,4),
        pcr_signal_correct      BOOLEAN,
        oi_support_held         BOOLEAN,
        max_pain_accurate       BOOLEAN,
        indicators_correct_count INTEGER,
        mismatch_severity       VARCHAR(10),
        gex                     DECIMAL(18,2),
        PRIMARY KEY (trade_date, index_name)
    )
""")

# ── Daily greeks (per-strike IV + greeks for NIFTY50 + BANKNIFTY) ──
# CRUDEOIL: no MCX options feed — TODO: MCX crude options feed
con.execute("""
    CREATE TABLE IF NOT EXISTS daily_greeks (
        trade_date      DATE NOT NULL,
        index_name      VARCHAR(20) NOT NULL,
        expiry_date     DATE NOT NULL,
        strike_price    DECIMAL(12,2) NOT NULL,
        opt_type        VARCHAR(2) NOT NULL,
        iv              DECIMAL(8,6),
        delta           DECIMAL(8,6),
        gamma           DECIMAL(12,8),
        theta           DECIMAL(8,6),
        vega            DECIMAL(8,6),
        open_interest   BIGINT,
        gex             DECIMAL(18,2),
        PRIMARY KEY (trade_date, index_name, expiry_date, strike_price, opt_type)
    )
""")

# ── Daily participant OI ──
con.execute("""
    CREATE TABLE IF NOT EXISTS daily_participant_oi (
        trade_date          DATE NOT NULL,
        client_type         VARCHAR(10) NOT NULL,
        fut_idx_long        BIGINT,
        fut_idx_short       BIGINT,
        fut_stk_long        BIGINT,
        fut_stk_short       BIGINT,
        opt_idx_call_long   BIGINT,
        opt_idx_put_long    BIGINT,
        opt_idx_call_short  BIGINT,
        opt_idx_put_short   BIGINT,
        opt_stk_call_long   BIGINT,
        opt_stk_put_long    BIGINT,
        opt_stk_call_short  BIGINT,
        opt_stk_put_short   BIGINT,
        total_long          BIGINT,
        total_short         BIGINT,
        net_idx_futures     BIGINT,
        PRIMARY KEY (trade_date, client_type)
    )
""")

# ── Daily global context ──
con.execute("""
    CREATE TABLE IF NOT EXISTS daily_global_context (
        trade_date          DATE PRIMARY KEY,
        sp500_close         DECIMAL(10,2),
        sp500_change_pct    DECIMAL(8,4),
        nasdaq_close        DECIMAL(10,2),
        nasdaq_change_pct   DECIMAL(8,4),
        dow_close           DECIMAL(10,2),
        dow_change_pct      DECIMAL(8,4),
        us_market_tone      VARCHAR(15),
        us_vix_close        DECIMAL(8,2),
        us_vix_change_pct   DECIMAL(8,4),
        us_10y_yield        DECIMAL(8,4),
        us_10y_change_bps   DECIMAL(8,2),
        us_2y_yield         DECIMAL(8,4),
        yield_curve_spread  DECIMAL(8,4),
        yield_regime        VARCHAR(15),
        dxy_close           DECIMAL(8,3),
        dxy_change_pct      DECIMAL(8,4),
        dxy_regime          VARCHAR(15),
        usdinr_close        DECIMAL(8,4),
        usdinr_change_pct   DECIMAL(8,4),
        usdinr_5d_trend     VARCHAR(15),
        brent_close         DECIMAL(10,2),
        brent_change_pct    DECIMAL(8,4),
        crude_regime        VARCHAR(15),
        gold_close          DECIMAL(10,2),
        gold_change_pct     DECIMAL(8,4),
        copper_close        DECIMAL(10,2),
        copper_change_pct   DECIMAL(8,4),
        nikkei_change_pct   DECIMAL(8,4),
        hangseng_change_pct DECIMAL(8,4),
        asia_tone           VARCHAR(15),
        dax_change_pct      DECIMAL(8,4),
        ftse_change_pct     DECIMAL(8,4),
        gift_nifty_gap      DECIMAL(10,2)
    )
""")

# ── Daily institutional flows ──
con.execute("""
    CREATE TABLE IF NOT EXISTS daily_institutional_flows (
        trade_date          DATE PRIMARY KEY,
        fii_gross_buy_cr    DECIMAL(12,2),
        fii_gross_sell_cr   DECIMAL(12,2),
        fii_net_cr          DECIMAL(12,2),
        fii_buy_sell_ratio  DECIMAL(8,4),
        fii_streak_days     INTEGER,
        fii_mtd_net_cr      DECIMAL(15,2),
        fii_intensity       VARCHAR(15),
        dii_gross_buy_cr    DECIMAL(12,2),
        dii_gross_sell_cr   DECIMAL(12,2),
        dii_net_cr          DECIMAL(12,2),
        dii_streak_days     INTEGER,
        fii_dii_combined_net DECIMAL(12,2),
        flow_dominant       VARCHAR(25)
    )
""")

# ── Daily news events ──
con.execute("""
    CREATE SEQUENCE IF NOT EXISTS seq_news_id START 1;
    CREATE TABLE IF NOT EXISTS daily_news_events (
        id                  INTEGER DEFAULT nextval('seq_news_id') PRIMARY KEY,
        trade_date          DATE NOT NULL,
        news_timestamp      TIMESTAMP,
        headline            VARCHAR(500) NOT NULL,
        source              VARCHAR(100),
        category            VARCHAR(30) NOT NULL,
        entity              VARCHAR(20),
        direction           VARCHAR(10),
        severity            VARCHAR(10) NOT NULL,
        scheduled           BOOLEAN DEFAULT FALSE,
        expected_impact     VARCHAR(10),
        novelty_weight      DECIMAL(5,4) DEFAULT 1.0,
        index_affected      VARCHAR(20),
        sector_affected     VARCHAR(30),
        actual_value        DECIMAL(12,4),
        expected_value      DECIMAL(12,4),
        surprise            DECIMAL(12,4),
        surprise_direction  VARCHAR(10)
    )
""")

# ── Daily scheduled events ──
con.execute("""
    CREATE SEQUENCE IF NOT EXISTS seq_events_id START 1;
    CREATE TABLE IF NOT EXISTS daily_scheduled_events (
        id                  INTEGER DEFAULT nextval('seq_events_id') PRIMARY KEY,
        trade_date          DATE NOT NULL,
        event_type          VARCHAR(30) NOT NULL,
        event_detail        VARCHAR(500),
        expected_volatility VARCHAR(10)
    )
""")

# ── Daily narrative ──
con.execute("""
    CREATE TABLE IF NOT EXISTS daily_narrative (
        trade_date              DATE PRIMARY KEY,
        narrative_summary       TEXT,
        dominant_factor         VARCHAR(200),
        dominant_category       VARCHAR(30),
        secondary_factors       TEXT,
        pattern_tags            TEXT,
        day_type                VARCHAR(30),
        indicators_correct      INTEGER,
        reliability_score       DECIMAL(6,2),
        most_reliable           VARCHAR(50),
        most_misleading         VARCHAR(50),
        mismatch_explanation    TEXT,
        similar_past_dates      TEXT,
        condition_rule          TEXT,
        key_lesson              TEXT,
        forward_implication     TEXT
    )
""")

# ── Condition rules ──
con.execute("""
    CREATE SEQUENCE IF NOT EXISTS seq_rules_id START 1;
    CREATE TABLE IF NOT EXISTS condition_rules (
        id                  INTEGER DEFAULT nextval('seq_rules_id') PRIMARY KEY,
        rule_text           TEXT NOT NULL,
        conditions          TEXT,
        outcome             VARCHAR(200),
        reliability_pct     DECIMAL(6,2),
        sample_size         INTEGER,
        supporting_dates    TEXT,
        first_observed      DATE,
        last_observed       DATE,
        category            VARCHAR(30)
    )
""")

# ── Predictions log ──
con.execute("""
    CREATE SEQUENCE IF NOT EXISTS seq_pred_id START 1;
    CREATE TABLE IF NOT EXISTS predictions_log (
        id                  INTEGER DEFAULT nextval('seq_pred_id') PRIMARY KEY,
        prediction_date     DATE NOT NULL,
        generated_at        TIMESTAMP NOT NULL,
        predicted_direction VARCHAR(10),
        confidence_pct      DECIMAL(6,2),
        predicted_range_low DECIMAL(12,2),
        predicted_range_high DECIMAL(12,2),
        strategy_recommended VARCHAR(50),
        similar_days_used   TEXT,
        similarity_scores   TEXT,
        actual_change_pct   DECIMAL(8,4),
        direction_correct   BOOLEAN,
        range_correct       BOOLEAN,
        strategy_pnl        DECIMAL(12,2),
        notes               TEXT
    )
""")

# ── Pipeline runs ──
con.execute("""
    CREATE TABLE IF NOT EXISTS pipeline_runs (
        id                  INTEGER PRIMARY KEY,
        job_name            VARCHAR(50) NOT NULL,
        started_at          TIMESTAMP NOT NULL,
        completed_at        TIMESTAMP,
        status              VARCHAR(15),
        records_processed   INTEGER,
        errors_count        INTEGER,
        error_details       TEXT,
        llm_tokens_used     INTEGER,
        llm_cost_usd        DECIMAL(8,4)
    )
""")

tables = con.execute(
    "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name"
).fetchall()
print(f"  Created {len(tables)} tables:")
for t in tables:
    print(f"    - {t[0]}")


# ── Step 3: Auto-detect trading days from Yahoo Finance ──
print(f"\n[Step 3] Building trading calendar ({HIST_START} to {HIST_END})...")
print("  Downloading Nifty 50 data from Yahoo Finance to detect trading days...")

nifty = yf.download(
    "^NSEI",
    start=HIST_START.isoformat(),
    end=(HIST_END + timedelta(days=1)).isoformat(),
    progress=False,
)

if nifty.empty:
    print("  ERROR: Could not download Nifty data from Yahoo Finance.")
    print("  Check your internet connection and try again.")
    sys.exit(1)

# Get actual trading dates from Yahoo Finance
actual_trading_dates = set(nifty.index.date)
print(f"  Yahoo Finance returned {len(actual_trading_dates)} trading days")

# Generate all calendar dates in range
all_dates = []
current = HIST_START
while current <= HIST_END:
    all_dates.append(current)
    current += timedelta(days=1)

print(f"  Calendar range: {len(all_dates)} total days")

# ── Step 4: Populate trading_day table with holiday detection ──
print(f"\n[Step 4] Populating trading_day table with auto-detected holidays...")

day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
records = []
holidays_detected = 0
trading_days_count = 0

for d in all_dates:
    is_weekend = d.weekday() >= 5
    is_trading = d in actual_trading_dates
    is_holiday = not is_weekend and not is_trading  # weekday with no data = holiday

    if is_holiday:
        holidays_detected += 1
    if is_trading:
        trading_days_count += 1

    # Weekly expiry: Thursday
    is_expiry_weekly = d.weekday() == 3 and is_trading

    # Monthly expiry: last Thursday of the month
    is_expiry_monthly = False
    if d.weekday() == 3 and is_trading:
        next_week = d + timedelta(days=7)
        if next_week.month != d.month:
            is_expiry_monthly = True

    records.append((
        d,
        is_trading,
        day_names[d.weekday()],
        is_expiry_weekly,
        is_expiry_monthly,
        False,  # is_holiday_adjacent (computed below)
        "weekend" if is_weekend else ("holiday" if is_holiday else None),
    ))

# Insert into DuckDB
con.execute("DELETE FROM trading_day")
con.executemany(
    """INSERT INTO trading_day 
       (trade_date, is_trading_day, day_of_week, is_expiry_weekly, 
        is_expiry_monthly, is_holiday_adjacent, holiday_name)
       VALUES (?, ?, ?, ?, ?, ?, ?)""",
    records,
)

# Mark holiday-adjacent days (trading day before or after a holiday/long weekend)
con.execute("""
    UPDATE trading_day SET is_holiday_adjacent = TRUE
    WHERE is_trading_day = TRUE
    AND (
        -- Day before a holiday
        trade_date IN (
            SELECT trade_date - INTERVAL '1 day'
            FROM trading_day
            WHERE is_trading_day = FALSE AND day_of_week NOT IN ('Saturday', 'Sunday')
        )
        OR
        -- Day after a holiday
        trade_date IN (
            SELECT trade_date + INTERVAL '1 day'
            FROM trading_day
            WHERE is_trading_day = FALSE AND day_of_week NOT IN ('Saturday', 'Sunday')
        )
    )
""")

print(f"  Trading days:      {trading_days_count}")
print(f"  Holidays detected: {holidays_detected}")
print(f"  Weekly expiries:   {con.execute('SELECT COUNT(*) FROM trading_day WHERE is_expiry_weekly').fetchone()[0]}")
print(f"  Monthly expiries:  {con.execute('SELECT COUNT(*) FROM trading_day WHERE is_expiry_monthly').fetchone()[0]}")
print(f"  Holiday-adjacent:  {con.execute('SELECT COUNT(*) FROM trading_day WHERE is_holiday_adjacent').fetchone()[0]}")

# Show detected holidays
print(f"\n  Detected NSE holidays (weekdays with no trading):")
holidays = con.execute("""
    SELECT trade_date, day_of_week 
    FROM trading_day 
    WHERE holiday_name = 'holiday'
    ORDER BY trade_date
""").fetchall()

for h in holidays:
    print(f"    {h[0]} ({h[1]})")


# ── Step 5: Verify setup ──
print(f"\n[Step 5] Verification...")

# Check all tables are empty (except trading_day)
for t in tables:
    tname = t[0]
    count = con.execute(f"SELECT COUNT(*) FROM {tname}").fetchone()[0]
    status = "OK" if (tname == "trading_day" and count > 0) or (tname != "trading_day" and count == 0) else "CHECK"
    print(f"  {tname}: {count} rows [{status}]")

# Check date range coverage
first_day = con.execute("SELECT MIN(trade_date) FROM trading_day WHERE is_trading_day").fetchone()[0]
last_day = con.execute("SELECT MAX(trade_date) FROM trading_day WHERE is_trading_day").fetchone()[0]
print(f"\n  Date range: {first_day} to {last_day}")
print(f"  Total trading days: {trading_days_count}")

# Show sample data
print(f"\n  Sample: first 5 trading days:")
sample = con.execute("""
    SELECT trade_date, day_of_week, is_expiry_weekly, is_expiry_monthly
    FROM trading_day WHERE is_trading_day ORDER BY trade_date LIMIT 5
""").fetchall()
for s in sample:
    exp = ""
    if s[2]:
        exp += " [WEEKLY EXPIRY]"
    if s[3]:
        exp += " [MONTHLY EXPIRY]"
    print(f"    {s[0]} ({s[1]}){exp}")

print(f"\n  Sample: last 5 trading days:")
sample = con.execute("""
    SELECT trade_date, day_of_week, is_expiry_weekly, is_expiry_monthly
    FROM trading_day WHERE is_trading_day ORDER BY trade_date DESC LIMIT 5
""").fetchall()
for s in reversed(sample):
    exp = ""
    if s[2]:
        exp += " [WEEKLY EXPIRY]"
    if s[3]:
        exp += " [MONTHLY EXPIRY]"
    print(f"    {s[0]} ({s[1]}){exp}")

con.close()

# ── Summary ──
print(f"\n{'=' * 60}")
print(f"  Setup complete!")
print(f"{'=' * 60}")
print(f"  Database: {DB_PATH}")
print(f"  Size:     {DB_PATH.stat().st_size / 1024:.1f} KB")
print(f"  Tables:   {len(tables)}")
print(f"  Trading days: {trading_days_count}")
print(f"  Holidays:     {holidays_detected}")
print(f"")
print(f"  Directories created:")
print(f"    {BHAV_DIR}")
print(f"    {PARTICIPANT_OI_DIR}")
print(f"    {ARTICLES_DIR}")
print(f"")
print(f"  Next step: python -m scrapers.global_fetcher")
print(f"  (Sprint 1: download Yahoo Finance + FRED data)")
print(f"{'=' * 60}")
