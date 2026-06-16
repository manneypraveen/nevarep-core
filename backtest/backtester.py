from __future__ import annotations

"""
NEVAREP — Walk-Forward Backtester (backtest/backtester.py)

Tests the similarity engine's prediction accuracy across historical data.
For each test day T:
  1. Build T's context using only data ≤ T-1
  2. Find similar days from history before T
  3. Generate prediction (direction, expected range)
  4. Compare against T's actual outcome
  5. Score: direction correct? range correct?

No look-ahead bias — each prediction uses only prior data.

Usage:
    python -m backtest.backtester                              # full test set
    python -m backtest.backtester --start 2025-01-01           # test from date
    python -m backtest.backtester --start 2024-01-01 --end 2024-12-31
    python -m backtest.backtester --min-history 500            # require 500 prior days
"""

import sys
import argparse
import time
from datetime import date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from tqdm import tqdm
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DB_PATH, HIST_START, HIST_END
from engine.similarity import find_similar_days, analyze_outcomes, build_context

Path("logs").mkdir(exist_ok=True)
logger.add("logs/backtester.log", rotation="10 MB")

# Minimum similar days to make a prediction
MIN_SIMILAR_DAYS = 3

# Minimum history before we start predicting
MIN_HISTORY_DAYS = 250  # ~1 year of data


# ═══════════════════════════════════════════════════════════
# SINGLE DAY PREDICTION
# ═══════════════════════════════════════════════════════════

def predict_day(target_date: date, min_sim: float = 0.45) -> dict | None:
    """
    Make a prediction for target_date using only prior data.
    Returns prediction dict or None if insufficient data.
    """
    similar = find_similar_days(
        target_date,
        lookback_only=True,
        min_similarity=min_sim,
        max_results=10,
    )

    if len(similar) < MIN_SIMILAR_DAYS:
        # Try lowering threshold
        similar = find_similar_days(
            target_date,
            lookback_only=True,
            min_similarity=max(min_sim - 0.15, 0.30),
            max_results=10,
        )

    if len(similar) < MIN_SIMILAR_DAYS:
        return None

    analysis = analyze_outcomes(similar)
    sd = analysis.get("same_day", {})

    return {
        "prediction_date": target_date,
        "similar_count": len(similar),
        "avg_similarity": round(np.mean([d["similarity"] for d in similar]), 3),
        "predicted_direction": analysis.get("direction"),
        "confidence": analysis.get("confidence", 0),
        "predicted_avg_change": sd.get("avg_change", 0),
        "predicted_median_change": sd.get("median_change", 0),
        "predicted_range_low": sd.get("min_change", 0),
        "predicted_range_high": sd.get("max_change", 0),
        "bullish_count": sd.get("bullish_count", 0),
        "bearish_count": sd.get("bearish_count", 0),
        "next_day_avg": analysis.get("next_day", {}).get("avg_change"),
        "similar_dates": [str(d["date"]) for d in similar[:5]],
    }


# ═══════════════════════════════════════════════════════════
# WALK-FORWARD BACKTEST
# ═══════════════════════════════════════════════════════════

