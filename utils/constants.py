"""
NEVAREP constants — enums and fixed values used across modules.
"""

# ── News categories (used by LLM structurer) ──
NEWS_CATEGORIES = [
    "rbi_policy",
    "government_fiscal",
    "sebi_regulatory",
    "earnings_corporate",
    "fii_dii_activity",
    "geopolitical",
    "us_fed",
    "global_central_bank",
    "crude_commodity",
    "currency",
    "sectoral",
    "technical_structural",
    "domestic_macro",
    "sentiment_flow",
    "black_swan",
]

# ── Severity levels ──
SEVERITY_LEVELS = ["low", "medium", "high", "critical"]

# ── Impact directions ──
IMPACT_DIRECTIONS = ["bullish", "bearish", "neutral"]

# ── Day types (assigned by LLM narrative) ──
DAY_TYPES = [
    "trending_bull",
    "trending_bear",
    "range_bound",
    "volatile_reversal",
    "gap_and_go",
    "gap_and_fade",
    "event_driven",
    "squeeze",
    "capitulation",
    "distribution",
    "accumulation",
    "dead_cat_bounce",
    "short_covering",
]

# ── Pattern tags ──
PATTERN_TAGS = [
    "#macro_override",
    "#fii_selling_streak",
    "#fii_buying_streak",
    "#crude_shock",
    "#crude_support",
    "#vix_spike_from_low",
    "#vix_collapse",
    "#oi_support_broken",
    "#oi_resistance_broken",
    "#max_pain_pin",
    "#expiry_pin",
    "#expiry_volatile",
    "#rbi_surprise_dovish",
    "#rbi_surprise_hawkish",
    "#rbi_in_line",
    "#budget_reaction",
    "#us_market_crash",
    "#us_market_rally",
    "#dxy_surge",
    "#dxy_weakness",
    "#yield_spike",
    "#yield_drop",
    "#inr_depreciation",
    "#inr_rally",
    "#earnings_shock_negative",
    "#earnings_beat",
    "#geopolitical_risk",
    "#sector_rotation",
    "#short_covering_rally",
    "#long_unwinding",
    "#capitulation",
    "#distribution",
    "#accumulation",
    "#gap_and_go",
    "#gap_and_fade",
    "#triple_threat",
    "#holiday_adjacent",
    "#low_volume_drift",
    "#high_volume_conviction",
]

# ── Candle types ──
CANDLE_TYPES = [
    "strong_bull",
    "weak_bull",
    "doji",
    "weak_bear",
    "strong_bear",
    "hammer",
    "shooting_star",
    "engulfing_bull",
    "engulfing_bear",
]

# ── Scheduled event types ──
EVENT_TYPES = [
    "rbi_policy",
    "fomc",
    "budget",
    "weekly_expiry",
    "monthly_expiry",
    "earnings_major",
    "msci_rebalance",
    "advance_tax",
    "holiday_adjacent",
    "election_result",
]
