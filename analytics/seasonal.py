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

    Uses a detrended approach: computes the seasonal shape within each
    calendar year independently, then averages per-year shapes across years.
    This prevents growth/decline trends from contaminating seasonality
    (e.g., a SKU doubling in sales would otherwise make recent months look
    "seasonal peaks").

    Returns:
        dict: {sku: {month_num(1-12): index_value}}
        Only includes SKUs that have enough data. Index values are normalized
        so the 12-month average is 1.0 for each SKU.
    """
    with get_db() as conn:
        # Get monthly aggregates per SKU per year
        rows = conn.execute("""
            SELECT
                sku,
                EXTRACT(YEAR FROM sale_date::date)::int AS cal_year,
                EXTRACT(MONTH FROM sale_date::date)::int AS cal_month,
                SUM(units_sold) AS total_units,
                COUNT(DISTINCT sale_date) AS num_days
            FROM daily_sku_sales
            WHERE sku = ANY(%s)
              AND units_sold > 0
            GROUP BY sku, EXTRACT(YEAR FROM sale_date::date),
                     EXTRACT(MONTH FROM sale_date::date)
            ORDER BY sku, cal_year, cal_month
        """, (list(FORECAST_SKUS),)).fetchall()

    # Organize: {sku: {year: {cal_month: {total_units, num_days}}}}
    sku_year_data = {}
    for r in rows:
        sku = r['sku']
        year = int(r['cal_year'])
        cal_month = int(r['cal_month'])
        if sku not in sku_year_data:
            sku_year_data[sku] = {}
        if year not in sku_year_data[sku]:
            sku_year_data[sku][year] = {}
        sku_year_data[sku][year][cal_month] = {
            'total_units': float(r['total_units']),
            'num_days': int(r['num_days']),
        }

    result = {}
    for sku, years_data in sku_year_data.items():
        # Only use years with at least 6 months of data (enough to compute
        # a meaningful within-year seasonal shape)
        valid_years = {
            yr: months for yr, months in years_data.items()
            if len(months) >= 6
        }
        if len(valid_years) < min_occurrences:
            continue

        # Compute within-year seasonal index for each valid year
        # For each year: index[month] = daily_rate[month] / avg_daily_rate_that_year
        per_year_indices = {}  # {year: {cal_month: index}}
        for year, months in valid_years.items():
            daily_rates = {}
            for cal_month, data in months.items():
                if data['num_days'] > 0:
                    daily_rates[cal_month] = data['total_units'] / data['num_days']

            if not daily_rates:
                continue
            year_avg = sum(daily_rates.values()) / len(daily_rates)
            if year_avg <= 0:
                continue

            per_year_indices[year] = {
                m: rate / year_avg for m, rate in daily_rates.items()
            }

        if not per_year_indices:
            continue

        # Average the per-year indices across years for each calendar month
        # This detrends: each year contributes equally regardless of volume
        raw_indices = {}
        for cal_month in range(1, 13):
            year_vals = [
                per_year_indices[yr][cal_month]
                for yr in per_year_indices
                if cal_month in per_year_indices[yr]
            ]
            if len(year_vals) >= min_occurrences:
                raw_indices[cal_month] = sum(year_vals) / len(year_vals)
            else:
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
    Update the global seasonal_indices table from actual Shopify repeat
    revenue patterns, NOT from SKU unit sales.

    The global indices are applied to the waterfall's repeat revenue
    projection, so they must reflect repeat purchase seasonality specifically.
    SKU-level unit sales include new customer acquisition timing which
    distorts the seasonal signal.
    """
    from db import upsert_seasonal_index, get_setting

    # Check if user has manually overridden (we respect manual edits)
    with get_db() as conn:
        mode = get_setting(conn, 'seasonality_mode', 'auto')

    if mode == 'manual':
        logger.info('[seasonal] Seasonality mode is manual — skipping global update')
        return

    # Compute global seasonal indices from actual Shopify repeat revenue
    with get_db() as conn:
        rows = conn.execute("""
            WITH first_orders AS (
                SELECT customer_id, MIN(order_date) AS first_date
                FROM orders
                WHERE source = %s AND total_amount > 0
                GROUP BY customer_id
            )
            SELECT
                EXTRACT(MONTH FROM o.order_date::date)::int AS cal_month,
                COUNT(DISTINCT EXTRACT(YEAR FROM o.order_date::date)) AS num_years,
                SUM(o.total_amount) AS total_repeat_rev
            FROM orders o
            JOIN first_orders fo ON o.customer_id = fo.customer_id
            WHERE o.source = %s
              AND o.total_amount > 0
              AND substr(o.order_date, 1, 7) <> substr(fo.first_date, 1, 7)
              AND o.order_date >= %s
            GROUP BY EXTRACT(MONTH FROM o.order_date::date)
            ORDER BY cal_month
        """, ('shopify', 'shopify', '2020-01-01')).fetchall()

    if not rows or len(rows) < 10:
        logger.warning('[seasonal] Not enough repeat revenue data for global indices')
        return

    # Average yearly repeat revenue per calendar month
    monthly_avgs = {}
    for r in rows:
        m = int(r['cal_month'])
        monthly_avgs[m] = float(r['total_repeat_rev']) / int(r['num_years'])

    if not monthly_avgs:
        return

    grand_avg = sum(monthly_avgs.values()) / len(monthly_avgs)
    if grand_avg <= 0:
        return

    # Compute and normalize indices
    global_indices = {}
    for m in range(1, 13):
        if m in monthly_avgs:
            global_indices[m] = monthly_avgs[m] / grand_avg
        else:
            global_indices[m] = 1.0

    avg = sum(global_indices.values()) / 12
    if avg > 0:
        global_indices = {m: round(v / avg, 4) for m, v in global_indices.items()}

    with get_db() as conn:
        for month_num, value in global_indices.items():
            upsert_seasonal_index(conn, month_num, value)

    logger.info('[seasonal] Updated global seasonal indices from Shopify repeat revenue')


