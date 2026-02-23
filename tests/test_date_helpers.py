"""Tests for shared date math utilities."""
import pytest
from datetime import datetime
from utils.date_helpers import month_str, parse_month, add_months, month_diff


class TestMonthStr:
    def test_formats_datetime(self):
        assert month_str(datetime(2026, 1, 15)) == '2026-01'

    def test_formats_first_of_month(self):
        assert month_str(datetime(2026, 12, 1)) == '2026-12'

    def test_single_digit_month_is_zero_padded(self):
        assert month_str(datetime(2026, 3, 10)) == '2026-03'


class TestParseMonth:
    def test_parses_valid_string(self):
        dt = parse_month('2026-03')
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 1

    def test_parses_december(self):
        dt = parse_month('2025-12')
        assert dt.year == 2025
        assert dt.month == 12

    def test_raises_on_invalid(self):
        with pytest.raises(ValueError):
            parse_month('not-a-date')

    def test_raises_on_full_date(self):
        with pytest.raises(ValueError):
            parse_month('2026-01-15')

    def test_raises_on_empty_string(self):
        with pytest.raises(ValueError):
            parse_month('')


class TestAddMonths:
    def test_positive(self):
        assert add_months('2026-01', 3) == '2026-04'

    def test_year_boundary(self):
        assert add_months('2025-11', 3) == '2026-02'

    def test_negative(self):
        assert add_months('2026-06', -3) == '2026-03'

    def test_zero(self):
        assert add_months('2026-01', 0) == '2026-01'

    def test_twelve_months(self):
        assert add_months('2025-01', 12) == '2026-01'

    def test_negative_year_boundary(self):
        assert add_months('2026-01', -2) == '2025-11'

    def test_large_positive(self):
        assert add_months('2025-01', 25) == '2027-02'


class TestMonthDiff:
    def test_positive_diff(self):
        assert month_diff('2026-03', '2025-12') == 3

    def test_zero_diff(self):
        assert month_diff('2026-01', '2026-01') == 0

    def test_negative_diff(self):
        assert month_diff('2025-06', '2026-01') == -7

    def test_multi_year(self):
        assert month_diff('2028-06', '2025-01') == 41

    def test_one_month(self):
        assert month_diff('2026-02', '2026-01') == 1

    def test_symmetry(self):
        assert month_diff('2026-06', '2025-01') == -month_diff('2025-01', '2026-06')
