"""
NEVAREP — Token Builder (pipeline/build_tokens.py)

For each (trade_date, instrument), assembles the state dict from the
existing DB tables and calls analysis.tokenizer to produce symbolic tokens,
then writes them to daily_tokens.

Also produces news-reaction tokens by joining daily_news_events to the
next-day forward return.

Instruments: NIFTY50, BANKNIFTY, CRUDEOIL

Usage:
    python -m pipeline.build_tokens
    python -m pipeline.build_tokens --start 2024-01-01 --end 2024-03-31
    python -m pipeline.build_tokens --instrument NIFTY50
"""

from __future__ import annotations

import sys
import argparse
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd
from tqdm import tqdm
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DB_PATH, HIST_START, HIST_END, INSTRUMENTS
from analysis.tokenizer import tokenize_day, crude_tokens, news_reaction_token

Path("logs").mkdir(exist_ok=True)
logger.add("logs/build_tokens.log", rotation="10 MB")

# Symbol in raw_fo_bhav → instrument name
_SYMBOL_MAP = {"NIFTY": "NIFTY50", "BANKNIFTY": "BANKNIFTY"}


# ─── State assembly ──────────────────────────────────────────────────────────

def _build_state(con: duckdb.DuckDBPyConnection, d: date, instrument: str) -> dict:
    """
    Pull all required fields from DB for one (date, instrument) pair.
    Returns a state dict for tokenize_day / crude_tokens.
    """
    state: dict = {}

    # ── Options snapshot (NIFTY50 / BANKNIFTY only; not CRUDEOIL) ──
    if instrument in ("NIFTY50", "BANKNIFTY"):
        row = con.execute("""
            SELECT pcr_oi, india_vix_close, vix_regime,
                   price_in_oi_range, atm_iv, iv_skew, gex
            FROM daily_options_snapshot
            WHERE trade_date = ? AND index_name = ?
        """, [d, instrument]).fetchone()
        if row:
            state["pcr"] = row[0]
            state["vix"] = row[1]
            state["vix_regime"] = row[2]
            state["price_in_range"] = row[3]
            state["atm_iv"] = row[4]    # stored as % (e.g. 14.5)
            state["iv_skew"] = row[5]
            state["gex"] = row[6]

    # ── FII flows ──
    row = con.execute("""
        SELECT fii_intensity, fii_streak_days, dii_intensity
        FROM daily_institutional_flows
        WHERE trade_date = ?
    """, [d]).fetchone()
    if row:
        state["fii_intensity"] = row[0]
        state["fii_streak_days"] = row[1]

    row = con.execute("""
        SELECT ROUND(fut_idx_long::FLOAT / NULLIF(fut_idx_short, 0), 4)
        FROM daily_participant_oi
        WHERE trade_date = ? AND client_type = 'FII'
    """, [d]).fetchone()
    if row and row[0]:
        state["fii_ls_ratio"] = row[0]

    # ── Global context (macro) ──
    row = con.execute("""
        SELECT us_market_tone, crude_regime, dxy_regime, yield_regime,
               brent_change_pct, dxy_change_pct, us_10y_change_bps, asia_tone
        FROM daily_global_context
        WHERE trade_date = ?
    """, [d]).fetchone()
    if row:
        state["us_tone"] = row[0]
        state["crude_regime"] = row[1]
        state["dxy_regime"] = row[2]
        state["yield_regime"] = row[3]
        state["crude_change_pct"] = row[4]
        state["dxy_regime"] = row[2]
        state["asia_tone"] = row[7]
        # triple_threat: crude pressure + strong DXY + yield pressure simultaneously
        state["triple_threat"] = (
            row[1] in ("pressure", "alarm") and
            row[2] in ("strong", "extreme") and
            row[3] in ("pressure", "alarm")
        )

    # ── Technical (OHLC / DMA) — index-specific ──
    ohlc_name = instrument if instrument != "CRUDEOIL" else "NIFTY50"
    row = con.execute("""
        SELECT above_200dma, vs_200dma_pct, gap_type, vol_vs_20d_avg,
               change_pct, candle_type
        FROM daily_index_ohlc
        WHERE trade_date = ? AND index_name = ?
    """, [d, ohlc_name]).fetchone()
    if row:
        state["above_200dma"] = row[0]
        state["vs_200dma_pct"] = row[1]
        state["gap_type"] = row[2]
        state["vol_ratio"] = row[3]
        state["nifty_change"] = row[4]
        state["candle_type"] = row[5]

    # ── News summary for the day ──
    row = con.execute("""
        SELECT category, direction, severity
        FROM daily_news_events
        WHERE trade_date = ?
        ORDER BY CASE severity
            WHEN 'critical' THEN 1 WHEN 'high' THEN 2
            WHEN 'med' THEN 3 ELSE 4 END
        LIMIT 1
    """, [d]).fetchone()
    if row:
        state["dominant_news_cat"] = row[0]
        state["news_direction"] = row[1]
        state["has_critical_news"] = row[2] in ("critical", "CRITICAL")

    # ── CRUDEOIL-specific extras ──
    if instrument == "CRUDEOIL":
        # 3-day crude momentum
        rows = con.execute("""
            SELECT brent_change_pct FROM daily_global_context
            WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 3
        """, [d]).fetchall()
        if len(rows) == 3:
            state["crude_3d_return"] = sum(r[0] or 0 for r in rows)

    return state


