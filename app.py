"""Lightweight HTTP server exposing a /health endpoint.

Returns JSON with:
  - uptime_seconds: seconds since this process started
  - version:        BUILD_VERSION from config.py
  - db_row_count:   total rows across core tables from db.py

Query parameters are sanitized on every request: whitespace is stripped,
values are limited to 500 chars, and empty strings are rejected with 400.
"""
import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from cache import Cache
from config import API_KEY, BUILD_VERSION, DATABASE_URL, DEBUG
from db import get_db
from utils.constants import (
    HEALTH_CONTENT_TYPE,
    HEALTH_DEFAULT_HOST,
    HEALTH_DEFAULT_PORT,
    HEALTH_ENDPOINT_PATH,
    HEALTH_STARTUP_MESSAGE,
    HEALTH_STATUS_DB_ERROR,
    HEALTH_STATUS_OK,
    MAX_INPUT_LENGTH,
)

_START_TIME = time.monotonic()
_cache = Cache(default_ttl=30)

_CORE_TABLES = [
    'customers',
    'orders',
    'order_items',
    'daily_sku_sales',
    'media_spend',
]


def _sanitize_input(value):
    """Strip whitespace, enforce 500-char limit, return None for empty input."""
    if not isinstance(value, str):
        return None
    value = value.strip()[:MAX_INPUT_LENGTH]
    return value if value else None


def _get_db_row_count():
    cached = _cache.get('db_row_count')
    if cached is not None:
        return cached
    total = 0
    with get_db() as conn:
        for table in _CORE_TABLES:
            row = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()
            if row:
                total += row[0]
    _cache.set('db_row_count', total)
    return total


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Sanitize all query parameters: strip whitespace, reject empty strings
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
            for v in values:
                if _sanitize_input(v) is None:
                    self._send_json(400, {'error': f"Invalid value for query parameter '{key}': must not be empty"})
                    return

        if path != HEALTH_ENDPOINT_PATH:
            self.send_response(404)
            self.end_headers()
            return

        if API_KEY:
            raw_auth = self.headers.get('Authorization', '')
            auth = _sanitize_input(raw_auth)
            if auth is None or auth != f'Bearer {API_KEY}':
                self._send_json(401, {'error': 'Unauthorized'})
                return

        try:
            db_row_count = _get_db_row_count()
            status = HEALTH_STATUS_OK
        except Exception as exc:
            db_row_count = None
            status = f'{HEALTH_STATUS_DB_ERROR}: {exc}'

        payload = {
            'status': status,
            'version': BUILD_VERSION,
            'uptime_seconds': round(time.monotonic() - _START_TIME, 2),
            'db_row_count': db_row_count,
            'database_url_set': bool(DATABASE_URL),
            'debug': DEBUG,
        }
        self._send_json(200, payload)

    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode()
        self.send_response(status_code)
        self.send_header('Content-Type', HEALTH_CONTENT_TYPE)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if DEBUG:
            super().log_message(fmt, *args)


def run(host=HEALTH_DEFAULT_HOST, port=HEALTH_DEFAULT_PORT):
    server = HTTPServer((host, port), HealthHandler)
    print(f'{HEALTH_STARTUP_MESSAGE} {host}:{port}{HEALTH_ENDPOINT_PATH}')
    server.serve_forever()


if __name__ == '__main__':
    run()