def run_backtest(
    start: date = None,
    end: date = None,
    min_history: int = MIN_HISTORY_DAYS,
    min_sim: float = 0.45,
    save_to_db: bool = True,
):
    """
    Run walk-forward backtest across all test days.
    """
    con = duckdb.connect(str(DB_PATH))

    # Get all trading days with data
    all_days = [
        row[0] for row in con.execute("""
            SELECT DISTINCT trade_date FROM daily_global_context
            ORDER BY trade_date
        """).fetchall()
    ]

    if not all_days:
        print("ERROR: No data in database.")
        con.close()
        return

    # Determine test window
    if start:
        test_start = start
    else:
        # Start testing after min_history days
        test_start = all_days[min(min_history, len(all_days) - 1)]

    test_end = end or all_days[-1]

    test_days = [d for d in all_days if test_start <= d <= test_end]

    # Get actual Nifty changes for scoring
    actual_data = {}
    for row in con.execute("""
        SELECT trade_date, change_pct, close_price, open_price,
               gap_type, candle_type, day_shape
        FROM daily_index_ohlc
        WHERE index_name = 'NIFTY50'
    """).fetchall():
        actual_data[row[0]] = {
            "change_pct": row[1],
            "close": row[2],
            "open": row[3],
            "gap_type": row[4],
            "candle": row[5],
            "shape": row[6],
        }

    con.close()

    print("=" * 70)
    print("  NEVAREP — Walk-Forward Backtester")
    print("=" * 70)
    print(f"  Full data range:  {all_days[0]} to {all_days[-1]} ({len(all_days)} days)")
    print(f"  Test window:      {test_start} to {test_end} ({len(test_days)} days)")
    print(f"  Min history:      {min_history} days before first prediction")
    print(f"  Min similarity:   {min_sim}")
    print()

    # ── Run predictions ──
    results = []
    skipped = 0
    start_time = time.time()

    for d in tqdm(test_days, desc="Backtesting"):
        pred = predict_day(d, min_sim=min_sim)

        actual = actual_data.get(d)
        if not actual or pred is None:
            skipped += 1
            continue


        actual_change = float(actual["change_pct"] or 0)
        pred_direction = pred["predicted_direction"]
        pred_avg = pred["predicted_avg_change"]

        # Score direction
        if pred_direction == "bullish":
            direction_correct = actual_change > 0.1
        elif pred_direction == "bearish":
            direction_correct = actual_change < -0.1
        else:
            direction_correct = abs(actual_change) < 0.5  # mixed = low vol

        # Score range
        range_correct = (
            pred["predicted_range_low"] <= actual_change <= pred["predicted_range_high"]
        )

        # Directional P&L using realized index return (symmetric, no scaling tricks).
        # Bullish → long the index: pnl = actual_change.
        # Bearish → short the index: pnl = -actual_change.
        # Mixed → no position: pnl = 0.
        # This is honest (no 2:1 win/loss asymmetry, no artificial dampening).
        if pred_direction == "bullish":
            strategy_pnl = actual_change
        elif pred_direction == "bearish":
            strategy_pnl = -actual_change
        else:
            strategy_pnl = 0

        results.append({
            "prediction_date": d,
            "predicted_direction": pred_direction,
            "confidence": pred["confidence"],
            "predicted_avg_change": pred_avg,
            "predicted_range_low": pred["predicted_range_low"],
            "predicted_range_high": pred["predicted_range_high"],
            "actual_change": actual_change,
            "direction_correct": direction_correct,
            "range_correct": range_correct,
            "strategy_pnl": round(strategy_pnl, 2),
            "similar_count": pred["similar_count"],
            "avg_similarity": pred["avg_similarity"],
        })

    elapsed = time.time() - start_time

    if not results:
        print("ERROR: No predictions generated!")
        return

    df = pd.DataFrame(results)

    # ── Compute scorecard ──
    total = len(df)
    dir_correct = df["direction_correct"].sum()
    dir_accuracy = dir_correct / total * 100
    range_correct = df["range_correct"].sum()
    range_accuracy = range_correct / total * 100

    # P&L
    cumulative_pnl = df["strategy_pnl"].sum()
    win_trades = (df["strategy_pnl"] > 0).sum()
    loss_trades = (df["strategy_pnl"] < 0).sum()
    win_rate = win_trades / max(win_trades + loss_trades, 1) * 100

    avg_win = df[df["strategy_pnl"] > 0]["strategy_pnl"].mean() if win_trades > 0 else 0
    avg_loss = abs(df[df["strategy_pnl"] < 0]["strategy_pnl"].mean()) if loss_trades > 0 else None
    reward_risk = (avg_win / avg_loss) if avg_loss else None

    # Sharpe (annualized, simplified)
    daily_returns = df["strategy_pnl"].values
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252)
    else:
        sharpe = 0

    # Max drawdown
    cumsum = np.cumsum(daily_returns)
    running_max = np.maximum.accumulate(cumsum)
    drawdowns = cumsum - running_max
    max_drawdown = drawdowns.min()

    # Accuracy by confidence level
    high_conf = df[df["confidence"] >= 70]
    med_conf = df[(df["confidence"] >= 55) & (df["confidence"] < 70)]
    low_conf = df[df["confidence"] < 55]

    # Accuracy by predicted direction
    bull_preds = df[df["predicted_direction"] == "bullish"]
    bear_preds = df[df["predicted_direction"] == "bearish"]
    mixed_preds = df[df["predicted_direction"] == "mixed"]

    # ── Display scorecard ──
    print(f"\n{'═' * 70}")
    print(f"  BACKTEST SCORECARD")
    print(f"{'═' * 70}")
    print(f"  Test period:       {df['prediction_date'].min()} to {df['prediction_date'].max()}")
    print(f"  Predictions made:  {total} (skipped {skipped})")
    print(f"  Time elapsed:      {elapsed:.0f}s ({elapsed/total:.1f}s per prediction)")
    print()

    print(f"  ── DIRECTION ACCURACY ──")
    print(f"  Overall:           {dir_accuracy:.1f}% ({dir_correct}/{total})")
    if len(high_conf) > 0:
        hc_acc = high_conf["direction_correct"].mean() * 100
        print(f"  High confidence:   {hc_acc:.1f}% ({len(high_conf)} predictions)")
    if len(med_conf) > 0:
        mc_acc = med_conf["direction_correct"].mean() * 100
        print(f"  Medium confidence: {mc_acc:.1f}% ({len(med_conf)} predictions)")
    if len(low_conf) > 0:
        lc_acc = low_conf["direction_correct"].mean() * 100
        print(f"  Low confidence:    {lc_acc:.1f}% ({len(low_conf)} predictions)")
    print()

    print(f"  ── BY DIRECTION ──")
    if len(bull_preds) > 0:
        ba = bull_preds["direction_correct"].mean() * 100
        print(f"  Bullish calls:     {ba:.1f}% ({len(bull_preds)} predictions)")
    if len(bear_preds) > 0:
        ba = bear_preds["direction_correct"].mean() * 100
        print(f"  Bearish calls:     {ba:.1f}% ({len(bear_preds)} predictions)")
    if len(mixed_preds) > 0:
        ma = mixed_preds["direction_correct"].mean() * 100
        print(f"  Mixed calls:       {ma:.1f}% ({len(mixed_preds)} predictions)")
    print()

    print(f"  ── RANGE ACCURACY ──")
    print(f"  Within predicted range: {range_accuracy:.1f}%")
    print()

    print(f"  ── STRATEGY P&L ──")
    print(f"  Cumulative P&L:    {cumulative_pnl:+.1f}%")
    print(f"  Win rate:          {win_rate:.1f}%")
    print(f"  Avg win:           {avg_win:+.2f}%")
    print(f"  Avg loss:          {(-avg_loss):+.2f}%" if avg_loss is not None else "  Avg loss:          N/A (no losing trades)")
    print(f"  Reward:Risk:       {reward_risk:.2f}:1" if reward_risk is not None else "  Reward:Risk:       undefined (no losing trades)")
    print(f"  Sharpe ratio:      {sharpe:.2f}")
    print(f"  Max drawdown:      {max_drawdown:+.1f}%")
    print()

    # ── Targets check ──
    print(f"  ── TARGETS ──")
    targets = [
        ("Direction accuracy > 60%", dir_accuracy > 60),
        ("Win rate > 55%", win_rate > 55),
        ("Reward:Risk > 1.5:1", reward_risk is not None and reward_risk > 1.5),
        ("Sharpe > 1.2", sharpe > 1.2),
        ("Max drawdown > -15%", max_drawdown > -15),
    ]
    for name, met in targets:
        status = "✅ PASS" if met else "❌ MISS"
        print(f"  {status}  {name}")

    print(f"{'═' * 70}")

    # ── Save to DB ──
    if save_to_db:
        con = duckdb.connect(str(DB_PATH))

        # Clear existing predictions for this range
        con.execute("""
            DELETE FROM predictions_log 
            WHERE prediction_date BETWEEN ? AND ?
        """, [df["prediction_date"].min(), df["prediction_date"].max()])

        for _, row in df.iterrows():
            con.execute("""
                INSERT INTO predictions_log 
                (prediction_date, generated_at, predicted_direction, confidence_pct,
                 predicted_range_low, predicted_range_high, actual_change_pct,
                 direction_correct, range_correct, strategy_pnl, notes)
                VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                row["prediction_date"],
                row["predicted_direction"],
                row["confidence"],
                row["predicted_range_low"],
                row["predicted_range_high"],
                row["actual_change"],
                row["direction_correct"],
                row["range_correct"],
                row["strategy_pnl"],
                f"sim_count={row['similar_count']}, avg_sim={row['avg_similarity']}",
            ])

        pred_count = con.execute("SELECT COUNT(*) FROM predictions_log").fetchone()[0]
        logger.info(f"Saved {pred_count} predictions to predictions_log")
        con.close()

    return df


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="NEVAREP — Walk-Forward Backtester")
    parser.add_argument("--start", type=str, help="Test start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="Test end date YYYY-MM-DD")
    parser.add_argument("--min-history", type=int, default=MIN_HISTORY_DAYS,
                        help=f"Min history days before testing (default: {MIN_HISTORY_DAYS})")
    parser.add_argument("--min-sim", type=float, default=0.45,
                        help="Min similarity threshold (default: 0.45)")
    parser.add_argument("--no-save", action="store_true",
                        help="Don't save predictions to DB")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None

    run_backtest(
        start=start,
        end=end,
        min_history=args.min_history,
        min_sim=args.min_sim,
        save_to_db=not args.no_save,
    )


if __name__ == "__main__":
    main()