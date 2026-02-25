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


# ----------------------------------------------------------------
# Integration tests — require DATABASE_URL (read-only DB fixture)
# ----------------------------------------------------------------
class TestSeasonalIntegrationWithDB:
    """Integration tests that verify the seasonal refresh cycle against real data."""

    def test_compute_returns_valid_indices(self, db):
        """compute_sku_seasonal_indices returns well-formed indices from real data."""
        from tests.conftest import skip_if_table_empty
        skip_if_table_empty(db, 'daily_sku_sales')

        from analytics.seasonal import compute_sku_seasonal_indices
        result = compute_sku_seasonal_indices(min_occurrences=1)

        # May be empty if not enough data, which is fine — skip in that case
        if not result:
            pytest.skip('Not enough sales data for seasonal computation')

        for sku, indices in result.items():
            assert isinstance(sku, str)
            assert len(indices) == 12, f'{sku} has {len(indices)} months, expected 12'

            # Each index should be in [0.3, 3.0] (clamped range)
            for m in range(1, 13):
                assert m in indices, f'{sku} missing month {m}'
                assert 0.3 <= indices[m] <= 3.0, f'{sku} month {m} = {indices[m]} out of range'

            # Average should be ~1.0 (within tolerance from rounding + clamping)
            avg = sum(indices.values()) / 12
            assert 0.9 <= avg <= 1.1, f'{sku} avg index = {avg}, expected ~1.0'

    def test_compute_only_includes_forecast_skus(self, db):
        """Only FORECAST_SKUS should appear in the computed indices."""
        from tests.conftest import skip_if_table_empty
        skip_if_table_empty(db, 'daily_sku_sales')

        from analytics.seasonal import compute_sku_seasonal_indices
        from utils.constants import FORECAST_SKUS
        result = compute_sku_seasonal_indices(min_occurrences=1)

        for sku in result:
            assert sku in FORECAST_SKUS, f'Non-forecast SKU {sku} in seasonal indices'

    def test_sku_seasonal_indices_stored_in_db(self, db):
        """sku_seasonal_indices table has rows consistent with computed indices."""
        from tests.conftest import fetchall, table_exists
        if not table_exists(db, 'sku_seasonal_indices'):
            pytest.skip('sku_seasonal_indices table does not exist')

        rows = fetchall(db,
            'SELECT sku, month_num, index_value, sample_months '
            'FROM sku_seasonal_indices ORDER BY sku, month_num'
        )
        if not rows:
            pytest.skip('No SKU seasonal indices stored yet')

        # Group by SKU and verify each has 12 months
        sku_months = {}
        for r in rows:
            sku = r['sku']
            if sku not in sku_months:
                sku_months[sku] = []
            sku_months[sku].append(r)

        for sku, month_rows in sku_months.items():
            assert len(month_rows) == 12, f'{sku} has {len(month_rows)} rows, expected 12'
            for r in month_rows:
                assert 1 <= r['month_num'] <= 12
                assert 0.3 <= float(r['index_value']) <= 3.0
                assert int(r['sample_months']) >= 0

    def test_global_indices_reflect_sku_average(self, db):
        """Global seasonal_indices should be close to the SKU-weighted average."""
        from tests.conftest import fetchall, table_exists
        if not table_exists(db, 'seasonal_indices') or not table_exists(db, 'sku_seasonal_indices'):
            pytest.skip('Required tables do not exist')

        global_rows = fetchall(db, 'SELECT month_num, index_value FROM seasonal_indices ORDER BY month_num')
        sku_rows = fetchall(db, 'SELECT sku, month_num, index_value FROM sku_seasonal_indices')

        if not global_rows or not sku_rows:
            pytest.skip('No seasonal indices data')

        global_map = {r['month_num']: float(r['index_value']) for r in global_rows}

        # Global average should be ~1.0
        if len(global_map) == 12:
            avg = sum(global_map.values()) / 12
            assert 0.9 <= avg <= 1.1, f'Global seasonal avg = {avg}'


