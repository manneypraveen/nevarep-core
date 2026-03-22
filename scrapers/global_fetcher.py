"""
NEVAREP Sprint 1 — Global Market Data Fetcher

Downloads ALL global market data from Yahoo Finance + FRED API
and populates two tables:
  - daily_index_ohlc      (Nifty, Bank Nifty, Sensex + derived fields)
  - daily_global_context   (US markets, yields, DXY, crude, gold, Asia, Europe)

Data Sources:
  Yahoo Finance: 17 tickers (free, no API key)
  FRED API:      2 series  (free, needs API key from fred.stlouisfed.org)

Usage:
    python -m scrapers.global_fetcher
    python -m scrapers.global_fetcher --start 2022-01-01 --end 2025-03-21
"""

import sys
import argparse
from datetime import date, timedelta, datetime
from pathlib import Path

import duckdb
import pandas as pd
import numpy as np
import yfinance as yf
from loguru import logger

# Setup logging
Path("logs").mkdir(exist_ok=True)
logger.add("logs/global_fetcher.log", rotation="10 MB")

# Import config
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import (
    DB_PATH, HIST_START, HIST_END, YF_TICKERS, FRED_SERIES,
    YF_INDIAN_INDICES, YF_US_MARKETS, YF_CURRENCIES,
    YF_COMMODITIES, YF_ASIAN, YF_EUROPEAN, FRED_API_KEY,
    # Thresholds for classification
    PCR_STRONGLY_BEARISH, PCR_BEARISH, PCR_BULLISH, PCR_STRONGLY_BULLISH,
    VIX_LOW, VIX_NORMAL, VIX_ELEVATED, VIX_HIGH,
    DXY_WEAK, DXY_NEUTRAL, DXY_STRONG,
    CRUDE_VERY_POSITIVE, CRUDE_COMFORTABLE, CRUDE_PRESSURE,
    YIELD_COMFORTABLE, YIELD_WATCHING, YIELD_PRESSURE,
    US_STRONG_UP, US_MILD_UP, US_MILD_DOWN, US_STRONG_DOWN,
    GAP_THRESHOLD,
)


# ═══════════════════════════════════════════════════════════
# STEP 1: Download raw data from Yahoo Finance
# ═══════════════════════════════════════════════════════════

def download_yf_data(start: date, end: date) -> dict[str, pd.DataFrame]:
    """
    Download OHLCV data for all tickers from Yahoo Finance.
    Returns dict: {ticker_name: DataFrame}
    """
    logger.info(f"Downloading Yahoo Finance data: {start} to {end}")

    all_data = {}
    tickers_list = list(YF_TICKERS.items())
    total = len(tickers_list)

    for i, (name, symbol) in enumerate(tickers_list, 1):
        try:
            logger.info(f"  [{i}/{total}] Downloading {name} ({symbol})...")
            df = yf.download(
                symbol,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                progress=False,
                auto_adjust=True,
            )

            if df.empty:
                logger.warning(f"  No data returned for {name} ({symbol})")
                continue

            # Flatten multi-level columns if present
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # Ensure index is date (not datetime)
            df.index = df.index.date

            all_data[name] = df
            logger.info(f"  {name}: {len(df)} rows ({df.index[0]} to {df.index[-1]})")

        except Exception as e:
            logger.error(f"  Failed to download {name} ({symbol}): {e}")

    logger.info(f"Downloaded {len(all_data)}/{total} tickers successfully")
    return all_data


# ═══════════════════════════════════════════════════════════
# STEP 2: Download yield data from FRED
# ═══════════════════════════════════════════════════════════

def download_fred_data(start: date, end: date) -> dict[str, pd.Series]:
    """
    Download US Treasury yields from FRED API.
    Returns dict: {series_name: Series}
    """
    if not FRED_API_KEY or FRED_API_KEY == "your_fred_api_key_here":
        logger.warning("FRED_API_KEY not set — skipping yield data")
        logger.warning("Get a free key at: https://fred.stlouisfed.org/docs/api/api_key.html")
        return {}

    logger.info(f"Downloading FRED yield data: {start} to {end}")

    try:
        from fredapi import Fred
        fred = Fred(api_key=FRED_API_KEY)
    except Exception as e:
        logger.error(f"Failed to initialize FRED API: {e}")
        return {}

    fred_data = {}
    for name, series_id in FRED_SERIES.items():
        try:
            logger.info(f"  Downloading {name} ({series_id})...")
            s = fred.get_series(
                series_id,
                observation_start=start.isoformat(),
                observation_end=end.isoformat(),
            )
            # FRED returns NaN for holidays — forward fill
            s = s.ffill()
            s.index = s.index.date
            fred_data[name] = s
            logger.info(f"  {name}: {len(s)} data points")
        except Exception as e:
            logger.error(f"  Failed to download {name}: {e}")

    return fred_data


