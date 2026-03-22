from __future__ import annotations

"""
NEVAREP Sprint 2 — NSE F&O Bhav Copy Downloader

Downloads historical F&O bhav copies from NSE archives for NIFTY and BANKNIFTY.
Handles both formats:
  - Old format (Jan 2022 — Jul 5, 2024): fo{dd}{MON}{yyyy}bhav.csv.zip
  - UDiFF format (Jul 8, 2024 — present): BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip

Features:
  - Auto-detects format based on date (switchover: July 8, 2024)
  - Filters for NIFTY + BANKNIFTY only (discards ~80% of rows)
  - Maps both formats to unified column schema
  - Session warmup + cookie management
  - User-Agent rotation pool
  - Consecutive error detection → auto session refresh
  - Progressive backoff on refreshes
  - HTML block detection (NSE returns 200 + HTML instead of ZIP)
  - Skips already-downloaded dates
  - Resumes from where it left off

Usage:
    python -m scrapers.nse_fo_downloader
    python -m scrapers.nse_fo_downloader --start 2024-01-01 --end 2024-12-31
    python -m scrapers.nse_fo_downloader --max-errors 3
    python -m scrapers.nse_fo_downloader --download-only   # skip DB loading
"""

import io
import sys
import time
import random
import zipfile
import argparse
from datetime import date, timedelta, datetime
from pathlib import Path

import duckdb
import pandas as pd
from tqdm import tqdm
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import (
    DB_PATH, HIST_START, HIST_END, BHAV_DIR,
    NSE_FO_BHAV_URL_OLD, NSE_FO_BHAV_URL_UDIFF,
    UDIFF_START_DATE, NSE_FO_SYMBOLS,
    OLD_TO_UNIFIED, UDIFF_TO_UNIFIED,
    NSE_MAX_ERRORS, NSE_SESSION_REFRESH_WAIT, MAX_SESSION_REFRESHES,
)
from utils.nse_session import create_nse_session, get_fresh_headers, is_html_response, polite_sleep
from utils.date_utils import format_nse_old, format_nse_udiff

Path("logs").mkdir(exist_ok=True)
logger.add("logs/nse_fo_downloader.log", rotation="10 MB")


# ═══════════════════════════════════════════════════════════
# URL BUILDERS
# ═══════════════════════════════════════════════════════════

def build_url(d: date) -> tuple[str, str]:
    """
    Build the download URL for a given date.
    Returns (url, format_type) where format_type is 'old' or 'udiff'.
    """
    if d < UDIFF_START_DATE:
        month_upper = d.strftime("%b").upper()
        url = NSE_FO_BHAV_URL_OLD.format(
            year=d.strftime("%Y"),
            month_upper=month_upper,
            day=d.strftime("%d"),
        )
        return url, "old"
    else:
        url = NSE_FO_BHAV_URL_UDIFF.format(
            date_yyyymmdd=format_nse_udiff(d),
        )
        return url, "udiff"


def get_csv_filename(d: date, fmt: str) -> str:
    """Get the expected CSV filename for a date."""
    if fmt == "old":
        return f"fo{format_nse_old(d)}bhav.csv"
    else:
        return f"BhavCopy_NSE_FO_{format_nse_udiff(d)}.csv"


# ═══════════════════════════════════════════════════════════
# SINGLE DAY DOWNLOAD
# ═══════════════════════════════════════════════════════════

def download_one_day(d: date, session, headers: dict) -> tuple[Path | None, str]:
    """
    Download a single day's bhav copy.
    Returns (csv_path, status) where status is:
        'ok', 'exists', 'holiday_404', 'blocked', 'error'
    """
    url, fmt = build_url(d)
    csv_filename = get_csv_filename(d, fmt)
    csv_path = BHAV_DIR / csv_filename

    # Skip if already downloaded
    if csv_path.exists() and csv_path.stat().st_size > 100:
        return csv_path, "exists"

    try:
        # Refresh cookies by hitting NSE homepage
        session.get("https://www.nseindia.com/", headers=headers, timeout=10)
        time.sleep(0.3 + random.uniform(0.1, 0.4))

        # Download the zip
        resp = session.get(url, headers=headers, timeout=30)

        # Handle HTTP errors
        if resp.status_code == 404:
            logger.debug(f"No data for {d} (holiday?) — 404")
            return None, "holiday_404"
        elif resp.status_code in (403, 401):
            logger.warning(f"Blocked by NSE for {d} — {resp.status_code}")
            return None, "blocked"
        elif resp.status_code != 200:
            logger.warning(f"HTTP {resp.status_code} for {d}")
            return None, "error"

        # Check for HTML block (NSE returns 200 + HTML instead of ZIP)
        if is_html_response(resp):
            logger.warning(f"HTML response for {d} (blocked by NSE)")
            return None, "blocked"

        # Extract CSV from zip
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                names = zf.namelist()
                if not names:
                    logger.warning(f"Empty zip for {d}")
                    return None, "error"
                with zf.open(names[0]) as f:
                    csv_path.write_bytes(f.read())
        except zipfile.BadZipFile:
            logger.warning(f"Bad zip for {d} — likely HTML block")
            return None, "blocked"

        logger.info(f"Downloaded {d} ({fmt} format, {csv_path.stat().st_size / 1024:.0f} KB)")
        return csv_path, "ok"

    except Exception as e:
        logger.error(f"Error downloading {d}: {e}")
        return None, "error"


