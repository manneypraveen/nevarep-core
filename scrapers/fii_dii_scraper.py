from __future__ import annotations

"""
NEVAREP Sprint 4 — FII/DII Institutional Flows

Builds daily_institutional_flows from Sprint 3's participant OI data.
Uses FII/DII derivatives positioning changes as directional proxy.

Usage:
    python -m scrapers.fii_dii_scraper
    python -m scrapers.fii_dii_scraper --start 2019-01-01 --end 2026-03-20
"""

import sys
import math
import argparse
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DB_PATH, HIST_START, HIST_END

Path("logs").mkdir(exist_ok=True)
logger.add("logs/fii_dii_scraper.log", rotation="10 MB")


def safe_float(val, default=0.0):
    if val is None:
        return default
    try:
        f = float(val)
        return default if math.isnan(f) else f
    except (ValueError, TypeError):
        return default


def run_backfill(start: date = None, end: date = None):
    start = start or HIST_START
    end = end or HIST_END

    print("=" * 60)
    print("  NEVAREP Sprint 4 — FII/DII Institutional Flows")
    print("=" * 60)
    print(f"  Range: {start} to {end}")
    print()

    con = duckdb.connect(str(DB_PATH))

    oi_count = con.execute("SELECT COUNT(*) FROM daily_participant_oi").fetchone()[0]
    if oi_count == 0:
        print("ERROR: No participant OI data. Run Sprint 3 first.")
        con.close()
        return

    print(f"  Using {oi_count} participant OI rows from Sprint 3")

    print("\n[1/3] Extracting FII + DII derivatives positioning...")

    fii_df = con.execute("""
        SELECT trade_date,
            fut_idx_long, fut_idx_short,
            (fut_idx_long - fut_idx_short) as net_idx_futures,
            (fut_stk_long - fut_stk_short) as net_stk_futures,
            opt_idx_call_long, opt_idx_put_long,
            opt_idx_call_short, opt_idx_put_short
        FROM daily_participant_oi
        WHERE client_type = 'FII'
        AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
    """, [start, end]).fetchdf()

    dii_df = con.execute("""
        SELECT trade_date,
            (fut_idx_long - fut_idx_short) as net_idx_futures
        FROM daily_participant_oi
        WHERE client_type = 'DII'
        AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
    """, [start, end]).fetchdf()

    print(f"  FII: {len(fii_df)} days, DII: {len(dii_df)} days")

    if fii_df.empty:
        print("ERROR: No FII data found!")
        con.close()
        return

    fii_df["trade_date"] = pd.to_datetime(fii_df["trade_date"]).dt.date
    dii_df["trade_date"] = pd.to_datetime(dii_df["trade_date"]).dt.date

    print("\n[2/3] Computing daily changes and flow metrics...")

    fii_df = fii_df.sort_values("trade_date").reset_index(drop=True)
    dii_df = dii_df.sort_values("trade_date").reset_index(drop=True)

    fii_df["fii_change"] = fii_df["net_idx_futures"].diff().fillna(0)
    fii_df["fii_stk_change"] = fii_df["net_stk_futures"].diff().fillna(0)
    fii_df["fii_ls_ratio"] = (fii_df["fut_idx_long"] / fii_df["fut_idx_short"].replace(0, 1)).round(4)

    dii_df["dii_change"] = dii_df["net_idx_futures"].diff().fillna(0)

    # Merge
    merged = fii_df[["trade_date", "net_idx_futures", "fii_change", "fii_stk_change",
                      "fii_ls_ratio", "fut_idx_long", "fut_idx_short"]].copy()

    dii_merge = dii_df[["trade_date", "dii_change"]].copy()
    merged = merged.merge(dii_merge, on="trade_date", how="left")
    merged["dii_change"] = merged["dii_change"].fillna(0)

    print(f"  Merged: {len(merged)} days")

    print("\n[3/3] Computing streaks and loading into DB...")

    records = []
    fii_streak = 0
    dii_streak = 0
    fii_mtd_net = 0.0
    prev_month = None

    for _, row in merged.iterrows():
        d = row["trade_date"]
        fii_chg = safe_float(row["fii_change"])
        dii_chg = safe_float(row["dii_change"])
        fii_ls = safe_float(row["fii_ls_ratio"])

        # Proxy: change in contracts → directional signal
        fii_proxy_cr = round(fii_chg * 0.5, 2)
        dii_proxy_cr = round(dii_chg * 0.3, 2)

        # Streaks
        if fii_chg > 100:
            fii_streak = max(fii_streak, 0) + 1
        elif fii_chg < -100:
            fii_streak = min(fii_streak, 0) - 1
        else:
            fii_streak = 0

        if dii_chg > 100:
            dii_streak = max(dii_streak, 0) + 1
        elif dii_chg < -100:
            dii_streak = min(dii_streak, 0) - 1
        else:
            dii_streak = 0

        # MTD
        if prev_month != d.month:
            fii_mtd_net = 0.0
            prev_month = d.month
        fii_mtd_net += fii_proxy_cr

        # Intensity
        if fii_chg > 5000:
            intensity = "heavy_buy"
        elif fii_chg > 1000:
            intensity = "mild_buy"
        elif fii_chg < -5000:
            intensity = "heavy_sell"
        elif fii_chg < -1000:
            intensity = "mild_sell"
        else:
            intensity = "neutral"

        # Dominance
        if fii_chg > 2000 and fii_chg > abs(dii_chg):
            dominant = "fii_buying_dominant"
        elif fii_chg < -2000 and abs(fii_chg) > abs(dii_chg):
            dominant = "fii_selling_dominant"
        elif dii_chg > 0 and fii_chg < 0:
            dominant = "dii_cushioning"
        else:
            dominant = "balanced"

        records.append({
            "trade_date": d,
            "fii_gross_buy_cr": round(safe_float(row["fut_idx_long"]) * 0.01, 2),
            "fii_gross_sell_cr": round(safe_float(row["fut_idx_short"]) * 0.01, 2),
            "fii_net_cr": fii_proxy_cr,
            "fii_buy_sell_ratio": round(fii_ls, 4) if fii_ls > 0 else None,
            "fii_streak_days": fii_streak,
            "fii_mtd_net_cr": round(fii_mtd_net, 2),
            "fii_intensity": intensity,
            "dii_gross_buy_cr": None,
            "dii_gross_sell_cr": None,
            "dii_net_cr": dii_proxy_cr,
            "dii_streak_days": dii_streak,
            "fii_dii_combined_net": round(fii_proxy_cr + dii_proxy_cr, 2),
            "flow_dominant": dominant,
        })

    df = pd.DataFrame(records)

    min_date = df["trade_date"].min()
    max_date = df["trade_date"].max()
    con.execute("DELETE FROM daily_institutional_flows WHERE trade_date BETWEEN ? AND ?", [min_date, max_date])
    con.execute("INSERT INTO daily_institutional_flows SELECT * FROM df")

    count = con.execute("SELECT COUNT(*) FROM daily_institutional_flows").fetchone()[0]
    logger.info(f"Loaded {count:,} rows into daily_institutional_flows")

    # Summary
    stats = con.execute("""
        SELECT COUNT(*) as days,
            MIN(trade_date) as first_date, MAX(trade_date) as last_date,
            ROUND(AVG(fii_net_cr), 2) as avg_fii_proxy,
            SUM(CASE WHEN fii_intensity='heavy_sell' THEN 1 ELSE 0 END) as heavy_sell_days,
            SUM(CASE WHEN fii_intensity='heavy_buy' THEN 1 ELSE 0 END) as heavy_buy_days,
            SUM(CASE WHEN fii_intensity='mild_sell' THEN 1 ELSE 0 END) as mild_sell_days,
            SUM(CASE WHEN fii_intensity='mild_buy' THEN 1 ELSE 0 END) as mild_buy_days,
            SUM(CASE WHEN fii_intensity='neutral' THEN 1 ELSE 0 END) as neutral_days
        FROM daily_institutional_flows
    """).fetchdf()
    print("\n  Institutional Flows Summary:")
    print(stats.to_string(index=False))

    recent = con.execute("""
        SELECT trade_date, fii_net_cr, fii_streak_days, 
               fii_buy_sell_ratio, fii_intensity, flow_dominant
        FROM daily_institutional_flows
        ORDER BY trade_date DESC LIMIT 10
    """).fetchdf()
    print("\n  Last 10 days:")
    print(recent.to_string(index=False))

    streaks = con.execute("""
        SELECT MIN(fii_streak_days) as max_sell_streak,
               MAX(fii_streak_days) as max_buy_streak
        FROM daily_institutional_flows
    """).fetchdf()
    print("\n  Streak extremes:")
    print(streaks.to_string(index=False))

    con.close()

    print(f"\n{'=' * 60}")
    print(f"  Sprint 4 complete!")
    print(f"{'=' * 60}")
    print(f"  daily_institutional_flows: {count:,} rows")
    print(f"  Source: FII/DII derivatives positioning changes")
    print(f"  DB size: {DB_PATH.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"\n  Next: python -m scrapers.news_scraper (Sprint 5)")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="NEVAREP Sprint 4")
    parser.add_argument("--start", type=str)
    parser.add_argument("--end", type=str)
    args = parser.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None
    run_backfill(start, end)


if __name__ == "__main__":
    main()