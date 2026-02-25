"""Tests for Klaviyo _aggregate_message_stats function.

Validates aggregation of per-message Reporting API results into
per-parent (flow/campaign) totals: count summation, weighted-average
rates, and conversion_value -> revenue mapping.
"""
import sys
import types
import pytest

# Stub out klaviyo_api so we can import from etl.klaviyo_client without
# the SDK installed locally.
if 'klaviyo_api' not in sys.modules:
    sys.modules['klaviyo_api'] = types.ModuleType('klaviyo_api')
    sys.modules['klaviyo_api'].KlaviyoAPI = type('KlaviyoAPI', (), {})

from etl.klaviyo_client import _aggregate_message_stats


# ---------------------------------------------------------------------------
# Helpers to build realistic Klaviyo Reporting API items
# ---------------------------------------------------------------------------
def _make_item(parent_id, id_key, stats):
    """Build a single Reporting API result item."""
    return {
        'groupings': {id_key: parent_id, 'send_channel': 'email'},
        'statistics': stats,
    }


# ---------------------------------------------------------------------------
# 1. Single message per parent -- stats pass through correctly
# ---------------------------------------------------------------------------
class TestSingleMessagePassthrough:
    def test_count_stats_pass_through(self):
        items = [_make_item('flow_abc', 'flow_id', {
            'recipients': 1000,
            'delivered': 950,
            'opens_unique': 400,
            'clicks_unique': 80,
            'unsubscribes': 5,
            'conversion_value': 2500.0,
        })]
        result = _aggregate_message_stats(items, 'flow_id')
        assert 'flow_abc' in result
        merged = result['flow_abc']
        assert merged['recipients'] == 1000
        assert merged['delivered'] == 950
        assert merged['opens_unique'] == 400
        assert merged['clicks_unique'] == 80
        assert merged['unsubscribes'] == 5

    def test_rate_stats_pass_through(self):
        items = [_make_item('camp_1', 'campaign_id', {
            'recipients': 500,
            'open_rate': 0.42,
            'click_rate': 0.08,
            'click_to_open_rate': 0.19,
            'unsubscribe_rate': 0.005,
            'bounce_rate': 0.02,
            'conversion_rate': 0.03,
            'revenue_per_recipient': 1.25,
            'average_order_value': 45.0,
        })]
        result = _aggregate_message_stats(items, 'campaign_id')
        merged = result['camp_1']
        assert merged['open_rate'] == pytest.approx(0.42)
        assert merged['click_rate'] == pytest.approx(0.08)
        assert merged['click_to_open_rate'] == pytest.approx(0.19)
        assert merged['unsubscribe_rate'] == pytest.approx(0.005)
        assert merged['bounce_rate'] == pytest.approx(0.02)
        assert merged['conversion_rate'] == pytest.approx(0.03)
        assert merged['revenue_per_recipient'] == pytest.approx(1.25)
        assert merged['average_order_value'] == pytest.approx(45.0)


