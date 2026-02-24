"""Tests for shared validation utilities."""
import pytest
from utils.validators import email_valid, url_valid


class TestEmailValid:
    def test_simple_valid_email(self):
        assert email_valid('user@example.com') is True

    def test_subdomain_email(self):
        assert email_valid('user@mail.example.com') is True

    def test_plus_addressing(self):
        assert email_valid('user+tag@example.com') is True

    def test_dots_in_local_part(self):
        assert email_valid('first.last@example.com') is True

    def test_numeric_local_part(self):
        assert email_valid('123@example.com') is True

    def test_hyphen_in_domain(self):
        assert email_valid('user@my-company.com') is True

    def test_uppercase_email(self):
        assert email_valid('USER@EXAMPLE.COM') is True

    def test_missing_at_sign(self):
        assert email_valid('userexample.com') is False

    def test_missing_domain(self):
        assert email_valid('user@') is False

    def test_missing_tld(self):
        assert email_valid('user@example') is False

    def test_double_at(self):
        assert email_valid('user@@example.com') is False

    def test_tld_too_short(self):
        assert email_valid('user@example.c') is False

    def test_empty_string(self):
        assert email_valid('') is False

    def test_none_value(self):
        assert email_valid(None) is False


class TestUrlValid:
    def test_http_url(self):
        assert url_valid('http://example.com') is True

    def test_https_url(self):
        assert url_valid('https://example.com') is True

    def test_url_with_path(self):
        assert url_valid('https://example.com/path/to/page') is True

    def test_url_with_port(self):
        assert url_valid('http://example.com:8080') is True

    def test_url_with_query_string(self):
        assert url_valid('https://example.com/search?q=test') is True

    def test_url_with_fragment(self):
        assert url_valid('https://example.com/page#section') is True

    def test_subdomain_url(self):
        assert url_valid('https://api.example.com') is True

    def test_missing_scheme(self):
        assert url_valid('example.com') is False

    def test_ftp_scheme_rejected(self):
        assert url_valid('ftp://example.com') is False

    def test_empty_string(self):
        assert url_valid('') is False

    def test_none_value(self):
        assert url_valid(None) is False

    def test_non_string_value(self):
        assert url_valid(123) is False

    def test_scheme_only(self):
        assert url_valid('https://') is False
