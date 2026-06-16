"""Tests for analysis/tokenizer.py."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from analysis.tokenizer import (
    tokenize_day, crude_tokens, news_reaction_token,
    _pcr_token, _vix_token, _atm_iv_token, _iv_skew_token,
    _gap_token, _volume_token,
)


# ── Bucket helpers ────────────────────────────────────────────────────────────

class TestBuckets:
    def test_pcr_very_low(self):
        assert _pcr_token(0.5) == "PCR_VERY_LOW"

    def test_pcr_neutral(self):
        assert _pcr_token(1.0) == "PCR_NEUTRAL"

    def test_pcr_high(self):
        assert _pcr_token(1.2) == "PCR_HIGH"

    def test_pcr_none_returns_none(self):
        assert _pcr_token(None) is None

    def test_vix_low(self):
        assert _vix_token(11) == "VIX_LOW"

    def test_vix_extreme(self):
        assert _vix_token(35) == "VIX_EXTREME"

    def test_atm_iv_normal(self):
        assert _atm_iv_token(17.0) == "IV_NORMAL"

    def test_iv_skew_put_heavy(self):
        assert _iv_skew_token(0.05) == "SKEW_PUT_HEAVY"

    def test_iv_skew_flat(self):
        assert _iv_skew_token(0.01) == "SKEW_FLAT"

    def test_iv_skew_call_heavy(self):
        assert _iv_skew_token(-0.03) == "SKEW_CALL_HEAVY"

    def test_gap_down(self):
        assert _gap_token("gap_down") == "GAP_DOWN"

    def test_gap_up_filled(self):
        assert _gap_token("gap_up_filled") == "GAP_UP_FILLED"

    def test_gap_none_returns_none(self):
        assert _gap_token("none") is None

    def test_vol_surge(self):
        assert _volume_token(2.0) == "VOL_SURGE"


# ── tokenize_day ─────────────────────────────────────────────────────────────

_BASE_STATE = {
    "pcr": 1.15,
    "vix": 18.0,
    "atm_iv": 14.5,
    "iv_skew": 0.03,
    "price_in_range": True,
    "gex": -3e8,
    "fii_intensity": "selling",
    "fii_streak_days": 4,
    "fii_ls_ratio": 0.7,
    "above_200dma": True,
    "vs_200dma_pct": 3.0,
    "gap_type": "gap_down",
    "vol_ratio": 1.3,
    "us_tone": "bearish",
    "crude_regime": "neutral",
    "dxy_regime": "neutral",
    "yield_regime": "neutral",
    "triple_threat": False,
    "crude_change_pct": 0.5,
    "dominant_news_cat": "us_fed",
    "news_direction": "BEARISH",
    "has_critical_news": False,
}


class TestTokenizeDay:
    def test_returns_list(self):
        toks = tokenize_day(_BASE_STATE, "NIFTY50")
        assert isinstance(toks, list)

    def test_no_none_in_output(self):
        toks = tokenize_day(_BASE_STATE, "NIFTY50")
        assert all(t is not None for t in toks)

    def test_no_empty_strings(self):
        toks = tokenize_day(_BASE_STATE, "NIFTY50")
        assert all(len(t) > 0 for t in toks)

    def test_pcr_token_present(self):
        toks = tokenize_day(_BASE_STATE, "NIFTY50")
        assert "PCR_HIGH" in toks

    def test_vix_token_present(self):
        toks = tokenize_day(_BASE_STATE, "NIFTY50")
        assert "VIX_ELEVATED" in toks

    def test_triple_threat_token(self):
        state = {**_BASE_STATE, "triple_threat": True,
                 "crude_regime": "pressure", "dxy_regime": "strong", "yield_regime": "pressure"}
        toks = tokenize_day(state, "NIFTY50")
        assert "TRIPLE_THREAT" in toks

    def test_no_triple_threat_when_false(self):
        toks = tokenize_day(_BASE_STATE, "NIFTY50")
        assert "TRIPLE_THREAT" not in toks

    def test_fii_streak_medium(self):
        toks = tokenize_day(_BASE_STATE, "NIFTY50")
        assert "FII_STREAK_MEDIUM" in toks

    def test_news_category_token(self):
        toks = tokenize_day(_BASE_STATE, "NIFTY50")
        assert "NEWS_US_FED" in toks

    def test_crudeoil_delegates_to_crude_tokens(self):
        crude_state = {"crude_change_pct": 2.0, "dxy_regime": "neutral"}
        toks_instrument = tokenize_day(crude_state, "CRUDEOIL")
        toks_crude = crude_tokens(crude_state)
        assert set(toks_instrument) == set(toks_crude)

    def test_banknifty_options_prefixed(self):
        toks = tokenize_day(_BASE_STATE, "BANKNIFTY")
        pcr_toks = [t for t in toks if t.startswith("BN_PCR_")]
        assert len(pcr_toks) > 0

    def test_empty_state_returns_list(self):
        """No crash on empty / all-None state."""
        toks = tokenize_day({}, "NIFTY50")
        assert isinstance(toks, list)

    def test_deduplication_in_pipeline(self):
        """tokenize_day should not return duplicates itself (no duplicate tokens)."""
        toks = tokenize_day(_BASE_STATE, "NIFTY50")
        assert len(toks) == len(set(toks))


# ── crude_tokens ─────────────────────────────────────────────────────────────

class TestCrudeTokens:
    def test_strong_rise(self):
        toks = crude_tokens({"crude_change_pct": 4.0})
        assert "CRUDE_STRONG_RISE" in toks

    def test_contango(self):
        toks = crude_tokens({"crude_futures_basis": -1.5})
        assert "CRUDE_CONTANGO" in toks

    def test_backwardation(self):
        toks = crude_tokens({"crude_futures_basis": 1.0})
        assert "CRUDE_BACKWARDATION" in toks

    def test_inventory_draw(self):
        toks = crude_tokens({"inventory_surprise": -3.0})
        assert "CRUDE_INVENTORY_DRAW" in toks

    def test_dxy_headwind(self):
        toks = crude_tokens({"dxy_regime": "strong"})
        assert "CRUDE_DXY_HEADWIND" in toks

    def test_bear_momentum(self):
        toks = crude_tokens({"crude_3d_return": -5.0})
        assert "CRUDE_BEAR_MOMENTUM" in toks

    def test_empty_state_no_crash(self):
        toks = crude_tokens({})
        assert isinstance(toks, list)


# ── news_reaction_token ───────────────────────────────────────────────────────

class TestNewsReactionToken:
    def test_confirmed_bull(self):
        tok = news_reaction_token("rbi_policy", 0.8, "BULLISH")
        assert tok == "RBI_POLICY->CONFIRMED_BULL"

    def test_confirmed_bear(self):
        tok = news_reaction_token("us_fed", -0.5, "BEARISH")
        assert tok == "US_FED->CONFIRMED_BEAR"

    def test_faded(self):
        tok = news_reaction_token("us_fed", 0.1, "BEARISH")
        assert tok == "US_FED->FADED"

    def test_reversed(self):
        tok = news_reaction_token("crude_commodity", 0.7, "BEARISH")
        assert tok == "CRUDE_COMMODITY->REVERSED_BULL"

    def test_none_fwd_return(self):
        assert news_reaction_token("rbi_policy", None, "BULLISH") is None

    def test_category_uppercased(self):
        tok = news_reaction_token("rbi_policy", 0.5, "bullish")
        assert tok.startswith("RBI_POLICY->")