# ---------------------------------------------------------------------------
# 2. Multiple messages per flow -- sums and weighted averages
# ---------------------------------------------------------------------------
class TestMultipleMessageAggregation:
    @pytest.fixture()
    def two_message_result(self):
        """Two messages in the same flow with different recipient counts."""
        items = [
            _make_item('flow_welcome', 'flow_id', {
                'recipients': 1000,
                'delivered': 950,
                'opens_unique': 400,
                'clicks_unique': 80,
                'unsubscribes': 5,
                'conversion_value': 2000.0,
                'open_rate': 0.40,
                'click_rate': 0.08,
                'click_to_open_rate': 0.20,
                'unsubscribe_rate': 0.005,
                'bounce_rate': 0.05,
                'conversion_rate': 0.02,
                'revenue_per_recipient': 2.00,
                'average_order_value': 40.0,
            }),
            _make_item('flow_welcome', 'flow_id', {
                'recipients': 500,
                'delivered': 480,
                'opens_unique': 250,
                'clicks_unique': 60,
                'unsubscribes': 3,
                'conversion_value': 1500.0,
                'open_rate': 0.50,
                'click_rate': 0.12,
                'click_to_open_rate': 0.24,
                'unsubscribe_rate': 0.006,
                'bounce_rate': 0.04,
                'conversion_rate': 0.04,
                'revenue_per_recipient': 3.00,
                'average_order_value': 50.0,
            }),
        ]
        return _aggregate_message_stats(items, 'flow_id')['flow_welcome']

    def test_count_stats_summed(self, two_message_result):
        merged = two_message_result
        assert merged['recipients'] == 1500
        assert merged['delivered'] == 1430
        assert merged['opens_unique'] == 650
        assert merged['clicks_unique'] == 140
        assert merged['unsubscribes'] == 8

    def test_rate_stats_weighted_average(self, two_message_result):
        merged = two_message_result
        # open_rate: (1000*0.40 + 500*0.50) / 1500 = 650/1500 = 0.4333...
        assert merged['open_rate'] == pytest.approx(650 / 1500)
        # click_rate: (1000*0.08 + 500*0.12) / 1500 = 140/1500 = 0.09333...
        assert merged['click_rate'] == pytest.approx(140 / 1500)
        # click_to_open_rate: (1000*0.20 + 500*0.24) / 1500 = 320/1500
        assert merged['click_to_open_rate'] == pytest.approx(320 / 1500)
        # unsubscribe_rate: (1000*0.005 + 500*0.006) / 1500 = 8/1500
        assert merged['unsubscribe_rate'] == pytest.approx(8 / 1500)
        # bounce_rate: (1000*0.05 + 500*0.04) / 1500 = 70/1500
        assert merged['bounce_rate'] == pytest.approx(70 / 1500)

    def test_weighted_avg_stats(self, two_message_result):
        merged = two_message_result
        # revenue_per_recipient: (1000*2.00 + 500*3.00) / 1500 = 3500/1500
        assert merged['revenue_per_recipient'] == pytest.approx(3500 / 1500)
        # average_order_value: (1000*40.0 + 500*50.0) / 1500 = 65000/1500
        assert merged['average_order_value'] == pytest.approx(65000 / 1500)

    def test_three_messages_summed(self):
        """Three messages in a single flow."""
        items = [
            _make_item('flow_x', 'flow_id', {
                'recipients': 100, 'delivered': 90, 'opens_unique': 50,
                'clicks_unique': 10, 'unsubscribes': 1, 'conversion_value': 500,
            }),
            _make_item('flow_x', 'flow_id', {
                'recipients': 200, 'delivered': 180, 'opens_unique': 80,
                'clicks_unique': 20, 'unsubscribes': 2, 'conversion_value': 800,
            }),
            _make_item('flow_x', 'flow_id', {
                'recipients': 300, 'delivered': 270, 'opens_unique': 120,
                'clicks_unique': 30, 'unsubscribes': 3, 'conversion_value': 1200,
            }),
        ]
        merged = _aggregate_message_stats(items, 'flow_id')['flow_x']
        assert merged['recipients'] == 600
        assert merged['delivered'] == 540
        assert merged['opens_unique'] == 250
        assert merged['clicks_unique'] == 60
        assert merged['unsubscribes'] == 6
        # conversion_value is mapped to revenue
        assert merged['revenue'] == 2500


# ---------------------------------------------------------------------------
# 3. conversion_value -> revenue mapping
# ---------------------------------------------------------------------------
class TestConversionValueMapping:
    def test_conversion_value_renamed_to_revenue(self):
        items = [_make_item('camp_99', 'campaign_id', {
            'recipients': 200,
            'conversion_value': 3456.78,
        })]
        result = _aggregate_message_stats(items, 'campaign_id')
        merged = result['camp_99']
        assert 'revenue' in merged
        assert 'conversion_value' not in merged
        assert merged['revenue'] == pytest.approx(3456.78)

    def test_revenue_zero_when_conversion_value_zero(self):
        items = [_make_item('camp_0', 'campaign_id', {
            'recipients': 100,
            'conversion_value': 0,
        })]
        merged = _aggregate_message_stats(items, 'campaign_id')['camp_0']
        assert merged['revenue'] == 0

    def test_revenue_summed_across_messages(self):
        items = [
            _make_item('flow_rev', 'flow_id', {
                'recipients': 100, 'conversion_value': 1000.0,
            }),
            _make_item('flow_rev', 'flow_id', {
                'recipients': 100, 'conversion_value': 2000.0,
            }),
        ]
        merged = _aggregate_message_stats(items, 'flow_id')['flow_rev']
        assert merged['revenue'] == pytest.approx(3000.0)


# ---------------------------------------------------------------------------
# 4. Empty items list
# ---------------------------------------------------------------------------
class TestEmptyInput:
    def test_empty_list_returns_empty_dict(self):
        assert _aggregate_message_stats([], 'flow_id') == {}

    def test_empty_list_returns_empty_dict_campaign(self):
        assert _aggregate_message_stats([], 'campaign_id') == {}


