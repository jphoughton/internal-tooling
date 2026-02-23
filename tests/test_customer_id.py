"""Tests for customer ID generation."""
import pytest
from etl.customer_id import generate_customer_id


class TestGenerateCustomerId:
    def test_rest_api_numeric_id(self):
        result = generate_customer_id({'id': 12345})
        assert result == 'shp-12345'

    def test_graphql_gid(self):
        result = generate_customer_id({'id': 'gid://shopify/Customer/67890'})
        assert result == 'shp-67890'

    def test_email_fallback(self):
        result = generate_customer_id({'email': 'test@example.com'})
        assert result is not None
        assert result.startswith('shp-')
        assert len(result) == 16  # 'shp-' + 12 hex chars

    def test_email_case_insensitive(self):
        r1 = generate_customer_id({'email': 'Test@Example.com'})
        r2 = generate_customer_id({'email': 'test@example.com'})
        assert r1 == r2

    def test_none_input(self):
        assert generate_customer_id(None) is None

    def test_empty_dict(self):
        assert generate_customer_id({}) is None

    def test_id_takes_priority_over_email(self):
        result = generate_customer_id({'id': 999, 'email': 'test@example.com'})
        assert result == 'shp-999'

    def test_string_numeric_id(self):
        result = generate_customer_id({'id': '54321'})
        assert result == 'shp-54321'

    def test_email_hash_is_deterministic(self):
        r1 = generate_customer_id({'email': 'hello@hydrant.com'})
        r2 = generate_customer_id({'email': 'hello@hydrant.com'})
        assert r1 == r2

    def test_different_emails_produce_different_ids(self):
        r1 = generate_customer_id({'email': 'alice@example.com'})
        r2 = generate_customer_id({'email': 'bob@example.com'})
        assert r1 != r2

    def test_zero_id(self):
        # id=0 is falsy but should still be handled if present
        result = generate_customer_id({'id': 0})
        # 0 is falsy so falls through to email, then to None
        assert result is None

    def test_empty_email(self):
        result = generate_customer_id({'email': ''})
        assert result is None
