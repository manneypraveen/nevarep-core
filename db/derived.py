from __future__ import annotations

"""
NEVAREP — Derived Fields Computer (db/derived.py)

Computes daily_options_snapshot from raw_fo_bhav data.
For each trading day × index, calculates:
  - PCR (put-call ratio from OI)
  - Max pain strike
  - Highest call/put OI strikes (resistance/support)
  - OI-implied range
  - Whether price stayed in OI range
  - India VIX regime
  - Net OI interpretation (long buildup / short covering etc.)

This is the analytical core — turning 6.4M raw rows into
~3,500 daily intelligence records.

Usage:
    python -m db.derived
    python -m db.derived --start 2024-01-01 --end 2024-12-31
"""

import sys
import argparse
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd
import numpy as np
from tqdm import tqdm
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import (
    DB_PATH, HIST_START, HIST_END,
    PCR_STRONGLY_BEARISH, PCR_BEARISH, PCR_BULLISH, PCR_STRONGLY_BULLISH,
    VIX_LOW, VIX_NORMAL, VIX_ELEVATED, VIX_HIGH,
)

Path("logs").mkdir(exist_ok=True)
logger.add("logs/derived.log", rotation="10 MB")


# ═══════════════════════════════════════════════════════════
# PCR CLASSIFICATION
# ═══════════════════════════════════════════════════════════

def classify_pcr(pcr: float) -> str:
    if pcr < PCR_STRONGLY_BEARISH:
        return "strongly_bearish"
    elif pcr < PCR_BEARISH:
        return "bearish"
    elif pcr < PCR_BULLISH:
        return "neutral"
    elif pcr < PCR_STRONGLY_BULLISH:
        return "bullish"
    return "strongly_bullish"


def classify_vix(vix: float) -> str:
    if vix is None or np.isnan(vix):
        return None
    if vix < VIX_LOW:
        return "low"
    elif vix < VIX_NORMAL:
        return "normal"
    elif vix < VIX_ELEVATED:
        return "elevated"
    elif vix < VIX_HIGH:
        return "high"
    return "extreme"


# ═══════════════════════════════════════════════════════════
# MAX PAIN CALCULATOR
# ═══════════════════════════════════════════════════════════

