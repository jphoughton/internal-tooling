"""Unit tests for utils/validators.py."""

import pytest
from utils.validators import email_valid, url_valid


# ---------------------------------------------------------------------------
# email_valid tests
# ---------------------------------------------------------------------------

class TestEmailValid:
    def test_standard_email(self):
        assert email_valid('user@example.com') is True

    def test_email_with_subdomain(self):
        assert email_valid('user@mail.example.co.uk') is True

    def test_email_with_plus(self):
        assert email_valid('user+tag@example.com') is True

    def test_email_with_dots_in_local(self):
        assert email_valid('first.last@example.com') is True

    def test_email_uppercase(self):
        assert email_valid('USER@EXAMPLE.COM') is True

    def test_missing_at_sign(self):
        assert email_valid('userexample.com') is False

    def test_missing_domain(self):
        assert email_valid('user@') is False

    def test_missing_local_part(self):
        assert email_valid('@example.com') is False

    def test_spaces_in_email(self):
        assert email_valid('user @example.com') is False

    def test_double_at(self):
        assert email_valid('user@@example.com') is False

    def test_empty_string(self):
        assert email_valid('') is False

    def test_non_string_input(self):
        assert email_valid(None) is False

    def test_tld_too_short(self):
        assert email_valid('user@example.c') is False


# ---------------------------------------------------------------------------
# url_valid tests
# ---------------------------------------------------------------------------

class TestUrlValid:
    def test_simple_http_url(self):
        assert url_valid('http://example.com') is True

    def test_simple_https_url(self):
        assert url_valid('https://example.com') is True

    def test_url_with_path(self):
        assert url_valid('https://example.com/path/to/page') is True

    def test_url_with_query(self):
        assert url_valid('https://example.com/search?q=hello&page=1') is True

    def test_url_with_port(self):
        assert url_valid('http://localhost:8501') is True

    def test_url_with_subdomain(self):
        assert url_valid('https://api.example.co.uk/v1') is True

    def test_url_with_fragment(self):
        assert url_valid('https://example.com/page#section') is True

    def test_missing_scheme(self):
        assert url_valid('example.com') is False

    def test_ftp_scheme(self):
        assert url_valid('ftp://example.com') is False

    def test_empty_string(self):
        assert url_valid('') is False

    def test_non_string_input(self):
        assert url_valid(42) is False

    def test_spaces_in_url(self):
        assert url_valid('https://exam ple.com') is False

    def test_scheme_only(self):
        assert url_valid('https://') is False
