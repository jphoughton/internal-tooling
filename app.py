"""Lightweight HTTP server exposing /health and /export/csv endpoints.

Returns JSON with:
  - uptime_seconds: seconds since this process started
  - version:        BUILD_VERSION from config.py
  - db_row_count:   total rows across core tables from db.py

GET /export/csv streams all daily_sku_sales rows as a CSV download.

Query parameters are sanitized on every request: whitespace is stripped,
values are limited to 500 chars, and empty strings are rejected with 400.
"""
import csv
import io
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
    EXPORT_CSV_CONTENT_TYPE,
    EXPORT_CSV_ENDPOINT_PATH,
    HEALTH_CONTENT_TYPE,
    HEALTH_DEFAULT_HOST,
    HEALTH_DEFAULT_PORT,
    HEALTH_ENDPOINT_PATH,
    HEALTH_STARTUP_MESSAGE,
    HEALTH_STATUS_DB_ERROR,
    HEALTH_STATUS_OK,
    MAX_INPUT_LENGTH,
    STARTUP_OPTIONAL_INTEGRATIONS,
    STARTUP_REQUIRED_ENV_VARS,
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

_EXPORT_COLUMNS = ['sale_date', 'sku', 'source', 'units_sold', 'revenue', 'order_count']
_EXPORT_BATCH_SIZE = 1000


def _stream_inventory_csv(wfile) -> None:
    """Stream daily_sku_sales as CSV rows directly to wfile in 1 000-row batches.

    Writing in batches avoids holding the entire result set in memory.
    db.py uses psycopg2 RealDictCursor wrapped in _Row (a dict subclass),
    so both row['col'] and list(row.values()) access work correctly.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_EXPORT_COLUMNS)
    wfile.write(buf.getvalue().encode('utf-8'))

    with get_db() as conn:
        cursor = conn.execute(
            'SELECT sale_date, sku, source, units_sold, revenue, order_count '
            'FROM daily_sku_sales ORDER BY sale_date, sku, source'
        )
        while True:
            rows = cursor.fetchmany(_EXPORT_BATCH_SIZE)
            if not rows:
                break
            buf.seek(0)
            buf.truncate(0)
            for row in rows:
                writer.writerow([row[col] for col in _EXPORT_COLUMNS])
            wfile.write(buf.getvalue().encode('utf-8'))


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
        parsed = urlparse(self.path)
        path = parsed.path

        # Sanitize all query parameters: strip whitespace, reject empty strings
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
            for v in values:
                if _sanitize_input(v) is None:
                    self._send_json(400, {'error': f"Invalid value for query parameter '{key}': must not be empty"})
                    return

        if path == EXPORT_CSV_ENDPOINT_PATH:
            self._handle_export_csv()
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

    def _handle_export_csv(self):
        """Handle GET /export/csv — stream daily_sku_sales as a CSV download.

        Rows are written in batches of _EXPORT_BATCH_SIZE to avoid loading
        the full result set into memory. Content-Length is omitted because
        the total size is not known before streaming begins.
        """
        if API_KEY:
            raw_auth = self.headers.get('Authorization', '')
            auth = _sanitize_input(raw_auth)
            if auth is None or auth != f'Bearer {API_KEY}':
                self._send_json(401, {'error': 'Unauthorized'})
                return

        date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        filename = f'inventory_export_{date_str}.csv'

        self.send_response(200)
        self.send_header('Content-Type', f'{EXPORT_CSV_CONTENT_TYPE}; charset=utf-8')
        self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()

        try:
            _stream_inventory_csv(self.wfile)
        except Exception as exc:
            # Headers already sent; log the error but cannot send an HTTP error response.
            print(json.dumps({'event': 'export_error', 'error': str(exc)}), flush=True)

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
