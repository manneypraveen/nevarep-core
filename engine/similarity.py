from __future__ import annotations

"""
NEVAREP — Similarity Engine (engine/similarity.py)

The core intelligence: given today's market conditions, find the
5-10 most similar historical days across 5 weighted dimensions.

Dimensions and weights:
  1. Global macro regime  (30%) — US markets, crude, DXY, yields
  2. FII flow pattern     (25%) — FII streak, intensity, long-short ratio
  3. News/event profile   (20%) — category of dominant news, severity
  4. Options positioning  (15%) — PCR band, VIX regime, OI structure
  5. Technical position   (10%) — vs 200 DMA, trend, gap size

Returns ranked list of similar days with their outcomes.

Usage:
    python -m engine.similarity                    # find similar to latest day
    python -m engine.similarity --date 2024-10-24  # find similar to specific date
"""

import sys
import argparse
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DB_PATH

Path("logs").mkdir(exist_ok=True)
logger.add("logs/similarity.log", rotation="10 MB")

# ═══════════════════════════════════════════════════════════
# DIMENSION WEIGHTS (tunable via walk-forward optimization)
# ═══════════════════════════════════════════════════════════

WEIGHTS = {
    "macro": 0.30,
    "fii_flow": 0.25,
    "news_event": 0.20,
    "options": 0.15,
    "technical": 0.10,
}

SIMILARITY_THRESHOLD = 0.50  # minimum to be considered "similar"
MAX_RESULTS = 10


# ═══════════════════════════════════════════════════════════
# HELPER: Band matching
# ═══════════════════════════════════════════════════════════

def band_match(val1, val2, bands: list[float]) -> float:
    """
    Score how close two values are based on which band they fall in.
    Returns 1.0 for same band, 0.5 for adjacent, 0.0 for distant.
    """
    if val1 is None or val2 is None:
        return 0.5  # neutral when data missing

    def get_band(v):
        for i, threshold in enumerate(bands):
            if v < threshold:
                return i
        return len(bands)

    b1 = get_band(val1)
    b2 = get_band(val2)
    diff = abs(b1 - b2)

    if diff == 0:
        return 1.0
    elif diff == 1:
        return 0.6
    elif diff == 2:
        return 0.3
    return 0.0


def exact_match(val1, val2) -> float:
    """1.0 if same, 0.0 if different, 0.5 if either is None."""
    if val1 is None or val2 is None:
        return 0.5
    return 1.0 if val1 == val2 else 0.0


def range_similarity(val1, val2, max_diff: float) -> float:
    """Continuous similarity based on numeric distance."""
    if val1 is None or val2 is None:
        return 0.5
    try:
        diff = abs(float(val1) - float(val2))
        return max(0, 1.0 - diff / max_diff)
    except (ValueError, TypeError):
        return 0.5


# ═══════════════════════════════════════════════════════════
# BUILD CONTEXT PROFILE FOR A DAY
# ═══════════════════════════════════════════════════════════