# ---------------------------------------------------------------------------
# 5. Items with missing/malformed groupings or statistics
# ---------------------------------------------------------------------------
class TestMissingData:
    def test_item_without_groupings_skipped(self):
        items = [{'statistics': {'recipients': 100}}]
        assert _aggregate_message_stats(items, 'flow_id') == {}

    def test_item_with_empty_groupings_skipped(self):
        items = [{'groupings': {}, 'statistics': {'recipients': 100}}]
        assert _aggregate_message_stats(items, 'flow_id') == {}

    def test_item_missing_id_key_in_groupings_skipped(self):
        items = [{'groupings': {'campaign_id': 'c1'}, 'statistics': {'recipients': 100}}]
        # Asking for flow_id but item only has campaign_id
        assert _aggregate_message_stats(items, 'flow_id') == {}

    def test_item_without_statistics_uses_defaults(self):
        items = [{'groupings': {'flow_id': 'flow_z'}}]
        result = _aggregate_message_stats(items, 'flow_id')
        merged = result['flow_z']
        # All count stats should be 0
        assert merged['recipients'] == 0
        assert merged['delivered'] == 0
        assert merged['revenue'] == 0

    def test_item_with_none_stat_values_treated_as_zero(self):
        items = [_make_item('flow_none', 'flow_id', {
            'recipients': None,
            'delivered': None,
            'opens_unique': None,
            'clicks_unique': None,
            'unsubscribes': None,
            'conversion_value': None,
            'open_rate': None,
            'click_rate': None,
        })]
        result = _aggregate_message_stats(items, 'flow_id')
        merged = result['flow_none']
        assert merged['recipients'] == 0
        assert merged['delivered'] == 0
        assert merged['revenue'] == 0
        # Rates default to 0 when total_weight (recipients) is 0
        assert merged['open_rate'] == 0
        assert merged['click_rate'] == 0

    def test_mixed_valid_and_invalid_items(self):
        """Valid items are aggregated; invalid ones are silently skipped."""
        items = [
            _make_item('flow_ok', 'flow_id', {'recipients': 100, 'delivered': 90}),
            {'statistics': {'recipients': 999}},                  # no groupings
            {'groupings': {}, 'statistics': {'recipients': 999}}, # no parent id
            _make_item('flow_ok', 'flow_id', {'recipients': 200, 'delivered': 190}),
        ]
        result = _aggregate_message_stats(items, 'flow_id')
        assert len(result) == 1
        assert result['flow_ok']['recipients'] == 300
        assert result['flow_ok']['delivered'] == 280


# ---------------------------------------------------------------------------
# 6. Multiple distinct parents in a single call
# ---------------------------------------------------------------------------
class TestMultipleParents:
    def test_separate_parents_not_merged(self):
        items = [
            _make_item('flow_a', 'flow_id', {'recipients': 100, 'conversion_value': 500}),
            _make_item('flow_b', 'flow_id', {'recipients': 200, 'conversion_value': 1000}),
        ]
        result = _aggregate_message_stats(items, 'flow_id')
        assert len(result) == 2
        assert result['flow_a']['recipients'] == 100
        assert result['flow_a']['revenue'] == pytest.approx(500)
        assert result['flow_b']['recipients'] == 200
        assert result['flow_b']['revenue'] == pytest.approx(1000)

    def test_interleaved_parents(self):
        """Messages from different parents interleaved in the list."""
        items = [
            _make_item('c1', 'campaign_id', {'recipients': 100, 'delivered': 90}),
            _make_item('c2', 'campaign_id', {'recipients': 200, 'delivered': 180}),
            _make_item('c1', 'campaign_id', {'recipients': 150, 'delivered': 140}),
        ]
        result = _aggregate_message_stats(items, 'campaign_id')
        assert result['c1']['recipients'] == 250
        assert result['c1']['delivered'] == 230
        assert result['c2']['recipients'] == 200
        assert result['c2']['delivered'] == 180


# ---------------------------------------------------------------------------
# 7. Numeric edge cases
# ---------------------------------------------------------------------------
class TestNumericEdgeCases:
    def test_string_stat_values_coerced_to_float(self):
        """The API sometimes returns stats as strings."""
        items = [_make_item('flow_str', 'flow_id', {
            'recipients': '500',
            'delivered': '480',
            'conversion_value': '1234.56',
            'open_rate': '0.35',
        })]
        merged = _aggregate_message_stats(items, 'flow_id')['flow_str']
        assert merged['recipients'] == 500.0
        assert merged['delivered'] == 480.0
        assert merged['revenue'] == pytest.approx(1234.56)
        assert merged['open_rate'] == pytest.approx(0.35)

    def test_zero_recipients_rate_defaults_to_zero(self):
        """When recipients is 0, weighted average should not divide by zero."""
        items = [_make_item('flow_zero', 'flow_id', {
            'recipients': 0,
            'open_rate': 0.5,
            'click_rate': 0.1,
        })]
        merged = _aggregate_message_stats(items, 'flow_id')['flow_zero']
        assert merged['open_rate'] == 0
        assert merged['click_rate'] == 0

    def test_all_count_stats_present_in_output(self):
        """Even if input has no stats, all count keys appear in output."""
        items = [_make_item('flow_min', 'flow_id', {})]
        merged = _aggregate_message_stats(items, 'flow_id')['flow_min']
        # Count stats (conversion_value renamed to revenue)
        for key in ['recipients', 'delivered', 'opens_unique', 'clicks_unique', 'unsubscribes']:
            assert key in merged, f'Missing count stat: {key}'
        assert 'revenue' in merged
        # Rate stats
        for key in ['open_rate', 'click_rate', 'click_to_open_rate',
                     'unsubscribe_rate', 'bounce_rate', 'conversion_rate',
                     'revenue_per_recipient', 'average_order_value']:
            assert key in merged, f'Missing rate stat: {key}'
