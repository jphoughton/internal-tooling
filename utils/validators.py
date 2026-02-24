"""Input validation utilities using regex."""
import re

_EMAIL_RE = re.compile(
    r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
)

_URL_RE = re.compile(
    r'^https?://'
    r'(?:(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,}|localhost)'
    r'(?::\d+)?'
    r'(?:/[^\s]*)?$'
)


def email_valid(email: str) -> bool:
    """Return True if email is a well-formed email address."""
    if not isinstance(email, str):
        return False
    return bool(_EMAIL_RE.match(email.strip()))


def url_valid(url: str) -> bool:
    """Return True if url is a well-formed http/https URL."""
    if not isinstance(url, str):
        return False
    return bool(_URL_RE.match(url.strip()))