# ═══════════════════════════════════════════════════════════
# STEP 3: Compute derived fields for daily_index_ohlc
# ═══════════════════════════════════════════════════════════

def classify_candle(open_p, high, low, close, prev_close):
    """Classify the candlestick pattern."""
    body = close - open_p
    full_range = high - low
    if full_range == 0:
        return "doji"

    body_ratio = abs(body) / full_range

    # Doji: tiny body relative to range
    if body_ratio < 0.1:
        return "doji"

    upper_wick = high - max(open_p, close)
    lower_wick = min(open_p, close) - low

    # Hammer: small body at top, long lower wick
    if lower_wick > 2 * abs(body) and upper_wick < abs(body) and body > 0:
        return "hammer"

    # Shooting star: small body at bottom, long upper wick
    if upper_wick > 2 * abs(body) and lower_wick < abs(body) and body < 0:
        return "shooting_star"

    # Engulfing patterns (need prev_close)
    if prev_close is not None:
        prev_body = close - prev_close  # simplified
        if body > 0 and abs(body) > abs(prev_body) * 1.5 and prev_body < 0:
            return "engulfing_bull"
        if body < 0 and abs(body) > abs(prev_body) * 1.5 and prev_body > 0:
            return "engulfing_bear"

    # Simple bull/bear based on body size
    if body > 0:
        return "strong_bull" if body_ratio > 0.6 else "weak_bull"
    else:
        return "strong_bear" if body_ratio > 0.6 else "weak_bear"


def classify_close_position(open_p, high, low, close):
    """Where did the close land in the day's range?"""
    full_range = high - low
    if full_range == 0:
        return "middle"
    position = (close - low) / full_range
    if position > 0.7:
        return "near_high"
    elif position < 0.3:
        return "near_low"
    return "middle"


def classify_gap(gap_pct, threshold=GAP_THRESHOLD):
    """Classify the opening gap."""
    if gap_pct > threshold:
        return "gap_up"
    elif gap_pct < -threshold:
        return "gap_down"
    return "flat"


def classify_day_shape(open_p, high, low, close, prev_close):
    """Classify the intraday shape/pattern."""
    gap = open_p - prev_close if prev_close else 0
    change = close - open_p
    range_size = high - low
    if range_size == 0:
        return "range_bound"

    close_pos = (close - low) / range_size

    if gap > 0 and close > open_p and close_pos > 0.7:
        return "gap_and_go"
    elif gap > 0 and close < open_p:
        return "gap_and_fade"
    elif gap < 0 and close > open_p and close_pos > 0.7:
        return "v_shaped_recovery"
    elif change > 0 and close_pos > 0.8:
        return "trending_up"
    elif change < 0 and close_pos < 0.2:
        return "trending_down"
    elif abs(change) < range_size * 0.2:
        return "range_bound"
    else:
        return "volatile_reversal"


