"""
NEVAREP — Day Tokenizer (analysis/tokenizer.py)

Encodes a trading day into symbolic tokens (a bag-of-strings) for
the similarity engine and predictor.

Instruments:
  NIFTY50   — full options chain tokens + macro/FII/news tokens
  BANKNIFTY — same (banking-specific labels where relevant)
  CRUDEOIL  — crude_tokens() only (no MCX options) + price/momentum tokens
              # TODO: MCX crude options feed for CRUDEOIL option-chain tokens

IMPORTANT: Bucket boundaries below are initial guesses.
Tune them on in-sample training folds only — never on test/validation data.

Public API:
    tokenize_day(state, instrument)  -> list[str]
    crude_tokens(state)              -> list[str]
    news_reaction_token(category, fwd_return, expected_direction) -> str | None
"""

from __future__ import annotations

from typing import Literal

Instrument = Literal["NIFTY50", "BANKNIFTY", "CRUDEOIL"]

# ─── Bucket helpers ─────────────────────────────────────────────────────────

def _bucket(value: float | None, thresholds: list[float], labels: list[str]) -> str | None:
    """
    Map value into a label bucket.
    thresholds: N breakpoints → N+1 labels (labels has len(thresholds)+1 elements).
    Returns None if value is None.
    """
    if value is None:
        return None
    for t, label in zip(thresholds, labels[:-1]):
        if value < t:
            return label
    return labels[-1]


# ─── Options-chain tokens (NIFTY50 / BANKNIFTY) ─────────────────────────────

def _pcr_token(pcr: float | None) -> str | None:
    return _bucket(pcr, [0.7, 0.9, 1.1, 1.3],
                   ["PCR_VERY_LOW", "PCR_LOW", "PCR_NEUTRAL", "PCR_HIGH", "PCR_VERY_HIGH"])


def _vix_token(vix: float | None) -> str | None:
    # India VIX
    return _bucket(vix, [13, 16, 22, 28],
                   ["VIX_LOW", "VIX_NORMAL", "VIX_ELEVATED", "VIX_HIGH", "VIX_EXTREME"])


def _atm_iv_token(atm_iv_pct: float | None) -> str | None:
    # atm_iv stored as annualised % (e.g. 14.5 = 14.5%)
    return _bucket(atm_iv_pct, [10, 15, 20, 25],
                   ["IV_VERY_LOW", "IV_LOW", "IV_NORMAL", "IV_HIGH", "IV_EXTREME"])


def _iv_skew_token(iv_skew: float | None) -> str | None:
    # Positive skew = put IV > call IV (normal/risk-off)
    if iv_skew is None:
        return None
    if iv_skew > 0.02:
        return "SKEW_PUT_HEAVY"
    elif iv_skew < -0.02:
        return "SKEW_CALL_HEAVY"
    return "SKEW_FLAT"


def _oi_quadrant_token(price_in_range: bool | None, pcr: float | None) -> str | None:
    """OI × Price quadrant token."""
    if price_in_range is None or pcr is None:
        return None
    if price_in_range:
        if pcr >= 1.0:
            return "OI_QUAD_IN_RANGE_BULLISH"
        return "OI_QUAD_IN_RANGE_BEARISH"
    else:
        if pcr >= 1.0:
            return "OI_QUAD_ABOVE_RANGE_BULLISH"
        return "OI_QUAD_BELOW_RANGE_BEARISH"


def _gex_token(gex: float | None) -> str | None:
    # GEX in index-point² units; negative GEX = vol amplifier (dealer short gamma)
    return _bucket(gex, [-1e9, -2e8, 0, 2e8, 1e9],
                   ["GEX_DEEPLY_NEGATIVE", "GEX_NEGATIVE", "GEX_NEUTRAL",
                    "GEX_POSITIVE", "GEX_STRONGLY_POSITIVE"])


# ─── FII / institutional tokens ─────────────────────────────────────────────

def _fii_flow_token(fii_intensity: str | None, streak_days: int | None) -> list[str]:
    tokens = []
    if fii_intensity:
        tokens.append(f"FII_{fii_intensity.upper()}")
    if streak_days is not None:
        if streak_days >= 5:
            tokens.append("FII_STREAK_LONG")
        elif streak_days >= 3:
            tokens.append("FII_STREAK_MEDIUM")
        elif streak_days != 0:
            tokens.append("FII_STREAK_SHORT")
    return tokens


