"""Simple in-memory cache with per-entry TTL support.

Usage:
    from cache import Cache

    _cache = Cache(default_ttl=30)
    _cache.set('my_key', expensive_result)        # uses default_ttl
    _cache.set('my_key', result, ttl_seconds=60)  # explicit TTL

    value = _cache.get('my_key')                  # None if missing/expired
    _cache.invalidate('my_key')                   # remove immediately
"""
from __future__ import annotations

import time
from typing import Any, Optional


class Cache:
    """In-memory cache backed by a plain dict with per-entry TTL expiry.

    Each entry is stored as ``(value, expiry_timestamp)``.  An entry is
    considered expired once ``time.monotonic()`` exceeds its timestamp.

    Args:
        default_ttl: Seconds to keep an entry when *ttl_seconds* is not
            supplied to :meth:`set`.  Defaults to 60 seconds.
    """

    def __init__(self, default_ttl: float = 60) -> None:
        self._default_ttl = default_ttl
        self._store: dict[str, tuple[Any, float]] = {}

    def get(self, key: str, default: Any = None) -> Any:
        """Return the cached value for *key*, or *default* if missing/expired."""
        entry = self._store.get(key)
        if entry is None:
            return default
        value, expiry = entry
        if time.monotonic() > expiry:
            del self._store[key]
            return default
        return value

    def set(self, key: str, value: Any, ttl_seconds: Optional[float] = None) -> None:
        """Store *value* under *key*.

        Args:
            key: Cache key.
            value: Value to store.
            ttl_seconds: How long to keep the entry.  Defaults to the
                *default_ttl* set on construction.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        self._store[key] = (value, time.monotonic() + ttl)

    def invalidate(self, key: str) -> None:
        """Remove *key* from the cache (no-op if absent)."""
        self._store.pop(key, None)