def compute_max_pain(strikes_df: pd.DataFrame) -> float:
    """
    Max pain = strike where total option seller loss is minimized.
    For each candidate strike S:
      - CE seller loss at S = sum of max(0, S - strike) * call_oi for all strikes
      - PE seller loss at S = sum of max(0, strike - S) * put_oi for all strikes
      - Total loss = CE loss + PE loss
    Max pain = S with minimum total loss.
    """
    if strikes_df.empty:
        return None

    # Get unique strikes with their call and put OI
    calls = strikes_df[strikes_df["option_type"] == "CE"][["strike_price", "open_interest"]].copy()
    calls.columns = ["strike", "call_oi"]
    calls = calls.groupby("strike")["call_oi"].sum().reset_index()

    puts = strikes_df[strikes_df["option_type"] == "PE"][["strike_price", "open_interest"]].copy()
    puts.columns = ["strike", "put_oi"]
    puts = puts.groupby("strike")["put_oi"].sum().reset_index()

    if calls.empty or puts.empty:
        return None

    all_strikes = sorted(set(calls["strike"].tolist() + puts["strike"].tolist()))

    if not all_strikes:
        return None

    call_oi_map = dict(zip(calls["strike"], calls["call_oi"]))
    put_oi_map = dict(zip(puts["strike"], puts["put_oi"]))

    min_pain = float("inf")
    max_pain_strike = all_strikes[len(all_strikes) // 2]  # default to middle

    for candidate in all_strikes:
        total_pain = 0

        # CE sellers lose when price > strike
        for s, oi in call_oi_map.items():
            if candidate > s:
                total_pain += (candidate - s) * oi

        # PE sellers lose when price < strike
        for s, oi in put_oi_map.items():
            if candidate < s:
                total_pain += (s - candidate) * oi

        if total_pain < min_pain:
            min_pain = total_pain
            max_pain_strike = candidate

    return max_pain_strike


# ═══════════════════════════════════════════════════════════
# COMPUTE FOR ONE DAY × ONE INDEX
# ═══════════════════════════════════════════════════════════

def compute_day_snapshot(
    day_df: pd.DataFrame,
    trade_date: date,
    index_name: str,
    close_price: float,
    vix_close: float = None,
    prev_pcr: float = None,
) -> dict:
    """
    Compute all options snapshot metrics for one day, one index.
    day_df: raw_fo_bhav rows for this day + symbol, filtered to nearest expiry options.
    """
    # Filter to options only (CE and PE)
    opts = day_df[day_df["option_type"].isin(["CE", "PE"])].copy()

    if opts.empty:
        return None

    # Find the nearest weekly expiry (smallest expiry_date >= trade_date)
    future_expiries = opts[opts["expiry_date"] >= trade_date]["expiry_date"].unique()
    if len(future_expiries) == 0:
        # All expiries passed — use the latest one
        nearest_expiry = opts["expiry_date"].max()
    else:
        nearest_expiry = min(future_expiries)

    # Filter to nearest expiry for PCR/OI analysis
    near_opts = opts[opts["expiry_date"] == nearest_expiry].copy()

    if near_opts.empty:
        return None

    calls = near_opts[near_opts["option_type"] == "CE"]
    puts = near_opts[near_opts["option_type"] == "PE"]

    # ── PCR ──
    total_call_oi = int(calls["open_interest"].sum())
    total_put_oi = int(puts["open_interest"].sum())

    pcr = round(total_put_oi / max(total_call_oi, 1), 4)
    pcr_class = classify_pcr(pcr)
    pcr_change = round(pcr - prev_pcr, 4) if prev_pcr is not None else None

    # ── Highest OI strikes ──
    if not calls.empty:
        call_oi_by_strike = calls.groupby("strike_price")["open_interest"].sum()
        highest_call_oi_strike = call_oi_by_strike.idxmax()
    else:
        highest_call_oi_strike = None

    if not puts.empty:
        put_oi_by_strike = puts.groupby("strike_price")["open_interest"].sum()
        highest_put_oi_strike = put_oi_by_strike.idxmax()
    else:
        highest_put_oi_strike = None

    # ── Max pain ──
    max_pain = compute_max_pain(near_opts)

    # ── OI range ──
    oi_range_low = highest_put_oi_strike
    oi_range_high = highest_call_oi_strike

    price_in_range = None
    breach_side = None
    if oi_range_low is not None and oi_range_high is not None and close_price:
        if oi_range_low <= close_price <= oi_range_high:
            price_in_range = True
            breach_side = "none"
        elif close_price > oi_range_high:
            price_in_range = False
            breach_side = "call_side"
        else:
            price_in_range = False
            breach_side = "put_side"

    # ── Net OI interpretation ──
    # Based on price direction + OI change
    # (simplified — full version uses previous day comparison)
    total_oi_change = int(near_opts["change_in_oi"].sum()) if "change_in_oi" in near_opts.columns else 0

    if close_price and total_oi_change > 0:
        # OI increased
        net_interpretation = "long_buildup"  # price up + OI up (simplified)
    elif close_price and total_oi_change < 0:
        net_interpretation = "long_unwinding"
    else:
        net_interpretation = "neutral"

    # ── VIX ──
    vix_regime = classify_vix(vix_close) if vix_close else None

    # ── Mismatch scoring (populated later by narrative generator) ──

    return {
        "trade_date": trade_date,
        "index_name": index_name,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "pcr_oi": pcr,
        "pcr_classification": pcr_class,
        "pcr_change": pcr_change,
        "max_pain_strike": max_pain,
        "highest_call_oi_strike": highest_call_oi_strike,
        "highest_put_oi_strike": highest_put_oi_strike,
        "oi_range_low": oi_range_low,
        "oi_range_high": oi_range_high,
        "price_in_oi_range": price_in_range,
        "oi_breach_side": breach_side,
        "net_oi_interpretation": net_interpretation,
        "india_vix_close": round(vix_close, 2) if vix_close else None,
        "india_vix_change_pct": None,  # computed in batch below
        "vix_regime": vix_regime,
        "atm_iv": None,  # TODO: compute from option prices + Black-Scholes
        "iv_percentile": None,
        "iv_skew": None,
        "fii_index_futures_net": None,  # filled from participant OI
        "fii_long_short_ratio": None,
        "pcr_signal_correct": None,  # filled post-day by narrative
        "oi_support_held": None,
        "max_pain_accurate": None,
        "indicators_correct_count": None,
        "mismatch_severity": None,
    }


# ═══════════════════════════════════════════════════════════
# BATCH COMPUTE
# ═══════════════════════════════════════════════════════════

def compute_all(start: date = None, end: date = None):
    """Compute daily_options_snapshot for all trading days."""
    start = start or HIST_START
    end = end or HIST_END

    print("=" * 60)
    print("  NEVAREP — Options Snapshot Computer")
    print("=" * 60)
    print(f"  Range: {start} to {end}")
    print(f"  Source: raw_fo_bhav ({6_461_233:,} rows)")
    print()

    con = duckdb.connect(str(DB_PATH))

    # Get trading days
    trading_days = [
        row[0] for row in con.execute("""
            SELECT DISTINCT trade_date FROM raw_fo_bhav
            WHERE trade_date BETWEEN ? AND ?
            ORDER BY trade_date
        """, [start, end]).fetchall()
    ]

    print(f"  Trading days with F&O data: {len(trading_days)}")

    # Get close prices for range check
    close_prices = {}
    for row in con.execute("""
        SELECT trade_date, index_name, close_price
        FROM daily_index_ohlc
        WHERE index_name IN ('NIFTY50', 'BANKNIFTY')
    """).fetchall():
        close_prices[(row[0], row[1])] = row[2]

    # Get India VIX data
    vix_data = {}
    for row in con.execute("""
        SELECT trade_date, close_price FROM daily_index_ohlc
        WHERE index_name = 'NIFTY50'
    """).fetchall():
        pass  # placeholder

    # Try to get VIX from daily_index_ohlc or raw Yahoo data
    vix_rows = con.execute("""
        SELECT trade_date, close_price FROM daily_index_ohlc
        WHERE index_name = 'INDIA_VIX'
    """).fetchall()

    # If no INDIA_VIX in daily_index_ohlc, check if we downloaded it separately
    if not vix_rows:
        # VIX might not be in daily_index_ohlc — compute from Yahoo data stored elsewhere
        # For now, leave VIX as None — it will be enriched later
        logger.info("India VIX not in daily_index_ohlc — VIX fields will be NULL")

    for row in vix_rows:
        vix_data[row[0]] = row[1]

    # Get FII data from participant OI
    fii_data = {}
    for row in con.execute("""
        SELECT trade_date, 
               (fut_idx_long - fut_idx_short) as net_futures,
               ROUND(fut_idx_long::FLOAT / NULLIF(fut_idx_short, 0), 4) as ls_ratio
        FROM daily_participant_oi
        WHERE client_type = 'FII'
    """).fetchall():
        fii_data[row[0]] = (row[1], row[2])

    # Symbol to index mapping
    symbol_index_map = {
        "NIFTY": "NIFTY50",
        "BANKNIFTY": "BANKNIFTY",
    }

    print(f"\n  Computing options snapshots...")
    print(f"  This processes 6.4M rows — takes 20-60 seconds.\n")

    all_records = []
    prev_pcr = {"NIFTY50": None, "BANKNIFTY": None}

    for d in tqdm(trading_days, desc="Computing options snapshots"):
        # Load this day's raw data
        day_df = con.execute("""
            SELECT symbol, expiry_date, strike_price, option_type,
                   close_price, open_interest, change_in_oi, contracts
            FROM raw_fo_bhav
            WHERE trade_date = ?
        """, [d]).fetchdf()

        if day_df.empty:
            continue

        # Ensure proper types
        day_df["expiry_date"] = pd.to_datetime(day_df["expiry_date"]).dt.date
        day_df["strike_price"] = pd.to_numeric(day_df["strike_price"], errors="coerce")
        day_df["open_interest"] = pd.to_numeric(day_df["open_interest"], errors="coerce").fillna(0)
        day_df["change_in_oi"] = pd.to_numeric(day_df["change_in_oi"], errors="coerce").fillna(0)

        for symbol, index_name in symbol_index_map.items():
            sym_df = day_df[day_df["symbol"] == symbol]
            if sym_df.empty:
                continue

            close = close_prices.get((d, index_name))
            vix = vix_data.get(d)

            record = compute_day_snapshot(
                sym_df, d, index_name, close, vix,
                prev_pcr=prev_pcr.get(index_name),
            )

            if record:
                # Add FII data
                fii = fii_data.get(d)
                if fii:
                    record["fii_index_futures_net"] = fii[0]
                    record["fii_long_short_ratio"] = fii[1]

                all_records.append(record)
                prev_pcr[index_name] = record["pcr_oi"]

    if not all_records:
        print("ERROR: No records computed!")
        con.close()
        return

    df = pd.DataFrame(all_records)

    # Compute VIX change %
    for idx in ["NIFTY50", "BANKNIFTY"]:
        mask = df["index_name"] == idx
        df.loc[mask, "india_vix_change_pct"] = df.loc[mask, "india_vix_close"].pct_change() * 100

    # Load into DB
    min_date = df["trade_date"].min()
    max_date = df["trade_date"].max()
    con.execute(
        "DELETE FROM daily_options_snapshot WHERE trade_date BETWEEN ? AND ?",
        [min_date, max_date],
    )
    con.execute("INSERT INTO daily_options_snapshot SELECT * FROM df")

    count = con.execute("SELECT COUNT(*) FROM daily_options_snapshot").fetchone()[0]
    logger.info(f"Loaded {count:,} rows into daily_options_snapshot")

    # ── Summary stats ──
    print(f"\n  Loaded {count:,} rows into daily_options_snapshot")

    summary = con.execute("""
        SELECT index_name,
               COUNT(*) as days,
               ROUND(AVG(pcr_oi), 3) as avg_pcr,
               ROUND(MIN(pcr_oi), 3) as min_pcr,
               ROUND(MAX(pcr_oi), 3) as max_pcr,
               SUM(CASE WHEN price_in_oi_range THEN 1 ELSE 0 END) as days_in_range,
               ROUND(SUM(CASE WHEN price_in_oi_range THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) as pct_in_range
        FROM daily_options_snapshot
        GROUP BY index_name ORDER BY index_name
    """).fetchdf()
    print("\n  Options Snapshot Summary:")
    print(summary.to_string(index=False))

    # PCR distribution
    pcr_dist = con.execute("""
        SELECT pcr_classification, COUNT(*) as count,
               ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 1) as pct
        FROM daily_options_snapshot
        GROUP BY pcr_classification ORDER BY count DESC
    """).fetchdf()
    print("\n  PCR Distribution:")
    print(pcr_dist.to_string(index=False))

    # Recent snapshot
    recent = con.execute("""
        SELECT trade_date, index_name, pcr_oi, pcr_classification,
               max_pain_strike, highest_put_oi_strike, highest_call_oi_strike,
               price_in_oi_range, fii_long_short_ratio
        FROM daily_options_snapshot
        WHERE index_name = 'NIFTY50'
        ORDER BY trade_date DESC LIMIT 5
    """).fetchdf()
    print("\n  Nifty Options (last 5 days):")
    print(recent.to_string(index=False))

    con.close()

    print(f"\n{'=' * 60}")
    print(f"  Options snapshot computation complete!")
    print(f"{'=' * 60}")
    print(f"  daily_options_snapshot: {count:,} rows")
    print(f"  Key insight: {summary['pct_in_range'].mean():.0f}% of days, price stayed in OI range")
    print(f"  DB size: {DB_PATH.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="NEVAREP — Options Snapshot Computer")
    parser.add_argument("--start", type=str)
    parser.add_argument("--end", type=str)
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None

    compute_all(start, end)


if __name__ == "__main__":
    main()