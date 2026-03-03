"""Shared date math utilities for month-string operations."""
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta


def _biz_tz():
    """Return the business timezone (lazy-import to avoid circular imports)."""
    from config import SYNC_TIMEZONE
    return ZoneInfo(SYNC_TIMEZONE)


def business_today():
    """Today's date in business timezone (America/Los_Angeles)."""
    return datetime.now(_biz_tz()).date()


def business_yesterday():
    """Yesterday's date in business timezone (America/Los_Angeles)."""
    return business_today() - timedelta(days=1)


def month_str(dt):
    """Format a datetime as YYYY-MM."""
    return dt.strftime("%Y-%m")


def parse_month(s):
    """Parse YYYY-MM string to datetime (first of month)."""
    return datetime.strptime(s, "%Y-%m")


def add_months(month_str_val, n):
    """Add n months to a YYYY-MM string."""
    dt = parse_month(month_str_val)
    dt += relativedelta(months=n)
    return month_str(dt)


def month_diff(a, b):
    """Return number of months from b to a (a - b)."""
    da = parse_month(a)
    db = parse_month(b)
    return (da.year - db.year) * 12 + (da.month - db.month)
