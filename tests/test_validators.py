"""Unit tests for utils/validators.py."""
import pytest
from utils.validators import email_valid, url_valid


class TestEmailValid:
    def test_simple_valid(self):
        assert email_valid('user@example.com') is True

    def test_subdomain(self):
        assert email_valid('user@mail.example.co.uk') is True

    def test_plus_tag(self):
        assert email_valid('user+tag@example.com') is True

    def test_dots_in_local(self):
        assert email_valid('first.last@example.com') is True

    def test_uppercase(self):
        assert email_valid('USER@EXAMPLE.COM') is True

    def test_numeric_local(self):
        assert email_valid('123@example.com') is True

    def test_missing_at(self):
        assert email_valid('userexample.com') is False

    def test_missing_domain(self):
        assert email_valid('user@') is False

    def test_missing_tld(self):
        assert email_valid('user@example') is False

    def test_double_at(self):
        assert email_valid('user@@example.com') is False

    def test_empty_string(self):
        assert email_valid('') is False

    def test_spaces(self):
        assert email_valid('user @example.com') is False

    def test_non_string(self):
        assert email_valid(None) is False

    def test_leading_whitespace_stripped(self):
        assert email_valid('  user@example.com  ') is True


class TestUrlValid:
    def test_simple_http(self):
        assert url_valid('http://example.com') is True

    def test_simple_https(self):
        assert url_valid('https://example.com') is True

    def test_with_path(self):
        assert url_valid('https://example.com/path/to/page') is True

    def test_with_query(self):
        assert url_valid('https://example.com/search?q=hydrant') is True

    def test_with_port(self):
        assert url_valid('http://localhost:8501') is True

    def test_subdomain(self):
        assert url_valid('https://api.example.co.uk/v1') is True

    def test_no_scheme(self):
        assert url_valid('example.com') is False

    def test_ftp_scheme(self):
        assert url_valid('ftp://example.com') is False

    def test_empty_string(self):
        assert url_valid('') is False

    def test_missing_tld(self):
        assert url_valid('https://myhost') is False

    def test_spaces_in_url(self):
        assert url_valid('https://exa mple.com') is False

    def test_non_string(self):
        assert url_valid(42) is False

    def test_leading_whitespace_stripped(self):
        assert url_valid('  https://example.com  ') is True
