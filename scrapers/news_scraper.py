from __future__ import annotations

"""
NEVAREP Sprint 5 — News & Events Collection

Collects market-moving news for each trading day using:
  1. GDELT Project API (free, historical, global news database)
  2. Google News RSS (for recent/live data)

Then optionally structures the raw headlines using Claude API
to extract categories, severity, and expected impact.

The GDELT approach is practical for backfill — it indexes
Indian financial media (MoneyControl, ET, LiveMint, Reuters India)
and allows searching by date + keyword.

Usage:
    python -m scrapers.news_scraper
    python -m scrapers.news_scraper --start 2024-01-01 --end 2024-03-31
    python -m scrapers.news_scraper --structure  # run LLM structuring pass
    python -m scrapers.news_scraper --load-only  # load existing JSON files
"""

import sys
import time
import json
import random
import argparse
from datetime import date, timedelta, datetime
from pathlib import Path
from urllib.parse import quote_plus

import duckdb
import pandas as pd
import requests
from tqdm import tqdm
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import (
    DB_PATH, HIST_START, HIST_END, ARTICLES_DIR, ANTHROPIC_API_KEY,
)
from llm.news_structurer import structure_articles

Path("logs").mkdir(exist_ok=True)
logger.add("logs/news_scraper.log", rotation="10 MB")

NEWS_DIR = ARTICLES_DIR
NEWS_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════
# GDELT API — Historical news search
# ═══════════════════════════════════════════════════════════

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT queries scoped to NIFTY50, BANKNIFTY, and CRUDEOIL (Tier-1 instruments)
GDELT_QUERIES = [
    # NIFTY50
    "nifty 50 india market",
    "RBI monetary policy repo rate india",
    "FII selling buying nifty india",
    "india inflation GDP CPI budget fiscal",
    "SEBI regulation india market",
    "FOMC fed rate india nifty impact",
    "india stock market crash rally",
    # BANKNIFTY
    "bank nifty banking sector india",
    # CRUDEOIL
    "crude oil price OPEC brent india rupee",
    "crude oil inventory EIA API",
]


def fetch_gdelt_articles(query: str, start_date: date, end_date: date,
                         max_records: int = 25) -> list[dict]:
    """
    Fetch articles from GDELT DOC 2.0 API.
    Returns list of {title, url, source, date, language} dicts.
    """
    # GDELT date format: YYYYMMDDHHMMSS
    start_str = start_date.strftime("%Y%m%d") + "000000"
    end_str = end_date.strftime("%Y%m%d") + "235959"

    params = {
        "query": f'{query} sourcelang:eng sourcecountry:IN',
        "mode": "artlist",
        "maxrecords": str(max_records),
        "format": "json",
        "startdatetime": start_str,
        "enddatetime": end_str,
        "sort": "datedesc",
    }

    try:
        resp = requests.get(GDELT_DOC_API, params=params, timeout=30)
        if resp.status_code != 200:
            return []

        data = resp.json()
        articles = data.get("articles", [])

        results = []
        for art in articles:
            results.append({
                "title": art.get("title", "").strip(),
                "url": art.get("url", ""),
                "source": art.get("domain", art.get("sourcecountry", "")),
                "seendate": art.get("seendate", ""),
                "language": art.get("language", "English"),
            })

        return results

    except requests.exceptions.JSONDecodeError:
        # GDELT sometimes returns empty or HTML for zero results
        return []
    except Exception as e:
        logger.debug(f"GDELT error for '{query}': {e}")
        return []


def collect_news_for_date(d: date) -> list[dict]:
    """
    Collect all news articles for a single trading day.
    Searches previous evening to current morning (overnight news window).
    """
    # Search window: previous day 3:30 PM to current day 9:30 AM
    # In practice, GDELT granularity is daily, so search prev day + current day
    prev_day = d - timedelta(days=1)

    all_articles = []
    seen_titles = set()

    for query in GDELT_QUERIES:
        articles = fetch_gdelt_articles(query, prev_day, d, max_records=10)

        for art in articles:
            title = art["title"]
            # Deduplicate by title similarity
            title_key = title.lower()[:80]
            if title_key not in seen_titles and len(title) > 20:
                seen_titles.add(title_key)
                art["trade_date"] = d.isoformat()
                all_articles.append(art)

        # Rate limit — GDELT is free but rate-limited
        time.sleep(0.5 + random.uniform(0, 0.3))

    return all_articles


