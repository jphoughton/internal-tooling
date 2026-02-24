"""Unit tests for utils/validators.py."""
import pytest
from utils.validators import email_valid, url_valid


class TestEmailValid:
    def test_valid_simple(self):
        assert email_valid('user@example.com') is True

    def test_valid_plus_addressing(self):
        assert email_valid('user+tag@example.com') is True

    def test_valid_subdomain(self):
        assert email_valid('user@mail.example.co.uk') is True

    def test_valid_numeric_local(self):
        assert email_valid('123@domain.org') is True

    def test_valid_hyphen_domain(self):
        assert email_valid('user@my-domain.io') is True

    def test_invalid_missing_at(self):
        assert email_valid('userexample.com') is False

    def test_invalid_missing_domain(self):
        assert email_valid('user@') is False

    def test_invalid_missing_tld(self):
        assert email_valid('user@domain') is False

    def test_invalid_empty_string(self):
        assert email_valid('') is False

    def test_invalid_non_string(self):
        assert email_valid(None) is False  # type: ignore[arg-type]

    def test_invalid_spaces_inside(self):
        assert email_valid('user @example.com') is False

    def test_strips_surrounding_whitespace(self):
        # Leading/trailing whitespace is stripped before validation
        assert email_valid('  user@example.com  ') is True


class TestUrlValid:
    def test_valid_https(self):
        assert url_valid('https://example.com') is True

    def test_valid_http(self):
        assert url_valid('http://example.com') is True

    def test_valid_ftp(self):
        assert url_valid('ftp://files.example.com') is True

    def test_valid_with_path(self):
        assert url_valid('https://example.com/path/to/page') is True

    def test_valid_with_port(self):
        assert url_valid('https://example.com:8080/api') is True

    def test_valid_subdomain(self):
        assert url_valid('https://sub.domain.example.com') is True

    def test_invalid_missing_scheme(self):
        assert url_valid('example.com') is False

    def test_invalid_empty_string(self):
        assert url_valid('') is False

    def test_invalid_non_string(self):
        assert url_valid(42) is False  # type: ignore[arg-type]

    def test_invalid_scheme_only(self):
        assert url_valid('https://') is False

    def test_strips_surrounding_whitespace(self):
        assert url_valid('  https://example.com  ') is True