def build_context(con: duckdb.DuckDBPyConnection, d: date) -> dict | None:
    """
    Build the full context profile for a given trading day.
    Pulls from all tables to create a single dict describing that day.
    """
    ctx = {"trade_date": d}

    # ── Global macro ──
    row = con.execute("""
        SELECT us_market_tone, crude_regime, dxy_regime, yield_regime,
               sp500_change_pct, brent_change_pct, dxy_change_pct,
               us_vix_close, asia_tone, usdinr_change_pct
        FROM daily_global_context WHERE trade_date = ?
    """, [d]).fetchone()

    if row:
        ctx["us_tone"] = row[0]
        ctx["crude_regime"] = row[1]
        ctx["dxy_regime"] = row[2]
        ctx["yield_regime"] = row[3]
        ctx["sp500_chg"] = row[4]
        ctx["brent_chg"] = row[5]
        ctx["dxy_chg"] = row[6]
        ctx["us_vix"] = row[7]
        ctx["asia_tone"] = row[8]
        ctx["usdinr_chg"] = row[9]

        # Triple threat flag
        ctx["triple_threat"] = (
            ctx.get("crude_regime") in ("pressure", "alarm") and
            ctx.get("dxy_regime") in ("strong", "extreme") and
            ctx.get("yield_regime") in ("pressure", "alarm")
        )
    else:
        return None  # no global data = can't compute similarity

    # ── FII flow ──
    row = con.execute("""
        SELECT fii_net_cr, fii_streak_days, fii_intensity,
               fii_buy_sell_ratio, flow_dominant
        FROM daily_institutional_flows WHERE trade_date = ?
    """, [d]).fetchone()

    if row:
        ctx["fii_net"] = row[0]
        ctx["fii_streak"] = row[1]
        ctx["fii_intensity"] = row[2]
        ctx["fii_ls_ratio"] = row[3]
        ctx["flow_dominant"] = row[4]

    # ── Options positioning (Nifty) ──
    row = con.execute("""
        SELECT pcr_oi, pcr_classification, vix_regime,
               price_in_oi_range, oi_breach_side,
               fii_long_short_ratio, max_pain_strike,
               highest_put_oi_strike, highest_call_oi_strike,
               atm_iv, iv_skew, iv_percentile, gex
        FROM daily_options_snapshot
        WHERE trade_date = ? AND index_name = 'NIFTY50'
    """, [d]).fetchone()

    if row:
        ctx["pcr"] = row[0]
        ctx["pcr_class"] = row[1]
        ctx["vix_regime"] = row[2]
        ctx["price_in_range"] = row[3]
        ctx["oi_breach"] = row[4]
        ctx["fii_deriv_ls"] = row[5]
        ctx["max_pain"] = row[6]
        ctx["oi_support"] = row[7]
        ctx["oi_resistance"] = row[8]
        ctx["atm_iv"] = row[9]    # annualised % (e.g. 14.5)
        ctx["iv_skew"] = row[10]  # 25δ put IV - call IV
        ctx["iv_percentile"] = row[11]
        ctx["gex"] = row[12]

    # ── Technical position (Nifty) ──
    row = con.execute("""
        SELECT close_price, change_pct, gap_type, gap_open_pct,
               above_200dma, vs_200dma_pct, vs_50dma_pct,
               candle_type, day_shape, vol_vs_20d_avg,
               dist_52w_high_pct
        FROM daily_index_ohlc
        WHERE trade_date = ? AND index_name = 'NIFTY50'
    """, [d]).fetchone()

    if row:
        ctx["nifty_close"] = row[0]
        ctx["nifty_change"] = row[1]
        ctx["gap_type"] = row[2]
        ctx["gap_pct"] = row[3]
        ctx["above_200dma"] = row[4]
        ctx["vs_200dma"] = row[5]
        ctx["vs_50dma"] = row[6]
        ctx["candle_type"] = row[7]
        ctx["day_shape"] = row[8]
        ctx["volume_ratio"] = row[9]
        ctx["dist_52w_high"] = row[10]

    # ── News density (count of news items) ──
    row = con.execute("""
        SELECT COUNT(*) as news_count,
               MAX(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END) as has_critical,
               MAX(CASE WHEN severity = 'high' THEN 1 ELSE 0 END) as has_high
        FROM daily_news_events WHERE trade_date = ?
    """, [d]).fetchone()

    if row:
        ctx["news_count"] = row[0]
        ctx["has_critical_news"] = bool(row[1])
        ctx["has_high_news"] = bool(row[2])

    # Get dominant news category
    row = con.execute("""
        SELECT category FROM daily_news_events
        WHERE trade_date = ? AND severity IN ('critical', 'high')
        ORDER BY CASE severity WHEN 'critical' THEN 1 WHEN 'high' THEN 2 ELSE 3 END
        LIMIT 1
    """, [d]).fetchone()

    ctx["dominant_news_cat"] = row[0] if row else None

    return ctx


