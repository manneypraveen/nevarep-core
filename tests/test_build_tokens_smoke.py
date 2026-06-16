"""
Smoke test for pipeline/build_tokens.py.
Runs on a tiny in-memory DuckDB with synthetic data for 5 trading days.
Does NOT require a real nevarep.duckdb — patches DB_PATH to a temp file.
"""

import sys
import math
import tempfile
import shutil
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import duckdb


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """
    Create a minimal DuckDB with the tables pipeline/build_tokens.py reads,
    seeded with 5 trading days of synthetic data. Patch DB_PATH to this file.
    """
    db_path = tmp_path / "test.duckdb"
    con = duckdb.connect(str(db_path))

    con.execute("""
        CREATE TABLE trading_day (
            trade_date DATE PRIMARY KEY,
            is_trading_day BOOLEAN
        )
    """)
    con.execute("""
        CREATE TABLE daily_global_context (
            trade_date DATE PRIMARY KEY,
            us_market_tone VARCHAR(15),
            crude_regime VARCHAR(15),
            dxy_regime VARCHAR(15),
            yield_regime VARCHAR(15),
            brent_change_pct DECIMAL(8,4),
            dxy_change_pct DECIMAL(8,4),
            us_10y_change_bps DECIMAL(8,2),
            asia_tone VARCHAR(15)
        )
    """)
    con.execute("""
        CREATE TABLE daily_options_snapshot (
            trade_date DATE,
            index_name VARCHAR(20),
            pcr_oi DECIMAL(8,4),
            india_vix_close DECIMAL(8,2),
            vix_regime VARCHAR(15),
            price_in_oi_range BOOLEAN,
            atm_iv DECIMAL(8,2),
            iv_skew DECIMAL(8,4),
            gex DECIMAL(18,2)
        )
    """)
    con.execute("""
        CREATE TABLE daily_institutional_flows (
            trade_date DATE PRIMARY KEY,
            fii_intensity VARCHAR(20),
            fii_streak_days INTEGER,
            dii_intensity VARCHAR(20)
        )
    """)
    con.execute("""
        CREATE TABLE daily_participant_oi (
            trade_date DATE,
            client_type VARCHAR(10),
            fut_idx_long BIGINT,
            fut_idx_short BIGINT
        )
    """)
    con.execute("""
        CREATE TABLE daily_index_ohlc (
            trade_date DATE,
            index_name VARCHAR(20),
            above_200dma BOOLEAN,
            vs_200dma_pct DECIMAL(8,4),
            gap_type VARCHAR(20),
            vol_vs_20d_avg DECIMAL(8,4),
            change_pct DECIMAL(8,4),
            candle_type VARCHAR(20)
        )
    """)
    con.execute("""
        CREATE TABLE daily_news_events (
            id INTEGER PRIMARY KEY,
            trade_date DATE,
            category VARCHAR(30),
            direction VARCHAR(10),
            severity VARCHAR(10)
        )
    """)
    con.execute("""
        CREATE TABLE daily_tokens (
            trade_date DATE,
            index_name VARCHAR(20),
            token VARCHAR(60),
            PRIMARY KEY (trade_date, index_name, token)
        )
    """)

    # Seed 5 trading days
    days = [date(2024, 1, d) for d in range(2, 7)]  # Jan 2-6 2024
    for i, d in enumerate(days):
        con.execute("INSERT INTO trading_day VALUES (?, TRUE)", [d])
        con.execute("""
            INSERT INTO daily_global_context VALUES (?,?,?,?,?,?,?,?,?)
        """, [d, "bearish", "neutral", "neutral", "neutral",
              -0.5 + i * 0.2, -0.1, -2.0, "neutral"])
        con.execute("""
            INSERT INTO daily_options_snapshot VALUES (?,?,?,?,?,?,?,?,?)
        """, [d, "NIFTY50", 1.1, 17.0, "normal", True, 14.5, 0.02, -2e8])
        con.execute("""
            INSERT INTO daily_institutional_flows VALUES (?,?,?,?)
        """, [d, "selling", 2, "buying"])
        con.execute("""
            INSERT INTO daily_participant_oi VALUES (?,?,?,?)
        """, [d, "FII", 100000, 120000])
        con.execute("""
            INSERT INTO daily_index_ohlc VALUES (?,?,?,?,?,?,?,?)
        """, [d, "NIFTY50", True, 2.5, "none", 1.1, -0.3 + i * 0.15, "doji"])
        if i < 3:
            con.execute("""
                INSERT INTO daily_news_events VALUES (?,?,?,?,?)
            """, [i + 1, d, "us_fed", "BEARISH", "med"])

    con.close()

    # Patch DB_PATH
    import config.settings as cfg
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    import pipeline.build_tokens as bt
    monkeypatch.setattr(bt, "DB_PATH", db_path)

    return db_path, days


def test_build_tokens_runs_without_error(temp_db):
    """build_tokens should complete without raising, even with minimal data."""
    db_path, days = temp_db
    from pipeline.build_tokens import build_tokens

    # Should not raise
    build_tokens(
        start=days[0],
        end=days[-1],
        instruments=["NIFTY50"],
    )


def test_tokens_written_to_db(temp_db):
    """After build_tokens, daily_tokens should have rows for NIFTY50."""
    db_path, days = temp_db
    from pipeline.build_tokens import build_tokens

    build_tokens(start=days[0], end=days[-1], instruments=["NIFTY50"])

    con = duckdb.connect(str(db_path))
    count = con.execute(
        "SELECT COUNT(*) FROM daily_tokens WHERE index_name='NIFTY50'"
    ).fetchone()[0]
    con.close()

    assert count > 0, "Expected token rows to be written to daily_tokens"


def test_tokens_are_strings(temp_db):
    """All token values should be non-empty strings."""
    db_path, days = temp_db
    from pipeline.build_tokens import build_tokens

    build_tokens(start=days[0], end=days[-1], instruments=["NIFTY50"])

    con = duckdb.connect(str(db_path))
    tokens = [r[0] for r in con.execute(
        "SELECT DISTINCT token FROM daily_tokens"
    ).fetchall()]
    con.close()

    assert all(isinstance(t, str) and len(t) > 0 for t in tokens)


def test_crudeoil_tokens_written(temp_db):
    """build_tokens for CRUDEOIL should produce tokens (crude momentum etc.)."""
    db_path, days = temp_db
    from pipeline.build_tokens import build_tokens

    build_tokens(start=days[0], end=days[-1], instruments=["CRUDEOIL"])

    con = duckdb.connect(str(db_path))
    count = con.execute(
        "SELECT COUNT(*) FROM daily_tokens WHERE index_name='CRUDEOIL'"
    ).fetchone()[0]
    con.close()

    assert count > 0
