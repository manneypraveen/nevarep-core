"""
NEVAREP — LLM News Structurer (llm/news_structurer.py)

Turns raw headlines (list[dict] from GDELT/RSS) into structured, deduplicated
news events for the daily_news_events table.

Instruments in scope: NIFTY50, BANKNIFTY, CRUDEOIL (+ MACRO for cross-instrument)

Pipeline:
  1. Dedup: cluster articles with similar titles within the same day → one article
     per event (simple token-overlap, no external embedding needed).
  2. Classify: call Claude Haiku API to extract structured JSON for each deduped set.
  3. Novelty decay: same category on consecutive days → weight decays by 0.5/day.
  4. Return list[dict] ready to INSERT into daily_news_events.
"""

from __future__ import annotations

import json
import time
import re
import math
from datetime import date
from typing import Optional

import requests
from loguru import logger

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import ANTHROPIC_API_KEY

# ─── Constants ──────────────────────────────────────────────────────────────

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_API_URL = "https://api.anthropic.com/v1/messages"

VALID_CATEGORIES = {
    "rbi_policy", "government_fiscal", "sebi_regulatory", "earnings_corporate",
    "fii_dii_activity", "geopolitical", "us_fed", "global_central_bank",
    "crude_commodity", "currency", "sectoral", "technical_structural",
    "domestic_macro", "sentiment_flow", "black_swan",
}
VALID_ENTITIES = {"NIFTY50", "BANKNIFTY", "CRUDEOIL", "MACRO"}
VALID_DIRECTIONS = {"BULLISH", "BEARISH", "NEUTRAL"}
VALID_SEVERITIES = {"LOW", "MED", "HIGH", "CRITICAL"}

# Categories that map to a single instrument
_ENTITY_MAP = {
    "crude_commodity": "CRUDEOIL",
    "rbi_policy": "NIFTY50",
    "us_fed": "MACRO",
    "global_central_bank": "MACRO",
    "geopolitical": "MACRO",
    "fii_dii_activity": "NIFTY50",
    "currency": "MACRO",
    "government_fiscal": "NIFTY50",
    "sebi_regulatory": "NIFTY50",
    "domestic_macro": "NIFTY50",
    "sentiment_flow": "NIFTY50",
    "technical_structural": "NIFTY50",
    "earnings_corporate": "NIFTY50",
    "sectoral": "NIFTY50",
    "black_swan": "MACRO",
}

# Novelty decay: same category on N consecutive prior days → weight * 0.5^N
_NOVELTY_DECAY = 0.5
_NOVELTY_LOOKBACK = 5


# ─── Deduplication ──────────────────────────────────────────────────────────

def _tokenize(text: str) -> set[str]:
    """Simple word-token set, lowercase, remove stop words."""
    stop = {"the", "a", "an", "of", "in", "on", "at", "to", "for",
            "is", "are", "was", "were", "by", "with", "and", "or", "as"}
    return {w for w in re.findall(r"[a-z]+", text.lower()) if w not in stop and len(w) > 2}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def deduplicate_articles(articles: list[dict], threshold: float = 0.5) -> list[dict]:
    """
    Cluster articles with Jaccard title-overlap >= threshold → keep first per cluster.
    Articles within a 24-hour window are candidates (all same day articles qualify).
    Returns one representative article per unique event.
    """
    if not articles:
        return []

    tokenised = [_tokenize(a.get("title", "")) for a in articles]
    kept_indices = []
    dropped = set()

    for i, (art, tok) in enumerate(zip(articles, tokenised)):
        if i in dropped:
            continue
        kept_indices.append(i)
        for j in range(i + 1, len(articles)):
            if j not in dropped and _jaccard(tok, tokenised[j]) >= threshold:
                dropped.add(j)

    return [articles[i] for i in kept_indices]


# ─── LLM Prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a financial news analyst for Indian equity markets.
Your job: classify news headlines into structured JSON for a trading system.

Instruments in scope: NIFTY50 (Nifty 50 index), BANKNIFTY (Bank Nifty), CRUDEOIL, MACRO (global/cross-instrument).

Respond ONLY with a valid JSON array. No markdown fences, no commentary, no preamble."""

_USER_TEMPLATE = """\
Date: {date}
Market context: Nifty {nifty_change:+.2f}%  BankNifty {bn_change:+.2f}%

Headlines (one per line):
{headlines}

For each headline that could move Nifty / BankNifty / Crude in the next 1-2 sessions, output one JSON object.
Return ONLY the JSON array — no markdown, no text outside the array.