# ═══════════════════════════════════════════════════════════
# GOOGLE NEWS RSS — For recent/current news
# ═══════════════════════════════════════════════════════════

def fetch_google_news_rss(query: str = "nifty market india") -> list[dict]:
    """Fetch recent headlines from Google News RSS."""
    import xml.etree.ElementTree as ET

    url = (
        f"https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    )

    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return []

        root = ET.fromstring(resp.content)
        items = root.findall(".//item")

        results = []
        for item in items[:20]:
            title = item.find("title")
            pub_date = item.find("pubDate")
            source = item.find("source")

            results.append({
                "title": title.text if title is not None else "",
                "source": source.text if source is not None else "Google News",
                "pub_date": pub_date.text if pub_date is not None else "",
                "url": "",
            })

        return results

    except Exception as e:
        logger.debug(f"Google News RSS error: {e}")
        return []


# ═══════════════════════════════════════════════════════════
# BULK DOWNLOAD
# ═══════════════════════════════════════════════════════════

def download_all(start: date, end: date):
    """Download news for all trading days in range."""
    con = duckdb.connect(str(DB_PATH))
    trading_days = [
        row[0] for row in con.execute("""
            SELECT trade_date FROM trading_day
            WHERE is_trading_day = TRUE
            AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
        """, [start, end]).fetchall()
    ]
    con.close()

    if not trading_days:
        logger.error("No trading days found.")
        return

    logger.info(f"Collecting news: {start} to {end} ({len(trading_days)} trading days)")

    total_articles = 0
    days_with_news = 0

    for d in tqdm(trading_days, desc="Collecting news"):
        out_path = NEWS_DIR / f"news_{d.isoformat()}.json"

        # Skip if already collected
        if out_path.exists() and out_path.stat().st_size > 50:
            existing = json.loads(out_path.read_text())
            total_articles += len(existing)
            days_with_news += 1
            continue

        articles = collect_news_for_date(d)

        if articles:
            out_path.write_text(json.dumps(articles, indent=2, ensure_ascii=False))
            total_articles += len(articles)
            days_with_news += 1
            logger.debug(f"{d}: {len(articles)} articles")
        else:
            # Save empty file so we don't retry
            out_path.write_text("[]")

        # Respect GDELT rate limits
        time.sleep(1 + random.uniform(0, 1))

    logger.info(
        f"\nNews Collection Summary:\n"
        f"  Trading days: {len(trading_days)}\n"
        f"  Days with news: {days_with_news}\n"
        f"  Total articles: {total_articles}\n"
        f"  Avg per day: {total_articles / max(days_with_news, 1):.1f}"
    )


# ═══════════════════════════════════════════════════════════
# LOAD INTO DATABASE
# ═══════════════════════════════════════════════════════════

def load_to_db(start: date = None, end: date = None, use_llm: bool = False):
    """
    Load news articles into daily_news_events table.
    Uses llm/news_structurer.py for dedup + classification + novelty weighting.
    Falls back to keyword classification when no API key is configured.
    """
    json_files = sorted(NEWS_DIR.glob("news_*.json"))
    if not json_files:
        logger.error("No news JSON files found. Run download first.")
        return

    con = duckdb.connect(str(DB_PATH))

    # Market data for LLM context
    market_data = {}
    for row in con.execute("""
        SELECT trade_date, change_pct FROM daily_index_ohlc WHERE index_name = 'NIFTY50'
    """).fetchall():
        market_data[row[0]] = row[1]

    bn_data = {}
    for row in con.execute("""
        SELECT trade_date, change_pct FROM daily_index_ohlc WHERE index_name = 'BANKNIFTY'
    """).fetchall():
        bn_data[row[0]] = row[1]

    logger.info(f"Processing {len(json_files)} news files (use_llm={use_llm})...")

    all_records = []
    prior_categories: list[str] = []  # rolling list for novelty decay

    for jf in tqdm(json_files, desc="Processing news"):
        date_str = jf.stem.replace("news_", "")
        try:
            trade_date = date.fromisoformat(date_str)
        except ValueError:
            continue
        if start and trade_date < start:
            continue
        if end and trade_date > end:
            continue

        articles = json.loads(jf.read_text())
        if not articles:
            continue

        nifty_chg = float(market_data.get(trade_date) or 0)
        bn_chg = float(bn_data.get(trade_date) or 0)

        rows = structure_articles(
            trade_date, articles,
            nifty_change=nifty_chg,
            bn_change=bn_chg,
            prior_categories=prior_categories,
            retry_raw=True,
        )

        for row in rows:
            all_records.append({
                "trade_date": trade_date,
                "news_timestamp": None,
                "headline": row["headline"],
                "source": "gdelt",
                "category": row["category"],
                "entity": row.get("entity", "MACRO"),
                "direction": row.get("direction", "NEUTRAL"),
                "severity": row["severity"].lower()[:10],
                "scheduled": row.get("scheduled", False),
                "expected_impact": row.get("expected_impact", "neutral"),
                "novelty_weight": row.get("novelty_weight", 1.0),
                "index_affected": None,
                "sector_affected": None,
                "actual_value": None,
                "expected_value": None,
                "surprise": None,
                "surprise_direction": None,
            })
            prior_categories.append(row["category"])

    if not all_records:
        logger.error("No news records to load!")
        con.close()
        return

    df = pd.DataFrame(all_records)
    min_date = df["trade_date"].min()
    max_date = df["trade_date"].max()

    con.execute(
        "DELETE FROM daily_news_events WHERE trade_date BETWEEN ? AND ?",
        [min_date, max_date],
    )
    con.execute("INSERT INTO daily_news_events SELECT nextval('seq_news_id'), * FROM df")

    count = con.execute("SELECT COUNT(*) FROM daily_news_events").fetchone()[0]
    logger.info(f"Loaded {count:,} rows into daily_news_events")

    cat_stats = con.execute("""
        SELECT category, entity, COUNT(*) as n,
               ROUND(AVG(novelty_weight), 3) as avg_novelty
        FROM daily_news_events
        GROUP BY category, entity ORDER BY n DESC LIMIT 20
    """).fetchdf()
    print("\n  News by category + entity:")
    print(cat_stats.to_string(index=False))

    day_stats = con.execute("""
        SELECT COUNT(DISTINCT trade_date) as days,
               COUNT(*) as total_events,
               ROUND(COUNT(*)*1.0/COUNT(DISTINCT trade_date),1) as avg_per_day
        FROM daily_news_events
    """).fetchdf()
    print("\n  Coverage:", day_stats.to_string(index=False))
    con.close()


# ═══════════════════════════════════════════════════════════
# BASIC KEYWORD CATEGORIZATION (no LLM fallback)
# ═══════════════════════════════════════════════════════════

CATEGORY_KEYWORDS = {
    "rbi_policy": ["rbi", "repo rate", "monetary policy", "mpc", "reserve bank", "interest rate cut", "interest rate hike"],
    "us_fed": ["fed", "fomc", "powell", "federal reserve", "us rate", "treasury yield"],
    "crude_commodity": ["crude", "oil price", "opec", "brent", "petroleum", "gold price"],
    "currency": ["rupee", "dollar", "dxy", "forex", "currency", "usd/inr", "inr"],
    "fii_dii_activity": ["fii", "dii", "foreign investor", "fpi", "institutional", "mutual fund flow"],
    "geopolitical": ["war", "tension", "military", "sanction", "border", "conflict", "attack", "missile"],
    "earnings_corporate": ["result", "profit", "revenue", "earnings", "quarterly", "q1", "q2", "q3", "q4", "guidance"],
    "government_fiscal": ["budget", "gst", "tax", "fiscal", "government", "modi", "parliament", "policy"],
    "sebi_regulatory": ["sebi", "regulation", "margin", "lot size", "compliance", "exchange rule"],
    "domestic_macro": ["gdp", "inflation", "cpi", "pmi", "iip", "manufacturing", "trade deficit"],
    "sectoral": ["banking", "it sector", "pharma", "auto", "real estate", "infrastructure", "fmcg"],
    "sentiment_flow": ["rally", "crash", "bull", "bear", "record high", "all-time", "selloff", "correction"],
    "black_swan": ["pandemic", "lockdown", "covid", "crisis", "collapse", "emergency", "earthquake"],
    "global_central_bank": ["ecb", "boj", "pboc", "bank of england", "central bank"],
    "technical_structural": ["expiry", "rollover", "open interest", "pcr", "vix", "option", "derivative"],
}

SEVERITY_KEYWORDS = {
    "critical": ["crash", "crisis", "war", "pandemic", "emergency", "collapse", "shock", "black swan"],
    "high": ["surge", "plunge", "spike", "record", "rbi rate", "fed rate", "fii selling", "crude spike"],
    "low": ["minor", "stable", "unchanged", "inline", "expected", "routine"],
}


def categorize_headline(headline: str) -> str:
    """Basic keyword-based categorization."""
    hl = headline.lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in hl:
                return cat
    return "sentiment_flow"


def estimate_severity(headline: str) -> str:
    """Basic severity estimation from keywords."""
    hl = headline.lower()
    for sev, keywords in SEVERITY_KEYWORDS.items():
        for kw in keywords:
            if kw in hl:
                return sev
    return "medium"


def estimate_impact(headline: str) -> str:
    """Basic impact direction from keywords."""
    hl = headline.lower()
    bullish = ["rally", "surge", "gain", "rise", "jump", "soar", "record high", "bull", "rate cut", "buy"]
    bearish = ["crash", "fall", "drop", "plunge", "sell", "decline", "slump", "bear", "rate hike", "war"]

    bull_score = sum(1 for w in bullish if w in hl)
    bear_score = sum(1 for w in bearish if w in hl)

    if bull_score > bear_score:
        return "bullish"
    elif bear_score > bull_score:
        return "bearish"
    return "neutral"


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def run_backfill(start: date = None, end: date = None, use_llm: bool = False):
    start = start or HIST_START
    end = end or HIST_END

    print("=" * 60)
    print("  NEVAREP Sprint 5 — News & Events Collection")
    print("=" * 60)
    print(f"  Range: {start} to {end}")
    print(f"  Source: GDELT Project API (free, historical)")
    print(f"  LLM structuring: {'ON (Claude Haiku)' if use_llm else 'OFF (keyword fallback)'}")
    print()

    print("[1/2] Collecting news from GDELT...")
    print("  Searching 10 query categories per day")
    print("  This may take 30-60 min for the full range.\n")
    download_all(start, end)

    print("\n[2/2] Loading into daily_news_events...")
    load_to_db(start, end, use_llm=use_llm)

    con = duckdb.connect(str(DB_PATH))
    count = con.execute("SELECT COUNT(*) FROM daily_news_events").fetchone()[0]
    days = con.execute("SELECT COUNT(DISTINCT trade_date) FROM daily_news_events").fetchone()[0]
    con.close()

    print(f"\n{'=' * 60}")
    print(f"  Sprint 5 complete!")
    print(f"{'=' * 60}")
    print(f"  daily_news_events: {count:,} rows across {days} days")
    print(f"  DB size: {DB_PATH.stat().st_size / 1024 / 1024:.1f} MB")
    if not use_llm:
        print(f"\n  To upgrade with LLM structuring:")
        print(f"    1. Set ANTHROPIC_API_KEY in .env")
        print(f"    2. Run: python -m scrapers.news_scraper --structure")
        print(f"    Cost: ~$5-10 for full 1,780 days using Claude Haiku")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="NEVAREP Sprint 5 — News Scraper")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD")
    parser.add_argument("--structure", action="store_true",
                        help="Use Claude API to structure headlines (costs ~$5-10)")
    parser.add_argument("--load-only", action="store_true",
                        help="Only load existing JSON files into DB")
    parser.add_argument("--download-only", action="store_true",
                        help="Only download, don't load into DB")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else HIST_START
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else HIST_END

    if args.load_only:
        print("Load-only mode...")
        load_to_db(start, end, use_llm=args.structure)
    elif args.download_only:
        print("Download-only mode...")
        download_all(start, end)
    else:
        run_backfill(start, end, use_llm=args.structure)


if __name__ == "__main__":
    main()