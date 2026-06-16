"""Tests for analysis/greeks.py — Black-Scholes Greeks + IV round-trip."""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from analysis.greeks import bs_price, greeks, implied_vol, gamma_exposure


# ── bs_price ──────────────────────────────────────────────────────────────────

class TestBsPrice:
    def test_atm_call_positive(self):
        """ATM call must have positive time value."""
        p = bs_price(22500, 22500, 30/365, 0.065, 0.14, "CE")
        assert p > 0

    def test_atm_put_positive(self):
        p = bs_price(22500, 22500, 30/365, 0.065, 0.14, "PE")
        assert p > 0

    def test_call_put_parity(self):
        """Put-call parity: C - P = S - K*e^(-rT)"""
        S, K, T, r, sigma = 22500, 22500, 30/365, 0.065, 0.14
        c = bs_price(S, K, T, r, sigma, "CE")
        p = bs_price(S, K, T, r, sigma, "PE")
        parity = S - K * math.exp(-r * T)
        assert abs((c - p) - parity) < 0.01

    def test_deep_itm_call_approaches_intrinsic(self):
        """Very deep ITM call ≈ S - K*e^(-rT)."""
        S, K, T, r, sigma = 25000, 20000, 7/365, 0.065, 0.14
        price = bs_price(S, K, T, r, sigma, "CE")
        intrinsic = S - K * math.exp(-r * T)
        assert abs(price - intrinsic) < 5

    def test_deep_otm_call_near_zero(self):
        price = bs_price(22500, 28000, 7/365, 0.065, 0.14, "CE")
        assert price < 1.0

    def test_banknifty_scale(self):
        """BankNifty at higher price scale should still price correctly."""
        p = bs_price(48000, 48000, 30/365, 0.065, 0.17, "CE")
        assert p > 0


# ── implied_vol ───────────────────────────────────────────────────────────────

class TestImpliedVol:
    def _round_trip(self, S, K, T, r, sigma, opt_type, tol=1e-4):
        price = bs_price(S, K, T, r, sigma, opt_type)
        recovered = implied_vol(price, S, K, T, r, opt_type)
        assert recovered is not None, f"IV solve failed for {opt_type} sigma={sigma}"
        assert abs(recovered - sigma) < tol, f"Got {recovered}, expected {sigma}"

    def test_atm_call_round_trip(self):
        self._round_trip(22500, 22500, 30/365, 0.065, 0.14, "CE")

    def test_atm_put_round_trip(self):
        self._round_trip(22500, 22500, 30/365, 0.065, 0.14, "PE")

    def test_otm_call_round_trip(self):
        self._round_trip(22500, 23000, 30/365, 0.065, 0.18, "CE")

    def test_itm_put_round_trip(self):
        self._round_trip(22500, 23000, 30/365, 0.065, 0.18, "PE")

    def test_high_vol_round_trip(self):
        self._round_trip(22500, 22500, 30/365, 0.065, 0.35, "CE")

    def test_low_vol_round_trip(self):
        self._round_trip(22500, 22500, 30/365, 0.065, 0.08, "CE")

    def test_zero_price_returns_none(self):
        assert implied_vol(0.0, 22500, 22500, 30/365, 0.065, "CE") is None

    def test_negative_T_returns_none(self):
        assert implied_vol(100.0, 22500, 22500, -1.0, 0.065, "CE") is None

    def test_intrinsic_only_returns_none(self):
        # Price exactly equals intrinsic → no time value → can't solve IV
        intrinsic = max(0, 22500 - 22000)
        assert implied_vol(float(intrinsic), 22500, 22000, 7/365, 0.065, "CE") is None


# ── greeks ────────────────────────────────────────────────────────────────────

class TestGreeks:
    def setup_method(self):
        self.g = greeks(22500, 22500, 30/365, 0.065, 0.14, "CE")
        self.gp = greeks(22500, 22500, 30/365, 0.065, 0.14, "PE")

    def test_call_delta_between_0_and_1(self):
        assert 0 < self.g["delta"] < 1

    def test_put_delta_between_minus1_and_0(self):
        assert -1 < self.gp["delta"] < 0

    def test_atm_call_delta_near_half(self):
        assert abs(self.g["delta"] - 0.5) < 0.1

    def test_call_put_delta_sum_near_1(self):
        """For same strike: call_delta - put_delta ≈ 1 (ignoring discount)."""
        assert abs(self.g["delta"] - self.gp["delta"] - 1.0) < 0.01

    def test_gamma_positive(self):
        assert self.g["gamma"] > 0
        assert self.gp["gamma"] > 0

    def test_call_put_gamma_equal(self):
        assert abs(self.g["gamma"] - self.gp["gamma"]) < 1e-8

    def test_vega_positive(self):
        assert self.g["vega"] > 0
        assert self.gp["vega"] > 0

    def test_theta_negative(self):
        """Time value erodes → theta must be negative."""
        assert self.g["theta"] < 0
        assert self.gp["theta"] < 0


# ── gamma_exposure ────────────────────────────────────────────────────────────

class TestGammaExposure:
    def test_call_gex_positive(self):
        gex = gamma_exposure(22500, 22500, 30/365, 0.065, 0.14, 1000, 75, "CE")
        assert gex > 0

    def test_put_gex_negative(self):
        gex = gamma_exposure(22500, 22500, 30/365, 0.065, 0.14, 1000, 75, "PE")
        assert gex < 0

    def test_gex_scales_with_oi(self):
        g1 = gamma_exposure(22500, 22500, 30/365, 0.065, 0.14, 1000, 75, "CE")
        g2 = gamma_exposure(22500, 22500, 30/365, 0.065, 0.14, 2000, 75, "CE")
        assert abs(g2 / g1 - 2.0) < 0.001
