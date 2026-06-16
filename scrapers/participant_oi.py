from __future__ import annotations

"""
NEVAREP Sprint 3 — NSE Participant-wise Open Interest

Downloads daily participant OI data from NSE archives.
Each file has 4 rows: Client, DII, FII, Pro — showing their
futures and options positions (long/short) across index and stock segments.

URL: https://archives.nseindia.com/content/nsccl/fao_participant_oi_{ddMMyyyy}.csv

This is one of the most predictive datasets — FII long-short ratio
in index futures often leads Nifty by 1-3 days.

Usage:
    python -m scrapers.participant_oi
    python -m scrapers.participant_oi --start 2019-01-01 --end 2026-03-20
    python -m scrapers.participant_oi --parse-only
"""

import sys
import time
import random
import argparse
from datetime import date, timedelta, datetime
from pathlib import Path

import duckdb
import pandas as pd
from tqdm import tqdm
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import (
    DB_PATH, HIST_START, HIST_END, PARTICIPANT_OI_DIR,
    NSE_PARTICIPANT_OI_URL, NSE_MAX_ERRORS,
)
from utils.nse_session import is_html_response, run_download_loop
from utils.date_utils import format_nse_participant_oi

Path("logs").mkdir(exist_ok=True)
logger.add("logs/participant_oi.log", rotation="10 MB")


# ═══════════════════════════════════════════════════════════
# DOWNLOAD
# ═══════════════════════════════════════════════════════════

def download_one_day(d: date, session, headers: dict) -> tuple[Path | None, str]:
    """Download participant OI CSV for a single day."""
    date_str = format_nse_participant_oi(d)
    filename = f"fao_participant_oi_{date_str}.csv"
    csv_path = PARTICIPANT_OI_DIR / filename

    if csv_path.exists() and csv_path.stat().st_size > 50:
        return csv_path, "exists"

    url = NSE_PARTICIPANT_OI_URL.format(date_ddMMyyyy=date_str)

    try:
        session.get("https://www.nseindia.com/", headers=headers, timeout=10)
        time.sleep(0.2 + random.uniform(0.1, 0.3))

        resp = session.get(url, headers=headers, timeout=20)

        if resp.status_code == 404:
            return None, "holiday_404"
        elif resp.status_code in (403, 401):
            return None, "blocked"
        elif resp.status_code != 200:
            return None, "error"

        if is_html_response(resp):
            return None, "blocked"

        csv_path.write_bytes(resp.content)
        logger.info(f"Downloaded participant OI {d} ({len(resp.content)} bytes)")
        return csv_path, "ok"

    except Exception as e:
        logger.error(f"Error downloading participant OI {d}: {e}")
        return None, "error"


def download_all(start: date, end: date, max_errors: int = NSE_MAX_ERRORS):
    """Download participant OI for all trading days."""
    con = duckdb.connect(str(DB_PATH))
    trading_days = [
        row[0] for row in con.execute("""
            SELECT trade_date FROM trading_day
            WHERE is_trading_day = TRUE
            AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
        """, [start, end]).fetchall()
    ]
    con.close()

    if not trading_days:
        logger.error("No trading days found. Run setup.py first.")
        return

    logger.info(f"Downloading participant OI: {start} to {end} ({len(trading_days)} trading days)")

    stats, _ = run_download_loop(
        trading_days, download_one_day, max_errors,
        desc="Downloading participant OI", sleep_base=0.5, sleep_jitter=0.5,
    )

    logger.info(
        f"\nDownload Summary:\n"
        f"  Downloaded:  {stats['downloaded']}\n"
        f"  Already had: {stats['skipped']}\n"
        f"  Holidays:    {stats['holidays']}\n"
        f"  Blocked:     {stats['blocked']}\n"
        f"  Errors:      {stats['errors']}\n"
        f"  Refreshes:   {stats['session_refreshes']}"
    )


# ═══════════════════════════════════════════════════════════
# PARSE + LOAD
# ═══════════════════════════════════════════════════════════

# NSE participant OI CSV columns (typical):
# Client Type | Future Index Long | Future Index Short |
# Future Stock Long | Future Stock Short |
# Option Index Call Long | Option Index Put Long |
# Option Index Call Short | Option Index Put Short |
# Option Stock Call Long | Option Stock Put Long |
# Option Stock Call Short | Option Stock Put Short |
# Total Long Contracts | Total Short Contracts

PARTICIPANT_COLUMNS = [
    "client_type",
    "fut_idx_long", "fut_idx_short",
    "fut_stk_long", "fut_stk_short",
    "opt_idx_call_long", "opt_idx_put_long",
    "opt_idx_call_short", "opt_idx_put_short",
    "opt_stk_call_long", "opt_stk_put_long",
    "opt_stk_call_short", "opt_stk_put_short",
    "total_long", "total_short",
]

CLIENT_TYPE_MAP = {
    "Client": "CLIENT",
    "DII": "DII",
    "FII": "FII",
    "Pro": "PRO",
    # Variations in older files
    "PROP": "PRO",
    "Proprietary": "PRO",
    "Foreign Investors": "FII",
    "Domestic Investors": "DII",
    "Clients": "CLIENT",
}


