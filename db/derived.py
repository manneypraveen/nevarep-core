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
    LOT_SIZES, RISK_FREE_RATE,
)
from analysis.greeks import implied_vol, greeks, gamma_exposure

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
# GREEKS + IV ENGINE
# ═══════════════════════════════════════════════════════════

def compute_greeks_for_day(
    opts_df: pd.DataFrame,
    trade_date: date,
    index_name: str,
    spot: float,
    r: float = RISK_FREE_RATE,
) -> tuple[list[dict], dict]:
    """
    Compute per-strike IV + greeks for one day × one index.
    CRUDEOIL: skipped — no MCX options feed.
    # TODO: MCX crude options feed for CRUDEOIL greeks

    Returns:
      greek_rows  – list of dicts ready for daily_greeks table
      summary     – dict with atm_iv, iv_skew, iv_percentile (None here,
                    caller computes iv_percentile over trailing window), gex
    """
    if spot is None or spot <= 0:
        return [], {}

    opts = opts_df[opts_df["option_type"].isin(["CE", "PE"])].copy()
    if opts.empty:
        return [], {}

    # Use nearest expiry
    future_expiries = opts[opts["expiry_date"] >= trade_date]["expiry_date"].unique()
    if len(future_expiries) == 0:
        return [], {}
    nearest_expiry = min(future_expiries)
    near_opts = opts[opts["expiry_date"] == nearest_expiry].copy()

    lot_size = LOT_SIZES.get(
        "NIFTY" if "NIFTY" in index_name else index_name, 75
    )

    # T in years
    from datetime import date as _date
    days_to_expiry = (nearest_expiry - trade_date).days
    T = max(days_to_expiry / 365.0, 1.0 / 365.0)

    greek_rows = []
    gex_total = 0.0

    for _, row in near_opts.iterrows():
        K = row.get("strike_price")
        opt_type = row.get("option_type")
        price = row.get("close_price") or row.get("settle_price")
        oi = row.get("open_interest", 0) or 0

        if not K or not opt_type or not price or price <= 0 or K <= 0:
            continue

        iv = implied_vol(float(price), float(spot), float(K), T, r, opt_type)
        if iv is None:
            continue

        g = greeks(float(spot), float(K), T, r, iv, opt_type)
        gex_contrib = gamma_exposure(
            float(spot), float(K), T, r, iv, float(oi), lot_size, opt_type
        )
        gex_total += gex_contrib

        greek_rows.append({
            "trade_date": trade_date,
            "index_name": index_name,
            "expiry_date": nearest_expiry,
            "strike_price": float(K),
            "opt_type": opt_type,
            "iv": iv,
            "delta": g["delta"],
            "gamma": g["gamma"],
            "theta": g["theta"],
            "vega": g["vega"],
            "open_interest": int(oi),
            "gex": round(gex_contrib, 2),
        })

    if not greek_rows:
        return [], {}

    # ATM IV — weighted average of nearest-ATM call and put IV
    df_g = pd.DataFrame(greek_rows)
    atm_strike = min(df_g["strike_price"].unique(), key=lambda k: abs(k - spot))
    atm_rows = df_g[df_g["strike_price"] == atm_strike]
    atm_ce = atm_rows[atm_rows["opt_type"] == "CE"]["iv"].values
    atm_pe = atm_rows[atm_rows["opt_type"] == "PE"]["iv"].values
    atm_iv_vals = [v for v in [
        atm_ce[0] if len(atm_ce) else None,
        atm_pe[0] if len(atm_pe) else None,
    ] if v is not None]
    atm_iv = round(float(np.mean(atm_iv_vals)), 6) if atm_iv_vals else None

    # IV skew: 25-delta put IV − 25-delta call IV
    # Approximate 25-delta strikes as strikes where |delta| ≈ 0.25
    def _nearest_delta_iv(df_sub, target_delta):
        """Find IV of the strike whose |delta| is closest to target."""
        if df_sub.empty:
            return None
        df_sub = df_sub.copy()
        df_sub["_d"] = (df_sub["delta"].abs() - target_delta).abs()
        row = df_sub.loc[df_sub["_d"].idxmin()]
        return float(row["iv"])

    iv_25put = _nearest_delta_iv(df_g[df_g["opt_type"] == "PE"], 0.25)
    iv_25call = _nearest_delta_iv(df_g[df_g["opt_type"] == "CE"], 0.25)
    iv_skew = None
    if iv_25put is not None and iv_25call is not None:
        iv_skew = round(iv_25put - iv_25call, 6)

    summary = {
        "atm_iv": atm_iv,
        "iv_skew": iv_skew,
        "iv_percentile": None,   # computed across trailing window in compute_all
        "gex": round(gex_total, 2),
    }
    return greek_rows, summary


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
        "atm_iv": None,       # filled from greeks_summary passed by compute_all
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

    print(f"\n  Computing options snapshots + greeks (this may take several minutes)...\n")

    all_records = []
    all_greek_rows = []
    prev_pcr = {"NIFTY50": None, "BANKNIFTY": None}
    # Trailing 252-day atm_iv history for iv_percentile
    atm_iv_history: dict[str, list[float]] = {"NIFTY50": [], "BANKNIFTY": []}

    for d in tqdm(trading_days, desc="Computing options snapshots"):
        # Load this day's raw data (include settle_price for greeks fallback)
        day_df = con.execute("""
            SELECT symbol, expiry_date, strike_price, option_type,
                   close_price, settle_price, open_interest, change_in_oi, contracts
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
        day_df["close_price"] = pd.to_numeric(day_df["close_price"], errors="coerce")
        day_df["settle_price"] = pd.to_numeric(day_df["settle_price"], errors="coerce")

        for symbol, index_name in symbol_index_map.items():
            sym_df = day_df[day_df["symbol"] == symbol]
            if sym_df.empty:
                continue

            close = close_prices.get((d, index_name))
            vix = vix_data.get(d)

            # ── Greeks + IV ──
            greek_rows, greeks_summary = compute_greeks_for_day(
                sym_df, d, index_name, close,
            )
            all_greek_rows.extend(greek_rows)

            # iv_percentile: rank today's atm_iv in trailing 252-day window
            atm_iv = greeks_summary.get("atm_iv")
            iv_pct = None
            history = atm_iv_history[index_name]
            if atm_iv is not None and len(history) >= 20:
                window = history[-252:]
                iv_pct = round(sum(v <= atm_iv for v in window) / len(window) * 100, 1)
            if atm_iv is not None:
                history.append(atm_iv)

            record = compute_day_snapshot(
                sym_df, d, index_name, close, vix,
                prev_pcr=prev_pcr.get(index_name),
            )

            if record:
                # Fill in IV fields from greeks computation
                record["atm_iv"] = round(atm_iv * 100, 4) if atm_iv else None  # store as %
                record["iv_skew"] = round(greeks_summary.get("iv_skew", None) or 0 * 100, 4) \
                    if greeks_summary.get("iv_skew") is not None else None
                record["iv_percentile"] = iv_pct
                record["gex"] = greeks_summary.get("gex")

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

    # Load snapshots into DB
    min_date = df["trade_date"].min()
    max_date = df["trade_date"].max()
    con.execute(
        "DELETE FROM daily_options_snapshot WHERE trade_date BETWEEN ? AND ?",
        [min_date, max_date],
    )
    con.execute("INSERT INTO daily_options_snapshot SELECT * FROM df")

    # Load greeks into DB
    if all_greek_rows:
        gdf = pd.DataFrame(all_greek_rows)
        con.execute(
            "DELETE FROM daily_greeks WHERE trade_date BETWEEN ? AND ?",
            [min_date, max_date],
        )
        con.execute("INSERT INTO daily_greeks SELECT * FROM gdf")
        logger.info(f"Loaded {len(gdf):,} rows into daily_greeks")
    else:
        logger.warning("No greeks rows computed (no option price data?)")

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

    # Recent snapshot with IV
    recent = con.execute("""
        SELECT trade_date, index_name, pcr_oi, pcr_classification,
               max_pain_strike, price_in_oi_range, atm_iv, iv_skew, iv_percentile, gex
        FROM daily_options_snapshot
        WHERE index_name = 'NIFTY50'
        ORDER BY trade_date DESC LIMIT 5
    """).fetchdf()
    print("\n  Nifty Options (last 5 days, incl. IV):")
    print(recent.to_string(index=False))

    greek_count = len(all_greek_rows)
    iv_coverage = len([r for r in all_records if r.get("atm_iv") is not None])
    con.close()

    print(f"\n{'=' * 60}")
    print(f"  Options snapshot + greeks computation complete!")
    print(f"{'=' * 60}")
    print(f"  daily_options_snapshot: {count:,} rows")
    print(f"  daily_greeks:           {greek_count:,} rows")
    print(f"  ATM IV populated:       {iv_coverage}/{len(all_records)} days ({iv_coverage*100//max(len(all_records),1)}%)")
    print(f"  OI range accuracy:      {summary['pct_in_range'].mean():.0f}% of days price in OI range")
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