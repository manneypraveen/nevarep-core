"""
NEVAREP — Black-Scholes Greeks & IV Engine (analysis/greeks.py)

NSE index options (NIFTY50, BANKNIFTY) are European-style,
so Black-Scholes is the correct pricing model.

Public API:
    bs_price(S, K, T, r, sigma, opt_type)  -> theoretical price
    greeks(S, K, T, r, sigma, opt_type)    -> dict of delta/gamma/theta/vega/rho
    implied_vol(market_price, S, K, T, r, opt_type, *, tol, max_iter) -> IV or None
    gamma_exposure(S, K, T, r, sigma, oi, lot_size) -> GEX contribution ($ gamma)

Instruments in scope: NIFTY50, BANKNIFTY
CRUDEOIL: no MCX options feed available.
# TODO: MCX crude options feed for CRUDEOIL greeks
"""

from __future__ import annotations

import math
from typing import Literal

OptType = Literal["CE", "PE"]

_SQRT_2PI = math.sqrt(2 * math.pi)
_MIN_T = 1 / (252 * 8)   # ~1 hour floor to avoid T→0 singularity
_MIN_SIGMA = 0.001
_MAX_SIGMA = 10.0         # 1000% IV cap — avoids runaway bisection


# ─── Normal distribution helpers ────────────────────────────────────────────

def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


# ─── Black-Scholes core ─────────────────────────────────────────────────────

def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    T = max(T, _MIN_T)
    sigma = max(sigma, _MIN_SIGMA)
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return d1, d2


def bs_price(S: float, K: float, T: float, r: float, sigma: float, opt_type: OptType) -> float:
    """
    Black-Scholes European option price.

    S         spot price
    K         strike price
    T         time to expiry in years (must be > 0)
    r         risk-free rate (annualised decimal, e.g. 0.065)
    sigma     volatility (annualised decimal, e.g. 0.18 = 18%)
    opt_type  "CE" (call) or "PE" (put)
    """
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    discount = math.exp(-r * T)
    if opt_type == "CE":
        return S * _norm_cdf(d1) - K * discount * _norm_cdf(d2)
    else:
        return K * discount * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def greeks(S: float, K: float, T: float, r: float, sigma: float, opt_type: OptType) -> dict:
    """
    Return Black-Scholes greeks for a European option.

    Returns dict with keys: delta, gamma, theta, vega, rho.
    Theta is per calendar day (divided by 365).
    Vega is per 1% move in vol (divided by 100).
    """
    T = max(T, _MIN_T)
    sigma = max(sigma, _MIN_SIGMA)
    sqrt_T = math.sqrt(T)
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    pdf_d1 = _norm_pdf(d1)
    discount = math.exp(-r * T)

    gamma = pdf_d1 / (S * sigma * sqrt_T)
    vega = S * pdf_d1 * sqrt_T / 100.0   # per 1% vol move

    if opt_type == "CE":
        delta = _norm_cdf(d1)
        theta = (
            -(S * pdf_d1 * sigma) / (2 * sqrt_T)
            - r * K * discount * _norm_cdf(d2)
        ) / 365.0
        rho = K * T * discount * _norm_cdf(d2) / 100.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (
            -(S * pdf_d1 * sigma) / (2 * sqrt_T)
            + r * K * discount * _norm_cdf(-d2)
        ) / 365.0
        rho = -K * T * discount * _norm_cdf(-d2) / 100.0

    return {
        "delta": round(delta, 6),
        "gamma": round(gamma, 8),
        "theta": round(theta, 6),
        "vega": round(vega, 6),
        "rho": round(rho, 6),
    }


def implied_vol(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    opt_type: OptType,
    *,
    tol: float = 1e-5,
    max_iter: int = 100,
) -> float | None:
    """
    Implied volatility via Brent's method (bisection fallback).

    Returns annualised IV as a decimal (e.g. 0.18 = 18%), or None if
    the market price is outside the no-arbitrage bounds or convergence fails.
    """
    if T <= 0 or S <= 0 or K <= 0:
        return None

    # No-arbitrage bounds
    intrinsic = max(0.0, S - K) if opt_type == "CE" else max(0.0, K - S)
    upper_bound = S if opt_type == "CE" else K * math.exp(-r * T)
    if market_price <= intrinsic or market_price >= upper_bound:
        return None

    def f(sigma):
        return bs_price(S, K, T, r, sigma, opt_type) - market_price

    lo, hi = _MIN_SIGMA, _MAX_SIGMA
    if f(lo) * f(hi) > 0:
        return None

    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        fmid = f(mid)
        if abs(fmid) < tol or (hi - lo) / 2.0 < tol:
            return round(mid, 6)
        if f(lo) * fmid < 0:
            hi = mid
        else:
            lo = mid

    return None


def gamma_exposure(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    open_interest: float,
    lot_size: int,
    opt_type: OptType,
) -> float:
    """
    Net Gamma Exposure (GEX) contribution of one strike/expiry.

    GEX = gamma * OI * lot_size * S^2 / 100
    Sign convention: calls add positive GEX, puts add negative GEX.
    (Dealers are typically short calls/long puts → negative net GEX = volatility amplifier.)

    Returns GEX in index-point² units (scale by notional for ₹ GEX).
    """
    g = greeks(S, K, T, r, sigma, opt_type)
    gex = g["gamma"] * open_interest * lot_size * S * S / 100.0
    return gex if opt_type == "CE" else -gex
