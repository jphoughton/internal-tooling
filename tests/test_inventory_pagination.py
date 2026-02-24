"""Unit tests for the paginated GET /items endpoint.

Covers:
  - get_order_items_page() in db.py: LIMIT/OFFSET SQL, total_count,
    correct page slicing, and error wrapping.
  - _handle_inventory_list() in app.py: parameter parsing, validation,
    and response metadata (total_pages, has_next, has_prev).
"""
import io
import json
import unittest
from http.server import BaseHTTPRequestHandler
from unittest.mock import MagicMock, patch

from db import DatabaseError, get_order_items_page


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn(rows, total_count):
    """Return a mock ConnectionWrapper.

    execute() returns different results depending on which query is run:
    - SELECT COUNT(*) -> a single row with 'cnt' = total_count
    - SELECT ... LIMIT -> rows (list of dicts accessible by column name)
    """
    count_row = MagicMock()
    count_row.__getitem__ = lambda self, key: total_count if key == 'cnt' else None

    data_rows = []
    for r in rows:
        row = MagicMock()
        row.__iter__ = lambda self, r=r: iter(r.keys())
        row.keys = lambda r=r: r.keys()
        # dict(row) calls needs keys()
        data_rows.append(r)

    def _execute(sql, params=None):
        cursor = MagicMock()
        if 'COUNT(*)' in sql:
            cursor.fetchone.return_value = count_row
            cursor.fetchall.return_value = [count_row]
        else:
            # Return real dicts so dict(row) works
            cursor.fetchall.return_value = rows
            cursor.fetchone.return_value = rows[0] if rows else None
        return cursor

    conn = MagicMock()
    conn.execute.side_effect = _execute
    return conn


# ---------------------------------------------------------------------------
# Tests for db.get_order_items_page
# ---------------------------------------------------------------------------

class TestGetInventoryItemsPage(unittest.TestCase):

    def _rows(self, n):
        return [
            {'id': i, 'order_id': 100 + i, 'sku': f'SKU-{i}',
             'quantity': 1, 'unit_price': 9.99}
            for i in range(1, n + 1)
        ]

    def test_returns_items_and_total_count(self):
        rows = self._rows(3)
        conn = MagicMock()

        count_row = MagicMock()
        count_row.__getitem__ = lambda self, k: 3

        def execute(sql, params=None):
            cur = MagicMock()
            if 'COUNT(*)' in sql:
                cur.fetchone.return_value = count_row
            else:
                cur.fetchall.return_value = rows
            return cur

        conn.execute.side_effect = execute

        items, total = get_order_items_page(conn, page=1, per_page=20)
        assert total == 3
        assert items == rows

    def test_uses_limit_and_offset(self):
        """LIMIT and OFFSET are passed as SQL parameters for page 2."""
        rows = self._rows(5)
        calls = []

        count_row = MagicMock()
        count_row.__getitem__ = lambda self, k: 50

        def execute(sql, params=None):
            calls.append((sql, params))
            cur = MagicMock()
            if 'COUNT(*)' in sql:
                cur.fetchone.return_value = count_row
            else:
                cur.fetchall.return_value = rows
            return cur

        conn = MagicMock()
        conn.execute.side_effect = execute

        get_order_items_page(conn, page=2, per_page=5)

        # Find the SELECT ... LIMIT call
        select_call = next(c for c in calls if 'LIMIT' in c[0])
        limit, offset = select_call[1]
        assert limit == 5
        assert offset == 5   # (page-1) * per_page = 1 * 5

    def test_first_page_has_zero_offset(self):
        calls = []
        count_row = MagicMock()
        count_row.__getitem__ = lambda self, k: 10

        def execute(sql, params=None):
            calls.append((sql, params))
            cur = MagicMock()
            if 'COUNT(*)' in sql:
                cur.fetchone.return_value = count_row
            else:
                cur.fetchall.return_value = []
            return cur

        conn = MagicMock()
        conn.execute.side_effect = execute

        get_order_items_page(conn, page=1, per_page=10)
        select_call = next(c for c in calls if 'LIMIT' in c[0])
        _, offset = select_call[1]
        assert offset == 0

    def test_empty_table_returns_zero_total(self):
        count_row = MagicMock()
        count_row.__getitem__ = lambda self, k: 0

        def execute(sql, params=None):
            cur = MagicMock()
            if 'COUNT(*)' in sql:
                cur.fetchone.return_value = count_row
            else:
                cur.fetchall.return_value = []
            return cur

        conn = MagicMock()
        conn.execute.side_effect = execute

        items, total = get_order_items_page(conn, page=1, per_page=20)
        assert total == 0
        assert items == []

    def test_wraps_unexpected_exception_in_database_error(self):
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError('boom')

        with self.assertRaises(DatabaseError):
            get_order_items_page(conn, page=1, per_page=20)

    def test_re_raises_database_error_unchanged(self):
        conn = MagicMock()
        conn.execute.side_effect = DatabaseError('original')

        with self.assertRaises(DatabaseError) as ctx:
            get_order_items_page(conn, page=1, per_page=20)
        assert 'original' in str(ctx.exception)