def compute_index_ohlc(name: str, df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute all derived fields for a single index.
    Returns a DataFrame ready for daily_index_ohlc table.
    """
    if df.empty:
        return pd.DataFrame()

    records = []
    closes = df["Close"].values
    opens = df["Open"].values
    highs = df["High"].values
    lows = df["Low"].values
    volumes = df["Volume"].values if "Volume" in df.columns else [0] * len(df)
    dates = list(df.index)

    # Pre-compute moving averages
    close_series = pd.Series(closes, index=dates)
    dma_20 = close_series.rolling(20, min_periods=1).mean()
    dma_50 = close_series.rolling(50, min_periods=1).mean()
    dma_200 = close_series.rolling(200, min_periods=1).mean()
    vol_series = pd.Series(volumes, index=dates)
    vol_20d_avg = vol_series.rolling(20, min_periods=1).mean()

    # 52-week high/low
    close_52w_high = close_series.rolling(252, min_periods=1).max()
    close_52w_low = close_series.rolling(252, min_periods=1).min()

    for i, d in enumerate(dates):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        v = volumes[i] if i < len(volumes) else 0
        prev_c = closes[i - 1] if i > 0 else None

        # Skip if data is NaN
        if pd.isna(c) or pd.isna(o):
            continue

        change_pts = c - prev_c if prev_c else 0
        change_pct = (change_pts / prev_c * 100) if prev_c and prev_c != 0 else 0
        gap_pts = o - prev_c if prev_c else 0
        gap_pct = (gap_pts / prev_c * 100) if prev_c and prev_c != 0 else 0
        gap_type = classify_gap(gap_pct)

        # Gap fill check
        gap_filled = False
        if prev_c:
            if gap_type == "gap_up" and l <= prev_c:
                gap_filled = True
            elif gap_type == "gap_down" and h >= prev_c:
                gap_filled = True

        day_range = h - l
        body = abs(c - o)
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l

        candle = classify_candle(o, h, l, c, prev_c)
        close_pos = classify_close_position(o, h, l, c)
        day_shape = classify_day_shape(o, h, l, c, prev_c) if prev_c else "trending_up"

        d20 = dma_20[d] if d in dma_20.index else None
        d50 = dma_50[d] if d in dma_50.index else None
        d200 = dma_200[d] if d in dma_200.index else None

        vs_20 = ((c - d20) / d20 * 100) if d20 and d20 != 0 else None
        vs_50 = ((c - d50) / d50 * 100) if d50 and d50 != 0 else None
        vs_200 = ((c - d200) / d200 * 100) if d200 and d200 != 0 else None

        hw = close_52w_high[d] if d in close_52w_high.index else c
        lw = close_52w_low[d] if d in close_52w_low.index else c
        dist_high = ((c - hw) / hw * 100) if hw and hw != 0 else 0
        dist_low = ((c - lw) / lw * 100) if lw and lw != 0 else 0

        v_avg = vol_20d_avg[d] if d in vol_20d_avg.index else v
        vol_ratio = (v / v_avg) if v_avg and v_avg != 0 else 1.0

        records.append({
            "trade_date": d,
            "index_name": name,
            "open_price": round(o, 2),
            "high_price": round(h, 2),
            "low_price": round(l, 2),
            "close_price": round(c, 2),
            "prev_close": round(prev_c, 2) if prev_c else None,
            "change_points": round(change_pts, 2),
            "change_pct": round(change_pct, 4),
            "volume": int(v) if not pd.isna(v) else 0,
            "gap_open_points": round(gap_pts, 2),
            "gap_open_pct": round(gap_pct, 4),
            "gap_type": gap_type,
            "gap_filled": gap_filled,
            "day_range": round(day_range, 2),
            "close_position": close_pos,
            "candle_body": round(body, 2),
            "upper_wick": round(upper_wick, 2),
            "lower_wick": round(lower_wick, 2),
            "candle_type": candle,
            "dma_20": round(d20, 2) if d20 and not pd.isna(d20) else None,
            "dma_50": round(d50, 2) if d50 and not pd.isna(d50) else None,
            "dma_200": round(d200, 2) if d200 and not pd.isna(d200) else None,
            "vs_20dma_pct": round(vs_20, 4) if vs_20 is not None else None,
            "vs_50dma_pct": round(vs_50, 4) if vs_50 is not None else None,
            "vs_200dma_pct": round(vs_200, 4) if vs_200 is not None else None,
            "above_200dma": c > d200 if d200 and not pd.isna(d200) else None,
            "dist_52w_high_pct": round(dist_high, 4),
            "dist_52w_low_pct": round(dist_low, 4),
            "vol_vs_20d_avg": round(vol_ratio, 4),
            "day_shape": day_shape,
        })

    return pd.DataFrame(records)


# ═══════════════════════════════════════════════════════════
# STEP 4: Build daily_global_context
# ═══════════════════════════════════════════════════════════

def classify_us_tone(change_pct):
    """Classify US market direction."""
    if pd.isna(change_pct):
        return None
    if change_pct > US_STRONG_UP:
        return "strong_up"
    elif change_pct > US_MILD_UP:
        return "mild_up"
    elif change_pct > US_MILD_DOWN:
        return "flat"
    elif change_pct > US_STRONG_DOWN:
        return "mild_down"
    return "strong_down"


def classify_yield_regime(yield_val):
    """Classify US 10Y yield regime."""
    if pd.isna(yield_val) or yield_val is None:
        return None
    if yield_val < YIELD_COMFORTABLE:
        return "comfortable"
    elif yield_val < YIELD_WATCHING:
        return "watching"
    elif yield_val < YIELD_PRESSURE:
        return "pressure"
    return "alarm"


def classify_dxy_regime(dxy_val):
    """Classify DXY regime."""
    if pd.isna(dxy_val) or dxy_val is None:
        return None
    if dxy_val < DXY_WEAK:
        return "weak"
    elif dxy_val < DXY_NEUTRAL:
        return "neutral"
    elif dxy_val < DXY_STRONG:
        return "strong"
    return "extreme"


def classify_crude_regime(crude_val):
    """Classify Brent crude regime."""
    if pd.isna(crude_val) or crude_val is None:
        return None
    if crude_val < CRUDE_VERY_POSITIVE:
        return "very_positive"
    elif crude_val < CRUDE_COMFORTABLE:
        return "comfortable"
    elif crude_val < CRUDE_PRESSURE:
        return "pressure"
    return "alarm"


def classify_asia_tone(nikkei_chg, hangseng_chg):
    """Classify Asian market tone."""
    values = [v for v in [nikkei_chg, hangseng_chg] if v is not None and not pd.isna(v)]
    if not values:
        return None
    avg = sum(values) / len(values)
    if avg > 0.5:
        return "risk_on"
    elif avg < -0.5:
        return "risk_off"
    return "mixed"


def safe_get(data_dict, name, d, col="Close"):
    """Safely get a value from downloaded data."""
    if name not in data_dict:
        return None
    df = data_dict[name]
    if d not in df.index:
        return None
    val = df.loc[d, col] if col in df.columns else None
    if val is not None and pd.isna(val):
        return None
    return val


def safe_pct_change(data_dict, name, d, col="Close"):
    """Compute % change from previous day."""
    if name not in data_dict:
        return None
    df = data_dict[name]
    if d not in df.index:
        return None

    idx = list(df.index)
    pos = idx.index(d) if d in idx else -1
    if pos <= 0:
        return None

    curr = df.loc[d, col] if col in df.columns else None
    prev = df.loc[idx[pos - 1], col] if col in df.columns else None

    if curr is None or prev is None or pd.isna(curr) or pd.isna(prev) or prev == 0:
        return None
    return round((curr - prev) / prev * 100, 4)


def compute_global_context(
    yf_data: dict, fred_data: dict, trading_dates: list[date]
) -> pd.DataFrame:
    """
    Build daily_global_context for all trading days.
    """
    records = []

    for d in trading_dates:
        # US Markets
        sp500_close = safe_get(yf_data, "SP500", d)
        sp500_chg = safe_pct_change(yf_data, "SP500", d)
        nasdaq_close = safe_get(yf_data, "NASDAQ", d)
        nasdaq_chg = safe_pct_change(yf_data, "NASDAQ", d)
        dow_close = safe_get(yf_data, "DOW", d)
        dow_chg = safe_pct_change(yf_data, "DOW", d)
        us_vix = safe_get(yf_data, "US_VIX", d)
        us_vix_chg = safe_pct_change(yf_data, "US_VIX", d)

        # Use S&P change for US tone
        us_tone = classify_us_tone(sp500_chg)

        # Yields
        us_10y = fred_data.get("US_10Y", pd.Series(dtype=float))
        us_2y = fred_data.get("US_2Y", pd.Series(dtype=float))
        yield_10y = us_10y[d] if d in us_10y.index else None
        yield_2y = us_2y[d] if d in us_2y.index else None

        # 10Y change in bps
        yield_10y_prev = None
        if d in us_10y.index:
            idx = list(us_10y.index)
            pos = idx.index(d)
            if pos > 0:
                yield_10y_prev = us_10y[idx[pos - 1]]
        yield_10y_chg_bps = round((yield_10y - yield_10y_prev) * 100, 2) if yield_10y and yield_10y_prev else None

        yield_spread = round(yield_10y - yield_2y, 4) if yield_10y and yield_2y else None
        yield_regime = classify_yield_regime(yield_10y)

        # Currencies
        dxy = safe_get(yf_data, "DXY", d)
        dxy_chg = safe_pct_change(yf_data, "DXY", d)
        usdinr = safe_get(yf_data, "USDINR", d)
        usdinr_chg = safe_pct_change(yf_data, "USDINR", d)

        # USDINR 5-day trend
        usdinr_5d_trend = None
        if "USDINR" in yf_data and d in yf_data["USDINR"].index:
            idx = list(yf_data["USDINR"].index)
            pos = idx.index(d)
            if pos >= 5:
                rate_now = yf_data["USDINR"].loc[d, "Close"]
                rate_5d = yf_data["USDINR"].loc[idx[pos - 5], "Close"]
                if rate_now and rate_5d and not pd.isna(rate_now) and not pd.isna(rate_5d):
                    diff = rate_now - rate_5d
                    if diff > 0.2:
                        usdinr_5d_trend = "depreciating"
                    elif diff < -0.2:
                        usdinr_5d_trend = "appreciating"
                    else:
                        usdinr_5d_trend = "stable"

        # Commodities
        brent = safe_get(yf_data, "BRENT", d)
        brent_chg = safe_pct_change(yf_data, "BRENT", d)
        gold = safe_get(yf_data, "GOLD", d)
        gold_chg = safe_pct_change(yf_data, "GOLD", d)
        copper = safe_get(yf_data, "COPPER", d)
        copper_chg = safe_pct_change(yf_data, "COPPER", d)

        # Asian markets
        nikkei_chg = safe_pct_change(yf_data, "NIKKEI", d)
        hangseng_chg = safe_pct_change(yf_data, "HANGSENG", d)

        # European markets
        dax_chg = safe_pct_change(yf_data, "DAX", d)
        ftse_chg = safe_pct_change(yf_data, "FTSE", d)

        records.append({
            "trade_date": d,
            "sp500_close": round(sp500_close, 2) if sp500_close else None,
            "sp500_change_pct": sp500_chg,
            "nasdaq_close": round(nasdaq_close, 2) if nasdaq_close else None,
            "nasdaq_change_pct": nasdaq_chg,
            "dow_close": round(dow_close, 2) if dow_close else None,
            "dow_change_pct": dow_chg,
            "us_market_tone": us_tone,
            "us_vix_close": round(us_vix, 2) if us_vix else None,
            "us_vix_change_pct": us_vix_chg,
            "us_10y_yield": round(yield_10y, 4) if yield_10y and not pd.isna(yield_10y) else None,
            "us_10y_change_bps": yield_10y_chg_bps,
            "us_2y_yield": round(yield_2y, 4) if yield_2y and not pd.isna(yield_2y) else None,
            "yield_curve_spread": yield_spread,
            "yield_regime": yield_regime,
            "dxy_close": round(dxy, 3) if dxy else None,
            "dxy_change_pct": dxy_chg,
            "dxy_regime": classify_dxy_regime(dxy),
            "usdinr_close": round(usdinr, 4) if usdinr else None,
            "usdinr_change_pct": usdinr_chg,
            "usdinr_5d_trend": usdinr_5d_trend,
            "brent_close": round(brent, 2) if brent else None,
            "brent_change_pct": brent_chg,
            "crude_regime": classify_crude_regime(brent),
            "gold_close": round(gold, 2) if gold else None,
            "gold_change_pct": gold_chg,
            "copper_close": round(copper, 2) if copper else None,
            "copper_change_pct": copper_chg,
            "nikkei_change_pct": nikkei_chg,
            "hangseng_change_pct": hangseng_chg,
            "asia_tone": classify_asia_tone(nikkei_chg, hangseng_chg),
            "dax_change_pct": dax_chg,
            "ftse_change_pct": ftse_chg,
            "gift_nifty_gap": None,  # computed separately if data available
        })

    return pd.DataFrame(records)


# ═══════════════════════════════════════════════════════════
# STEP 5: Load into DuckDB
# ═══════════════════════════════════════════════════════════

def load_to_db(index_ohlc_df: pd.DataFrame, global_ctx_df: pd.DataFrame):
    """Load computed DataFrames into DuckDB."""
    con = duckdb.connect(str(DB_PATH))

    # Load daily_index_ohlc
    if not index_ohlc_df.empty:
        con.execute("DELETE FROM daily_index_ohlc")
        con.execute("INSERT INTO daily_index_ohlc SELECT * FROM index_ohlc_df")
        count = con.execute("SELECT COUNT(*) FROM daily_index_ohlc").fetchone()[0]
        logger.info(f"Loaded {count:,} rows into daily_index_ohlc")

        # Stats per index
        stats = con.execute("""
            SELECT index_name, COUNT(*) as days,
                   MIN(trade_date) as first, MAX(trade_date) as last,
                   ROUND(MIN(close_price),0) as min_close,
                   ROUND(MAX(close_price),0) as max_close
            FROM daily_index_ohlc GROUP BY index_name ORDER BY index_name
        """).fetchdf()
        print("\n  Index OHLC summary:")
        print(stats.to_string(index=False))

    # Load daily_global_context
    if not global_ctx_df.empty:
        con.execute("DELETE FROM daily_global_context")
        con.execute("INSERT INTO daily_global_context SELECT * FROM global_ctx_df")
        count = con.execute("SELECT COUNT(*) FROM daily_global_context").fetchone()[0]
        logger.info(f"Loaded {count:,} rows into daily_global_context")

        # Sample
        sample = con.execute("""
            SELECT trade_date, us_market_tone, crude_regime, dxy_regime, yield_regime
            FROM daily_global_context
            ORDER BY trade_date DESC LIMIT 5
        """).fetchdf()
        print("\n  Global context (last 5 days):")
        print(sample.to_string(index=False))

    con.close()


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def run_backfill(start: date = None, end: date = None):
    """Run the full Sprint 1 backfill."""
    start = start or HIST_START
    end = end or HIST_END

    print("=" * 60)
    print("  NEVAREP Sprint 1 — Global Market Data Fetcher")
    print("=" * 60)
    print(f"  Range: {start} to {end}")
    print(f"  Database: {DB_PATH}")
    print()

    # ── Get trading dates from DB ──
    con = duckdb.connect(str(DB_PATH))
    trading_dates = [
        row[0] for row in con.execute(
            "SELECT trade_date FROM trading_day WHERE is_trading_day ORDER BY trade_date"
        ).fetchall()
    ]
    con.close()

    if not trading_dates:
        print("ERROR: No trading days found in database.")
        print("Run setup.py first: python setup.py")
        sys.exit(1)

    print(f"  Trading days in DB: {len(trading_dates)}")
    print()

    # ── Step 1: Download Yahoo Finance ──
    print("[1/5] Downloading Yahoo Finance data...")
    yf_data = download_yf_data(start, end)

    # ── Step 2: Download FRED ──
    print("\n[2/5] Downloading FRED yield data...")
    fred_data = download_fred_data(start, end)

    # ── Step 3: Compute index OHLC with derived fields ──
    print("\n[3/5] Computing daily_index_ohlc (with 30+ derived fields)...")
    index_mapping = {
        "NIFTY50": "NIFTY50",
        "BANKNIFTY": "BANKNIFTY",
        "SENSEX": "SENSEX",
    }

    all_ohlc = []
    for yf_name, db_name in index_mapping.items():
        if yf_name in yf_data:
            logger.info(f"  Computing derived fields for {db_name}...")
            df = compute_index_ohlc(db_name, yf_data[yf_name])
            all_ohlc.append(df)
            print(f"  {db_name}: {len(df)} days processed")

    index_ohlc_df = pd.concat(all_ohlc, ignore_index=True) if all_ohlc else pd.DataFrame()

    # ── Step 4: Compute global context ──
    print("\n[4/5] Computing daily_global_context (US, yields, crude, DXY, Asia)...")
    global_ctx_df = compute_global_context(yf_data, fred_data, trading_dates)
    print(f"  Global context: {len(global_ctx_df)} days processed")

    # ── Step 5: Load into DuckDB ──
    print("\n[5/5] Loading into DuckDB...")
    load_to_db(index_ohlc_df, global_ctx_df)

    # ── Summary ──
    con = duckdb.connect(str(DB_PATH))
    ohlc_count = con.execute("SELECT COUNT(*) FROM daily_index_ohlc").fetchone()[0]
    ctx_count = con.execute("SELECT COUNT(*) FROM daily_global_context").fetchone()[0]
    con.close()

    print(f"\n{'=' * 60}")
    print(f"  Sprint 1 complete!")
    print(f"{'=' * 60}")
    print(f"  daily_index_ohlc:    {ohlc_count:,} rows (3 indices x ~800 days)")
    print(f"  daily_global_context: {ctx_count:,} rows")
    print(f"  Database: {DB_PATH}")
    print(f"  DB size:  {DB_PATH.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"\n  Next step: python -m scrapers.nse_fo_downloader")
    print(f"  (Sprint 2: NSE F&O bhav copy download)")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="NEVAREP Sprint 1 — Global Data Fetcher")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None

    run_backfill(start, end)


if __name__ == "__main__":
    main()