# ═══════════════════════════════════════════════════════════
# PARSE + FILTER CSV
# ═══════════════════════════════════════════════════════════

def parse_bhav_csv(csv_path: Path, trade_date: date) -> pd.DataFrame | None:
    """
    Parse a bhav copy CSV, auto-detect format (old vs UDiFF),
    filter for NIFTY + BANKNIFTY, and map to unified schema.
    """
    try:
        df = pd.read_csv(csv_path)
        df.columns = df.columns.str.strip()

        # Auto-detect format by checking column names
        if "TckrSymb" in df.columns:
            fmt = "udiff"
            symbol_col = "TckrSymb"
            mapping = UDIFF_TO_UNIFIED
        elif "SYMBOL" in df.columns:
            fmt = "old"
            symbol_col = "SYMBOL"
            mapping = OLD_TO_UNIFIED
        else:
            logger.error(f"Unknown format for {csv_path}: columns = {list(df.columns[:5])}")
            return None

        # Filter for our symbols only
        df[symbol_col] = df[symbol_col].str.strip()
        df = df[df[symbol_col].isin(NSE_FO_SYMBOLS)]

        if df.empty:
            logger.debug(f"No NIFTY/BANKNIFTY data in {csv_path.name}")
            return None

        # Rename columns to unified schema
        rename_map = {k: v for k, v in mapping.items() if k in df.columns}
        df = df.rename(columns=rename_map)

        # Add trade_date
        df["trade_date"] = trade_date

        # Clean string columns
        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].str.strip()
        if "option_type" in df.columns:
            df["option_type"] = df["option_type"].str.strip()
        if "instrument" in df.columns:
            df["instrument"] = df["instrument"].str.strip()

        # Parse expiry date
        if "expiry_date" in df.columns:
            if fmt == "old":
                # Old format: 26-Jan-2023
                df["expiry_date"] = pd.to_datetime(
                    df["expiry_date"].str.strip(), format="%d-%b-%Y"
                ).dt.date
            else:
                # UDiFF format: 2023-01-26
                df["expiry_date"] = pd.to_datetime(
                    df["expiry_date"].str.strip()
                ).dt.date

        # Add source format
        df["source_format"] = fmt

        # Ensure all unified columns exist (fill missing with None)
        unified_cols = [
            "trade_date", "instrument", "symbol", "expiry_date",
            "strike_price", "option_type", "open_price", "high_price",
            "low_price", "close_price", "settle_price", "last_price",
            "prev_close", "underlying_price", "contracts", "value_lakhs",
            "open_interest", "change_in_oi", "num_trades", "lot_size",
            "source_format",
        ]
        for col in unified_cols:
            if col not in df.columns:
                df[col] = None

        # Select and order columns
        df = df[unified_cols]

        # Convert numeric columns
        # Fill NULLs for futures (no strike price, no option type)
        df["strike_price"] = df["strike_price"].fillna(0)
        df["option_type"] = df["option_type"].fillna("XX")
        numeric_cols = [
            "strike_price", "open_price", "high_price", "low_price",
            "close_price", "settle_price", "last_price", "prev_close",
            "underlying_price", "contracts", "value_lakhs",
            "open_interest", "change_in_oi", "num_trades", "lot_size",
        ]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    except Exception as e:
        logger.error(f"Error parsing {csv_path}: {e}")
        return None


# ═══════════════════════════════════════════════════════════
# BULK DOWNLOAD
# ═══════════════════════════════════════════════════════════

