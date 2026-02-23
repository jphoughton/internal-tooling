"""
Waterfall demand forecast engine.
Splits forecast into repeat customer demand (base) + new customer demand (from media spend).
"""
import pandas as pd
import time
from datetime import datetime
from dateutil.relativedelta import relativedelta
from db import get_db
from analytics.retention import get_customer_cohort_data

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
    Average the cohort retention matrix across clean cohorts to produce
    a single retention curve: {month_offset: retention_rate}.
    Month 0 is always 1.0 (the first-purchase month).

    Auto-detects and excludes contaminated cohorts (pre-existing customers
    misclassified as new due to data window truncation). Extrapolates
    the curve beyond available data using the plateau rate from months 12+.

    When recency_weighted=True, applies weighting:
        Last 12 months = 60%, Next 12 = 30%, Rest = 10%
    (matches the Repeat Model spreadsheet methodology).
    """
    matrix = _get_cached(
        f"cohort_matrix_{source_filter}",
        lambda: get_customer_cohort_data(source_filter=source_filter)
    )
    if matrix.empty:
        return {}

    # Auto-detect and exclude contaminated cohorts
    contaminated = _detect_contaminated_cohorts(matrix)
    clean_cohorts = [c for c in sorted(matrix.index.tolist()) if c not in contaminated]
    if len(clean_cohorts) < 3:
        clean_cohorts = sorted(matrix.index.tolist())
    matrix = matrix.loc[clean_cohorts]

    # --- Recency weighting ---
    # Assign each cohort a weight based on how recent it is.
    # Last 12 months get 60% of total weight, next 12 get 30%, rest get 10%.
    sorted_cohorts = sorted(matrix.index.tolist())
    n = len(sorted_cohorts)

    if recency_weighted and n >= 6:
        # Split into three tiers by cohort position
        tier1_start = max(0, n - 12)   # last 12 cohorts
        tier2_start = max(0, n - 24)   # months 13-24

        tier1_cohorts = sorted_cohorts[tier1_start:]
        tier2_cohorts = sorted_cohorts[tier2_start:tier1_start]
        tier3_cohorts = sorted_cohorts[:tier2_start]

        # Assign per-cohort weights so tiers sum to 0.6, 0.3, 0.1
        weights = {}
        for c in tier1_cohorts:
            weights[c] = 0.60 / len(tier1_cohorts) if tier1_cohorts else 0
        for c in tier2_cohorts:
            weights[c] = 0.30 / len(tier2_cohorts) if tier2_cohorts else 0
        for c in tier3_cohorts:
            weights[c] = 0.10 / len(tier3_cohorts) if tier3_cohorts else 0

        # If a tier is empty, redistribute its weight
        if not tier2_cohorts and not tier3_cohorts:
            for c in tier1_cohorts:
                weights[c] = 1.0 / len(tier1_cohorts)
        elif not tier3_cohorts:
            for c in tier1_cohorts:
                weights[c] = 0.70 / len(tier1_cohorts)
            for c in tier2_cohorts:
                weights[c] = 0.30 / len(tier2_cohorts)
    else:
        # Equal weighting
        weights = {c: 1.0 / n for c in sorted_cohorts} if n > 0 else {}

    curve = {}
    for col in matrix.columns:
        offset = int(col)
        values = matrix[col].dropna()
        if len(values) == 0:
            continue

        if recency_weighted and n >= 6:
            # Weighted average
            w_sum = 0.0
            val_sum = 0.0
            for cohort in values.index:
                w = weights.get(cohort, 0)
                val_sum += values[cohort] * w
                w_sum += w
            if w_sum > 0:
                curve[offset] = val_sum / w_sum
        else:
            # Simple mean — use recent 24 cohorts when available
            recent_cohorts_24 = sorted_cohorts[-24:] if n > 24 else sorted_cohorts
            recent_values = values[values.index.isin(recent_cohorts_24)]
            if len(recent_values) >= min_cohorts:
                curve[offset] = float(recent_values.mean())
            elif len(values) >= min_cohorts:
                curve[offset] = float(values.mean())
            elif len(values) > 0:
                curve[offset] = float(values.mean())

    # Extrapolate beyond available data with a decaying curve.
    # Compute decay rate from months 12+ (the long-tail plateau region).
    max_offset = max((o for o in curve if o > 0), default=0)
    late_offsets = sorted(o for o in curve if 12 <= o <= max_offset and curve[o] > 0)

    if len(late_offsets) >= 2:
        first_o, first_r = late_offsets[0], curve[late_offsets[0]]
        last_o, last_r = late_offsets[-1], curve[late_offsets[-1]]
        span = last_o - first_o
        if span > 0 and first_r > 0 and last_r > 0:
            decay = (last_r / first_r) ** (1.0 / span)
            decay = min(decay, 0.99)  # never grow
        else:
            decay = 0.98
    else:
        decay = 0.98  # default gentle decay

    last_known = curve.get(max_offset, 0.05)
    floor = 0.005  # 0.5% retention floor (matches Repeat Model terminal floor)
    for m in range(max_offset + 1, max_offset + 60):
        last_known *= decay
        last_known = max(last_known, floor)
        curve[m] = last_known

    return curve


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


def _month_str(dt):
    """Format a datetime as YYYY-MM."""
    return dt.strftime("%Y-%m")


def _parse_month(s):
    """Parse YYYY-MM string to datetime (first of month)."""
    return datetime.strptime(s, "%Y-%m")


def _add_months(month_str, n):
    """Add n months to a YYYY-MM string."""
    dt = _parse_month(month_str)
    dt += relativedelta(months=n)
    return _month_str(dt)


def _month_diff(a, b):
    """Return number of months from b to a (a - b)."""
    da = _parse_month(a)
    db = _parse_month(b)
    return (da.year - db.year) * 12 + (da.month - db.month)


def compute_repeat_demand(source_filter=None, horizon_months=12):
    """
    For each future month, sum repeat purchase contributions from all
    historical cohorts using the retention curve.

    Returns:
        dict: {month_str: predicted_repeat_units}
    """
    retention = get_average_retention_curve(source_filter)
    if not retention:
        return {}

    new_customers = get_monthly_new_customers(source_filter)
    if not new_customers:
        return {}

    metrics = get_aov_and_units(source_filter)
    rep_upc = metrics["units_per_repeat_customer"]

    now = datetime.utcnow()
    current_month = _month_str(now)

    repeat = {}
    for offset in range(horizon_months):
        future_month = _add_months(current_month, offset)
        total_units = 0.0

        for cohort_month, cohort_size in new_customers.items():
            months_since = _month_diff(future_month, cohort_month)
            if months_since <= 0:
                continue
            rate = retention.get(months_since, 0)
            total_units += cohort_size * rate * rep_upc

        repeat[future_month] = round(total_units, 1)

    return repeat


def compute_new_customer_demand(media_plan, source_filter=None, horizon_months=12):
    """
    For each month with planned media spend, compute:
    1. First-order units from newly acquired customers
    2. Repeat units from those new customers in subsequent months

    Args:
        media_plan: list of dicts with keys: month, spend, new_customer_roas

    Returns:
        dict: {month_str: predicted_new_customer_units}
        dict: {month_str: new_customers_acquired}
    """
    retention = get_average_retention_curve(source_filter)
    metrics = get_aov_and_units(source_filter)
    new_customer_aov = metrics.get("new_customer_aov") or metrics["aov"]
    new_upc = metrics["units_per_new_customer"]
    rep_upc = metrics["units_per_repeat_customer"]

    if new_customer_aov <= 0:
        new_customer_aov = 25.0

    now = datetime.utcnow()
    current_month = _month_str(now)

    new_demand = {}
    acquired = {}

    for entry in media_plan:
        plan_month = entry["month"]
        spend = entry.get("spend", 0)
        roas = entry.get("new_customer_roas", 1.0)

        if spend <= 0:
            continue

        new_revenue = spend * roas
        new_customers = new_revenue / new_customer_aov
        acquired[plan_month] = round(new_customers, 1)

        # First-order units (new customer UPC)
        first_units = new_customers * new_upc
        new_demand[plan_month] = new_demand.get(plan_month, 0) + round(first_units, 1)

        # Repeat from these new customers (repeat customer UPC)
        for future_offset in range(1, horizon_months):
            future_month = _add_months(plan_month, future_offset)
            if _month_diff(future_month, current_month) >= horizon_months:
                break
            rate = retention.get(future_offset, 0)
            repeat_units = new_customers * rate * rep_upc
            new_demand[future_month] = new_demand.get(future_month, 0) + round(repeat_units, 1)

    return new_demand, acquired


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
    Combine repeat + new customer demand into a single DataFrame.

    Uses separate units-per-customer for new vs repeat customers and
    handles contaminated early cohorts (pre-existing customers) with
    their own observed retention rates.

    Three sources of demand:
    1. Repeat from historical cohorts (already acquired)
    2. Repeat from future cohorts (media-acquired + organic) that compound
    3. New customer first-order demand (media + organic)

    Args:
        seasonal_indices: optional dict {month_num(1-12): multiplier}.
            When provided, each month's units are scaled by the seasonal
            factor for that calendar month.  E.g. {7: 1.12} means July
            demand is 12% higher than the baseline.

    Returns:
        DataFrame with columns:
        [month, repeat_units, new_customer_units, total_units, new_customers_acquired]
    """
    # Use TTL cache for expensive DB queries — these don't change when media spend changes
    cache_key = f"retention_{source_filter}"
    retention = _get_cached(cache_key, lambda: get_average_retention_curve(source_filter))
    if not retention:
        return pd.DataFrame()

    metrics = _get_cached(f"metrics_{source_filter}", lambda: get_aov_and_units(source_filter))
    new_upc = metrics["units_per_new_customer"]
    rep_upc = metrics["units_per_repeat_customer"]
    # Use new-customer-specific AOV for converting spend→customers.
    # The overall AOV includes repeat customers (who buy smaller orders),
    # which would overcount new customers from media spend.
    new_customer_aov = metrics.get("new_customer_aov") or metrics["aov"] or 25.0

    historical_customers = _get_cached(f"new_custs_{source_filter}", lambda: get_monthly_new_customers(source_filter))

    # Detect contaminated cohorts and get their actual return rates
    matrix = _get_cached(f"cohort_matrix_{source_filter}", lambda: get_customer_cohort_data(source_filter=source_filter))
    contaminated = _detect_contaminated_cohorts(matrix) if not matrix.empty else set()
    contaminated_rates = _get_cached(
        f"contam_rates_{source_filter}_{tuple(sorted(contaminated))}",
        lambda: _get_contaminated_cohort_rates(contaminated, source_filter)
    )

    # Organic baseline (used only for months with no media spend)
    organic_per_month = _get_organic_baseline(historical_customers)

    now = datetime.utcnow()
    current_month = _month_str(now)
    future_months = [_add_months(current_month, i) for i in range(horizon_months)]

    # Parse media plan: spend × ROAS = new customer revenue, then ÷ new customer AOV = customers
    media_custs_by_month = {}
    for entry in media_plan:
        m = entry["month"]
        spend = entry.get("spend", 0)
        roas = entry.get("new_customer_roas") or entry.get("roas", 1.0)
        if spend > 0:
            media_custs_by_month[m] = (spend * roas) / new_customer_aov

    # When media spend is present, it IS the new customer plan (includes organic).
    # Only fall back to organic baseline for months with zero spend.
    future_new_customers = {}
    for m in future_months:
        if m in media_custs_by_month:
            future_new_customers[m] = media_custs_by_month[m]
        else:
            future_new_customers[m] = organic_per_month

    rows = []
    for offset, month in enumerate(future_months):
        repeat_units = 0.0

        # 1) Historical cohorts
        for cohort_month, cohort_size in historical_customers.items():
            months_since = _month_diff(month, cohort_month)
            if months_since <= 0:
                continue

            if cohort_month in contaminated_rates:
                # Contaminated cohort: use observed return rate
                rate = contaminated_rates[cohort_month]
            else:
                rate = retention.get(months_since, 0)

            repeat_units += cohort_size * rate * rep_upc

        # 2) Future cohorts (acquired in earlier forecast months)
        for prev_offset in range(offset):
            prev_month = future_months[prev_offset]
            new_custs = future_new_customers.get(prev_month, 0)
            if new_custs <= 0:
                continue
            months_since = _month_diff(month, prev_month)
            if months_since <= 0:
                continue
            rate = retention.get(months_since, 0)
            repeat_units += new_custs * rate * rep_upc

        # 3) New customer first-order demand
        total_new_custs = future_new_customers.get(month, 0)
        new_units = total_new_custs * new_upc

        # 4) Apply seasonal adjustment (if enabled)
        if seasonal_indices:
            cal_month = _parse_month(month).month  # 1-12
            factor = seasonal_indices.get(cal_month, 1.0)
            repeat_units *= factor
            new_units *= factor

        rows.append({
            "month": month,
            "repeat_units": round(repeat_units, 0),
            "new_customer_units": round(new_units, 0),
            "total_units": round(repeat_units + new_units, 0),
            "new_customers_acquired": round(total_new_custs, 0),
        })

    return pd.DataFrame(rows)


