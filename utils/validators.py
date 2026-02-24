"""Input validation utilities using regex patterns."""

import re

_EMAIL_PATTERN = re.compile(
    r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
)

_URL_PATTERN = re.compile(
    r'^https?://'                                              # scheme
    r'(?:(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,}|localhost)'       # domain or localhost
    r'(?::\d{1,5})?'                                          # optional port
    r'(?:[/?#][^\s]*)?$'                                      # optional path/query/fragment
)


def email_valid(email_str: str) -> bool:
    """Return True if email_str is a valid email address."""
    if not isinstance(email_str, str):
        return False
    return bool(_EMAIL_PATTERN.match(email_str.strip()))


def url_valid(url_str: str) -> bool:
    """Return True if url_str is a valid http/https URL."""
    if not isinstance(url_str, str):
        return False
    return bool(_URL_PATTERN.match(url_str.strip()))
