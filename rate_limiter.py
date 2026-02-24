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

import threading
import time


class RateLimiter:
    """Fixed-window in-memory rate limiter keyed by IP address.

    Thread-safe: all access to the shared window dict is protected by a lock.
    Expired entries are evicted periodically to prevent unbounded memory growth.

    Args:
        max_requests: Maximum number of requests allowed per *window_seconds*.
        window_seconds: Duration of each counting window in seconds.
        evict_interval: How often (in seconds) to scan and drop expired entries.
    """

    def __init__(
        self,
        max_requests: int = 60,
        window_seconds: float = 60,
        evict_interval: float = 300,
    ) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._evict_interval = evict_interval
        # ip -> (request_count, window_start_monotonic)
        self._windows: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self._last_eviction = time.monotonic()

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
        with self._lock:
            self._maybe_evict(now)
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

    def _maybe_evict(self, now: float) -> None:
        """Remove expired entries if the eviction interval has elapsed.

        Must be called with *self._lock* already held.
        """
        if now - self._last_eviction < self._evict_interval:
            return
        cutoff = now - self._window_seconds
        expired = [ip for ip, (_, start) in self._windows.items() if start < cutoff]
        for ip in expired:
            del self._windows[ip]
        self._last_eviction = now