def _fii_ls_token(ls_ratio: float | None) -> str | None:
    return _bucket(ls_ratio, [0.5, 0.8, 1.0, 1.5],
                   ["FII_LS_VERY_BEARISH", "FII_LS_BEARISH", "FII_LS_NEUTRAL",
                    "FII_LS_BULLISH", "FII_LS_VERY_BULLISH"])


# ─── Technical tokens ────────────────────────────────────────────────────────

def _dma_token(above_200dma: bool | None, vs_200dma_pct: float | None) -> list[str]:
    tokens = []
    if above_200dma is True:
        tokens.append("ABOVE_200DMA")
    elif above_200dma is False:
        tokens.append("BELOW_200DMA")
    dist = _bucket(vs_200dma_pct, [-5, -2, 0, 2, 5],
                   ["DMA_FAR_BELOW", "DMA_BELOW", "DMA_NEAR_BELOW",
                    "DMA_NEAR_ABOVE", "DMA_ABOVE", "DMA_FAR_ABOVE"])
    if dist:
        tokens.append(dist)
    return tokens


def _gap_token(gap_type: str | None) -> str | None:
    if not gap_type or gap_type in ("none", "no_gap"):
        return None
    # gap_type values: gap_up, gap_down, gap_up_filled, gap_down_filled
    clean = gap_type.upper().replace("GAP_", "")
    return f"GAP_{clean}"


def _volume_token(vol_ratio: float | None) -> str | None:
    return _bucket(vol_ratio, [0.7, 0.9, 1.1, 1.5],
                   ["VOL_VERY_LOW", "VOL_LOW", "VOL_NORMAL", "VOL_HIGH", "VOL_SURGE"])


# ─── Macro tokens ────────────────────────────────────────────────────────────

def _macro_tokens(state: dict) -> list[str]:
    tokens = []
    us_tone = state.get("us_tone")
    if us_tone:
        tokens.append(f"US_{us_tone.upper()}")
    crude_regime = state.get("crude_regime")
    if crude_regime:
        tokens.append(f"CRUDE_{crude_regime.upper()}")
    dxy_regime = state.get("dxy_regime")
    if dxy_regime:
        tokens.append(f"DXY_{dxy_regime.upper()}")
    yield_regime = state.get("yield_regime")
    if yield_regime:
        tokens.append(f"YIELD_{yield_regime.upper()}")
    if state.get("triple_threat"):
        tokens.append("TRIPLE_THREAT")
    asia_tone = state.get("asia_tone")
    if asia_tone:
        tokens.append(f"ASIA_{asia_tone.upper()}")
    return tokens


# ─── Crude-specific tokens ───────────────────────────────────────────────────

def crude_tokens(state: dict) -> list[str]:
    """
    Tokens for CRUDEOIL instrument.
    # TODO: MCX crude options feed for option-chain tokens (contango/backwardation
    #       from futures curve, MCX option PCR, crude IV)

    Currently uses price momentum and global supply/demand signals.
    """
    tokens = []

    # Price momentum (crude change %)
    change = state.get("crude_change_pct")
    t = _bucket(change, [-3, -1, 1, 3],
                ["CRUDE_STRONG_FALL", "CRUDE_FALL", "CRUDE_FLAT",
                 "CRUDE_RISE", "CRUDE_STRONG_RISE"])
    if t:
        tokens.append(t)

    # Supply/demand signals
    if state.get("opec_event"):
        tokens.append("CRUDE_OPEC_EVENT")
    if state.get("inventory_surprise"):
        surprise = state["inventory_surprise"]
        if surprise > 0:
            tokens.append("CRUDE_INVENTORY_BUILD")    # bearish for crude
        else:
            tokens.append("CRUDE_INVENTORY_DRAW")     # bullish for crude

    # Futures curve shape (if available)
    basis = state.get("crude_futures_basis")  # spot - near futures
    if basis is not None:
        if basis > 0.5:
            tokens.append("CRUDE_BACKWARDATION")      # bullish signal
        elif basis < -0.5:
            tokens.append("CRUDE_CONTANGO")           # bearish signal

    # DXY impact: strong dollar → crude pressure
    dxy = state.get("dxy_regime")
    if dxy in ("strong", "extreme"):
        tokens.append("CRUDE_DXY_HEADWIND")

    # 3-day momentum
    mom3 = state.get("crude_3d_return")
    if mom3 is not None:
        if mom3 > 3:
            tokens.append("CRUDE_BULL_MOMENTUM")
        elif mom3 < -3:
            tokens.append("CRUDE_BEAR_MOMENTUM")

    return tokens


# ─── News-reaction token ─────────────────────────────────────────────────────

