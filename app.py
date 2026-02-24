"""Lightweight HTTP server exposing /health and /export/csv endpoints.

Returns JSON with:
  - uptime_seconds: seconds since this process started
  - version:        BUILD_VERSION from config.py
  - db_row_count:   total rows across core tables from db.py
"""
import csv
import io
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from config import API_KEY, BUILD_VERSION, DATABASE_URL, DEBUG
from db import fetch_table, get_db
from utils.constants import (
    HEALTH_CONTENT_TYPE,
    HEALTH_DEFAULT_HOST,
    HEALTH_DEFAULT_PORT,
    HEALTH_ENDPOINT_PATH,
    HEALTH_STARTUP_MESSAGE,
    HEALTH_STATUS_DB_ERROR,
    EXPORT_CSV_ALLOWED_TABLES,
    EXPORT_CSV_CONTENT_TYPE,
    EXPORT_CSV_DEFAULT_TABLE,
    EXPORT_CSV_ENDPOINT_PATH,
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
        parsed = urlparse(self.path)
        path = parsed.path

        if API_KEY:
            auth = self.headers.get('Authorization', '')
            if auth != f'Bearer {API_KEY}':
                self._send_json(401, {'error': 'Unauthorized'})
                return

        if path == HEALTH_ENDPOINT_PATH:
            self._handle_health()
        elif path == EXPORT_CSV_ENDPOINT_PATH:
            params = parse_qs(parsed.query)
            raw_table = params.get('table', [EXPORT_CSV_DEFAULT_TABLE])[0]
            table = raw_table.strip() or EXPORT_CSV_DEFAULT_TABLE
            if table not in EXPORT_CSV_ALLOWED_TABLES:
                self._send_json(400, {'error': f'Unknown table: {table!r}. Allowed: {sorted(EXPORT_CSV_ALLOWED_TABLES)}'})
                return
            self._handle_export_csv(table)
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_health(self):
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

    def _handle_export_csv(self, table: str):
        try:
            with get_db() as conn:
                columns, rows = fetch_table(conn, table)
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(columns)
            writer.writerows(rows)
            body = buf.getvalue().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', EXPORT_CSV_CONTENT_TYPE)
            self.send_header('Content-Disposition', f'attachment; filename="{table}.csv"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            self._send_json(500, {'error': str(exc)})

    def _send_json(self, status_code: int, payload: dict):
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