def _compute_raw_seasonal(source, conn):
    """Compute raw seasonal indices + per-month year counts for a source."""
    rows = conn.execute("""
        WITH first_orders AS (
            SELECT customer_id, MIN(order_date) AS first_date
            FROM orders
            WHERE source = %s AND total_amount > 0
            GROUP BY customer_id
        )
        SELECT
            EXTRACT(MONTH FROM o.order_date::date)::int AS cal_month,
            COUNT(DISTINCT EXTRACT(YEAR FROM o.order_date::date)) AS num_years,
            SUM(o.total_amount) AS total_rev
        FROM orders o
        JOIN first_orders fo ON o.customer_id = fo.customer_id
        WHERE o.source = %s
          AND o.total_amount > 0
          AND substr(o.order_date, 1, 7) <> substr(fo.first_date, 1, 7)
          AND o.order_date >= %s
        GROUP BY EXTRACT(MONTH FROM o.order_date::date)
        ORDER BY cal_month
    """, (source, source, '2020-01-01')).fetchall()
    return rows


def _normalize_indices(raw):
    """Normalize index dict so average = 1.0, fill missing months."""
    indices = {}
    for m in range(1, 13):
        indices[m] = raw.get(m, 1.0)
    avg = sum(indices.values()) / 12
    if avg > 0:
        indices = {m: round(v / avg, 4) for m, v in indices.items()}
    return indices


def compute_channel_seasonal_indices(source='shopify'):
    """
    Compute seasonal indices for a specific channel from its own repeat revenue.

    For non-Shopify channels with limited data (< 2 years per month), blends
    with DTC (Shopify) indices as a Bayesian prior. The blend weight is based
    on how many years of data the channel has for each calendar month:
      - 0 years: 100% DTC
      - 1 year:  70% DTC / 30% channel
      - 2 years: 30% DTC / 70% channel
      - 3+ years: 100% channel

    Returns:
        dict: {month_num(1-12): index_value} normalized so average = 1.0
        Returns None if insufficient data.
    """
    with get_db() as conn:
        rows = _compute_raw_seasonal(source, conn)

        # For non-shopify sources, also get DTC as prior
        dtc_rows = None
        if source != 'shopify':
            dtc_rows = _compute_raw_seasonal('shopify', conn)

    if not rows or len(rows) < 6:
        logger.warning('[seasonal] Not enough data for %s seasonal indices (%d months)',
                       source, len(rows) if rows else 0)
        return None

    # Build raw indices from channel data
    monthly_avgs = {}
    month_years = {}
    for r in rows:
        m = int(r['cal_month'])
        ny = max(int(r['num_years']), 1)
        monthly_avgs[m] = float(r['total_rev']) / ny
        month_years[m] = ny

    if not monthly_avgs:
        return None

    grand_avg = sum(monthly_avgs.values()) / len(monthly_avgs)
    if grand_avg <= 0:
        return None

    raw_indices = {}
    for m in range(1, 13):
        raw_indices[m] = monthly_avgs.get(m, grand_avg) / grand_avg

    channel_indices = _normalize_indices(raw_indices)

    # Blend with DTC prior if channel has limited history
    if source != 'shopify' and dtc_rows and len(dtc_rows) >= 6:
        dtc_avgs = {}
        for r in dtc_rows:
            dm = int(r['cal_month'])
            dtc_avgs[dm] = float(r['total_rev']) / max(int(r['num_years']), 1)
        dtc_grand = sum(dtc_avgs.values()) / len(dtc_avgs) if dtc_avgs else 1.0
        if dtc_grand > 0:
            dtc_raw = {m: dtc_avgs.get(m, dtc_grand) / dtc_grand for m in range(1, 13)}
            dtc_indices = _normalize_indices(dtc_raw)

            blended = {}
            for m in range(1, 13):
                ny = month_years.get(m, 0)
                # Channel weight increases with data years
                if ny >= 3:
                    cw = 1.0
                elif ny == 2:
                    cw = 0.7
                elif ny == 1:
                    cw = 0.3
                else:
                    cw = 0.0
                blended[m] = cw * channel_indices[m] + (1 - cw) * dtc_indices[m]

            channel_indices = _normalize_indices(blended)
            logger.info('[seasonal] Blended %s indices with DTC prior (max %d years/month)',
                        source, max(month_years.values()) if month_years else 0)
    else:
        logger.info('[seasonal] Computed %s seasonal indices (no blending needed)', source)

    return channel_indices