def news_reaction_token(
    category: str,
    fwd_return: float | None,
    expected_direction: str | None,
) -> str | None:
    """
    Produce an EVENT→REACTION token linking a news category to the next-day outcome.
    Returns None if fwd_return is unavailable.

    Format: "{CATEGORY}->CONFIRMED_BULL" / "->CONFIRMED_BEAR" / "->FADED"
    """
    if fwd_return is None:
        return None

    cat = (category or "").upper()
    expected = (expected_direction or "").upper()

    if fwd_return > 0.3:
        actual = "BULL"
    elif fwd_return < -0.3:
        actual = "BEAR"
    else:
        actual = "FLAT"

    if expected in ("BULLISH", "BULL") and actual == "BULL":
        label = "CONFIRMED_BULL"
    elif expected in ("BEARISH", "BEAR") and actual == "BEAR":
        label = "CONFIRMED_BEAR"
    elif actual == "FLAT":
        label = "FADED"
    else:
        label = f"REVERSED_{actual}"

    return f"{cat}->{label}"


# ─── Main tokeniser ──────────────────────────────────────────────────────────

def tokenize_day(state: dict, instrument: Instrument = "NIFTY50") -> list[str]:
    """
    Encode a trading day state into a bag of symbolic tokens.

    `state` dict keys (all optional — missing keys → token omitted):
        # Options / IV
        pcr, vix, atm_iv, iv_skew, price_in_range, gex
        # FII
        fii_intensity, fii_streak_days, fii_ls_ratio
        # Technical
        above_200dma, vs_200dma_pct, gap_type, vol_ratio
        # Macro (also used for Nifty CRUDE_ prefix tokens)
        us_tone, crude_regime, dxy_regime, yield_regime, triple_threat, asia_tone
        # Crude cross-instrument channel (for NIFTY50 row, prefixed CRUDE_)
        crude_change_pct, crude_3d_return
        # News summary
        dominant_news_cat, news_direction, has_critical_news

    For CRUDEOIL instrument, delegates to crude_tokens() only.
    """
    if instrument == "CRUDEOIL":
        return crude_tokens(state)

    tokens: list[str] = []

    # ── Options chain ──
    for t in [
        _pcr_token(state.get("pcr")),
        _vix_token(state.get("vix")),
        _atm_iv_token(state.get("atm_iv")),
        _iv_skew_token(state.get("iv_skew")),
        _oi_quadrant_token(state.get("price_in_range"), state.get("pcr")),
        _gex_token(state.get("gex")),
    ]:
        if t:
            tokens.append(t)

    # ── FII ──
    tokens.extend(_fii_flow_token(
        state.get("fii_intensity"), state.get("fii_streak_days")
    ))
    t = _fii_ls_token(state.get("fii_ls_ratio"))
    if t:
        tokens.append(t)

    # ── Technical ──
    tokens.extend(_dma_token(state.get("above_200dma"), state.get("vs_200dma_pct")))
    t = _gap_token(state.get("gap_type"))
    if t:
        tokens.append(t)
    t = _volume_token(state.get("vol_ratio"))
    if t:
        tokens.append(t)

    # ── Macro ──
    tokens.extend(_macro_tokens(state))

    # ── Crude cross-instrument channel (importer-cost channel for Nifty) ──
    crude_chg = state.get("crude_change_pct")
    if crude_chg is not None:
        ct = _bucket(crude_chg, [-3, -1, 1, 3],
                     ["CRUDE_STRONG_FALL", "CRUDE_FALL", "CRUDE_FLAT",
                      "CRUDE_RISE", "CRUDE_STRONG_RISE"])
        if ct:
            tokens.append(ct)
    crude_mom = state.get("crude_3d_return")
    if crude_mom is not None:
        if crude_mom > 3:
            tokens.append("CRUDE_BULL_MOMENTUM")
        elif crude_mom < -3:
            tokens.append("CRUDE_BEAR_MOMENTUM")

    # ── News ──
    news_cat = state.get("dominant_news_cat")
    if news_cat:
        tokens.append(f"NEWS_{news_cat.upper()}")
    news_dir = state.get("news_direction")
    if news_dir:
        tokens.append(f"NEWS_{news_dir.upper()}")
    if state.get("has_critical_news"):
        tokens.append("NEWS_CRITICAL")

    # BankNifty-specific label on some tokens
    if instrument == "BANKNIFTY":
        tokens = [f"BN_{t}" if t.startswith(("PCR_", "OI_QUAD_", "IV_", "GEX_")) else t
                  for t in tokens]

    return [t for t in tokens if t]  # drop any None that slipped through