Schema (follow exactly):
[
  {{
    "category": "<one of: rbi_policy | government_fiscal | sebi_regulatory | earnings_corporate | fii_dii_activity | geopolitical | us_fed | global_central_bank | crude_commodity | currency | sectoral | technical_structural | domestic_macro | sentiment_flow | black_swan>",
    "entity":    "<NIFTY50 | BANKNIFTY | CRUDEOIL | MACRO>",
    "direction": "<BULLISH | BEARISH | NEUTRAL>",
    "severity":  "<LOW | MED | HIGH | CRITICAL>",
    "scheduled": <true if this is a pre-announced event (RBI policy, FOMC, earnings date, expiry) else false>,
    "headline":  "<concise summary ≤100 chars>"
  }}
]

Rules:
- Max 8 items. Only include market-moving events.
- Skip purely stock-specific news unless it's a Nifty-50 heavyweight (HDFC Bank, Reliance, Infosys, TCS, ICICI Bank).
- crude_commodity → entity CRUDEOIL.
- banking sector news → entity BANKNIFTY.
- RBI/SEBI/Govt/FII/macro → entity NIFTY50 or MACRO as appropriate.
- CRITICAL: RBI surprise rate move, US crash >3%, geopolitical escalation.
- HIGH: FII outflow >₹3000 cr, crude ±3%, FOMC hawkish surprise.
- MED: scheduled data releases (CPI, GDP), earnings misses.
- LOW: routine regulatory updates.
- If no market-moving events, return [].
"""


# ─── API Call ────────────────────────────────────────────────────────────────

def _call_claude(prompt: str) -> list[dict] | None:
    """Call Claude Haiku and parse the JSON array response. Returns None on failure."""
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY.startswith("sk-ant-xxxxx"):
        return None

    try:
        resp = requests.post(
            _API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _HAIKU_MODEL,
                "max_tokens": 1500,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )

        if resp.status_code != 200:
            logger.warning(f"Claude API {resp.status_code}: {resp.text[:200]}")
            return None

        text = resp.json()["content"][0]["text"].strip()

        # Strip accidental markdown fences
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()

        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else None

    except json.JSONDecodeError as e:
        logger.warning(f"Claude response JSON parse error: {e}")
        return None
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return None


# ─── Field validation ────────────────────────────────────────────────────────

def _clean_item(item: dict) -> dict | None:
    """Validate and normalise one structured item. Returns None if invalid."""
    if not isinstance(item, dict):
        return None

    category = item.get("category", "").lower()
    if category not in VALID_CATEGORIES:
        category = "sentiment_flow"

    entity = (item.get("entity") or "").upper()
    if entity not in VALID_ENTITIES:
        entity = _ENTITY_MAP.get(category, "MACRO")

    direction = (item.get("direction") or "NEUTRAL").upper()
    if direction not in VALID_DIRECTIONS:
        direction = "NEUTRAL"

    severity = (item.get("severity") or "LOW").upper()
    if severity not in VALID_SEVERITIES:
        severity = "MED"

    headline = str(item.get("headline") or item.get("category") or "")[:500]
    if not headline:
        return None

    return {
        "category": category,
        "entity": entity,
        "direction": direction,
        "severity": severity,
        "scheduled": bool(item.get("scheduled", False)),
        "headline": headline,
    }


# ─── Novelty weight ──────────────────────────────────────────────────────────

def compute_novelty_weights(
    items: list[dict],
    prior_categories: list[str],
) -> list[float]:
    """
    Decay weight for a category that appeared repeatedly in recent days.
    prior_categories: flat list of categories seen over the past N days (oldest first).
    Returns one weight per item in the same order.
    """
    weights = []
    for item in items:
        cat = item["category"]
        # Count how many of the last _NOVELTY_LOOKBACK days had the same category
        recent = prior_categories[-(5 * _NOVELTY_LOOKBACK):]  # cap search window
        streak = 0
        for c in reversed(recent):
            if c == cat:
                streak += 1
            else:
                break
        w = max(round(_NOVELTY_DECAY ** streak, 4), 0.0625)  # floor at 1/16
        weights.append(w)
    return weights


# ─── Public API ──────────────────────────────────────────────────────────────

def structure_articles(
    trade_date: date,
    articles: list[dict],
    nifty_change: float = 0.0,
    bn_change: float = 0.0,
    prior_categories: list[str] | None = None,
    retry_raw: bool = True,
) -> list[dict]:
    """
    Main entry point. Given raw articles for a day, return structured event rows
    ready to INSERT into daily_news_events (minus id, trade_date — caller adds those).

    prior_categories: list of category strings from recent past days (for novelty decay).
    retry_raw: if True and no API key is set, fall back to keyword-based categorisation.

    Each returned dict has keys:
        headline, category, entity, direction, severity, scheduled, novelty_weight,
        expected_impact (alias of direction, lowercase)
    """
    if not articles:
        return []

    # Step 1: dedup
    deduped = deduplicate_articles(articles)

    # Step 2: classify
    structured = None
    if ANTHROPIC_API_KEY and not ANTHROPIC_API_KEY.startswith("sk-ant-xxxxx"):
        headline_block = "\n".join(
            f"- {a.get('title', '')}" for a in deduped[:30]
        )
        prompt = _USER_TEMPLATE.format(
            date=trade_date.isoformat(),
            nifty_change=nifty_change,
            bn_change=bn_change,
            headlines=headline_block,
        )
        structured = _call_claude(prompt)
        if structured is not None:
            time.sleep(0.3)  # polite rate limit

    if structured is None:
        if not retry_raw:
            return []
        # Keyword fallback (no API key or parse failure)
        structured = _keyword_classify(deduped)

    # Step 3: validate fields
    cleaned = [_clean_item(it) for it in (structured or [])]
    cleaned = [c for c in cleaned if c is not None]

    # Step 4: novelty weights
    prior = prior_categories or []
    weights = compute_novelty_weights(cleaned, prior)

    # Step 5: assemble rows
    rows = []
    for item, w in zip(cleaned, weights):
        rows.append({
            "headline": item["headline"],
            "category": item["category"],
            "entity": item["entity"],
            "direction": item["direction"],
            "severity": item["severity"],
            "scheduled": item["scheduled"],
            "novelty_weight": w,
            "expected_impact": item["direction"].lower(),  # alias for compatibility
        })

    return rows


# ─── Keyword fallback (no API key) ──────────────────────────────────────────

_KW_CATEGORIES: list[tuple[str, list[str]]] = [
    ("rbi_policy", ["rbi", "repo rate", "monetary policy", "mpc", "reserve bank"]),
    ("us_fed", ["fed", "fomc", "powell", "federal reserve", "treasury yield"]),
    ("crude_commodity", ["crude", "oil price", "opec", "brent", "petroleum"]),
    ("currency", ["rupee", "dollar", "dxy", "forex", "usd/inr"]),
    ("fii_dii_activity", ["fii", "dii", "fpi", "foreign investor", "institutional"]),
    ("geopolitical", ["war", "tension", "sanction", "conflict", "attack"]),
    ("earnings_corporate", ["result", "profit", "earnings", "quarterly", "guidance"]),
    ("government_fiscal", ["budget", "gst", "fiscal", "government", "policy"]),
    ("sebi_regulatory", ["sebi", "regulation", "margin", "exchange rule"]),
    ("domestic_macro", ["gdp", "inflation", "cpi", "pmi", "iip"]),
    ("black_swan", ["pandemic", "crisis", "collapse", "emergency"]),
    ("technical_structural", ["expiry", "rollover", "open interest", "pcr"]),
]

_KW_DIRECTION = {
    "bullish": ["rally", "surge", "gain", "rise", "jump", "record high", "rate cut"],
    "bearish": ["crash", "fall", "drop", "plunge", "selloff", "rate hike", "war"],
}

_KW_SEVERITY = {
    "CRITICAL": ["crash", "crisis", "war", "pandemic", "collapse"],
    "HIGH": ["surge", "plunge", "spike", "record", "rbi rate", "fii selling"],
    "LOW": ["minor", "stable", "unchanged", "routine"],
}


def _keyword_classify(articles: list[dict]) -> list[dict]:
    results = []
    for art in articles[:8]:
        title = art.get("title", "")
        hl = title.lower()

        category = "sentiment_flow"
        for cat, kws in _KW_CATEGORIES:
            if any(k in hl for k in kws):
                category = cat
                break

        direction = "NEUTRAL"
        bull = sum(1 for w in _KW_DIRECTION["bullish"] if w in hl)
        bear = sum(1 for w in _KW_DIRECTION["bearish"] if w in hl)
        if bull > bear:
            direction = "BULLISH"
        elif bear > bull:
            direction = "BEARISH"

        severity = "MED"
        for sev, kws in _KW_SEVERITY.items():
            if any(k in hl for k in kws):
                severity = sev
                break

        results.append({
            "category": category,
            "entity": _ENTITY_MAP.get(category, "MACRO"),
            "direction": direction,
            "severity": severity,
            "scheduled": False,
            "headline": title[:100],
        })
    return results