def parse_participant_csv(csv_path: Path, trade_date: date) -> pd.DataFrame | None:
    """Parse a single participant OI CSV file."""
    try:
        df = pd.read_csv(csv_path)
        df.columns = df.columns.str.strip()

        # The CSV has varying column names across years — normalize
        if len(df.columns) < 15:
            logger.warning(f"Unexpected column count in {csv_path.name}: {len(df.columns)}")
            return None

        # Rename to standard names
        col_mapping = {}
        for i, std_name in enumerate(PARTICIPANT_COLUMNS):
            if i < len(df.columns):
                col_mapping[df.columns[i]] = std_name
        df = df.rename(columns=col_mapping)

        # Normalize client type names
        if "client_type" in df.columns:
            df["client_type"] = df["client_type"].str.strip().map(CLIENT_TYPE_MAP)
            df = df.dropna(subset=["client_type"])

        if df.empty or len(df) < 3:
            return None

        df["trade_date"] = trade_date

        # Convert numeric columns
        numeric_cols = [c for c in PARTICIPANT_COLUMNS if c != "client_type"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col].astype(str).str.replace(",", "").str.strip(),
                    errors="coerce"
                ).fillna(0).astype(int)

        # Compute net index futures position
        if "fut_idx_long" in df.columns and "fut_idx_short" in df.columns:
            df["net_idx_futures"] = df["fut_idx_long"] - df["fut_idx_short"]

        # Select columns matching our schema
        out_cols = ["trade_date", "client_type"] + numeric_cols + ["net_idx_futures"]
        out_cols = [c for c in out_cols if c in df.columns]

        return df[out_cols]

    except Exception as e:
        logger.error(f"Error parsing {csv_path}: {e}")
        return None


def load_to_db(start: date = None, end: date = None):
    """Parse all downloaded participant OI CSVs and load into DB."""
    csv_files = sorted(PARTICIPANT_OI_DIR.glob("fao_participant_oi_*.csv"))
    if not csv_files:
        logger.error("No participant OI CSVs found. Run download first.")
        return

    logger.info(f"Parsing {len(csv_files)} participant OI files...")

    all_dfs = []
    parsed = 0
    failed = 0

    for csv_path in tqdm(csv_files, desc="Parsing participant OI"):
        # Extract date from filename: fao_participant_oi_02012019.csv
        fname = csv_path.stem
        date_str = fname.replace("fao_participant_oi_", "")

        try:
            trade_date = datetime.strptime(date_str, "%d%m%Y").date()
        except ValueError:
            failed += 1
            continue

        if start and trade_date < start:
            continue
        if end and trade_date > end:
            continue

        df = parse_participant_csv(csv_path, trade_date)
        if df is not None and not df.empty:
            all_dfs.append(df)
            parsed += 1

    if not all_dfs:
        logger.error("No data parsed!")
        return

    logger.info(f"Parsed {parsed} files, {failed} failed")

    combined = pd.concat(all_dfs, ignore_index=True)

    con = duckdb.connect(str(DB_PATH))

    min_date = combined["trade_date"].min()
    max_date = combined["trade_date"].max()
    con.execute(
        "DELETE FROM daily_participant_oi WHERE trade_date BETWEEN ? AND ?",
        [min_date, max_date],
    )

    con.execute("INSERT INTO daily_participant_oi SELECT * FROM combined")

    count = con.execute("SELECT COUNT(*) FROM daily_participant_oi").fetchone()[0]
    logger.info(f"Loaded {count:,} rows into daily_participant_oi")

    # Stats
    stats = con.execute("""
        SELECT
            client_type,
            COUNT(DISTINCT trade_date) as days,
            ROUND(AVG(net_idx_futures), 0) as avg_net_idx_fut,
            MIN(trade_date) as first_date,
            MAX(trade_date) as last_date
        FROM daily_participant_oi
        GROUP BY client_type
        ORDER BY client_type
    """).fetchdf()
    print("\n  Participant OI Summary:")
    print(stats.to_string(index=False))

    # FII positioning trend (last 10 days)
    fii_recent = con.execute("""
        SELECT trade_date, net_idx_futures,
               fut_idx_long, fut_idx_short,
               ROUND(fut_idx_long::FLOAT / NULLIF(fut_idx_short, 0), 2) as long_short_ratio
        FROM daily_participant_oi
        WHERE client_type = 'FII'
        ORDER BY trade_date DESC
        LIMIT 10
    """).fetchdf()
    print("\n  FII Index Futures (last 10 days):")
    print(fii_recent.to_string(index=False))

    con.close()


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def run_backfill(start: date = None, end: date = None):
    start = start or HIST_START
    end = end or HIST_END

    print("=" * 60)
    print("  NEVAREP Sprint 3 — NSE Participant-wise OI")
    print("=" * 60)
    print(f"  Range: {start} to {end}")
    print(f"  Download dir: {PARTICIPANT_OI_DIR}")
    print()

    print("[1/2] Downloading from NSE archives...")
    download_all(start, end)

    print("\n[2/2] Parsing and loading into DuckDB...")
    load_to_db(start, end)

    con = duckdb.connect(str(DB_PATH))
    count = con.execute("SELECT COUNT(*) FROM daily_participant_oi").fetchone()[0]
    days = con.execute("SELECT COUNT(DISTINCT trade_date) FROM daily_participant_oi").fetchone()[0]
    con.close()

    print(f"\n{'=' * 60}")
    print(f"  Sprint 3 complete!")
    print(f"{'=' * 60}")
    print(f"  daily_participant_oi: {count:,} rows across {days} trading days")
    print(f"  DB size: {DB_PATH.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"\n  Next step: python -m scrapers.fii_dii_scraper")
    print(f"  (Sprint 4: FII/DII cash market flows)")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="NEVAREP Sprint 3 — Participant OI")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--parse-only", action="store_true")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else HIST_START
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else HIST_END

    if args.parse_only:
        print("Parse-only mode...")
        load_to_db(start, end)
    elif args.download_only:
        print("Download-only mode...")
        download_all(start, end)
    else:
        run_backfill(start, end)


if __name__ == "__main__":
    main()