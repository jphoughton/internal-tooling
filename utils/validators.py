"""Shared validation utilities for email addresses and URLs."""
import re

_EMAIL_RE = re.compile(
    r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
)

_URL_RE = re.compile(
    r'^https?://'                                                  # scheme
    r'(?:'
    r'(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,}'                          # dotted domain (e.g. example.com)
    r'|localhost'                                                    # or bare localhost
    r'|(?:\d{1,3}\.){3}\d{1,3}'                                    # or IPv4 address
    r')'
    r'(?::\d{1,5})?'                                               # optional port
    r'(?:/[^\s]*)?$'                                               # optional path/query/fragment
)


def email_valid(value):
    """Return True if value is a syntactically valid email address."""
    if not isinstance(value, str):
        return False
    return bool(_EMAIL_RE.match(value))


def url_valid(value):
    """Return True if value is a valid http/https URL."""
    if not isinstance(value, str):
        return False
    return bool(_URL_RE.match(value))