def _extract_variant(product_name):
    """Extract variant name from product name (text after last ' - ')."""
    if not product_name:
        return ""
    parts = product_name.split(" - ")
    return parts[-1].strip() if len(parts) > 1 else product_name.strip()


# Age buckets for variant mix curves (months since first order)
_AGE_BUCKETS = [
    (0, 0, "first_order"),
    (1, 2, "months_1_2"),
    (3, 5, "months_3_5"),
    (6, 11, "months_6_11"),
    (12, 99, "months_12_plus"),
]


def get_variant_mix_by_age(source_filter=None):
    """
    Compute variant purchase mix at each cohort age bucket.

    Uses only orders from the last 12 months so the mix reflects the
    current product line (not discontinued SKUs from years ago).

    Returns:
        dict: {age_label: {sku: mix_pct}}  — mix_pct sums to 1.0 per bucket
        dict: {sku: variant_name}          — SKU to variant name mapping
    """
    with get_db() as conn:
        source_clause = f"AND o.source = '{source_filter}'" if source_filter else ""
        # Only use recent orders for mix calculation so we reflect current product line
        recency_clause = "AND o.order_date >= date('now', '-12 months')"

        age_mixes = {}
        sku_variants = {}

        for lo, hi, label in _AGE_BUCKETS:
            if lo == 0 and hi == 0:
                rows = conn.execute(f"""
                    SELECT oi.sku, oi.product_name, SUM(oi.quantity) as qty
                    FROM order_items oi
                    JOIN orders o ON oi.order_id = o.order_id
                    JOIN customers c ON o.customer_id = c.customer_id
                    WHERE o.order_date = c.first_order_date
                      {source_clause} {recency_clause}
                    GROUP BY oi.sku, oi.product_name
                """).fetchall()
            else:
                rows = conn.execute(f"""
                    SELECT oi.sku, oi.product_name, SUM(oi.quantity) as qty
                    FROM order_items oi
                    JOIN orders o ON oi.order_id = o.order_id
                    JOIN customers c ON o.customer_id = c.customer_id
                    WHERE o.order_date > c.first_order_date
                      AND CAST((julianday(o.order_date) - julianday(c.first_order_date))
                          / 30.44 AS INTEGER) BETWEEN {lo} AND {hi}
                      {source_clause} {recency_clause}
                    GROUP BY oi.sku, oi.product_name
                """).fetchall()

            total = sum(r["qty"] for r in rows) or 1
            mix = {}
            for r in rows:
                mix[r["sku"]] = r["qty"] / total
                if r["sku"] not in sku_variants:
                    sku_variants[r["sku"]] = _extract_variant(r["product_name"])
            age_mixes[label] = mix

        return age_mixes, sku_variants


