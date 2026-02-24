"""Retry decorator with exponential backoff for ETL API calls."""
import functools
import logging
import time

import requests

log = logging.getLogger(__name__)

_RETRY_DELAYS = (1, 2, 4)


def _is_retryable(exc):
    """Return True for transient errors that are safe to retry."""
    if isinstance(exc, (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(exc.response, 'status_code', None)
        return status is not None and status >= 500
    # For SDK / other errors, check the message for transient signals
    msg = str(exc).lower()
    return any(kw in msg for kw in (
        'timeout', 'timed out', 'connection', 'network',
        '503', '502', '500', 'throttl', 'service unavailable',
    ))


def with_retry(func):
    """Retry *func* up to 3 times with 1s / 2s / 4s exponential backoff.

    Only retries on transient errors (network failures, timeouts, 5xx
    responses).  Permanent errors (4xx auth/validation) are re-raised
    immediately after the first failure.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exc = None
        delays = [0] + list(_RETRY_DELAYS)  # [0, 1, 2, 4] -> 1 try + 3 retries
        for attempt, delay in enumerate(delays):
            if delay:
                log.warning(
                    'Retry %d/%d for %s (waiting %ds)...',
                    attempt, len(_RETRY_DELAYS), func.__name__, delay,
                )
                time.sleep(delay)
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                if not _is_retryable(exc):
                    raise
                last_exc = exc
                log.warning(
                    '%s attempt %d/%d failed: %s',
                    func.__name__, attempt + 1, len(delays), exc,
                )
        log.error(
            '%s failed after %d attempts: %s',
            func.__name__, len(delays), last_exc,
        )
        raise last_exc
    return wrapper