# ═══════════════════════════════════════════════════════════
# DIMENSION SCORERS
# ═══════════════════════════════════════════════════════════

def score_macro(ctx1: dict, ctx2: dict) -> float:
    """Score macro regime similarity (weight: 30%)."""
    scores = []

    # US market tone
    scores.append(exact_match(ctx1.get("us_tone"), ctx2.get("us_tone")))

    # Crude regime
    scores.append(exact_match(ctx1.get("crude_regime"), ctx2.get("crude_regime")))

    # DXY regime
    scores.append(exact_match(ctx1.get("dxy_regime"), ctx2.get("dxy_regime")))

    # Yield regime
    scores.append(exact_match(ctx1.get("yield_regime"), ctx2.get("yield_regime")))

    # Triple threat (both true = strong match)
    if ctx1.get("triple_threat") and ctx2.get("triple_threat"):
        scores.append(1.0)
    elif ctx1.get("triple_threat") != ctx2.get("triple_threat"):
        scores.append(0.2)
    else:
        scores.append(0.7)

    # Asia tone
    scores.append(exact_match(ctx1.get("asia_tone"), ctx2.get("asia_tone")))

    # S&P 500 change magnitude
    scores.append(range_similarity(ctx1.get("sp500_chg"), ctx2.get("sp500_chg"), 3.0))

    return sum(scores) / len(scores)


def score_fii_flow(ctx1: dict, ctx2: dict) -> float:
    """Score FII flow pattern similarity (weight: 25%)."""
    scores = []

    # FII intensity (exact)
    scores.append(exact_match(ctx1.get("fii_intensity"), ctx2.get("fii_intensity")))

    # FII streak direction and length
    s1 = ctx1.get("fii_streak", 0) or 0
    s2 = ctx2.get("fii_streak", 0) or 0

    # Same sign = good, same magnitude = better
    if (s1 > 0 and s2 > 0) or (s1 < 0 and s2 < 0):
        scores.append(0.7 + 0.3 * range_similarity(abs(s1), abs(s2), 10))
    elif s1 == 0 and s2 == 0:
        scores.append(0.8)
    else:
        scores.append(0.1)

    # FII long-short ratio band
    scores.append(band_match(
        ctx1.get("fii_deriv_ls"), ctx2.get("fii_deriv_ls"),
        [0.3, 0.5, 0.8, 1.0, 1.5]
    ))

    # Flow dominant
    scores.append(exact_match(ctx1.get("flow_dominant"), ctx2.get("flow_dominant")))

    return sum(scores) / len(scores)


def score_options(ctx1: dict, ctx2: dict) -> float:
    """Score options positioning similarity (weight: 15%)."""
    scores = []

    # PCR band
    scores.append(band_match(
        ctx1.get("pcr"), ctx2.get("pcr"),
        [0.7, 0.9, 1.1, 1.3]
    ))

    # VIX regime
    scores.append(exact_match(ctx1.get("vix_regime"), ctx2.get("vix_regime")))

    # Price in OI range
    scores.append(exact_match(ctx1.get("price_in_range"), ctx2.get("price_in_range")))

    # PCR classification
    scores.append(exact_match(ctx1.get("pcr_class"), ctx2.get("pcr_class")))

    # ATM IV band (when available — was always NULL before greeks engine)
    # Bands: <10%, 10-15%, 15-20%, 20-25%, >25% annualised IV
    scores.append(band_match(
        ctx1.get("atm_iv"), ctx2.get("atm_iv"),
        [10.0, 15.0, 20.0, 25.0]
    ))

    # IV skew direction: both negative (put-skew dominant) vs both positive
    iv_skew1 = ctx1.get("iv_skew")
    iv_skew2 = ctx2.get("iv_skew")
    if iv_skew1 is not None and iv_skew2 is not None:
        same_sign = (iv_skew1 >= 0) == (iv_skew2 >= 0)
        scores.append(1.0 if same_sign else 0.3)
    else:
        scores.append(0.5)  # unknown

    return sum(scores) / len(scores)


