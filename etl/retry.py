"""Shared retry decorator for ETL HTTP calls.

Provides with_retry, a decorator that retries API calls with exponential
backoff (1s, 2s, 4s) on transient network failures and server errors.

Usage:
    from etl.retry import with_retry

    @with_retry()
    def fetch_data():
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()
"""
import functools
import logging
import time

import requests

log = logging.getLogger(__name__)

_DELAYS = (1, 2, 4)
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def with_retry(max_retries=3, delays=_DELAYS):
    """Decorator that retries a function on transient HTTP/network failures.

    Retries up to max_retries times with delays between attempts (default: 1s, 2s, 4s).
    Retries on:
    - requests.exceptions.Timeout
    - requests.exceptions.ConnectionError
    - requests.exceptions.HTTPError with status in {429, 500, 502, 503, 504}

    After all retries are exhausted, the last exception is re-raised.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else None
                    if status in _RETRYABLE_STATUS and attempt < max_retries:
                        delay = delays[attempt] if attempt < len(delays) else delays[-1]
                        log.warning(
                            '%s: HTTP %s (attempt %d/%d) -- retrying in %ds',
                            func.__name__, status, attempt + 1, max_retries + 1, delay,
                        )
                        time.sleep(delay)
                        last_exc = exc
                        continue
                    raise
                except (requests.exceptions.Timeout,
                        requests.exceptions.ConnectionError) as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        delay = delays[attempt] if attempt < len(delays) else delays[-1]
                        log.warning(
                            '%s: %s (attempt %d/%d) -- retrying in %ds',
                            func.__name__, type(exc).__name__, attempt + 1, max_retries + 1, delay,
                        )
                        time.sleep(delay)
                        continue
                    raise
            raise last_exc  # exhausted retries without returning
        return wrapper
    return decorator
