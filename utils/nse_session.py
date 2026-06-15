"""
Shared NSE session manager.
Handles cookie warmup, User-Agent rotation, and anti-scraping defenses.
Used by all NSE scrapers (bhav copy, participant OI, etc.)
"""

import time
import random
import requests
from datetime import date
from typing import Callable
from loguru import logger
from tqdm import tqdm

from config.settings import NSE_HEADERS, get_random_ua, NSE_SESSION_REFRESH_WAIT, MAX_SESSION_REFRESHES


def create_nse_session() -> requests.Session:
    """
    Create a new requests session and warm it up with NSE cookies.
    NSE requires valid cookies from the homepage before allowing archive access.
    """
    session = requests.Session()
    headers = NSE_HEADERS.copy()
    headers["User-Agent"] = get_random_ua()

    try:
        logger.debug("Warming up new NSE session — hitting homepage...")
        resp = session.get(
            "https://www.nseindia.com/",
            headers=headers,
            timeout=15,
        )
        logger.debug(f"Homepage status: {resp.status_code}, cookies: {len(session.cookies)}")
        time.sleep(1)

        # Hit a lightweight API to validate the session
        session.get(
            "https://www.nseindia.com/api/marketStatus",
            headers=headers,
            timeout=10,
        )
        time.sleep(0.5)

    except Exception as e:
        logger.warning(f"Session warmup had issues (continuing anyway): {e}")

    return session


def get_fresh_headers() -> dict:
    """Get NSE headers with a rotated User-Agent."""
    headers = NSE_HEADERS.copy()
    headers["User-Agent"] = get_random_ua()
    return headers


def is_html_response(response: requests.Response) -> bool:
    """
    Check if NSE returned an HTML page instead of actual data.
    NSE sometimes returns HTTP 200 with an HTML login/block page.
    """
    content_type = response.headers.get("Content-Type", "")
    if "html" in content_type.lower():
        return True
    if len(response.content) < 500:
        return True
    return False


def polite_sleep(base: float = 1.0, jitter: float = 1.0):
    """Sleep with randomized jitter to look more human."""
    time.sleep(base + random.uniform(0, jitter))


def run_download_loop(
    trading_days: list[date],
    download_fn: Callable[[date, requests.Session, dict], tuple],
    max_errors: int,
    desc: str = "Downloading",
    sleep_base: float = 1.0,
    sleep_jitter: float = 1.0,
) -> tuple[dict, list]:
    """
    Shared retry/session-refresh loop used by all NSE bulk downloaders.

    `download_fn(d, session, headers)` must return `(result, status)` where
    status is one of: "ok", "exists", "holiday_404", "blocked", "error".

    Returns (stats, results) where results is a list of (date, result)
    for statuses "ok"/"exists" with a non-None result.
    """
    session = create_nse_session()
    headers = get_fresh_headers()

    stats = {
        "downloaded": 0, "skipped": 0, "holidays": 0,
        "blocked": 0, "errors": 0, "session_refreshes": 0,
    }
    consecutive_errors = 0
    session_refreshes = 0
    results = []

    i = 0
    pbar = tqdm(total=len(trading_days), desc=desc)

    while i < len(trading_days):
        d = trading_days[i]
        result, status = download_fn(d, session, headers)

        if status == "ok":
            stats["downloaded"] += 1
            consecutive_errors = 0
            if result is not None:
                results.append((d, result))

        elif status == "exists":
            stats["skipped"] += 1
            consecutive_errors = 0
            if result is not None:
                results.append((d, result))

        elif status == "holiday_404":
            stats["holidays"] += 1
            consecutive_errors += 1

        elif status in ("blocked", "error"):
            if status == "blocked":
                stats["blocked"] += 1
            else:
                stats["errors"] += 1
            consecutive_errors += 1

        # Session refresh on consecutive errors
        if consecutive_errors >= max_errors:
            if session_refreshes < MAX_SESSION_REFRESHES:
                session_refreshes += 1
                stats["session_refreshes"] += 1
                wait_time = NSE_SESSION_REFRESH_WAIT * session_refreshes

                logger.warning(
                    f"Session refresh #{session_refreshes}/{MAX_SESSION_REFRESHES}: "
                    f"{consecutive_errors} consecutive errors. Waiting {wait_time}s..."
                )

                session.close()
                time.sleep(wait_time)

                headers = get_fresh_headers()
                session = create_nse_session()
                consecutive_errors = 0

                # Rewind to retry failed dates
                rewind = min(max_errors, i)
                i -= rewind
                stats["blocked"] = max(0, stats["blocked"] - rewind)
                stats["holidays"] = max(0, stats["holidays"] - rewind)

                logger.info(f"Retrying from {trading_days[i]} with fresh session")
                time.sleep(2)
                continue
            else:
                logger.error(
                    f"Exhausted all {MAX_SESSION_REFRESHES} session refreshes. "
                    f"NSE may be rate-limiting your IP. Try again later."
                )
                consecutive_errors = 0

        i += 1
        pbar.update(1)
        polite_sleep(base=sleep_base, jitter=sleep_jitter)

    pbar.close()
    session.close()
    return stats, results
