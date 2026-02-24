"""In-memory per-IP rate limiter using a fixed sliding window.

Each IP address is allowed at most *max_requests* requests within any
*window_seconds*-wide time window.  Once the limit is exceeded the caller
receives False until the window resets.

Usage::

    from rate_limiter import RateLimiter

    _limiter = RateLimiter(max_requests=60, window_seconds=60)

    if not _limiter.is_allowed(client_ip):
        # respond with HTTP 429
        ...
"""
from __future__ import annotations

import time


class RateLimiter:
    """Fixed-window in-memory rate limiter keyed by IP address.

    Args:
        max_requests: Maximum number of requests allowed per *window_seconds*.
        window_seconds: Duration of each counting window in seconds.
    """

    def __init__(self, max_requests: int = 60, window_seconds: float = 60) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        # ip -> (request_count, window_start_monotonic)
        self._windows: dict[str, tuple[int, float]] = {}

    @property
    def window_seconds(self) -> float:
        """Duration of each counting window in seconds."""
        return self._window_seconds

    def is_allowed(self, ip: str) -> bool:
        """Check whether a request from *ip* is within the rate limit.

        Increments the counter for *ip* if allowed.  Returns False without
        incrementing when the limit is already reached for the current window.

        Args:
            ip: The client IP address string.

        Returns:
            True if the request is permitted, False if it should be rejected.
        """
        now = time.monotonic()
        entry = self._windows.get(ip)

        if entry is None or now - entry[1] >= self._window_seconds:
            # First request or previous window has expired — open a new window.
            self._windows[ip] = (1, now)
            return True

        count, window_start = entry
        if count >= self._max_requests:
            return False

        self._windows[ip] = (count + 1, window_start)
        return True


class RateLimitMiddleware:
    """WSGI middleware that enforces per-IP rate limiting.

    Wraps any WSGI application. Requests beyond the configured limit receive
    an HTTP 429 Too Many Requests response; the inner app is not called.

    Usage::

        from rate_limiter import RateLimiter, RateLimitMiddleware

        limiter = RateLimiter(max_requests=60, window_seconds=60)
        app = RateLimitMiddleware(your_wsgi_app, limiter)
    """

    def __init__(self, app, limiter: RateLimiter | None = None) -> None:
        self._app = app
        self._limiter = limiter if limiter is not None else RateLimiter()

    @staticmethod
    def _get_client_ip(environ: dict) -> str:
        """Extract the real client IP, respecting X-Forwarded-For."""
        forwarded_for = environ.get('HTTP_X_FORWARDED_FOR', '')
        if forwarded_for:
            # Take the first (leftmost) address — the original client
            return forwarded_for.split(',')[0].strip()
        return environ.get('REMOTE_ADDR', '127.0.0.1')

    def __call__(self, environ: dict, start_response):
        ip = self._get_client_ip(environ)
        if not self._limiter.is_allowed(ip):
            headers = [
                ('Content-Type', 'application/json'),
                ('Retry-After', str(int(self._limiter.window_seconds))),
            ]
            start_response('429 Too Many Requests', headers)
            return [b'{"error":"Too Many Requests","detail":"Rate limit exceeded. Try again later."}']
        return self._app(environ, start_response)
