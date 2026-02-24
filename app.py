"""Lightweight HTTP server exposing a /health endpoint.

Returns JSON with:
  - uptime_seconds: seconds since this process started
  - version:        BUILD_VERSION from config.py
  - db_row_count:   total rows across core tables from db.py
"""
import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

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
    STARTUP_OPTIONAL_INTEGRATIONS,
    STARTUP_REQUIRED_ENV_VARS,
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

        if API_KEY:
            auth = self.headers.get('Authorization', '')
            if auth != f'Bearer {API_KEY}':
                self.send_response(401)
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
            'database_url_set': bool(DATABASE_URL),
            'debug': DEBUG,
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
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
    print('  Config:')
    print(f'    DATABASE_URL : {"set" if DATABASE_URL else "NOT SET"}')
    print(f'    API_KEY auth : {"enabled" if API_KEY else "disabled (open)"}')
    print(f'    Debug mode   : {DEBUG}')
    print('  Integrations:')
    for label, var in STARTUP_OPTIONAL_INTEGRATIONS.items():
        status = 'configured' if os.environ.get(var) else 'not configured'
        print(f'    {label:<16}: {status}')
    missing = [v for v in STARTUP_REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        print('  Warnings:')
        for var in missing:
            print(f'    [MISSING] {var} is not set — app will not function correctly')
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