def score_news_event(ctx1: dict, ctx2: dict) -> float:
    """Score news/event profile similarity (weight: 20%)."""
    scores = []

    # Dominant news category
    scores.append(exact_match(ctx1.get("dominant_news_cat"), ctx2.get("dominant_news_cat")))

    # Has critical/high news
    scores.append(exact_match(ctx1.get("has_critical_news"), ctx2.get("has_critical_news")))
    scores.append(exact_match(ctx1.get("has_high_news"), ctx2.get("has_high_news")))

    # News density similarity (both heavy or both light)
    scores.append(range_similarity(
        ctx1.get("news_count", 0), ctx2.get("news_count", 0), 15
    ))

    return sum(scores) / len(scores)


def score_technical(ctx1: dict, ctx2: dict) -> float:
    """Score technical position similarity (weight: 10%)."""
    scores = []

    # Above/below 200 DMA
    scores.append(exact_match(ctx1.get("above_200dma"), ctx2.get("above_200dma")))

    # Distance from 200 DMA band
    scores.append(band_match(
        ctx1.get("vs_200dma"), ctx2.get("vs_200dma"),
        [-5, -2, 0, 2, 5, 10]
    ))

    # Gap type
    scores.append(exact_match(ctx1.get("gap_type"), ctx2.get("gap_type")))

    # Volume ratio band
    scores.append(band_match(
        ctx1.get("volume_ratio"), ctx2.get("volume_ratio"),
        [0.7, 0.9, 1.1, 1.3, 1.5]
    ))

    # Distance from 52W high
    scores.append(range_similarity(
        ctx1.get("dist_52w_high"), ctx2.get("dist_52w_high"), 20
    ))

    return sum(scores) / len(scores)


# ═══════════════════════════════════════════════════════════
# MAIN SIMILARITY COMPUTATION
# ═══════════════════════════════════════════════════════════

def compute_similarity(ctx_target: dict, ctx_candidate: dict) -> tuple[float, dict]:
    """
    Compute total similarity between two day contexts.
    Returns (total_score, dimension_scores).
    """
    dims = {
        "macro": score_macro(ctx_target, ctx_candidate),
        "fii_flow": score_fii_flow(ctx_target, ctx_candidate),
        "options": score_options(ctx_target, ctx_candidate),
        "news_event": score_news_event(ctx_target, ctx_candidate),
        "technical": score_technical(ctx_target, ctx_candidate),
    }

    total = sum(WEIGHTS[k] * dims[k] for k in WEIGHTS)

    return round(total, 4), {k: round(v, 3) for k, v in dims.items()}


