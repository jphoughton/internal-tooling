"""Shared date math utilities for month-string operations."""
from datetime import datetime
from dateutil.relativedelta import relativedelta


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
