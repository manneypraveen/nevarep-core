"""
Shared NSE session manager.
Handles cookie warmup, User-Agent rotation, and anti-scraping defenses.
Used by all NSE scrapers (bhav copy, participant OI, etc.)
"""

import time
import random
import requests
from loguru import logger

from config.settings import NSE_HEADERS, get_random_ua


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