def find_similar_days(
    target_date: date,
    lookback_only: bool = True,
    min_similarity: float = SIMILARITY_THRESHOLD,
    max_results: int = MAX_RESULTS,
) -> list[dict]:
    """
    Find the most similar historical days to the target date.
    
    If lookback_only=True (default), only searches days BEFORE target_date.
    This is essential for prediction — no peeking at future data.
    """
    con = duckdb.connect(str(DB_PATH))

    # Build target context
    target_ctx = build_context(con, target_date)
    if not target_ctx:
        logger.error(f"Could not build context for {target_date}")
        con.close()
        return []

    # Get all candidate days
    if lookback_only:
        candidate_dates = [
            row[0] for row in con.execute("""
                SELECT DISTINCT trade_date FROM daily_global_context
                WHERE trade_date < ?
                ORDER BY trade_date
            """, [target_date]).fetchall()
        ]
    else:
        candidate_dates = [
            row[0] for row in con.execute("""
                SELECT DISTINCT trade_date FROM daily_global_context
                WHERE trade_date != ?
                ORDER BY trade_date
            """, [target_date]).fetchall()
        ]

    logger.info(f"Searching {len(candidate_dates)} candidate days for similarity to {target_date}")

    # Build all contexts (this is the expensive part)
    results = []
    for cd in candidate_dates:
        cand_ctx = build_context(con, cd)
        if not cand_ctx:
            continue

        score, dims = compute_similarity(target_ctx, cand_ctx)

        if score >= min_similarity:
            # Get outcome for this day
            outcome = con.execute("""
                SELECT change_pct, gap_type, candle_type, day_shape
                FROM daily_index_ohlc
                WHERE trade_date = ? AND index_name = 'NIFTY50'
            """, [cd]).fetchone()

            # Get next day outcome
            next_day = con.execute("""
                SELECT trade_date, change_pct FROM daily_index_ohlc
                WHERE trade_date > ? AND index_name = 'NIFTY50'
                ORDER BY trade_date LIMIT 1
            """, [cd]).fetchone()

            # Get 3-day forward outcome
            three_day = con.execute("""
                SELECT SUM(change_pct) as cum_change FROM (
                    SELECT change_pct FROM daily_index_ohlc
                    WHERE trade_date > ? AND index_name = 'NIFTY50'
                    ORDER BY trade_date LIMIT 3
                )
            """, [cd]).fetchone()

            results.append({
                "date": cd,
                "similarity": score,
                "dimensions": dims,
                "nifty_change": outcome[0] if outcome else None,
                "gap_type": outcome[1] if outcome else None,
                "candle": outcome[2] if outcome else None,
                "day_shape": outcome[3] if outcome else None,
                "next_day_change": next_day[1] if next_day else None,
                "three_day_change": three_day[0] if three_day else None,
            })

    # Sort by similarity (highest first)
    results.sort(key=lambda x: x["similarity"], reverse=True)

    con.close()
    return results[:max_results]


# ═══════════════════════════════════════════════════════════
# ANALYZE OUTCOMES
# ═══════════════════════════════════════════════════════════

def analyze_outcomes(similar_days: list[dict]) -> dict:
    """
    Analyze the outcomes of similar historical days.
    Returns prediction summary.
    """
    if not similar_days:
        return {"error": "No similar days found"}

    changes = [d["nifty_change"] for d in similar_days if d["nifty_change"] is not None]
    next_changes = [d["next_day_change"] for d in similar_days if d["next_day_change"] is not None]
    three_day = [d["three_day_change"] for d in similar_days if d["three_day_change"] is not None]

    if not changes:
        return {"error": "No outcome data"}

    bullish = sum(1 for c in changes if c > 0.1)
    bearish = sum(1 for c in changes if c < -0.1)
    neutral = len(changes) - bullish - bearish

    # Direction
    if bullish > bearish * 1.5:
        direction = "bullish"
    elif bearish > bullish * 1.5:
        direction = "bearish"
    else:
        direction = "mixed"

    # Confidence
    majority = max(bullish, bearish)
    confidence = round(majority / len(changes) * 100, 1)

    return {
        "sample_size": len(similar_days),
        "direction": direction,
        "confidence": confidence,
        "same_day": {
            "avg_change": round(np.mean(changes), 2),
            "median_change": round(np.median(changes), 2),
            "min_change": round(min(changes), 2),
            "max_change": round(max(changes), 2),
            "bullish_count": bullish,
            "bearish_count": bearish,
            "neutral_count": neutral,
        },
        "next_day": {
            "avg_change": round(np.mean(next_changes), 2) if next_changes else None,
            "bullish_pct": round(sum(1 for c in next_changes if c > 0) / max(len(next_changes), 1) * 100, 1),
        },
        "three_day_forward": {
            "avg_change": round(np.mean(three_day), 2) if three_day else None,
            "bullish_pct": round(sum(1 for c in three_day if c > 0) / max(len(three_day), 1) * 100, 1),
        },
    }


# ═══════════════════════════════════════════════════════════
# DISPLAY RESULTS
# ═══════════════════════════════════════════════════════════

