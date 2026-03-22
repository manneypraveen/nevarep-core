"""Date utility functions for trading day calculations."""

from datetime import date, timedelta
from typing import List


def get_trading_days(start: date, end: date) -> List[date]:
    """Generate all weekdays (Mon-Fri) in the date range."""
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def is_expiry_day(d: date) -> bool:
    """Check if date is a Thursday (weekly expiry)."""
    return d.weekday() == 3


def is_monthly_expiry(d: date) -> bool:
    """Check if date is the last Thursday of the month."""
    if d.weekday() != 3:
        return False
    next_week = d + timedelta(days=7)
    return next_week.month != d.month


def format_nse_old(d: date) -> str:
    """Format date for old NSE URL: 01JAN2024."""
    return d.strftime("%d%b%Y").upper()


def format_nse_udiff(d: date) -> str:
    """Format date for UDiFF URL: 20240108."""
    return d.strftime("%Y%m%d")


def format_nse_participant_oi(d: date) -> str:
    """Format date for participant OI URL: 08012024 (ddMMyyyy)."""
    return d.strftime("%d%m%Y")


def format_nse_old_expiry(d: date) -> str:
    """Format date as NSE old expiry format: 01-Jan-2024."""
    return d.strftime("%d-%b-%Y")
