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
from rate_limiter import RateLimiter
from utils.constants import (
    HEALTH_CONTENT_TYPE,
    HEALTH_DEFAULT_HOST,
    HEALTH_DEFAULT_PORT,
    HEALTH_ENDPOINT_PATH,
    HEALTH_STARTUP_MESSAGE,
    HEALTH_STATUS_DB_ERROR,
    HEALTH_STATUS_OK,
    MAX_INPUT_LENGTH,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    STARTUP_OPTIONAL_INTEGRATIONS,
    STARTUP_REQUIRED_ENV_VARS,
)

_START_TIME = time.monotonic()
_cache = Cache(default_ttl=30)
_rate_limiter = RateLimiter(max_requests=RATE_LIMIT_REQUESTS, window_seconds=RATE_LIMIT_WINDOW_SECONDS)

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
    def handle_one_request(self):
        self._req_start = time.monotonic()
        super().handle_one_request()

    def log_request(self, code='-', size='-'):
        elapsed_ms = round((time.monotonic() - self._req_start) * 1000, 2)
        entry = {
            'method': getattr(self, 'command', None) or '-',
            'path': getattr(self, 'path', None) or '-',
            'status': int(code) if str(code).isdigit() else str(code),
            'response_time_ms': elapsed_ms,
        }
        print(json.dumps(entry), flush=True)

    def do_GET(self):
        # Rate limit: 60 requests per minute per IP
        client_ip = self.client_address[0]
        if not _rate_limiter.is_allowed(client_ip):
            self._send_json(429, {'error': 'Too Many Requests'})
            return

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


def _validate_startup() -> None:
    """Log a startup banner, check required env vars, and verify DB connectivity."""
    sep = '=' * 60
    print(sep)
    print('  Hydrant Command Center — Health Server')
    print(f'  Version : {BUILD_VERSION}')
    print(f'  Started : {datetime.now(timezone.utc).isoformat(timespec="seconds")} UTC')
    print(f'  Endpoint: {HEALTH_DEFAULT_HOST}:{HEALTH_DEFAULT_PORT}{HEALTH_ENDPOINT_PATH}')
    print(sep)

    # --- Config summary ---
    print('  Config:')
    print(f'    DATABASE_URL : {"set" if DATABASE_URL else "NOT SET ⚠"}')
    print(f'    API_KEY auth : {"enabled" if API_KEY else "disabled (open)"}')
    print(f'    Debug mode   : {DEBUG}')

    # --- Optional integrations ---
    print('  Integrations:')
    for label, var in STARTUP_OPTIONAL_INTEGRATIONS.items():
        status = 'configured' if os.environ.get(var) else 'not configured'
        print(f'    {label:<16}: {status}')

    # --- Required env var warnings ---
    missing = [v for v in STARTUP_REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        print('  Warnings:')
        for var in missing:
            print(f'    [MISSING] {var} is not set — app will not function correctly')

    # --- DB connectivity test ---
    print('  DB connection  : ', end='', flush=True)
    try:
        with get_db() as conn:
            conn.execute('SELECT 1').fetchone()
        print('ok')
    except Exception as exc:
        print(f'FAILED — {exc}')
        print(f'  [DB ERROR] {exc}')

    print(sep)


def run(host=HEALTH_DEFAULT_HOST, port=HEALTH_DEFAULT_PORT):
    _validate_startup()
    server = HTTPServer((host, port), HealthHandler)
    server.serve_forever()


if __name__ == '__main__':
    run()