def display_results(target_date: date, similar_days: list[dict], analysis: dict):
    """Pretty print the similarity search results."""
    print(f"\n{'═' * 70}")
    print(f"  SIMILAR DAYS TO {target_date}")
    print(f"{'═' * 70}")

    if not similar_days:
        print("  No similar days found above threshold.")
        return

    print(f"\n  {'Date':<12} {'Sim':>5} {'Nifty%':>8} {'Next':>6} {'3Day':>6}  "
          f"{'Macro':>6} {'FII':>5} {'News':>5} {'Opts':>5} {'Tech':>5}")
    print(f"  {'─' * 68}")

    for d in similar_days:
        dims = d["dimensions"]
        next_day_change = d.get("next_day_change")
        next_day_str = f"{next_day_change:>5.1f}% " if next_day_change is not None else f"{'N/A':>6} "
        print(f"  {str(d['date']):<12} {d['similarity']:>5.2f} "
              f"{d['nifty_change']:>7.1f}% "
              f"{next_day_str}", end="")
        print(f"{d.get('three_day_change', 0) or 0:>5.1f}%  "
              f"{dims['macro']:>5.2f} {dims['fii_flow']:>5.2f} "
              f"{dims['news_event']:>5.2f} {dims['options']:>5.2f} {dims['technical']:>5.2f}")

    print(f"\n{'─' * 70}")
    print(f"  OUTCOME ANALYSIS")
    print(f"{'─' * 70}")

    sd = analysis.get("same_day", {})
    nd = analysis.get("next_day", {})
    td = analysis.get("three_day_forward", {})

    print(f"  Direction:     {analysis.get('direction', 'N/A').upper()}")
    print(f"  Confidence:    {analysis.get('confidence', 0):.0f}%")
    print(f"  Sample size:   {analysis.get('sample_size', 0)} similar days")
    print(f"")
    print(f"  Same day:      avg {sd.get('avg_change', 0):+.2f}% | "
          f"range [{sd.get('min_change', 0):+.1f}% to {sd.get('max_change', 0):+.1f}%]")
    print(f"                 {sd.get('bullish_count', 0)} bullish, "
          f"{sd.get('bearish_count', 0)} bearish, "
          f"{sd.get('neutral_count', 0)} neutral")
    print(f"  Next day:      avg {nd.get('avg_change', 0):+.2f}% | "
          f"{nd.get('bullish_pct', 0):.0f}% bullish")
    print(f"  3-day forward: avg {td.get('avg_change', 0):+.2f}% | "
          f"{td.get('bullish_pct', 0):.0f}% bullish")
    print(f"{'═' * 70}")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="NEVAREP — Similarity Engine")
    parser.add_argument("--date", type=str, help="Target date YYYY-MM-DD (default: latest)")
    parser.add_argument("--min-sim", type=float, default=SIMILARITY_THRESHOLD,
                        help=f"Minimum similarity threshold (default: {SIMILARITY_THRESHOLD})")
    parser.add_argument("--max-results", type=int, default=MAX_RESULTS,
                        help=f"Max results (default: {MAX_RESULTS})")
    parser.add_argument("--all-dates", action="store_true",
                        help="Search all dates (not just lookback)")
    args = parser.parse_args()

    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        # Use latest trading day
        con = duckdb.connect(str(DB_PATH))
        target = con.execute("""
            SELECT MAX(trade_date) FROM daily_global_context
        """).fetchone()[0]
        con.close()

    print(f"\n  Searching for days similar to {target}...")
    print(f"  Threshold: {args.min_sim}, Max results: {args.max_results}")
    print(f"  Lookback only: {not args.all_dates}\n")

    similar = find_similar_days(
        target,
        lookback_only=not args.all_dates,
        min_similarity=args.min_sim,
        max_results=args.max_results,
    )

    analysis = analyze_outcomes(similar)
    display_results(target, similar, analysis)


if __name__ == "__main__":
    main()