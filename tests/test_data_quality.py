"""
Data quality tests — run read-only queries against the live database
to verify that ETL-synced data actually makes sense.

32 tests across 10 quality dimensions.

Usage:
    pytest tests/test_data_quality.py -v
    pytest tests/test_data_quality.py -v -k "TestRevenueSanity"
    DATABASE_URL=postgresql://... pytest tests/test_data_quality.py -v
"""
import pytest
from tests.conftest import (
    fetchone, fetchall, scalar,
    skip_if_table_empty, skip_if_source_never_synced, is_mock_data,
)

# 17 core Hydrant production SKUs
FORECAST_SKUS = {
    'ENBP-BP0030-LEB0', 'HYBP-BP0030-CHLB0', 'HYBP-BP0030-GFB0',
    'HYBP-BP0030-ICLEB0', 'HYBP-BP0050-NAB0', 'SLBP-BP0030-CHB0',
    'ENPO-ST0030-RSLEB0', 'HYPO-ST0030-BOB0', 'HYPO-ST0030-FRPUB0',
    'HYPO-ST0030-LELMB0', 'HYPO-ST0030-VPBFLB0', 'IMPO-ST0030-ELB0',
    'NSPO-ST0030-BEB0', 'NSPO-ST0030-LEB0', 'NSPO-ST0030-VPWLBB0',
    'NSPO-ST0030-WALEB0', 'SLPO-ST0030-ELB0',
}


# ═══════════════════════════════════════════════════════════════════
# 1. Sync Freshness — Has the ETL run recently?
# ═══════════════════════════════════════════════════════════════════
class TestSyncFreshness:

    def test_sync_log_has_entries(self, db):
        skip_if_table_empty(db, 'sync_log')

    @pytest.mark.parametrize('source', ['amazon', 'shopify'])
    def test_sync_happened_within_48h(self, db, source):
        skip_if_source_never_synced(db, source)
        row = fetchone(db,
            "SELECT MAX(created_at::timestamp) AS last_sync FROM sync_log "
            "WHERE source = %s AND status = 'success'",
            (source,)
        )
        assert row is not None and row['last_sync'] is not None, \
            f'No successful sync found for {source}'
        hours_ago = scalar(db,
            "SELECT EXTRACT(EPOCH FROM NOW() - %s::timestamp) / 3600",
            (row['last_sync'],)
        )
        assert hours_ago < 48, \
            f'{source} last synced {hours_ago:.1f}h ago (threshold: 48h)'

    @pytest.mark.parametrize('source', ['amazon', 'shopify'])
    def test_last_sync_fetched_nonzero_records(self, db, source):
        skip_if_source_never_synced(db, source)
        row = fetchone(db,
            "SELECT records_fetched FROM sync_log "
            "WHERE source = %s AND status = 'success' "
            "ORDER BY created_at DESC LIMIT 1",
            (source,)
        )
        assert row is not None, f'No successful sync for {source}'
        assert row['records_fetched'] > 0, \
            f'{source} last sync fetched 0 records'


