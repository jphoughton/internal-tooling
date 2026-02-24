"""Tests for email and URL validation utilities."""
import pytest
from utils.validators import email_valid, url_valid


class TestEmailValid:
    def test_standard_email(self):
        assert email_valid('user@example.com') is True

    def test_subdomain(self):
        assert email_valid('user@mail.example.com') is True

    def test_plus_addressing(self):
        assert email_valid('user+tag@example.com') is True

    def test_dots_in_local(self):
        assert email_valid('first.last@example.com') is True

    def test_numeric_local(self):
        assert email_valid('123@example.com') is True

    def test_hyphen_in_domain(self):
        assert email_valid('user@my-domain.org') is True

    def test_two_char_tld(self):
        assert email_valid('user@example.io') is True

    def test_missing_at(self):
        assert email_valid('userexample.com') is False

    def test_missing_domain(self):
        assert email_valid('user@') is False

    def test_missing_tld(self):
        assert email_valid('user@example') is False

    def test_single_char_tld(self):
        assert email_valid('user@example.c') is False

    def test_double_at(self):
        assert email_valid('user@@example.com') is False

    def test_empty_string(self):
        assert email_valid('') is False

    def test_whitespace(self):
        assert email_valid('user @example.com') is False

    def test_non_string(self):
        assert email_valid(None) is False

    def test_integer_input(self):
        assert email_valid(42) is False


class TestUrlValid:
    def test_http(self):
        assert url_valid('http://example.com') is True

    def test_https(self):
        assert url_valid('https://example.com') is True

    def test_with_path(self):
        assert url_valid('https://example.com/path/to/page') is True

    def test_with_query(self):
        assert url_valid('https://example.com/search?q=hello') is True

    def test_with_port(self):
        assert url_valid('http://localhost:8501') is True

    def test_with_subdomain(self):
        assert url_valid('https://app.example.com') is True

    def test_with_fragment(self):
        assert url_valid('https://example.com/page#section') is True

    def test_hyphen_in_domain(self):
        assert url_valid('https://my-site.example.com') is True

    def test_missing_scheme(self):
        assert url_valid('example.com') is False

    def test_ftp_scheme(self):
        assert url_valid('ftp://example.com') is False

    def test_empty_string(self):
        assert url_valid('') is False

    def test_scheme_only(self):
        assert url_valid('https://') is False

    def test_whitespace_in_url(self):
        assert url_valid('https://example.com/path with spaces') is False

    def test_non_string(self):
        assert url_valid(None) is False

    def test_integer_input(self):
        assert url_valid(404) is False
