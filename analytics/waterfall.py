"""
Waterfall demand forecast engine.
Splits forecast into repeat customer demand (base) + new customer demand (from media spend).
"""
import pandas as pd
import time
from datetime import datetime
from db import get_db
from analytics.retention import get_customer_cohort_data, get_revenue_retention_data
from utils.date_helpers import month_str as _month_str, parse_month as _parse_month, add_months as _add_months, month_diff as _month_diff

# Simple TTL cache for expensive computations (survives within a Streamlit rerun cycle).
_cache = {}
_CACHE_TTL = 300  # 5 minutes


def _get_cached(key, fn):
    """Return cached result if fresh, otherwise compute and cache."""
    now = time.time()
    if key in _cache:
        val, ts = _cache[key]
        if now - ts < _CACHE_TTL:
            return val
    val = fn()
    _cache[key] = (val, now)
    return val


def clear_waterfall_cache():
    """Clear all cached computations (call after data refresh)."""
    _cache.clear()


def get_active_sources():
    """Return list of sources that have sales data."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT source FROM daily_sku_sales ORDER BY source"
        ).fetchall()
    return [r["source"] for r in rows]


def get_configured_sources():
    """Return list of sources that have API credentials configured."""
    import config as cfg

    sources = []
    has_amazon = all(getattr(cfg, k, "") for k in [
        "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
    ])
    if has_amazon:
        sources.append("amazon")

    has_shopify = bool(getattr(cfg, "SHOPIFY_ACCESS_TOKEN", ""))
    if has_shopify:
        sources.append("shopify")

    return sources


def _detect_contaminated_cohorts(matrix):
    """
    Auto-detect cohorts contaminated by data truncation.

    The first months in a dataset contain pre-existing customers whose
    real first order predates our data window. These appear as "new"
    with artificially high retention.

    Detection uses two signals:
    - Month-1 retention > 40% (clearly pre-existing customers)
    - Month-1 > 25% AND month-6 > 20% (sustained abnormal retention)

    Returns:
        set of cohort month strings (e.g. {"2024-02", "2024-03"})
    """
    if matrix.empty or 1 not in matrix.columns:
        return set()

    contaminated = set()
    for cohort in matrix.index:
        m1 = matrix.loc[cohort, 1] if 1 in matrix.columns else float('nan')
        m6 = matrix.loc[cohort, 6] if 6 in matrix.columns else float('nan')

        if m1 != m1:  # NaN
            continue
        if m1 > 0.40:
            contaminated.add(cohort)
        elif m1 > 0.25 and m6 == m6 and m6 > 0.20:
            contaminated.add(cohort)

    return contaminated


def _get_contaminated_cohort_rates(contaminated_cohorts, source_filter=None):
    """
    For contaminated cohorts, compute their stable monthly return rate
    based on recent actual behavior (last 6 months average).

    Returns:
        dict: {cohort_month: avg_monthly_return_rate}
    """
    if not contaminated_cohorts:
        return {}

    rates = {}
    with get_db() as conn:
        source_clause = ""
        params_extra = []
        if source_filter:
            source_clause = "AND o.source = ?"
            params_extra = [source_filter]

        # Find the last 6 complete months
        last_month_row = conn.execute(
            "SELECT MAX(strftime('%Y-%m', order_date)) as m FROM orders"
        ).fetchone()
        if not last_month_row or not last_month_row["m"]:
            return {}

        from dateutil.relativedelta import relativedelta as rd
        last_dt = datetime.strptime(last_month_row["m"], "%Y-%m")
        lookback_months = []
        for i in range(1, 7):
            m = last_dt - rd(months=i)
            lookback_months.append(m.strftime("%Y-%m"))

        for cohort in contaminated_cohorts:
            cohort_size = conn.execute(
                "SELECT COUNT(*) as c FROM customers WHERE strftime('%Y-%m', first_order_date) = ?",
                (cohort,)
            ).fetchone()["c"]
            if cohort_size == 0:
                continue

            monthly_rates = []
            for target in lookback_months:
                active = conn.execute(f"""
                    SELECT COUNT(DISTINCT o.customer_id) as c
                    FROM orders o
                    JOIN customers c ON o.customer_id = c.customer_id
                    WHERE strftime('%Y-%m', c.first_order_date) = ?
                      AND strftime('%Y-%m', o.order_date) = ?
                      {source_clause}
                """, [cohort, target] + params_extra).fetchone()["c"]
                monthly_rates.append(active / cohort_size)

            if monthly_rates:
                rates[cohort] = sum(monthly_rates) / len(monthly_rates)

    return rates


def get_average_retention_curve(source_filter=None, min_cohorts=3, recency_weighted=True):
    """
    Build a revenue-based retention curve matching the spreadsheet methodology.

    Retention is defined as incremental revenue per month as a fraction of
    first-order revenue:
        retention[N] = (CumulativeRevenue[N] - CumulativeRevenue[N-1]) / 1st_Order_Revenue

    This captures both return rate AND order value changes over time, producing
    a single curve that can be multiplied by a cohort's 1st_Order_Total to
    project future revenue directly.

    Weighting: Last 12 months = 60%, Next 12 = 30%, Rest = 10%
    Extrapolation: geometric decay at 0.98x/month, floor 0.5%.
    """
    rev_data = _get_cached(
        f"rev_retention_{source_filter}",
        lambda: get_revenue_retention_data(source_filter=source_filter)
    )
    matrix = rev_data['matrix']
    if matrix.empty:
        # Fall back to customer-count retention if no revenue data
        return _get_customer_retention_curve(source_filter, min_cohorts, recency_weighted)

    # Auto-detect contaminated cohorts
    # Two signals for contamination in revenue retention:
    # 1. M1 retention > 50% (abnormally high same-quarter repeat)
    # 2. Small cohorts with <50 customers (noisy, unreliable rates)
    cohort_sizes = rev_data.get('cohort_sizes', pd.Series(dtype=float))
    contaminated = set()
    for cohort in matrix.index:
        # Exclude tiny cohorts — their rates are too noisy
        size = cohort_sizes.get(cohort, 0)
        if size < 50:
            contaminated.add(cohort)
            continue
        m1 = matrix.loc[cohort, 1] if 1 in matrix.columns else float('nan')
        if pd.isna(m1):
            continue
        if m1 > 0.50:
            contaminated.add(cohort)

    clean_cohorts = [c for c in sorted(matrix.index.tolist()) if c not in contaminated]
    if len(clean_cohorts) < 3:
        clean_cohorts = sorted(matrix.index.tolist())
    matrix = matrix.loc[clean_cohorts]

    # Clip extreme per-cohort retention values before averaging.
    # A single month returning >50% of first-order revenue is an artifact.
    MAX_RETENTION_PER_MONTH = 0.50
    matrix = matrix.clip(upper=MAX_RETENTION_PER_MONTH)

    # --- Recency weighting (60/30/10) ---
    sorted_cohorts = sorted(matrix.index.tolist())
    n = len(sorted_cohorts)
    weights = _compute_recency_weights(sorted_cohorts, recency_weighted)

    # Weighted average across cohorts for each month offset.
    # Only trust offsets where enough recent cohorts contribute data.
    # Beyond ~M36, only old (noisy) cohorts have data — use extrapolation.
    MAX_DATA_OFFSET = 36
    curve = {}
    for col in matrix.columns:
        offset = int(col)
        if offset > MAX_DATA_OFFSET:
            continue  # Will be filled by extrapolation
        values = matrix[col].dropna()
        if len(values) < min_cohorts:
            continue

        if recency_weighted and n >= 6:
            w_sum = 0.0
            val_sum = 0.0
            for cohort in values.index:
                w = weights.get(cohort, 0)
                val_sum += values[cohort] * w
                w_sum += w
            if w_sum > 0:
                curve[offset] = val_sum / w_sum
        else:
            recent_cohorts_24 = sorted_cohorts[-24:] if n > 24 else sorted_cohorts
            recent_values = values[values.index.isin(recent_cohorts_24)]
            if len(recent_values) >= min_cohorts:
                curve[offset] = float(recent_values.mean())
            elif len(values) > 0:
                curve[offset] = float(values.mean())

    # Extrapolate beyond MAX_DATA_OFFSET using geometric decay
    curve = _extrapolate_curve(curve)
    return curve


def _compute_recency_weights(sorted_cohorts, recency_weighted=True):
    """Compute 60/30/10 recency weights for cohort list."""
    n = len(sorted_cohorts)
    if recency_weighted and n >= 6:
        tier1_start = max(0, n - 12)
        tier2_start = max(0, n - 24)
        tier1_cohorts = sorted_cohorts[tier1_start:]
        tier2_cohorts = sorted_cohorts[tier2_start:tier1_start]
        tier3_cohorts = sorted_cohorts[:tier2_start]

        weights = {}
        for c in tier1_cohorts:
            weights[c] = 0.60 / len(tier1_cohorts) if tier1_cohorts else 0
        for c in tier2_cohorts:
            weights[c] = 0.30 / len(tier2_cohorts) if tier2_cohorts else 0
        for c in tier3_cohorts:
            weights[c] = 0.10 / len(tier3_cohorts) if tier3_cohorts else 0

        if not tier2_cohorts and not tier3_cohorts:
            for c in tier1_cohorts:
                weights[c] = 1.0 / len(tier1_cohorts)
        elif not tier3_cohorts:
            for c in tier1_cohorts:
                weights[c] = 0.70 / len(tier1_cohorts)
            for c in tier2_cohorts:
                weights[c] = 0.30 / len(tier2_cohorts)
    else:
        weights = {c: 1.0 / n for c in sorted_cohorts} if n > 0 else {}
    return weights


def _extrapolate_curve(curve):
    """Extend retention curve beyond observed data using geometric decay."""
    if not curve:
        return curve

    max_offset = max((o for o in curve if o > 0), default=0)
    late_offsets = sorted(o for o in curve if 12 <= o <= max_offset and curve[o] > 0)

    if len(late_offsets) >= 2:
        first_o, first_r = late_offsets[0], curve[late_offsets[0]]
        last_o, last_r = late_offsets[-1], curve[late_offsets[-1]]
        span = last_o - first_o
        if span > 0 and first_r > 0 and last_r > 0:
            decay = (last_r / first_r) ** (1.0 / span)
            decay = min(decay, 0.99)
        else:
            decay = 0.98
    else:
        decay = 0.98

    last_known = curve.get(max_offset, 0.05)
    floor = 0.005
    for m in range(max_offset + 1, max_offset + 60):
        last_known *= decay
        last_known = max(last_known, floor)
        curve[m] = last_known

    return curve


def _get_customer_retention_curve(source_filter=None, min_cohorts=3, recency_weighted=True):
    """Fallback: customer-count based retention curve (original method)."""
    matrix = _get_cached(
        f"cohort_matrix_{source_filter}",
        lambda: get_customer_cohort_data(source_filter=source_filter)
    )
    if matrix.empty:
        return {}

    contaminated = _detect_contaminated_cohorts(matrix)
    clean_cohorts = [c for c in sorted(matrix.index.tolist()) if c not in contaminated]
    if len(clean_cohorts) < 3:
        clean_cohorts = sorted(matrix.index.tolist())
    matrix = matrix.loc[clean_cohorts]

    sorted_cohorts = sorted(matrix.index.tolist())
    n = len(sorted_cohorts)
    weights = _compute_recency_weights(sorted_cohorts, recency_weighted)

    curve = {}
    for col in matrix.columns:
        offset = int(col)
        values = matrix[col].dropna()
        if len(values) == 0:
            continue
        if recency_weighted and n >= 6:
            w_sum = 0.0
            val_sum = 0.0
            for cohort in values.index:
                w = weights.get(cohort, 0)
                val_sum += values[cohort] * w
                w_sum += w
            if w_sum > 0:
                curve[offset] = val_sum / w_sum
        else:
            recent_cohorts_24 = sorted_cohorts[-24:] if n > 24 else sorted_cohorts
            recent_values = values[values.index.isin(recent_cohorts_24)]
            if len(recent_values) >= min_cohorts:
                curve[offset] = float(recent_values.mean())
            elif len(values) > 0:
                curve[offset] = float(values.mean())

    return _extrapolate_curve(curve)


def get_aov_and_units(source_filter=None):
    """
    Compute average order value (AOV) and unit-level metrics.

    The key insight: to convert ad spend → units, the correct formula is:
        units = (spend × ROAS) / revenue_per_unit
    NOT:
        units = (spend × ROAS) / AOV × units_per_customer  (WRONG — mismatched denominators)

    We also track new-customer-specific AOV for computing the number of new
    customers acquired (needed for the retention cascade), and units-per-repeat-
    customer-per-month for the repeat demand model.

    Returns:
        dict with keys:
            aov                        — overall avg order value (all orders)
            avg_units_per_order        — overall units per order
            units_per_new_customer     — units per new customer in their first month
            units_per_repeat_customer  — units per repeat customer per return month
            new_customer_aov           — AOV for new customer first-month orders
            new_customer_rev_per_unit  — revenue per unit for new customer orders
            repeat_rev_per_unit        — revenue per unit for repeat customer orders
    """
    with get_db() as conn:
        # Prevent disk-spill crashes on large hash joins: disable parallel
        # workers and give the query enough memory to run in-process.
        conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        conn.execute("SET LOCAL work_mem = '256MB'")

        source_clause = ""
        params = []
        if source_filter:
            source_clause = "AND o.source = ?"
            params.append(source_filter)

        # Overall AOV and UPO
        row = conn.execute(f"""
            SELECT
                AVG(o.total_amount) as aov,
                AVG(item_units) as avg_units
            FROM orders o
            JOIN (
                SELECT order_id, SUM(quantity) as item_units
                FROM order_items GROUP BY order_id
            ) oi ON o.order_id = oi.order_id
            WHERE 1=1 {source_clause}
        """, params).fetchone()

        # New customer metrics: AOV, units per customer, revenue per unit
        # These are computed from first-month orders only (last 12 months)
        new_metrics = conn.execute(f"""
            SELECT
                SUM(oi.total_price) as total_rev,
                SUM(oi.quantity) as total_units,
                COUNT(DISTINCT o.customer_id) as num_customers,
                COUNT(DISTINCT o.order_id) as num_orders
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            JOIN customers c ON o.customer_id = c.customer_id
            WHERE strftime('%Y-%m', o.order_date) = strftime('%Y-%m', c.first_order_date)
              {source_clause}
              AND o.order_date >= date('now', '-12 months')
        """, params).fetchone()

        # Repeat customer metrics: revenue per unit
        rep_metrics = conn.execute(f"""
            SELECT
                SUM(oi.total_price) as total_rev,
                SUM(oi.quantity) as total_units,
                COUNT(DISTINCT o.customer_id) as num_customers
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            JOIN customers c ON o.customer_id = c.customer_id
            WHERE strftime('%Y-%m', o.order_date) != strftime('%Y-%m', c.first_order_date)
              {source_clause}
              AND o.order_date >= date('now', '-12 months')
        """, params).fetchone()

        # Units per repeat customer per month (for retention cascade)
        rep_upc_rows = conn.execute(f"""
            SELECT AVG(monthly_upc) as upc FROM (
                SELECT strftime('%Y-%m', o.order_date) as month,
                       CAST(SUM(oi.quantity) AS REAL) / COUNT(DISTINCT o.customer_id) as monthly_upc
                FROM orders o
                JOIN order_items oi ON o.order_id = oi.order_id
                JOIN customers c ON o.customer_id = c.customer_id
                WHERE strftime('%Y-%m', o.order_date) != strftime('%Y-%m', c.first_order_date)
                  {source_clause}
                  AND o.order_date >= date('now', '-12 months')
                GROUP BY strftime('%Y-%m', o.order_date)
            )
        """, params).fetchone()

    avg_units = float(row["avg_units"] or 1)

    # New customer derived metrics
    new_rev = float(new_metrics["total_rev"] or 0)
    new_units = float(new_metrics["total_units"] or 0)
    new_custs = float(new_metrics["num_customers"] or 0)
    new_orders = float(new_metrics["num_orders"] or 0)

    new_customer_aov = new_rev / new_orders if new_orders > 0 else float(row["aov"] or 0)
    new_rev_per_unit = new_rev / new_units if new_units > 0 else float(row["aov"] or 0)
    new_upc = new_units / new_custs if new_custs > 0 else avg_units

    # Repeat customer derived metrics
    rep_rev = float(rep_metrics["total_rev"] or 0)
    rep_units_total = float(rep_metrics["total_units"] or 0)
    rep_rev_per_unit = rep_rev / rep_units_total if rep_units_total > 0 else new_rev_per_unit
    rep_upc = float(rep_upc_rows["upc"] or avg_units) if rep_upc_rows["upc"] else avg_units

    return {
        "aov": float(row["aov"] or 0),
        "avg_units_per_order": avg_units,
        "units_per_new_customer": new_upc,
        "units_per_repeat_customer": rep_upc,
        "new_customer_aov": new_customer_aov,
        "new_customer_rev_per_unit": new_rev_per_unit,
        "repeat_rev_per_unit": rep_rev_per_unit,
    }


def get_monthly_new_customers(source_filter=None):
    """
    Count new customers per month using first_order_date.

    Returns:
        dict: {month_str: count} e.g. {"2025-03": 45, "2025-04": 38}
    """
    with get_db() as conn:
        where = ""
        params = []
        if source_filter:
            where = "WHERE source = ?"
            params.append(source_filter)

        rows = conn.execute(f"""
            SELECT
                strftime('%Y-%m', first_order_date) as month,
                COUNT(*) as new_customers
            FROM customers
            {where}
            GROUP BY strftime('%Y-%m', first_order_date)
            ORDER BY month
        """, params).fetchall()

    return {r["month"]: r["new_customers"] for r in rows}


def _get_organic_baseline(historical_customers, lookback_months=6):
    """
    Estimate organic new customer acquisition rate from recent history.
    Uses the median of the last N months to avoid outlier skew.
    """
    months = sorted(historical_customers.keys())
    recent = months[-lookback_months:] if len(months) >= lookback_months else months
    values = sorted([historical_customers[m] for m in recent])
    if not values:
        return 0
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def build_waterfall(media_plan, source_filter=None, horizon_months=12,
                    seasonal_indices=None):
    """
    Revenue-based waterfall forecast matching the spreadsheet methodology.

    Uses revenue retention: each month's repeat revenue is projected as
        1st_Order_Total * retention_curve[age] * seasonality[calendar_month]
    instead of customer-count retention × units.

    Three sources of revenue:
    1. Repeat from historical cohorts (already acquired)
    2. Repeat from future cohorts (media-acquired + organic) that compound
    3. New customer first-order revenue (media + organic)

    Still outputs units (for SKU allocation) by dividing revenue by
    revenue-per-unit metrics, but the underlying model is revenue-first.

    Args:
        seasonal_indices: optional dict {month_num(1-12): multiplier}.
            Applied to repeat revenue only (not first-order), matching
            the spreadsheet approach.

    Returns:
        DataFrame with columns:
        [month, repeat_units, new_customer_units, total_units,
         new_customers_acquired, repeat_revenue, new_customer_revenue, total_revenue]
    """
    # Revenue-based retention curve
    cache_key = f"retention_{source_filter}"
    retention = _get_cached(cache_key, lambda: get_average_retention_curve(source_filter))
    if not retention:
        return pd.DataFrame()

    metrics = _get_cached(f"metrics_{source_filter}", lambda: get_aov_and_units(source_filter))
    new_upc = metrics["units_per_new_customer"]
    new_customer_aov = metrics.get("new_customer_aov") or metrics["aov"] or 25.0
    new_rev_per_unit = metrics.get("new_customer_rev_per_unit") or new_customer_aov
    rep_rev_per_unit = metrics.get("repeat_rev_per_unit") or new_rev_per_unit

    # Historical cohort data: need both customer counts and first-order revenue
    historical_customers = _get_cached(
        f"new_custs_{source_filter}",
        lambda: get_monthly_new_customers(source_filter)
    )

    # Get first-order revenue per cohort for the revenue-based projection
    rev_data = _get_cached(
        f"rev_retention_{source_filter}",
        lambda: get_revenue_retention_data(source_filter=source_filter)
    )
    historical_first_order_rev = rev_data.get('first_order_revenue', pd.Series(dtype=float))

    # Organic baseline
    organic_per_month = _get_organic_baseline(historical_customers)

    now = datetime.utcnow()
    current_month = _month_str(now)
    future_months = [_add_months(current_month, i) for i in range(horizon_months)]

    # Parse media plan: spend × ROAS = 1st order revenue for that cohort
    media_first_order_rev = {}
    media_custs_by_month = {}
    for entry in media_plan:
        m = entry["month"]
        spend = entry.get("spend", 0)
        roas = entry.get("new_customer_roas") or entry.get("roas", 1.0)
        if spend > 0:
            first_order_rev = spend * roas
            media_first_order_rev[m] = first_order_rev
            media_custs_by_month[m] = first_order_rev / new_customer_aov

    # Future new customers and their first-order revenue
    future_new_customers = {}
    future_first_order_rev = {}
    for m in future_months:
        if m in media_custs_by_month:
            future_new_customers[m] = media_custs_by_month[m]
            future_first_order_rev[m] = media_first_order_rev[m]
        else:
            future_new_customers[m] = organic_per_month
            future_first_order_rev[m] = organic_per_month * new_customer_aov

    rows = []
    for offset, month in enumerate(future_months):
        repeat_revenue = 0.0

        # 1) Historical cohorts: project repeat revenue using
        #    1st_Order_Total * retention_curve[age] * seasonality
        for cohort_month in historical_first_order_rev.index:
            fo_rev = historical_first_order_rev.get(cohort_month, 0)
            if fo_rev <= 0:
                continue
            months_since = _month_diff(month, cohort_month)
            if months_since <= 0:
                continue
            rate = retention.get(months_since, 0)
            cohort_repeat_rev = fo_rev * rate
            repeat_revenue += cohort_repeat_rev

        # Also include historical cohorts that have customers but no
        # first_order_revenue data (shouldn't happen but be safe)
        for cohort_month, cohort_size in historical_customers.items():
            if cohort_month in historical_first_order_rev.index:
                continue  # Already handled above
            months_since = _month_diff(month, cohort_month)
            if months_since <= 0:
                continue
            rate = retention.get(months_since, 0)
            # Estimate first-order revenue from cohort size × AOV
            repeat_revenue += cohort_size * new_customer_aov * rate

        # 2) Future cohorts (acquired in earlier forecast months)
        for prev_offset in range(offset):
            prev_month = future_months[prev_offset]
            fo_rev = future_first_order_rev.get(prev_month, 0)
            if fo_rev <= 0:
                continue
            months_since = _month_diff(month, prev_month)
            if months_since <= 0:
                continue
            rate = retention.get(months_since, 0)
            repeat_revenue += fo_rev * rate

        # 3) Apply seasonal adjustment to repeat revenue only (not first-order)
        if seasonal_indices:
            cal_month = _parse_month(month).month  # 1-12
            factor = seasonal_indices.get(cal_month, 1.0)
            repeat_revenue *= factor

        # 4) New customer first-order revenue (no seasonality applied, per spreadsheet)
        total_new_custs = future_new_customers.get(month, 0)
        new_revenue = future_first_order_rev.get(month, 0)

        # Convert revenue to units for SKU allocation
        repeat_units = repeat_revenue / rep_rev_per_unit if rep_rev_per_unit > 0 else 0
        new_units = total_new_custs * new_upc

        rows.append({
            "month": month,
            "repeat_units": round(repeat_units, 0),
            "new_customer_units": round(new_units, 0),
            "total_units": round(repeat_units + new_units, 0),
            "new_customers_acquired": round(total_new_custs, 0),
            "repeat_revenue": round(repeat_revenue, 2),
            "new_customer_revenue": round(new_revenue, 2),
            "total_revenue": round(repeat_revenue + new_revenue, 2),
        })

    return pd.DataFrame(rows)


def _extract_variant(product_name):
    """Extract variant name from product name (text after last ' - ')."""
    if not product_name:
        return ""
    parts = product_name.split(" - ")
    return parts[-1].strip() if len(parts) > 1 else product_name.strip()


def _get_sku_mix(source_filter=None, lookback_months=3):
    """
    Compute SKU % of sales from recent orders.

    Uses the last N months of actual sales to determine what fraction
    of total units each SKU represents. This is the same approach as
    static % of sales spreadsheets — simple and accurate.

    Returns:
        dict: {sku: pct}  — sums to 1.0
        dict: {sku: variant_name}
    """
    with get_db() as conn:
        source_clause = f"AND o.source = '{source_filter}'" if source_filter else ""
        rows = conn.execute(f"""
            SELECT oi.sku, oi.product_name, SUM(oi.quantity) as qty
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            WHERE o.order_date >= date('now', '-{lookback_months} months')
              {source_clause}
            GROUP BY oi.sku, oi.product_name
        """).fetchall()

    total = sum(r["qty"] for r in rows) or 1
    mix = {}
    variants = {}
    for r in rows:
        mix[r["sku"]] = r["qty"] / total
        variants[r["sku"]] = _extract_variant(r["product_name"])
    return mix, variants


def build_sku_forecast_table(waterfall_df, source_filter=None, min_mix_pct=0.005,
                             sku_seasonal_indices=None, global_seasonal_indices=None):
    """
    Allocate the waterfall total units to SKUs using recent % of sales,
    adjusted by per-SKU seasonal indices when available.

    When sku_seasonal_indices are provided, the base mix % is adjusted each
    month by the ratio of the SKU's seasonal index to the global seasonal
    index for that calendar month. This shifts units between SKUs based on
    their individual seasonality while preserving the monthly total.

    Args:
        waterfall_df: Output from build_waterfall()
        source_filter: Optional source filter ('shopify', 'amazon', etc.)
        min_mix_pct: Minimum mix % to include a SKU (default 0.5%)
        sku_seasonal_indices: {sku: {month_num(1-12): index_value}} from DB
        global_seasonal_indices: {month_num(1-12): index_value} from DB

    Returns:
        DataFrame with SKU rows and month columns showing units.
    """
    if waterfall_df.empty:
        return pd.DataFrame()

    sku_mix, sku_variants = _get_sku_mix(source_filter)
    if not sku_mix:
        return pd.DataFrame()

    months = waterfall_df["month"].tolist()
    total_by_month = dict(zip(waterfall_df["month"], waterfall_df["total_units"]))

    # Filter to SKUs with meaningful share
    significant_skus = {sku for sku, pct in sku_mix.items() if pct >= min_mix_pct}

    # If we have per-SKU seasonal data, compute month-varying mix
    has_sku_seasonal = bool(sku_seasonal_indices and global_seasonal_indices)

    rows = []
    for sku in significant_skus:
        base_pct = sku_mix[sku]
        variant = sku_variants.get(sku, sku)[:55]
        row = {"SKU": sku, "Variant": variant}

        for m in months:
            if has_sku_seasonal and sku in sku_seasonal_indices:
                cal_month = _parse_month(m).month
                sku_factor = sku_seasonal_indices[sku].get(cal_month, 1.0)
                global_factor = global_seasonal_indices.get(cal_month, 1.0)
                # Relative adjustment: how much more/less seasonal is this SKU
                # compared to the global average for this month
                if global_factor > 0:
                    relative_factor = sku_factor / global_factor
                else:
                    relative_factor = 1.0
                row[m] = total_by_month.get(m, 0) * base_pct * relative_factor
            else:
                row[m] = total_by_month.get(m, 0) * base_pct

        rows.append(row)

    if not rows:
        return pd.DataFrame()

    # Renormalize each month so SKU units sum to the waterfall total
    # (the relative seasonal adjustments may have shifted the total)
    result = pd.DataFrame(rows)
    for m in months:
        col_total = result[m].sum()
        target_total = total_by_month.get(m, 0)
        if col_total > 0 and target_total > 0:
            result[m] = result[m] * (target_total / col_total)
        result[m] = result[m].round(0)

    if months:
        result = result.sort_values(months[0], ascending=False).reset_index(drop=True)
    return result