# ---------------------------------------------------------------------------
# Tests for app._handle_inventory_list (response metadata)
# ---------------------------------------------------------------------------

class TestInventoryListEndpointMetadata(unittest.TestCase):
    """Test the response shape produced by _handle_inventory_list."""

    def _call_handler(self, query_string, db_items, db_total):
        """Invoke _handle_inventory_list and return the parsed JSON response."""
        from app import HealthHandler

        responses = []

        class FakeHandler(HealthHandler):
            def __init__(self):
                # Skip BaseHTTPRequestHandler.__init__ entirely
                pass

            def _send_json(self, status, payload):
                responses.append((status, payload))

        handler = FakeHandler()

        with patch('app.get_db') as mock_get_db, \
             patch('app.get_order_items_page', return_value=(db_items, db_total)):
            handler._handle_inventory_list(query_string)

        return responses[0]  # (status_code, payload)

    def test_default_params_return_metadata(self):
        items = [{'id': 1, 'sku': 'ABC', 'quantity': 2, 'unit_price': 5.0}]
        status, payload = self._call_handler('', items, 1)
        assert status == 200
        assert payload['page'] == 1
        assert payload['per_page'] == 20
        assert payload['total_count'] == 1
        assert payload['total_pages'] == 1
        assert payload['has_next'] is False
        assert payload['has_prev'] is False
        assert payload['items'] == items

    def test_page_2_of_3(self):
        items = [{'id': 11}]
        status, payload = self._call_handler('page=2&per_page=10', items, 25)
        assert status == 200
        assert payload['page'] == 2
        assert payload['per_page'] == 10
        assert payload['total_count'] == 25
        assert payload['total_pages'] == 3
        assert payload['has_next'] is True
        assert payload['has_prev'] is True

    def test_last_page_has_no_next(self):
        items = [{'id': 5}]
        status, payload = self._call_handler('page=3&per_page=10', items, 25)
        assert payload['has_next'] is False
        assert payload['has_prev'] is True

    def test_invalid_page_returns_400(self):
        from app import HealthHandler

        responses = []

        class FakeHandler(HealthHandler):
            def __init__(self):
                pass

            def _send_json(self, status, payload):
                responses.append((status, payload))

        handler = FakeHandler()
        handler._handle_inventory_list('page=0')
        status, payload = responses[0]
        assert status == 400
        assert 'page' in payload['error']

    def test_non_integer_per_page_returns_400(self):
        from app import HealthHandler

        responses = []

        class FakeHandler(HealthHandler):
            def __init__(self):
                pass

            def _send_json(self, status, payload):
                responses.append((status, payload))

        handler = FakeHandler()
        handler._handle_inventory_list('per_page=abc')
        status, payload = responses[0]
        assert status == 400
        assert 'per_page' in payload['error']

    def test_per_page_exceeding_max_returns_400(self):
        from app import HealthHandler
        from utils.constants import INVENTORY_ITEMS_MAX_PER_PAGE

        responses = []

        class FakeHandler(HealthHandler):
            def __init__(self):
                pass

            def _send_json(self, status, payload):
                responses.append((status, payload))

        handler = FakeHandler()
        handler._handle_inventory_list(f'per_page={INVENTORY_ITEMS_MAX_PER_PAGE + 1}')
        status, payload = responses[0]
        assert status == 400

    def test_total_pages_rounds_up(self):
        items = []
        status, payload = self._call_handler('per_page=10', items, 21)
        assert payload['total_pages'] == 3  # ceil(21/10)

    def test_empty_table_returns_zero_pages(self):
        status, payload = self._call_handler('', [], 0)
        assert payload['total_count'] == 0
        assert payload['total_pages'] == 0
        assert payload['has_next'] is False
        assert payload['has_prev'] is False


if __name__ == '__main__':
    unittest.main()