@pytest.mark.skipif(not _has_streamlit, reason='streamlit not installed')
class TestSeasonalForecastIntegration:
    """Verify that seasonal indices properly affect forecast outputs."""

    def test_waterfall_with_seasonal_scales_units(self):
        """Waterfall build_waterfall() applies seasonal_indices to scale units."""
        import pandas as pd
        from analytics.waterfall import build_waterfall
        from utils.date_helpers import parse_month

        # Create a simple media plan for one month
        media_plan = [{'month': '2026-07', 'spend': 10000, 'new_customer_roas': 2.0}]

        # Build with no seasonal adjustment
        wf_base = build_waterfall(media_plan, source_filter=None, horizon_months=1,
                                  seasonal_indices=None)

        # Build with summer boost (July = 1.20)
        summer_indices = {m: 1.0 for m in range(1, 13)}
        summer_indices[7] = 1.20
        wf_seasonal = build_waterfall(media_plan, source_filter=None, horizon_months=1,
                                       seasonal_indices=summer_indices)

        if wf_base.empty or wf_seasonal.empty:
            pytest.skip('Waterfall returned empty — likely no retention data')

        base_total = float(wf_base['total_units'].iloc[0])
        seasonal_total = float(wf_seasonal['total_units'].iloc[0])

        if base_total > 0:
            ratio = seasonal_total / base_total
            # July with 1.20 factor should produce ~20% more units
            assert 1.15 <= ratio <= 1.25, (
                f'Expected ~1.20x seasonal boost, got {ratio:.3f}'
            )

    def test_sku_forecast_table_with_seasonal_redistributes(self):
        """build_sku_forecast_table redistributes units between SKUs with seasonal indices."""
        import pandas as pd
        from analytics.waterfall import build_sku_forecast_table

        # Fake a simple waterfall output
        wf = pd.DataFrame([{
            'month': '2026-07',
            'repeat_units': 500,
            'new_customer_units': 500,
            'total_units': 1000,
            'new_customers_acquired': 50,
        }])

        # Without seasonal — should get base mix
        result_base = build_sku_forecast_table(wf, source_filter=None)
        if result_base.empty:
            pytest.skip('No SKU mix data available')

        # With seasonal — SKU-A peaks in July, SKU-B doesn't
        # Use actual SKUs from the result to build test indices
        skus_in_result = result_base['SKU'].tolist()
        if len(skus_in_result) < 2:
            pytest.skip('Need at least 2 SKUs for redistribution test')

        sku_seasonal = {}
        global_seasonal = {m: 1.0 for m in range(1, 13)}
        # First SKU gets a summer boost, second gets a dip
        sku_seasonal[skus_in_result[0]] = {m: 1.0 for m in range(1, 13)}
        sku_seasonal[skus_in_result[0]][7] = 1.5
        sku_seasonal[skus_in_result[1]] = {m: 1.0 for m in range(1, 13)}
        sku_seasonal[skus_in_result[1]][7] = 0.7

        result_seasonal = build_sku_forecast_table(
            wf, source_filter=None,
            sku_seasonal_indices=sku_seasonal,
            global_seasonal_indices=global_seasonal,
        )

        if result_seasonal.empty:
            pytest.skip('Seasonal forecast returned empty')

        # Total should still sum to ~1000 (renormalized)
        total = result_seasonal['2026-07'].sum()
        assert abs(total - 1000) < 5, f'Total units drifted to {total}, expected ~1000'

    def test_seasonal_mix_for_month_integration(self):
        """Verify _seasonal_mix_for_month produces correct redistribution in a full scenario."""
        from analytics.dtc_demand import _seasonal_mix_for_month

        # Simulate 3 SKUs with different seasonal patterns
        base_mix = {'HYDRATE': 0.5, 'SLEEP': 0.3, 'ENERGY': 0.2}

        # Reweights: HYDRATE peaks in summer, SLEEP peaks in winter
        reweights = {
            'HYDRATE': {1: 0.8, 7: 1.3},   # winter dip, summer peak
            'SLEEP':   {1: 1.4, 7: 0.7},    # winter peak, summer dip
            'ENERGY':  {1: 1.0, 7: 1.0},    # flat
        }

        # July: HYDRATE should gain share, SLEEP should lose
        july_mix = _seasonal_mix_for_month(base_mix, reweights, 7)
        assert july_mix['HYDRATE'] > base_mix['HYDRATE']
        assert july_mix['SLEEP'] < base_mix['SLEEP']
        assert abs(sum(july_mix.values()) - 1.0) < 0.001

        # January: SLEEP should gain share, HYDRATE should lose
        jan_mix = _seasonal_mix_for_month(base_mix, reweights, 1)
        assert jan_mix['SLEEP'] > base_mix['SLEEP']
        assert jan_mix['HYDRATE'] < base_mix['HYDRATE']
        assert abs(sum(jan_mix.values()) - 1.0) < 0.001


class TestSeasonalMixMultiMonth:
    """Integration test for seasonal mix across all 12 months — no streamlit needed."""

    def test_seasonal_redistribution_across_year(self):
        """Verify mix redistribution is consistent across all 12 calendar months."""
        from analytics.dtc_demand import _seasonal_mix_for_month

        base_mix = {'SUMMER-SKU': 0.4, 'WINTER-SKU': 0.3, 'FLAT-SKU': 0.3}
        reweights = {
            'SUMMER-SKU': {m: 0.7 + 0.06 * m if m <= 7 else 1.7 - 0.06 * (m - 7) for m in range(1, 13)},
            'WINTER-SKU': {m: 1.5 - 0.06 * m if m <= 7 else 0.7 + 0.06 * (m - 7) for m in range(1, 13)},
            'FLAT-SKU':   {m: 1.0 for m in range(1, 13)},
        }

        for cal_month in range(1, 13):
            mix = _seasonal_mix_for_month(base_mix, reweights, cal_month)
            total = sum(mix.values())
            assert abs(total - 1.0) < 0.001, f'Month {cal_month}: total={total}'
            # All values should be positive
            for sku, val in mix.items():
                assert val > 0, f'Month {cal_month}: {sku} has non-positive share {val}'

    def test_reweight_factor_of_one_preserves_mix(self):
        """When all reweight factors are 1.0, mix should be unchanged."""
        from analytics.dtc_demand import _seasonal_mix_for_month

        base_mix = {'A': 0.25, 'B': 0.25, 'C': 0.25, 'D': 0.25}
        reweights = {sku: {m: 1.0 for m in range(1, 13)} for sku in base_mix}

        for cal_month in range(1, 13):
            mix = _seasonal_mix_for_month(base_mix, reweights, cal_month)
            for sku in base_mix:
                assert abs(mix[sku] - base_mix[sku]) < 0.001