# ─── News-reaction tokens ────────────────────────────────────────────────────

def _build_news_reaction_tokens(
    con: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    instrument: str,
) -> list[tuple[date, str, str]]:
    """
    For each news event, look up the next trading day's Nifty return and emit
    a reaction token. Returns list of (trade_date, instrument, token).
    """
    # Next-day return lookup
    index_name = "NIFTY50" if instrument == "CRUDEOIL" else instrument
    returns = {}
    for row in con.execute("""
        SELECT a.trade_date, b.change_pct
        FROM trading_day a
        JOIN daily_index_ohlc b
          ON b.trade_date = (
              SELECT MIN(trade_date) FROM trading_day
              WHERE trade_date > a.trade_date AND is_trading_day = TRUE
          )
          AND b.index_name = ?
        WHERE a.is_trading_day = TRUE
        AND a.trade_date BETWEEN ? AND ?
    """, [index_name, start, end]).fetchall():
        returns[row[0]] = row[1]

    rows = con.execute("""
        SELECT trade_date, category, direction
        FROM daily_news_events
        WHERE trade_date BETWEEN ? AND ?
    """, [start, end]).fetchall()

    results = []
    for trade_date, category, direction in rows:
        fwd = returns.get(trade_date)
        tok = news_reaction_token(category, fwd, direction)
        if tok:
            results.append((trade_date, instrument, tok))
    return results


# ─── Main builder ────────────────────────────────────────────────────────────

def build_tokens(
    start: date | None = None,
    end: date | None = None,
    instruments: list[str] | None = None,
):
    """
    Build daily_tokens for all (date, instrument) pairs in range.
    """
    start = start or HIST_START
    end = end or HIST_END
    instruments = instruments or INSTRUMENTS

    con = duckdb.connect(str(DB_PATH))

    trading_days = [
        row[0] for row in con.execute("""
            SELECT trade_date FROM trading_day
            WHERE is_trading_day = TRUE AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
        """, [start, end]).fetchall()
    ]

    if not trading_days:
        logger.error("No trading days found. Run setup.py first.")
        con.close()
        return

    logger.info(f"Building tokens: {start} → {end} ({len(trading_days)} days, "
                f"instruments: {instruments})")

    token_rows: list[dict] = []

    for d in tqdm(trading_days, desc="Building tokens"):
        for instrument in instruments:
            state = _build_state(con, d, instrument)
            if not state:
                continue

            toks = tokenize_day(state, instrument)

            for tok in set(toks):  # deduplicate within same day
                token_rows.append({
                    "trade_date": d,
                    "index_name": instrument,
                    "token": tok,
                })

    # News-reaction tokens (joint across instruments)
    for instrument in instruments:
        reaction_rows = _build_news_reaction_tokens(con, start, end, instrument)
        for trade_date, inst, tok in reaction_rows:
            token_rows.append({
                "trade_date": trade_date,
                "index_name": inst,
                "token": tok,
            })

    if not token_rows:
        logger.warning("No tokens produced — check that upstream tables are populated.")
        con.close()
        return

    df = pd.DataFrame(token_rows).drop_duplicates(
        subset=["trade_date", "index_name", "token"]
    )

    # Write to DB
    con.execute(
        "DELETE FROM daily_tokens WHERE trade_date BETWEEN ? AND ?",
        [start, end],
    )
    con.execute("INSERT INTO daily_tokens SELECT * FROM df")

    count = con.execute("SELECT COUNT(*) FROM daily_tokens").fetchone()[0]
    logger.info(f"Inserted {count:,} token rows")

    # Summary
    summary = con.execute("""
        SELECT index_name,
               COUNT(DISTINCT trade_date) as days,
               COUNT(*) as total_tokens,
               ROUND(COUNT(*)*1.0/COUNT(DISTINCT trade_date),1) as avg_per_day
        FROM daily_tokens
        WHERE trade_date BETWEEN ? AND ?
        GROUP BY index_name ORDER BY index_name
    """, [start, end]).fetchdf()
    print("\n  Token Coverage:")
    print(summary.to_string(index=False))

    sample = con.execute("""
        SELECT token, COUNT(*) as n
        FROM daily_tokens
        WHERE trade_date BETWEEN ? AND ?
        AND index_name = 'NIFTY50'
        GROUP BY token ORDER BY n DESC LIMIT 20
    """, [start, end]).fetchdf()
    print("\n  Top NIFTY50 tokens:")
    print(sample.to_string(index=False))

    con.close()


# ─── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NEVAREP — Token Builder")
    parser.add_argument("--start", type=str)
    parser.add_argument("--end", type=str)
    parser.add_argument("--instrument", type=str, choices=INSTRUMENTS,
                        help="Build tokens for a single instrument only")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None
    instruments = [args.instrument] if args.instrument else None

    build_tokens(start, end, instruments)


if __name__ == "__main__":
    main()