def download_all(start: date, end: date, max_errors: int = NSE_MAX_ERRORS):
    """Download bhav copies for all trading days in range."""

    # Get trading days from DB
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
        logger.error("No trading days found in database. Run setup.py first.")
        return []

    logger.info(f"Downloading bhav copies: {start} to {end} ({len(trading_days)} trading days)")
    logger.info(f"Old format: {sum(1 for d in trading_days if d < UDIFF_START_DATE)} days")
    logger.info(f"UDiFF format: {sum(1 for d in trading_days if d >= UDIFF_START_DATE)} days")
    logger.info(f"Session refresh threshold: {max_errors} consecutive errors")

    session = create_nse_session()
    headers = get_fresh_headers()

    stats = {
        "downloaded": 0, "skipped": 0, "holidays": 0,
        "blocked": 0, "errors": 0, "session_refreshes": 0,
    }
    consecutive_errors = 0
    session_refreshes = 0
    downloaded_paths = []

    i = 0
    pbar = tqdm(total=len(trading_days), desc="Downloading bhav copies")

    while i < len(trading_days):
        d = trading_days[i]
        csv_path, status = download_one_day(d, session, headers)

        if status == "ok":
            stats["downloaded"] += 1
            consecutive_errors = 0
            if csv_path:
                downloaded_paths.append((d, csv_path))

        elif status == "exists":
            stats["skipped"] += 1
            consecutive_errors = 0
            if csv_path:
                downloaded_paths.append((d, csv_path))

        elif status == "holiday_404":
            stats["holidays"] += 1
            consecutive_errors += 1

        elif status in ("blocked", "error"):
            if status == "blocked":
                stats["blocked"] += 1
            else:
                stats["errors"] += 1
            consecutive_errors += 1

        # Session refresh on consecutive errors
        if consecutive_errors >= max_errors:
            if session_refreshes < MAX_SESSION_REFRESHES:
                session_refreshes += 1
                stats["session_refreshes"] += 1
                wait_time = NSE_SESSION_REFRESH_WAIT * session_refreshes

                logger.warning(
                    f"Session refresh #{session_refreshes}/{MAX_SESSION_REFRESHES}: "
                    f"{consecutive_errors} consecutive errors. Waiting {wait_time}s..."
                )

                session.close()
                time.sleep(wait_time)

                headers = get_fresh_headers()
                session = create_nse_session()
                consecutive_errors = 0

                # Rewind to retry failed dates
                rewind = min(max_errors, i)
                i -= rewind
                stats["blocked"] = max(0, stats["blocked"] - rewind)
                stats["holidays"] = max(0, stats["holidays"] - rewind)

                logger.info(f"Retrying from {trading_days[i]} with fresh session")
                time.sleep(2)
                continue
            else:
                logger.error(
                    f"Exhausted all {MAX_SESSION_REFRESHES} session refreshes. "
                    f"NSE may be rate-limiting your IP. Try again later."
                )
                consecutive_errors = 0

        i += 1
        pbar.update(1)
        polite_sleep(base=1.0, jitter=1.2)

    pbar.close()
    session.close()

    logger.info(
        f"\nDownload Summary:\n"
        f"  Downloaded:  {stats['downloaded']}\n"
        f"  Already had: {stats['skipped']}\n"
        f"  Holidays:    {stats['holidays']}\n"
        f"  Blocked:     {stats['blocked']}\n"
        f"  Errors:      {stats['errors']}\n"
        f"  Refreshes:   {stats['session_refreshes']}"
    )

    return downloaded_paths


# ═══════════════════════════════════════════════════════════
# LOAD INTO DATABASE
# ═══════════════════════════════════════════════════════════