# ═══════════════════════════════════════════════════════════════════
# 2. Revenue Sanity — Is revenue plausible?
# ═══════════════════════════════════════════════════════════════════
class TestRevenueSanity:

    def test_no_negative_revenue(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        count = scalar(db,
            "SELECT COUNT(*) FROM daily_sku_sales WHERE revenue < 0"
        )
        assert count == 0, f'{count} rows have negative revenue'

    def test_no_revenue_exceeding_daily_threshold(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        row = fetchone(db,
            "SELECT sale_date, sku, source, revenue "
            "FROM daily_sku_sales WHERE revenue > 500000 "
            "LIMIT 1"
        )
        assert row is None, \
            f'Revenue > $500K: {row["sku"]} on {row["sale_date"]} = ${row["revenue"]:,.2f}'

    def test_revenue_per_unit_plausible_by_sku(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        bad = fetchall(db, """
            SELECT sku, SUM(revenue) / NULLIF(SUM(units_sold), 0) AS rev_per_unit,
                   SUM(units_sold) AS total_units
            FROM daily_sku_sales
            GROUP BY sku
            HAVING SUM(units_sold) >= 100
               AND (SUM(revenue) / NULLIF(SUM(units_sold), 0) < 1
                    OR SUM(revenue) / NULLIF(SUM(units_sold), 0) > 500)
        """)
        assert len(bad) == 0, \
            f'{len(bad)} SKUs with implausible rev/unit: ' + \
            ', '.join(f'{r["sku"]}=${r["rev_per_unit"]:.2f}' for r in bad[:5])

    def test_total_revenue_in_daily_sales_is_nonzero(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        total = scalar(db, "SELECT SUM(revenue) FROM daily_sku_sales")
        assert total is not None and total > 0, \
            'daily_sku_sales has zero total revenue'


# ═══════════════════════════════════════════════════════════════════
# 3. Units Sanity — Are unit counts reasonable?
# ═══════════════════════════════════════════════════════════════════
class TestUnitsSanity:

    def test_no_negative_units_sold(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        count = scalar(db,
            "SELECT COUNT(*) FROM daily_sku_sales WHERE units_sold < 0"
        )
        assert count == 0, f'{count} rows have negative units_sold'

    def test_no_extreme_single_day_sku_volume(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        row = fetchone(db,
            "SELECT sale_date, sku, source, units_sold "
            "FROM daily_sku_sales WHERE units_sold > 10000 "
            "LIMIT 1"
        )
        assert row is None, \
            f'Extreme volume: {row["sku"]} on {row["sale_date"]} = {row["units_sold"]:,} units'

    def test_no_revenue_without_units(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        count = scalar(db,
            "SELECT COUNT(*) FROM daily_sku_sales "
            "WHERE revenue > 0 AND units_sold = 0"
        )
        assert count == 0, \
            f'{count} rows have revenue > 0 but units_sold = 0'


# ═══════════════════════════════════════════════════════════════════
# 4. Date Validity — Are dates well-formed and bounded?
# ═══════════════════════════════════════════════════════════════════
class TestDateValidity:

    @pytest.mark.parametrize('table,col', [
        ('daily_sku_sales', 'sale_date'),
        ('orders', 'order_date'),
        ('customers', 'first_order_date'),
    ])
    def test_date_format_is_iso8601(self, db, table, col):
        skip_if_table_empty(db, table)
        bad = scalar(db,
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE {col} IS NOT NULL "
            f"AND {col} !~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'"
        )
        assert bad == 0, \
            f'{bad} rows in {table}.{col} are not YYYY-MM-DD format'

    def test_no_future_sale_dates(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        count = scalar(db,
            "SELECT COUNT(*) FROM daily_sku_sales "
            "WHERE sale_date::date > CURRENT_DATE"
        )
        assert count == 0, f'{count} sale dates are in the future'

    def test_no_prehistoric_sale_dates(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        count = scalar(db,
            "SELECT COUNT(*) FROM daily_sku_sales "
            "WHERE sale_date::date < '2020-01-01'::date"
        )
        assert count == 0, f'{count} sale dates are before 2020'

    def test_no_future_order_dates(self, db):
        skip_if_table_empty(db, 'orders')
        count = scalar(db,
            "SELECT COUNT(*) FROM orders "
            "WHERE order_date::date > CURRENT_DATE"
        )
        assert count == 0, f'{count} order dates are in the future'

    def test_no_prehistoric_order_dates(self, db):
        skip_if_table_empty(db, 'orders')
        count = scalar(db,
            "SELECT COUNT(*) FROM orders "
            "WHERE order_date::date < '2020-01-01'::date"
        )
        assert count == 0, f'{count} order dates are before 2020'


# ═══════════════════════════════════════════════════════════════════
# 5. Referential Integrity — Are FK relationships intact?
# ═══════════════════════════════════════════════════════════════════
class TestReferentialIntegrity:

    def test_no_orphaned_order_items(self, db):
        skip_if_table_empty(db, 'order_items')
        count = scalar(db,
            "SELECT COUNT(*) FROM order_items oi "
            "LEFT JOIN orders o ON oi.order_id = o.order_id "
            "WHERE o.order_id IS NULL"
        )
        assert count == 0, f'{count} order_items reference non-existent orders'

    def test_no_orders_referencing_missing_customers(self, db):
        skip_if_table_empty(db, 'orders')
        count = scalar(db,
            "SELECT COUNT(*) FROM orders o "
            "LEFT JOIN customers c ON o.customer_id = c.customer_id "
            "WHERE o.customer_id IS NOT NULL AND c.customer_id IS NULL"
        )
        assert count == 0, f'{count} orders reference non-existent customers'

    def test_daily_sku_sales_skus_exist_in_sku_master(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        skip_if_table_empty(db, 'sku_master')
        orphans = fetchall(db,
            "SELECT DISTINCT d.sku FROM daily_sku_sales d "
            "LEFT JOIN sku_master s ON d.sku = s.sku "
            "WHERE s.sku IS NULL"
        )
        assert len(orphans) == 0, \
            f'{len(orphans)} SKUs in daily_sku_sales missing from sku_master: ' + \
            ', '.join(r['sku'] for r in orphans[:10])

    def test_order_items_skus_exist_in_sku_master(self, db):
        skip_if_table_empty(db, 'order_items')
        skip_if_table_empty(db, 'sku_master')
        orphans = fetchall(db,
            "SELECT DISTINCT oi.sku FROM order_items oi "
            "LEFT JOIN sku_master s ON oi.sku = s.sku "
            "WHERE s.sku IS NULL"
        )
        assert len(orphans) == 0, \
            f'{len(orphans)} SKUs in order_items missing from sku_master: ' + \
            ', '.join(r['sku'] for r in orphans[:10])


# ═══════════════════════════════════════════════════════════════════
# 6. SKU Quality — Are SKU codes valid?
# ═══════════════════════════════════════════════════════════════════
class TestSKUQuality:

    def test_no_high_volume_unknown_skus(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        total_units = scalar(db, "SELECT SUM(units_sold) FROM daily_sku_sales")
        if total_units == 0:
            pytest.skip('No units sold')
        unknown_units = scalar(db,
            "SELECT COALESCE(SUM(units_sold), 0) FROM daily_sku_sales "
            "WHERE UPPER(sku) LIKE '%UNKNOWN%' OR UPPER(sku) LIKE '%UNMATCHED%'"
        )
        pct = (unknown_units / total_units) * 100
        assert pct < 1.0, \
            f'UNKNOWN/UNMATCHED SKUs account for {pct:.2f}% of total units (threshold: 1%)'

    def test_forecast_skus_present_in_sales_data(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        if is_mock_data(db):
            pytest.skip('Mock data detected — FORECAST_SKUS not applicable')
        found = scalar(db,
            "SELECT COUNT(DISTINCT sku) FROM daily_sku_sales WHERE sku = ANY(%s)",
            (list(FORECAST_SKUS),)
        )
        assert found >= 10, \
            f'Only {found}/17 FORECAST_SKUS have sales data (expected >= 10)'

    def test_sku_master_has_active_skus(self, db):
        skip_if_table_empty(db, 'sku_master')
        count = scalar(db,
            "SELECT COUNT(*) FROM sku_master WHERE is_active = 1"
        )
        assert count > 0, 'No active SKUs in sku_master'


# ═══════════════════════════════════════════════════════════════════
# 7. Customer Cohort Health — Are customer records complete?
# ═══════════════════════════════════════════════════════════════════
class TestCustomerCohortHealth:

    def test_all_customers_have_first_order_date(self, db):
        skip_if_table_empty(db, 'customers')
        count = scalar(db,
            "SELECT COUNT(*) FROM customers "
            "WHERE first_order_date IS NULL OR first_order_date = ''"
        )
        assert count == 0, \
            f'{count} customers missing first_order_date'

    def test_first_order_date_not_before_2020(self, db):
        skip_if_table_empty(db, 'customers')
        count = scalar(db,
            "SELECT COUNT(*) FROM customers "
            "WHERE first_order_date IS NOT NULL "
            "AND first_order_date != '' "
            "AND first_order_date::date < '2020-01-01'::date"
        )
        assert count == 0, \
            f'{count} customers have first_order_date before 2020'

    def test_first_order_date_not_after_customer_earliest_order(self, db):
        skip_if_table_empty(db, 'customers')
        skip_if_table_empty(db, 'orders')
        bad = fetchall(db, """
            SELECT c.customer_id, c.first_order_date,
                   MIN(o.order_date) AS earliest_order
            FROM customers c
            JOIN orders o ON c.customer_id = o.customer_id
            WHERE c.first_order_date IS NOT NULL
              AND c.first_order_date != ''
            GROUP BY c.customer_id, c.first_order_date
            HAVING c.first_order_date::date > MIN(o.order_date)::date
            LIMIT 10
        """)
        assert len(bad) == 0, \
            f'{len(bad)} customers have first_order_date after their earliest order: ' + \
            ', '.join(f'{r["customer_id"]}({r["first_order_date"]} > {r["earliest_order"]})' for r in bad[:5])

    def test_customer_sources_are_valid(self, db):
        skip_if_table_empty(db, 'customers')
        bad = fetchall(db,
            "SELECT DISTINCT source FROM customers "
            "WHERE source NOT IN ('shopify', 'amazon')"
        )
        assert len(bad) == 0, \
            f'Invalid customer sources: {[r["source"] for r in bad]}'


# ═══════════════════════════════════════════════════════════════════
# 8. Cross-Table Consistency — Does daily_sku_sales match order_items?
# ═══════════════════════════════════════════════════════════════════
class TestCrossTableConsistency:

    def test_shopify_revenue_matches_order_items_aggregation(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        skip_if_table_empty(db, 'order_items')
        mismatches = fetchall(db, """
            SELECT
                COALESCE(d.sale_date, oi_agg.order_date) AS dt,
                COALESCE(d.sku, oi_agg.sku) AS sku,
                COALESCE(d.revenue, 0) AS daily_rev,
                COALESCE(oi_agg.rev, 0) AS oi_rev
            FROM daily_sku_sales d
            FULL OUTER JOIN (
                SELECT o.order_date, oi.sku,
                       SUM(oi.total_price) AS rev
                FROM orders o
                JOIN order_items oi ON o.order_id = oi.order_id
                WHERE o.source = 'shopify'
                GROUP BY o.order_date, oi.sku
            ) oi_agg ON d.sale_date = oi_agg.order_date
                    AND d.sku = oi_agg.sku
                    AND d.source = 'shopify'
            WHERE d.source = 'shopify'
              AND ABS(COALESCE(d.revenue, 0) - COALESCE(oi_agg.rev, 0)) > 0.01
            LIMIT 20
        """)
        assert len(mismatches) == 0, \
            f'{len(mismatches)} date/SKU combos have revenue mismatch between daily_sku_sales and order_items'

    def test_shopify_units_match_order_items_aggregation(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        skip_if_table_empty(db, 'order_items')
        mismatches = fetchall(db, """
            SELECT
                COALESCE(d.sale_date, oi_agg.order_date) AS dt,
                COALESCE(d.sku, oi_agg.sku) AS sku,
                COALESCE(d.units_sold, 0) AS daily_units,
                COALESCE(oi_agg.qty, 0) AS oi_units
            FROM daily_sku_sales d
            FULL OUTER JOIN (
                SELECT o.order_date, oi.sku,
                       SUM(oi.quantity) AS qty
                FROM orders o
                JOIN order_items oi ON o.order_id = oi.order_id
                WHERE o.source = 'shopify'
                GROUP BY o.order_date, oi.sku
            ) oi_agg ON d.sale_date = oi_agg.order_date
                    AND d.sku = oi_agg.sku
                    AND d.source = 'shopify'
            WHERE d.source = 'shopify'
              AND COALESCE(d.units_sold, 0) != COALESCE(oi_agg.qty, 0)
            LIMIT 20
        """)
        assert len(mismatches) == 0, \
            f'{len(mismatches)} date/SKU combos have units mismatch between daily_sku_sales and order_items'


# ═══════════════════════════════════════════════════════════════════
# 9. Duplicate Detection — Are PKs actually unique?
# ═══════════════════════════════════════════════════════════════════
class TestDuplicateDetection:

    def test_no_duplicate_daily_sku_sales_rows(self, db):
        skip_if_table_empty(db, 'daily_sku_sales')
        count = scalar(db, """
            SELECT COUNT(*) FROM (
                SELECT sale_date, sku, source
                FROM daily_sku_sales
                GROUP BY sale_date, sku, source
                HAVING COUNT(*) > 1
            ) dupes
        """)
        assert count == 0, f'{count} duplicate (sale_date, sku, source) combinations'

    def test_no_duplicate_order_ids(self, db):
        skip_if_table_empty(db, 'orders')
        count = scalar(db, """
            SELECT COUNT(*) FROM (
                SELECT order_id FROM orders
                GROUP BY order_id HAVING COUNT(*) > 1
            ) dupes
        """)
        assert count == 0, f'{count} duplicate order_ids'

    def test_no_duplicate_customers(self, db):
        skip_if_table_empty(db, 'customers')
        count = scalar(db, """
            SELECT COUNT(*) FROM (
                SELECT customer_id FROM customers
                GROUP BY customer_id HAVING COUNT(*) > 1
            ) dupes
        """)
        assert count == 0, f'{count} duplicate customer_ids'


# ═══════════════════════════════════════════════════════════════════
# 10. Sync Health — Is the ETL pipeline stable?
# ═══════════════════════════════════════════════════════════════════
class TestSyncHealth:

    @pytest.mark.parametrize('source', ['amazon', 'shopify'])
    def test_no_consecutive_sync_errors(self, db, source):
        skip_if_source_never_synced(db, source)
        last_three = fetchall(db,
            "SELECT status FROM sync_log "
            "WHERE source = %s ORDER BY created_at DESC LIMIT 3",
            (source,)
        )
        if len(last_three) < 3:
            pytest.skip(f'Fewer than 3 sync records for {source}')
        all_errors = all(r['status'] == 'error' for r in last_three)
        assert not all_errors, \
            f'{source}: last 3 syncs all failed — ETL may be broken'

    @pytest.mark.parametrize('source', ['amazon', 'shopify'])
    def test_sync_error_rate_below_50pct_last_30_days(self, db, source):
        skip_if_source_never_synced(db, source)
        stats = fetchone(db, """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'error') AS errors
            FROM sync_log
            WHERE source = %s
              AND created_at::timestamp > NOW() - INTERVAL '30 days'
        """, (source,))
        if stats['total'] == 0:
            pytest.skip(f'No syncs for {source} in last 30 days')
        error_rate = stats['errors'] / stats['total']
        assert error_rate < 0.5, \
            f'{source}: {error_rate:.0%} error rate in last 30 days ({stats["errors"]}/{stats["total"]})'
