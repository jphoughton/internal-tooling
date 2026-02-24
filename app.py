"""Lightweight HTTP server exposing /health and /items endpoints.

Endpoints:
  GET /health          - uptime, version, db row count
  GET /items           - paginated list of orders with pagination metadata
    Query params:
      page     (int, default 1)   - 1-based page number
      per_page (int, default 20)  - rows per page (max 100)

Returns JSON with:
  /health:
    - status, version, uptime_seconds, db_row_count, database_url_set, debug
  /items:
    - items, total_count, page, per_page, total_pages
"""
import json
import math
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from cache import Cache
from config import API_KEY, BUILD_VERSION, DATABASE_URL, DEBUG
from db import get_db, get_items_page
from utils.constants import (
    HEALTH_CONTENT_TYPE,
    HEALTH_DEFAULT_HOST,
    HEALTH_DEFAULT_PORT,
    HEALTH_ENDPOINT_PATH,
    HEALTH_STARTUP_MESSAGE,
    HEALTH_STATUS_DB_ERROR,
    HEALTH_STATUS_OK,
    ITEMS_DEFAULT_PAGE,
    ITEMS_DEFAULT_PER_PAGE,
    ITEMS_ENDPOINT_PATH,
    ITEMS_MAX_PER_PAGE,
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


def _parse_int_param(params, key, default, min_val=1, max_val=None):
    """Parse an integer query parameter with bounds checking."""
    values = params.get(key)
    if not values:
        return default
    try:
        val = int(values[0])
    except (ValueError, TypeError):
        return default
    val = max(min_val, val)
    if max_val is not None:
        val = min(max_val, val)
    return val


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == HEALTH_ENDPOINT_PATH:
            self._handle_health()
        elif path == ITEMS_ENDPOINT_PATH:
            self._handle_items(parsed)
        else:
            self.send_response(404)
            self.end_headers()

    def _check_auth(self):
        """Return True if auth passes (or no API key configured)."""
        if not API_KEY:
            return True
        auth = self.headers.get('Authorization', '')
        return auth == f'Bearer {API_KEY}'

    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode()
        self.send_response(status_code)
        self.send_header('Content-Type', HEALTH_CONTENT_TYPE)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_health(self):
        if not self._check_auth():
            self.send_response(401)
            self.end_headers()
            return

        try:
            db_row_count = _get_db_row_count()
            status = HEALTH_STATUS_OK
        except Exception as exc:
            db_row_count = None
            status = f'{HEALTH_STATUS_DB_ERROR}: {exc}'

        self._send_json(200, {
            'status': status,
            'version': BUILD_VERSION,
            'uptime_seconds': round(time.monotonic() - _START_TIME, 2),
            'db_row_count': db_row_count,
            'database_url_set': bool(DATABASE_URL),
            'debug': DEBUG,
        })

    def _handle_items(self, parsed):
        if not self._check_auth():
            self.send_response(401)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        page = _parse_int_param(params, 'page', ITEMS_DEFAULT_PAGE, min_val=1)
        per_page = _parse_int_param(
            params, 'per_page', ITEMS_DEFAULT_PER_PAGE,
            min_val=1, max_val=ITEMS_MAX_PER_PAGE,
        )

        try:
            with get_db() as conn:
                items, total_count = get_items_page(conn, page=page, per_page=per_page)
            total_pages = math.ceil(total_count / per_page) if per_page else 0
            self._send_json(200, {
                'items': items,
                'total_count': total_count,
                'page': page,
                'per_page': per_page,
                'total_pages': total_pages,
            })
        except Exception as exc:
            self._send_json(500, {'error': str(exc)})

    def log_message(self, fmt, *args):
        if DEBUG:
            super().log_message(fmt, *args)


def run(host=HEALTH_DEFAULT_HOST, port=HEALTH_DEFAULT_PORT):
    server = HTTPServer((host, port), HealthHandler)
    print(f'{HEALTH_STARTUP_MESSAGE} {host}:{port}{HEALTH_ENDPOINT_PATH}')
    server.serve_forever()


if __name__ == '__main__':
    run()