def load_to_db(start: date = None, end: date = None):
    """Parse all downloaded CSVs and load into raw_fo_bhav."""

    csv_files = sorted(BHAV_DIR.glob("*.csv"))
    if not csv_files:
        logger.error("No CSV files found. Run download first.")
        return

    logger.info(f"Parsing {len(csv_files)} bhav copy files...")

    all_dfs = []
    parsed = 0
    failed = 0
    total_rows = 0

    for csv_path in tqdm(csv_files, desc="Parsing bhav copies"):
        # Extract date from filename
        fname = csv_path.stem

        try:
            if fname.startswith("fo") and "bhav" in fname:
                # Old format: fo03JAN2022bhav
                date_str = fname[2:-4]  # 03JAN2022
                trade_date = datetime.strptime(date_str, "%d%b%Y").date()
            elif fname.startswith("BhavCopy_NSE_FO_"):
                # UDiFF format: BhavCopy_NSE_FO_20240925
                date_str = fname.replace("BhavCopy_NSE_FO_", "")
                trade_date = datetime.strptime(date_str, "%Y%m%d").date()
            else:
                logger.debug(f"Skipping unrecognized file: {fname}")
                continue
        except ValueError:
            logger.warning(f"Cannot parse date from {fname}")
            failed += 1
            continue

        # Filter by date range if specified
        if start and trade_date < start:
            continue
        if end and trade_date > end:
            continue

        df = parse_bhav_csv(csv_path, trade_date)
        if df is not None and not df.empty:
            all_dfs.append(df)
            total_rows += len(df)
            parsed += 1

    if not all_dfs:
        logger.error("No data parsed from any file!")
        return

    logger.info(f"Parsed {parsed} files, {failed} failed, {total_rows:,} total rows")

    combined = pd.concat(all_dfs, ignore_index=True)

    # Load into DuckDB
    con = duckdb.connect(str(DB_PATH))

    # Delete existing data for the date range we're loading
    min_date = combined["trade_date"].min()
    max_date = combined["trade_date"].max()
    con.execute(
        "DELETE FROM raw_fo_bhav WHERE trade_date BETWEEN ? AND ?",
        [min_date, max_date],
    )

    con.execute("INSERT INTO raw_fo_bhav SELECT * FROM combined")

    # Verify
    count = con.execute("SELECT COUNT(*) FROM raw_fo_bhav").fetchone()[0]
    logger.info(f"Loaded {count:,} total rows into raw_fo_bhav")

    # Stats
    stats = con.execute("""
        SELECT
            symbol,
            COUNT(DISTINCT trade_date) as trading_days,
            COUNT(*) as total_rows,
            SUM(CASE WHEN option_type IN ('CE', 'PE') THEN 1 ELSE 0 END) as option_rows,
            SUM(CASE WHEN option_type = 'XX' OR option_type IS NULL THEN 1 ELSE 0 END) as futures_rows,
            MIN(trade_date) as first_date,
            MAX(trade_date) as last_date
        FROM raw_fo_bhav
        GROUP BY symbol
        ORDER BY symbol
    """).fetchdf()
    print("\n  Data Summary:")
    print(stats.to_string(index=False))

    # Format split
    fmt_stats = con.execute("""
        SELECT source_format, COUNT(DISTINCT trade_date) as days, COUNT(*) as rows
        FROM raw_fo_bhav GROUP BY source_format
    """).fetchdf()
    print("\n  Format split:")
    print(fmt_stats.to_string(index=False))

    con.close()


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def run_backfill(start: date = None, end: date = None, max_errors: int = NSE_MAX_ERRORS):
    """Full Sprint 2: download + parse + load."""
    start = start or HIST_START
    end = end or HIST_END

    print("=" * 60)
    print("  NEVAREP Sprint 2 — NSE F&O Bhav Copy Downloader")
    print("=" * 60)
    print(f"  Range: {start} to {end}")
    print(f"  Symbols: {', '.join(NSE_FO_SYMBOLS)}")
    print(f"  Format switchover: {UDIFF_START_DATE}")
    print(f"  Download dir: {BHAV_DIR}")
    print(f"  Max consecutive errors: {max_errors}")
    print()

    # Download
    print("[1/2] Downloading from NSE archives...")
    print("  This will take 4-8 hours due to NSE rate limiting.")
    print("  Safe to interrupt (Ctrl+C) — resumes from where it left off.\n")
    download_all(start, end, max_errors)

    # Parse and load
    print("\n[2/2] Parsing and loading into DuckDB...")
    load_to_db(start, end)

    # Final summary
    con = duckdb.connect(str(DB_PATH))
    count = con.execute("SELECT COUNT(*) FROM raw_fo_bhav").fetchone()[0]
    days = con.execute("SELECT COUNT(DISTINCT trade_date) FROM raw_fo_bhav").fetchone()[0]
    con.close()

    print(f"\n{'=' * 60}")
    print(f"  Sprint 2 complete!")
    print(f"{'=' * 60}")
    print(f"  raw_fo_bhav: {count:,} rows across {days} trading days")
    print(f"  DB size: {DB_PATH.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"\n  Next step: python -m scrapers.participant_oi")
    print(f"  (Sprint 3: NSE participant-wise OI)")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="NEVAREP Sprint 2 — NSE F&O Bhav Copy")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD")
    parser.add_argument("--max-errors", type=int, default=NSE_MAX_ERRORS,
                        help=f"Consecutive errors before session refresh (default: {NSE_MAX_ERRORS})")
    parser.add_argument("--download-only", action="store_true",
                        help="Only download, don't parse into DB")
    parser.add_argument("--parse-only", action="store_true",
                        help="Only parse existing CSVs into DB")

    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else HIST_START
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else HIST_END

    print("=" * 60)
    print("  NEVAREP Sprint 2 — NSE F&O Bhav Copy Downloader")
    print("=" * 60)
    print(f"  Range: {start} to {end}")
    print(f"  Symbols: {', '.join(NSE_FO_SYMBOLS)}")
    print(f"  Format switchover: {UDIFF_START_DATE}")
    print()

    if args.parse_only:
        print("Parse-only mode: loading existing CSVs into DB...")
        load_to_db(start, end)
    elif args.download_only:
        print("Download-only mode: downloading without DB loading...")
        download_all(start, end, args.max_errors)
    else:
        run_backfill(start, end, args.max_errors)


if __name__ == "__main__":
    main()