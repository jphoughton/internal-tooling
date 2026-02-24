"""Tests for GET /items paginated endpoint in app.py."""
import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import HTTPServer
from unittest.mock import MagicMock, patch

import app
from app import HealthHandler
from utils.constants import (
    INVENTORY_ITEMS_DEFAULT_PAGE,
    INVENTORY_ITEMS_DEFAULT_PER_PAGE,
    INVENTORY_ITEMS_ENDPOINT_PATH,
    INVENTORY_ITEMS_MAX_PER_PAGE,
)

_FAKE_ITEMS = [
    {'id': 1, 'order_id': 'ORD-1', 'sku': 'SKU-A', 'quantity': 2, 'unit_price': 10.0},
    {'id': 2, 'order_id': 'ORD-2', 'sku': 'SKU-B', 'quantity': 3, 'unit_price': 20.0},
]
_FAKE_TOTAL = 42  # total rows in table (more than items to test paging math)


def _make_request(path, headers=None, items=None, total_count=None):
    """Start a one-shot test server, send GET, return (status, headers, body_str)."""
    if items is None:
        items = _FAKE_ITEMS
    if total_count is None:
        total_count = _FAKE_TOTAL

    mock_conn = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_ctx.__exit__ = MagicMock(return_value=False)

    server = HTTPServer(('127.0.0.1', 0), HealthHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    try:
        with patch('app.get_db', return_value=mock_ctx), \
             patch('app.get_inventory_items_page', return_value=(items, total_count)):
            conn = HTTPConnection('127.0.0.1', port, timeout=5)
            conn.request('GET', path, headers=headers or {})
            resp = conn.getresponse()
            status = resp.status
            resp_headers = dict(resp.getheaders())
            body = resp.read().decode('utf-8')
            conn.close()
    finally:
        t.join(timeout=2)
        server.server_close()
    return status, resp_headers, body


class TestInventoryItemsAuthentication(unittest.TestCase):
    def setUp(self):
        self._orig_api_key = app.API_KEY
        self._orig_is_allowed = app._rate_limiter.is_allowed
        app._rate_limiter.is_allowed = lambda _ip: True

    def tearDown(self):
        app.API_KEY = self._orig_api_key
        app._rate_limiter.is_allowed = self._orig_is_allowed

    def test_no_auth_required_when_api_key_unset(self):
        app.API_KEY = ''
        status, _, _ = _make_request(INVENTORY_ITEMS_ENDPOINT_PATH)
        self.assertEqual(status, 200)

    def test_returns_401_when_no_auth_header(self):
        app.API_KEY = 'supersecret'
        server = HTTPServer(('127.0.0.1', 0), HealthHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.handle_request, daemon=True)
        t.start()
        try:
            conn = HTTPConnection('127.0.0.1', port, timeout=5)
            conn.request('GET', INVENTORY_ITEMS_ENDPOINT_PATH)
            resp = conn.getresponse()
            status = resp.status
            conn.close()
        finally:
            t.join(timeout=2)
            server.server_close()
        self.assertEqual(status, 401)

    def test_returns_401_with_wrong_bearer_token(self):
        app.API_KEY = 'supersecret'
        server = HTTPServer(('127.0.0.1', 0), HealthHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.handle_request, daemon=True)
        t.start()
        try:
            conn = HTTPConnection('127.0.0.1', port, timeout=5)
            conn.request('GET', INVENTORY_ITEMS_ENDPOINT_PATH, headers={'Authorization': 'Bearer wrong'})
            resp = conn.getresponse()
            status = resp.status
            conn.close()
        finally:
            t.join(timeout=2)
            server.server_close()
        self.assertEqual(status, 401)

    def test_returns_200_with_correct_bearer_token(self):
        app.API_KEY = 'supersecret'
        status, _, _ = _make_request(
            INVENTORY_ITEMS_ENDPOINT_PATH,
            headers={'Authorization': 'Bearer supersecret'},
        )
        self.assertEqual(status, 200)


class TestInventoryItemsValidation(unittest.TestCase):
    def setUp(self):
        self._orig_api_key = app.API_KEY
        self._orig_is_allowed = app._rate_limiter.is_allowed
        app.API_KEY = ''
        app._rate_limiter.is_allowed = lambda _ip: True

    def tearDown(self):
        app.API_KEY = self._orig_api_key
        app._rate_limiter.is_allowed = self._orig_is_allowed

    def test_non_integer_page_returns_400(self):
        status, _, body = _make_request(f'{INVENTORY_ITEMS_ENDPOINT_PATH}?page=abc')
        self.assertEqual(status, 400)
        self.assertIn('integer', json.loads(body)['error'])

    def test_non_integer_per_page_returns_400(self):
        status, _, body = _make_request(f'{INVENTORY_ITEMS_ENDPOINT_PATH}?per_page=xyz')
        self.assertEqual(status, 400)
        self.assertIn('integer', json.loads(body)['error'])

    def test_page_zero_returns_400(self):
        status, _, body = _make_request(f'{INVENTORY_ITEMS_ENDPOINT_PATH}?page=0')
        self.assertEqual(status, 400)
        self.assertIn('page', json.loads(body)['error'])

    def test_page_negative_returns_400(self):
        status, _, _ = _make_request(f'{INVENTORY_ITEMS_ENDPOINT_PATH}?page=-1')
        self.assertEqual(status, 400)

    def test_per_page_zero_returns_400(self):
        status, _, _ = _make_request(f'{INVENTORY_ITEMS_ENDPOINT_PATH}?per_page=0')
        self.assertEqual(status, 400)

    def test_per_page_exceeds_max_returns_400(self):
        status, _, body = _make_request(
            f'{INVENTORY_ITEMS_ENDPOINT_PATH}?per_page={INVENTORY_ITEMS_MAX_PER_PAGE + 1}'
        )
        self.assertEqual(status, 400)
        self.assertIn(str(INVENTORY_ITEMS_MAX_PER_PAGE), json.loads(body)['error'])

    def test_per_page_at_max_returns_200(self):
        status, _, _ = _make_request(
            f'{INVENTORY_ITEMS_ENDPOINT_PATH}?per_page={INVENTORY_ITEMS_MAX_PER_PAGE}'
        )
        self.assertEqual(status, 200)

    def test_page_one_returns_200(self):
        status, _, _ = _make_request(f'{INVENTORY_ITEMS_ENDPOINT_PATH}?page=1')
        self.assertEqual(status, 200)


class TestInventoryItemsResponseFormat(unittest.TestCase):
    def setUp(self):
        self._orig_api_key = app.API_KEY
        self._orig_is_allowed = app._rate_limiter.is_allowed
        app.API_KEY = ''
        app._rate_limiter.is_allowed = lambda _ip: True

    def tearDown(self):
        app.API_KEY = self._orig_api_key
        app._rate_limiter.is_allowed = self._orig_is_allowed

    def _get_json(self, qs='', items=None, total_count=None):
        path = INVENTORY_ITEMS_ENDPOINT_PATH + qs
        _, _, body = _make_request(path, items=items, total_count=total_count)
        return json.loads(body)

    def test_response_contains_items_key(self):
        data = self._get_json()
        self.assertIn('items', data)

    def test_response_contains_page_key(self):
        data = self._get_json()
        self.assertIn('page', data)

    def test_response_contains_per_page_key(self):
        data = self._get_json()
        self.assertIn('per_page', data)

    def test_response_contains_total_count_key(self):
        data = self._get_json()
        self.assertIn('total_count', data)

    def test_response_contains_total_pages_key(self):
        data = self._get_json()
        self.assertIn('total_pages', data)

    def test_default_page_is_1(self):
        data = self._get_json()
        self.assertEqual(data['page'], INVENTORY_ITEMS_DEFAULT_PAGE)

    def test_default_per_page(self):
        data = self._get_json()
        self.assertEqual(data['per_page'], INVENTORY_ITEMS_DEFAULT_PER_PAGE)

    def test_custom_page_reflected_in_response(self):
        data = self._get_json('?page=3')
        self.assertEqual(data['page'], 3)

    def test_custom_per_page_reflected_in_response(self):
        data = self._get_json('?per_page=5')
        self.assertEqual(data['per_page'], 5)

    def test_total_count_matches_mock(self):
        data = self._get_json(total_count=99)
        self.assertEqual(data['total_count'], 99)

    def test_total_pages_ceiling_division(self):
        # 99 items, 20 per page -> ceil(99/20) = 5 pages
        data = self._get_json('?per_page=20', total_count=99)
        self.assertEqual(data['total_pages'], 5)

    def test_total_pages_exact_division(self):
        # 100 items, 20 per page -> exactly 5 pages
        data = self._get_json('?per_page=20', total_count=100)
        self.assertEqual(data['total_pages'], 5)

    def test_total_pages_minimum_is_1_when_empty(self):
        data = self._get_json(items=[], total_count=0)
        self.assertEqual(data['total_pages'], 1)

    def test_items_list_matches_mock(self):
        data = self._get_json()
        self.assertEqual(len(data['items']), 2)

    def test_empty_items_returns_empty_list(self):
        data = self._get_json(items=[], total_count=0)
        self.assertEqual(data['items'], [])

    def test_items_contain_expected_fields(self):
        data = self._get_json()
        first = data['items'][0]
        self.assertIn('id', first)
        self.assertIn('sku', first)


if __name__ == '__main__':
    unittest.main()
