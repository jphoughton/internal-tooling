"""Lightweight HTTP server exposing a /health endpoint.

Returns JSON with:
  - uptime_seconds: seconds since this process started
  - version:        BUILD_VERSION from config.py
  - db_row_count:   total rows across core tables from db.py
"""
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from config import BUILD_VERSION
from db import get_db
from utils.constants import (
    HEALTH_CONTENT_TYPE,
    HEALTH_DEFAULT_HOST,
    HEALTH_DEFAULT_PORT,
    HEALTH_ENDPOINT_PATH,
    HEALTH_STATUS_DB_ERROR,
    HEALTH_STATUS_OK,
)

_START_TIME = time.monotonic()

_CORE_TABLES = [
    'customers',
    'orders',
    'order_items',
    'daily_sku_sales',
    'media_spend',
]


def _get_db_row_count():
    total = 0
    with get_db() as conn:
        for table in _CORE_TABLES:
            row = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()
            if row:
                total += row[0]
    return total


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != HEALTH_ENDPOINT_PATH:
            self.send_response(404)
            self.end_headers()
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
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', HEALTH_CONTENT_TYPE)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # suppress default access log noise
        pass


def run(host=HEALTH_DEFAULT_HOST, port=HEALTH_DEFAULT_PORT):
    server = HTTPServer((host, port), HealthHandler)
    print(f'Health check server listening on {host}:{port}{HEALTH_ENDPOINT_PATH}')
    server.serve_forever()


if __name__ == '__main__':
    run()
