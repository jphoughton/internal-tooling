"""Tests for shared constants."""
from utils.constants import (
    FORECAST_SKUS,
    TW_ADJUSTMENT,
    DEFAULT_SEASONAL_INDICES,
    PACING_ON_TRACK_LOW,
    PACING_ON_TRACK_HIGH,
    FBA_TRANSFER_LEAD_TIME_WEEKS,
)


def test_forecast_skus_count():
    assert len(FORECAST_SKUS) == 17


def test_forecast_skus_are_strings():
    for sku in FORECAST_SKUS:
        assert isinstance(sku, str)
        assert len(sku) > 5


def test_forecast_skus_is_a_set():
    assert isinstance(FORECAST_SKUS, set)


def test_tw_adjustment():
    assert TW_ADJUSTMENT == 0.5


def test_seasonal_indices_twelve_months():
    assert len(DEFAULT_SEASONAL_INDICES) == 12
    assert set(DEFAULT_SEASONAL_INDICES.keys()) == set(range(1, 13))


def test_seasonal_indices_average_near_one():
    avg = sum(DEFAULT_SEASONAL_INDICES.values()) / 12
    assert 0.95 <= avg <= 1.05


def test_seasonal_indices_values_positive():
    for month, index in DEFAULT_SEASONAL_INDICES.items():
        assert index > 0, f'Month {month} has non-positive index {index}'


def test_pacing_bounds_make_sense():
    assert PACING_ON_TRACK_LOW < PACING_ON_TRACK_HIGH
    assert 0 < PACING_ON_TRACK_LOW < 1.0
    assert PACING_ON_TRACK_HIGH > 1.0


def test_fba_transfer_lead_time():
    assert FBA_TRANSFER_LEAD_TIME_WEEKS == 4
    assert isinstance(FBA_TRANSFER_LEAD_TIME_WEEKS, int)