def _get_age_mix(age_mixes, months_since):
    """
    Get the variant mix for a given cohort age (months since acquisition).
    Interpolates between the defined age buckets.
    """
    if months_since == 0:
        return age_mixes.get("first_order", {})
    elif months_since <= 2:
        return age_mixes.get("months_1_2", {})
    elif months_since <= 5:
        return age_mixes.get("months_3_5", {})
    elif months_since <= 11:
        return age_mixes.get("months_6_11", {})
    else:
        return age_mixes.get("months_12_plus", {})


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


def build_sku_forecast_table(waterfall_df, source_filter=None, min_mix_pct=0.005):
    """
    Allocate the waterfall total units to SKUs using recent % of sales.

    Takes the total_units from each forecast month and multiplies by
    each SKU's recent share of sales. Simple, stable, and matches
    the spreadsheet approach used in inventory planning.

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

    rows = []
    for sku in significant_skus:
        pct = sku_mix[sku]
        variant = sku_variants.get(sku, sku)[:55]
        row = {"SKU": sku, "Variant": variant}
        for m in months:
            row[m] = round(total_by_month.get(m, 0) * pct)
        rows.append(row)

    result = pd.DataFrame(rows)
    if months:
        result = result.sort_values(months[0], ascending=False).reset_index(drop=True)
    return result
