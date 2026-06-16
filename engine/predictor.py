"""
NEVAREP — Predictor (engine/predictor.py)

Given a target date, finds historical analog days using a combination of:
  1. Token-overlap (Jaccard over daily_tokens bag-of-symbols) — new 6th dimension
  2. The existing 5-dimension weighted similarity (macro/FII/news/options/technical)

The two scores are combined: final_score = 0.7 * sim5d + 0.3 * token_jaccard
(weight is deliberately conservative; re-tune on walk-forward folds.)

Returns: direction, confidence, predicted_range, matched_days list.

Usage:
    python -m engine.predictor                    # predict for latest day in DB
    python -m engine.predictor --date 2024-10-24  # predict specific date
"""

from __future__ import annotations

import sys
import argparse
from datetime import date, datetime
from pathlib import Path

import duckdb
import numpy as np
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DB_PATH
from engine.similarity import (
    build_context, compute_similarity, analyze_outcomes,
    SIMILARITY_THRESHOLD, MAX_RESULTS,
)

Path("logs").mkdir(exist_ok=True)
logger.add("logs/predictor.log", rotation="10 MB")

# Weight of token-Jaccard vs 5-dim similarity in the combined score.
# Intentionally small — tokens are an additive signal, not a replacement.
# Tune on training folds only.
TOKEN_WEIGHT = 0.30
SIM5D_WEIGHT = 1.0 - TOKEN_WEIGHT

MIN_COMBINED_SCORE = 0.45  # minimum combined score for a match
MIN_SIMILAR_DAYS = 3        # fall back to lower threshold if fewer matches


# ─── Token overlap ───────────────────────────────────────────────────────────

def _load_tokens(con: duckdb.DuckDBPyConnection, d: date, instrument: str = "NIFTY50") -> frozenset[str]:
    rows = con.execute("""
        SELECT token FROM daily_tokens
        WHERE trade_date = ? AND index_name = ?
    """, [d, instrument]).fetchall()
    return frozenset(r[0] for r in rows)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ─── Combined scoring ────────────────────────────────────────────────────────

def find_analogs(
    target_date: date,
    instrument: str = "NIFTY50",
    min_score: float = MIN_COMBINED_SCORE,
    max_results: int = MAX_RESULTS,
) -> list[dict]:
    """
    Find historical analog days using combined 5-dim + token-Jaccard score.
    lookback_only = True enforced (no future data leakage).

    Returns ranked list of dicts:
        date, combined_score, sim5d_score, token_score, dimensions,
        nifty_change, next_day_change, three_day_change
    """
    con = duckdb.connect(str(DB_PATH))

    target_ctx = build_context(con, target_date)
    if not target_ctx:
        logger.error(f"Cannot build context for {target_date} — is global context populated?")
        con.close()
        return []

    target_tokens = _load_tokens(con, target_date, instrument)
    has_tokens = len(target_tokens) > 0

    if not has_tokens:
        logger.warning(
            f"No tokens for {target_date} / {instrument}. "
            "Run pipeline.build_tokens first. Falling back to 5-dim only."
        )

    candidate_dates = [
        row[0] for row in con.execute("""
            SELECT DISTINCT trade_date FROM daily_global_context
            WHERE trade_date < ?
            ORDER BY trade_date
        """, [target_date]).fetchall()
    ]

    logger.info(f"Scoring {len(candidate_dates)} candidate days for {target_date}")

    results = []

    for cd in candidate_dates:
        cand_ctx = build_context(con, cd)
        if not cand_ctx:
            continue

        sim5d, dims = compute_similarity(target_ctx, cand_ctx)

        if has_tokens:
            cand_tokens = _load_tokens(con, cd, instrument)
            tok_score = _jaccard(target_tokens, cand_tokens)
            combined = SIM5D_WEIGHT * sim5d + TOKEN_WEIGHT * tok_score
        else:
            tok_score = 0.0
            combined = sim5d

        if combined < min_score:
            continue

        # Outcome data
        outcome = con.execute("""
            SELECT change_pct FROM daily_index_ohlc
            WHERE trade_date = ? AND index_name = 'NIFTY50'
        """, [cd]).fetchone()

        next_day = con.execute("""
            SELECT change_pct FROM daily_index_ohlc
            WHERE trade_date > ? AND index_name = 'NIFTY50'
            ORDER BY trade_date LIMIT 1
        """, [cd]).fetchone()

        three_day = con.execute("""
            SELECT SUM(change_pct) FROM (
                SELECT change_pct FROM daily_index_ohlc
                WHERE trade_date > ? AND index_name = 'NIFTY50'
                ORDER BY trade_date LIMIT 3
            )
        """, [cd]).fetchone()

        results.append({
            "date": cd,
            "combined_score": round(combined, 4),
            "sim5d_score": round(sim5d, 4),
            "token_score": round(tok_score, 4),
            "dimensions": dims,
            "nifty_change": outcome[0] if outcome else None,
            "next_day_change": next_day[0] if next_day else None,
            "three_day_change": three_day[0] if three_day else None,
        })

    con.close()

    results.sort(key=lambda x: x["combined_score"], reverse=True)
    return results[:max_results]


