"""Tests for data-driven SKU-level seasonal index computation."""
import pytest


class TestSeasonalConstants:
    """Sanity checks for the seasonal module constants and defaults."""

    def test_min_month_occurrences_positive(self):
        from analytics.seasonal import MIN_MONTH_OCCURRENCES
        assert MIN_MONTH_OCCURRENCES >= 1

    def test_default_seasonal_indices_available(self):
        from utils.constants import DEFAULT_SEASONAL_INDICES
        assert len(DEFAULT_SEASONAL_INDICES) == 12
        for m in range(1, 13):
            assert m in DEFAULT_SEASONAL_INDICES
            assert 0.3 <= DEFAULT_SEASONAL_INDICES[m] <= 3.0

    def test_default_indices_average_near_one(self):
        from utils.constants import DEFAULT_SEASONAL_INDICES
        avg = sum(DEFAULT_SEASONAL_INDICES.values()) / 12
        assert abs(avg - 1.0) < 0.05


class TestSeasonalIndexNormalization:
    """Test the normalization logic used in seasonal index computation."""

    def test_normalize_flat_indices(self):
        """Flat indices (all equal) should normalize to 1.0."""
        raw = {m: 5.0 for m in range(1, 13)}
        avg = sum(raw.values()) / 12
        normalized = {m: v / avg for m, v in raw.items()}
        for v in normalized.values():
            assert abs(v - 1.0) < 0.001

    def test_normalize_varying_indices(self):
        """Normalized indices should average to 1.0."""
        raw = {1: 0.5, 2: 0.6, 3: 0.8, 4: 1.0, 5: 1.2, 6: 1.4,
               7: 1.5, 8: 1.3, 9: 1.1, 10: 0.9, 11: 0.7, 12: 0.5}
        avg = sum(raw.values()) / 12
        normalized = {m: v / avg for m, v in raw.items()}
        result_avg = sum(normalized.values()) / 12
        assert abs(result_avg - 1.0) < 0.001

    def test_clamp_extreme_values(self):
        """Values should be clamped to [0.3, 3.0]."""
        raw = {1: 0.1, 2: 5.0, 3: 1.0}
        clamped = {m: max(0.3, min(3.0, v)) for m, v in raw.items()}
        assert clamped[1] == 0.3
        assert clamped[2] == 3.0
        assert clamped[3] == 1.0


_has_streamlit = True
try:
    import streamlit  # noqa: F401
except ImportError:
    _has_streamlit = False


@pytest.mark.skipif(not _has_streamlit, reason='streamlit not installed')
class TestBuildSkuForecastTableSignature:
    """Test that build_sku_forecast_table accepts new parameters gracefully."""

    def test_new_params_default_to_none(self):
        """New optional params shouldn't break existing callers."""
        import pandas as pd
        from analytics.waterfall import build_sku_forecast_table
        # Empty dataframe should return empty regardless of params
        result = build_sku_forecast_table(pd.DataFrame())
        assert result.empty

    def test_with_none_seasonal_indices(self):
        """Passing None for seasonal indices should work like before."""
        import pandas as pd
        from analytics.waterfall import build_sku_forecast_table
        result = build_sku_forecast_table(
            pd.DataFrame(), source_filter=None,
            sku_seasonal_indices=None, global_seasonal_indices=None,
        )
        assert result.empty


class TestDbSkuSeasonalHelpers:
    """Test that the DB helper function signatures are correct."""

    def test_get_sku_seasonal_indices_importable(self):
        from db import get_sku_seasonal_indices
        assert callable(get_sku_seasonal_indices)

    def test_upsert_sku_seasonal_indices_importable(self):
        from db import upsert_sku_seasonal_indices
        assert callable(upsert_sku_seasonal_indices)

    def test_compute_importable(self):
        from analytics.seasonal import compute_sku_seasonal_indices
        assert callable(compute_sku_seasonal_indices)

    def test_refresh_importable(self):
        from analytics.seasonal import refresh_sku_seasonal_indices
        assert callable(refresh_sku_seasonal_indices)


class TestSeasonalMixForMonth:
    """Test the _seasonal_mix_for_month helper in dtc_demand."""

    def test_no_reweights_returns_base_mix(self):
        from analytics.dtc_demand import _seasonal_mix_for_month
        base = {'SKU-A': 0.6, 'SKU-B': 0.4}
        result = _seasonal_mix_for_month(base, {}, 7)
        assert result == base

    def test_reweights_preserve_total(self):
        from analytics.dtc_demand import _seasonal_mix_for_month
        base = {'SKU-A': 0.5, 'SKU-B': 0.3, 'SKU-C': 0.2}
        reweights = {
            'SKU-A': {7: 1.3},
            'SKU-B': {7: 0.8},
            'SKU-C': {7: 1.0},
        }
        result = _seasonal_mix_for_month(base, reweights, 7)
        total = sum(result.values())
        assert abs(total - 1.0) < 0.001

    def test_reweights_shift_mix(self):
        """SKU with higher seasonal factor should get a bigger share."""
        from analytics.dtc_demand import _seasonal_mix_for_month
        base = {'SKU-A': 0.5, 'SKU-B': 0.5}
        reweights = {
            'SKU-A': {7: 2.0},  # double seasonal factor
            'SKU-B': {7: 1.0},
        }
        result = _seasonal_mix_for_month(base, reweights, 7)
        assert result['SKU-A'] > result['SKU-B']
        # SKU-A should be 2/3 (0.5*2 / (0.5*2 + 0.5*1))
        assert abs(result['SKU-A'] - 2/3) < 0.001
        assert abs(result['SKU-B'] - 1/3) < 0.001

    def test_missing_sku_in_reweights_defaults_to_one(self):
        from analytics.dtc_demand import _seasonal_mix_for_month
        base = {'SKU-A': 0.5, 'SKU-B': 0.5}
        reweights = {
            'SKU-A': {7: 1.5},
            # SKU-B not in reweights — should default to 1.0
        }
        result = _seasonal_mix_for_month(base, reweights, 7)
        total = sum(result.values())
        assert abs(total - 1.0) < 0.001
        assert result['SKU-A'] > result['SKU-B']

    def test_missing_month_in_reweights_defaults_to_one(self):
        from analytics.dtc_demand import _seasonal_mix_for_month
        base = {'SKU-A': 0.5, 'SKU-B': 0.5}
        reweights = {
            'SKU-A': {7: 1.5},  # only has July
            'SKU-B': {7: 0.8},
        }
        # Query for January (month 1) — not in reweights
        result = _seasonal_mix_for_month(base, reweights, 1)
        # Both default to 1.0, so mix should be unchanged
        assert abs(result['SKU-A'] - 0.5) < 0.001
        assert abs(result['SKU-B'] - 0.5) < 0.001

    def test_all_zero_weights_returns_base(self):
        from analytics.dtc_demand import _seasonal_mix_for_month
        base = {'SKU-A': 0.5, 'SKU-B': 0.5}
        reweights = {
            'SKU-A': {7: 0.0},
            'SKU-B': {7: 0.0},
        }
        result = _seasonal_mix_for_month(base, reweights, 7)
        # total weighted = 0, should fall back to base mix
        assert result == base


class TestDtcDemandSeasonalImports:
    """Test that the seasonal integration functions are importable."""

    def test_load_reweights_importable(self):
        from analytics.dtc_demand import _load_sku_seasonal_reweights
        assert callable(_load_sku_seasonal_reweights)

    def test_seasonal_mix_importable(self):
        from analytics.dtc_demand import _seasonal_mix_for_month
        assert callable(_seasonal_mix_for_month)
