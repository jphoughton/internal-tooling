"""Tests for Shopify refund extraction helpers."""
import pytest
from etl.shopify_client import _refund_amount, _refund_units, _total_refund_amount


class TestRefundAmount:
    def test_transactions_path(self):
        # Successful refund transactions are summed (most accurate path).
        refund = {
            'transactions': [
                {'kind': 'refund', 'status': 'success', 'amount': '10.00'},
                {'kind': 'refund', 'status': 'success', 'amount': '5.50'},
            ],
            'refund_line_items': [
                {'subtotal': 99.0, 'total_tax': 9.0, 'quantity': 3},
            ],
        }
        # Transactions win over line items when present.
        assert _refund_amount(refund) == pytest.approx(15.50)

    def test_transactions_ignore_non_success_and_non_refund(self):
        refund = {
            'transactions': [
                {'kind': 'refund', 'status': 'success', 'amount': '12.00'},
                {'kind': 'refund', 'status': 'failure', 'amount': '100.00'},
                {'kind': 'sale', 'status': 'success', 'amount': '200.00'},
            ],
        }
        assert _refund_amount(refund) == pytest.approx(12.00)

    def test_fallback_path_no_transactions(self):
        # No transactions -> fall back to line-item subtotal + tax.
        refund = {
            'refund_line_items': [
                {'subtotal': 20.0, 'total_tax': 2.0, 'quantity': 1},
                {'subtotal': 5.0, 'total_tax': 0.5, 'quantity': 1},
            ],
        }
        assert _refund_amount(refund) == pytest.approx(27.5)

    def test_fallback_path_when_transactions_zero(self):
        # Transactions present but none successful -> fall back to line items.
        refund = {
            'transactions': [
                {'kind': 'refund', 'status': 'pending', 'amount': '50.00'},
            ],
            'refund_line_items': [
                {'subtotal': 8.0, 'total_tax': 0.75, 'quantity': 2},
            ],
        }
        assert _refund_amount(refund) == pytest.approx(8.75)

    def test_empty_refund(self):
        assert _refund_amount({}) == 0.0


class TestRefundUnits:
    def test_units_sum(self):
        refund = {
            'refund_line_items': [
                {'quantity': 2},
                {'quantity': 3},
                {'quantity': 1},
            ],
        }
        assert _refund_units(refund) == 6

    def test_units_no_line_items(self):
        assert _refund_units({'transactions': [{'amount': '5.00'}]}) == 0

    def test_units_missing_quantity_defaults_zero(self):
        refund = {'refund_line_items': [{'subtotal': 10.0}, {'quantity': 4}]}
        assert _refund_units(refund) == 4


class TestTotalRefundAmount:
    def test_sums_across_multiple_refunds(self):
        order = {
            'id': 123,
            'refunds': [
                {'transactions': [{'kind': 'refund', 'status': 'success', 'amount': '10.00'}]},
                {'refund_line_items': [{'subtotal': 5.0, 'total_tax': 0.5, 'quantity': 1}]},
            ],
        }
        assert _total_refund_amount(order) == pytest.approx(15.5)

    def test_no_refunds(self):
        assert _total_refund_amount({'id': 1, 'refunds': []}) == 0.0

    def test_missing_refunds_key(self):
        assert _total_refund_amount({'id': 1}) == 0.0