# ─── Prediction ──────────────────────────────────────────────────────────────

def predict(
    target_date: date,
    instrument: str = "NIFTY50",
    min_score: float = MIN_COMBINED_SCORE,
    max_results: int = MAX_RESULTS,
) -> dict:
    """
    Predict next-day direction for `target_date`.

    Returns dict with:
        predicted_direction   "bullish" | "bearish" | "mixed"
        confidence            float (%)
        predicted_avg_change  float (%)
        predicted_range_low   float (%)
        predicted_range_high  float (%)
        similar_count         int
        avg_combined_score    float
        has_token_signal      bool
        analog_days           list[dict]  (the matched days)
        analysis              dict  (outcome statistics)
    """
    analogs = find_analogs(target_date, instrument, min_score, max_results)

    # Fall back to lower threshold if too few matches
    if len(analogs) < MIN_SIMILAR_DAYS and min_score > 0.30:
        logger.warning(
            f"Only {len(analogs)} analogs at score {min_score}. "
            f"Retrying at 0.30."
        )
        analogs = find_analogs(target_date, instrument, 0.30, max_results)

    if not analogs:
        return {
            "predicted_direction": "mixed",
            "confidence": 0,
            "predicted_avg_change": 0,
            "predicted_range_low": 0,
            "predicted_range_high": 0,
            "similar_count": 0,
            "avg_combined_score": 0,
            "has_token_signal": False,
            "analog_days": [],
            "analysis": {"error": "No analogs found"},
        }

    analysis = analyze_outcomes(analogs)
    next_changes = [d["next_day_change"] for d in analogs if d["next_day_change"] is not None]

    avg_change = round(float(np.mean(next_changes)), 2) if next_changes else 0.0
    std_change = round(float(np.std(next_changes)), 2) if len(next_changes) > 1 else 0.5
    has_tokens = any(d["token_score"] > 0 for d in analogs)

    return {
        "predicted_direction": analysis.get("direction", "mixed"),
        "confidence": analysis.get("confidence", 0),
        "predicted_avg_change": avg_change,
        "predicted_range_low": round(avg_change - std_change, 2),
        "predicted_range_high": round(avg_change + std_change, 2),
        "similar_count": len(analogs),
        "avg_combined_score": round(
            sum(d["combined_score"] for d in analogs) / len(analogs), 4
        ),
        "has_token_signal": has_tokens,
        "analog_days": analogs,
        "analysis": analysis,
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NEVAREP — Predictor")
    parser.add_argument("--date", type=str, help="Target date YYYY-MM-DD (default: latest in DB)")
    parser.add_argument("--instrument", type=str, default="NIFTY50")
    parser.add_argument("--min-score", type=float, default=MIN_COMBINED_SCORE)
    args = parser.parse_args()

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        con = duckdb.connect(str(DB_PATH))
        row = con.execute("""
            SELECT MAX(trade_date) FROM daily_global_context
        """).fetchone()
        con.close()
        if not row or not row[0]:
            print("ERROR: No data in DB. Run setup + backfill first.")
            return
        target_date = row[0]

    print(f"\nPredicting for: {target_date} / {args.instrument}")
    result = predict(target_date, args.instrument, args.min_score)

    print(f"\n{'═'*60}")
    print(f"  PREDICTION: {result['predicted_direction'].upper()}")
    print(f"  Confidence: {result['confidence']:.0f}%")
    print(f"  Avg next-day change: {result['predicted_avg_change']:+.2f}%")
    print(f"  Range: {result['predicted_range_low']:+.2f}% to {result['predicted_range_high']:+.2f}%")
    print(f"  Analog days: {result['similar_count']} (avg score {result['avg_combined_score']:.3f})")
    print(f"  Token signal: {'yes' if result['has_token_signal'] else 'no (build_tokens not run)'}")
    print(f"{'═'*60}")

    print(f"\n  Top analog days:")
    print(f"  {'Date':<12} {'Combined':>8} {'5-Dim':>6} {'Tokens':>7} {'NextDay':>8}")
    print(f"  {'─'*48}")
    for d in result["analog_days"][:5]:
        nd = f"{d['next_day_change']:+.1f}%" if d["next_day_change"] is not None else "  N/A"
        print(f"  {str(d['date']):<12} {d['combined_score']:>8.4f} "
              f"{d['sim5d_score']:>6.4f} {d['token_score']:>7.4f} {nd:>8}")


if __name__ == "__main__":
    main()
