"""
Data-driven SKU-level seasonal index computation.

Computes monthly seasonal multipliers per SKU from actual daily_sku_sales data.
Each SKU gets its own seasonal shape (e.g., some flavors peak harder in summer).
Indices are normalized so the average across all 12 months is 1.0.

Algorithm:
    1. For each SKU, query monthly totals from daily_sku_sales
    2. Average daily units for each calendar month (across all years)
    3. Compute grand average across all calendar months
    4. Index = month_avg / grand_avg, then normalize to mean 1.0
    5. Require min_occurrences of each calendar month; fall back to global default otherwise
"""
import logging
from db import get_db, upsert_sku_seasonal_indices
from utils.constants import FORECAST_SKUS, DEFAULT_SEASONAL_INDICES

logger = logging.getLogger(__name__)

# Minimum number of distinct year-months a calendar month must appear in
# before we trust the data-driven index (e.g., need at least 2 Julys).
MIN_MONTH_OCCURRENCES = 2


def compute_sku_seasonal_indices(min_occurrences=MIN_MONTH_OCCURRENCES):
    """
    Compute seasonal index per SKU per calendar month from daily_sku_sales.

    Returns:
        dict: {sku: {month_num(1-12): index_value}}
        Only includes SKUs that have enough data. Index values are normalized
        so the 12-month average is 1.0 for each SKU.
    """
    with get_db() as conn:
        # Get monthly aggregates per SKU: total units and number of days
        # for each (sku, calendar_month) combination, averaged across years.
        rows = conn.execute("""
            SELECT
                sku,
                EXTRACT(MONTH FROM sale_date::date)::int AS cal_month,
                COUNT(DISTINCT TO_CHAR(sale_date::date, 'YYYY-MM')) AS num_year_months,
                SUM(units_sold) AS total_units,
                COUNT(DISTINCT sale_date) AS num_days
            FROM daily_sku_sales
            WHERE sku = ANY(%s)
              AND units_sold > 0
            GROUP BY sku, EXTRACT(MONTH FROM sale_date::date)
            ORDER BY sku, cal_month
        """, (list(FORECAST_SKUS),)).fetchall()

    # Organize: {sku: {cal_month: {total_units, num_days, num_year_months}}}
    sku_data = {}
    for r in rows:
        sku = r['sku']
        cal_month = int(r['cal_month'])
        if sku not in sku_data:
            sku_data[sku] = {}
        sku_data[sku][cal_month] = {
            'total_units': float(r['total_units']),
            'num_days': int(r['num_days']),
            'num_year_months': int(r['num_year_months']),
        }

    result = {}
    for sku, months in sku_data.items():
        # Check if we have enough data: need at least min_occurrences for
        # a majority of calendar months (8 of 12) to compute a meaningful curve
        months_with_enough_data = sum(
            1 for m in range(1, 13)
            if m in months and months[m]['num_year_months'] >= min_occurrences
        )
        if months_with_enough_data < 8:
            continue

        # Compute average daily rate per calendar month
        daily_rates = {}
        for cal_month in range(1, 13):
            if cal_month in months and months[cal_month]['num_days'] > 0:
                daily_rates[cal_month] = (
                    months[cal_month]['total_units'] / months[cal_month]['num_days']
                )
            else:
                daily_rates[cal_month] = None

        # Grand average daily rate (only from months with data)
        valid_rates = [r for r in daily_rates.values() if r is not None]
        if not valid_rates:
            continue
        grand_avg = sum(valid_rates) / len(valid_rates)
        if grand_avg <= 0:
            continue

        # Compute raw indices
        raw_indices = {}
        for cal_month in range(1, 13):
            if daily_rates[cal_month] is not None:
                raw_indices[cal_month] = daily_rates[cal_month] / grand_avg
            else:
                # Fall back to global default for missing months
                raw_indices[cal_month] = DEFAULT_SEASONAL_INDICES.get(cal_month, 1.0)

        # Normalize so average is exactly 1.0
        avg_idx = sum(raw_indices.values()) / 12
        if avg_idx > 0:
            indices = {m: round(v / avg_idx, 4) for m, v in raw_indices.items()}
        else:
            indices = {m: 1.0 for m in range(1, 13)}

        # Clamp to reasonable range [0.3, 3.0]
        indices = {m: max(0.3, min(3.0, v)) for m, v in indices.items()}

        result[sku] = indices

    return result


def refresh_sku_seasonal_indices(min_occurrences=MIN_MONTH_OCCURRENCES):
    """
    Compute SKU-level seasonal indices and persist to the database.

    Also updates the global seasonal_indices table with the sales-weighted
    average across all SKUs (so the global index reflects actual data too).

    Returns:
        dict with 'sku_count' and 'global_updated' keys.
    """
    sku_indices = compute_sku_seasonal_indices(min_occurrences)
    if not sku_indices:
        logger.warning('[seasonal] No SKUs had enough data for seasonal indices')
        return {'sku_count': 0, 'global_updated': False}

    # Persist per-SKU indices
    with get_db() as conn:
        for sku, indices in sku_indices.items():
            # Count how many year-months of data this SKU has
            row = conn.execute(
                "SELECT COUNT(DISTINCT TO_CHAR(sale_date::date, 'YYYY-MM')) AS cnt "
                "FROM daily_sku_sales WHERE sku = %s AND units_sold > 0",
                (sku,)
            ).fetchone()
            sample_months = int(row['cnt']) if row else 0
            upsert_sku_seasonal_indices(conn, sku, indices, sample_months)

    logger.info(
        '[seasonal] Updated seasonal indices for %d SKUs',
        len(sku_indices),
    )

    # Compute a sales-weighted global average from the per-SKU indices
    _update_global_from_sku_indices(sku_indices)

    return {'sku_count': len(sku_indices), 'global_updated': True}


def _update_global_from_sku_indices(sku_indices):
    """
    Update the global seasonal_indices table with a sales-weighted average
    of the per-SKU indices, so the waterfall total reflects actual data.
    """
    from db import upsert_seasonal_index, get_setting

    # Check if user has manually overridden (we respect manual edits)
    with get_db() as conn:
        mode = get_setting(conn, 'seasonality_mode', 'auto')

    if mode == 'manual':
        logger.info('[seasonal] Seasonality mode is manual — skipping global update')
        return

    # Sales-weighted average: weight each SKU by its recent total units
    with get_db() as conn:
        sku_weights = {}
        for sku in sku_indices:
            row = conn.execute(
                "SELECT COALESCE(SUM(units_sold), 0) AS total "
                "FROM daily_sku_sales WHERE sku = %s "
                "AND sale_date::date >= CURRENT_DATE - INTERVAL '90 days'",
                (sku,)
            ).fetchone()
            sku_weights[sku] = float(row['total']) if row else 0

    total_weight = sum(sku_weights.values())
    if total_weight <= 0:
        return

    global_indices = {}
    for cal_month in range(1, 13):
        weighted_sum = sum(
            sku_indices[sku].get(cal_month, 1.0) * sku_weights[sku]
            for sku in sku_indices
        )
        global_indices[cal_month] = round(weighted_sum / total_weight, 4)

    # Normalize to mean 1.0
    avg = sum(global_indices.values()) / 12
    if avg > 0:
        global_indices = {m: round(v / avg, 4) for m, v in global_indices.items()}

    with get_db() as conn:
        for month_num, value in global_indices.items():
            upsert_seasonal_index(conn, month_num, value)

    logger.info('[seasonal] Updated global seasonal indices from %d SKUs', len(sku_indices))
