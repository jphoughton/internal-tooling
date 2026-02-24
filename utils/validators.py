"""Shared validation utilities."""
import re

_EMAIL_RE = re.compile(
    r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
)

_URL_RE = re.compile(
    r'^https?://'
    r'(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,}'
    r'(?::\d+)?'
    r'(?:/[^\s]*)?$'
)


def email_valid(value):
    """Return True if value is a valid email address."""
    if not isinstance(value, str):
        return False
    return bool(_EMAIL_RE.match(value))


def url_valid(value):
    """Return True if value is a valid http/https URL."""
    if not isinstance(value, str):
        return False
    return bool(_URL_RE.match(value))
