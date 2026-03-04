"""
Waterfall demand forecast engine (DTC / Shopify only).

Splits forecast into repeat customer demand (base) + new customer demand (from media spend).
Matches the gold standard Repeat Model spreadsheet methodology:
  - Revenue-based retention curve with 60/30/10 recency weighting
  - Decay rate 0.98, terminal floor 0.005
  - Cohorts from 2020-01 onwards
  - Revenue Matrix: cohort x calendar month
  - Monthly Summary: Total / New / Repeat revenue
"""
import pandas as pd
import time
from datetime import datetime
from db import get_db
from analytics.retention import get_revenue_retention_data, get_customer_cohort_data
from utils.date_helpers import month_str as _month_str, parse_month as _parse_month, add_months as _add_months, month_diff as _month_diff

# Simple TTL cache for expensive computations (survives within a Streamlit rerun cycle).
_cache = {}
_CACHE_TTL = 300  # 5 minutes

# Gold standard parameters
DECAY_RATE = 0.98
TERMINAL_FLOOR = 0.005
COHORT_START = '2020-01'


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


def get_average_retention_curve(source_filter=None, min_cohorts=3, recency_weighted=True):
    """
    Build a revenue-based retention curve matching the gold standard spreadsheet.

    Retention is defined as incremental revenue per month as a fraction of
    first-order revenue:
        retention[N] = (CumulativeRevenue[N] - CumulativeRevenue[N-1]) / 1st_Order_Revenue

    Methodology (matches gold standard exactly):
    - Only cohorts from 2020-01 onwards
    - Weighted average across all cohorts that have data at each offset
    - Weighting: Last 12 months = 60%, Next 12 = 30%, Rest = 10%
    - Extrapolation beyond observed data: 0.98 decay, 0.50% floor

    The source_filter parameter controls which channel's data to use.
    Defaults to 'shopify' when not specified (original behaviour).
    """
    _sf = source_filter or 'shopify'
    _cache_key = f"rev_retention_{_sf}"
    rev_data = _get_cached(
        _cache_key,
        lambda: get_revenue_retention_data(source_filter=_sf)
    )
    matrix = rev_data['matrix']
    if matrix.empty:
        return {}

    # Only include cohorts from 2020-01 onwards (matching spreadsheet scope)
    all_cohorts = sorted(matrix.index.tolist())
    cohorts = [c for c in all_cohorts if c >= COHORT_START]
    if len(cohorts) < min_cohorts:
        cohorts = all_cohorts
    matrix = matrix.loc[cohorts]

    # --- Recency weighting (60/30/10) ---
    sorted_cohorts = sorted(matrix.index.tolist())
    n = len(sorted_cohorts)
    weights = _compute_recency_weights(sorted_cohorts, recency_weighted)

    # Weighted average across cohorts for each month offset.
    curve = {}
    for col in matrix.columns:
        offset = int(col)
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
            curve[offset] = float(values.mean())

    # Extrapolate beyond observed data
    curve = _extrapolate_curve(curve)
    return curve


def get_customer_retention_curve(source_filter=None, min_cohorts=3, recency_weighted=True):
    """Build a customer repurchase retention curve with recency weighting.

    Uses the same customer cohort data as the DTC/Amazon Retention pages
    (get_customer_cohort_data) but applies 60/30/10 recency weighting and
    decay extrapolation — so rates match the Retention page charts exactly
    when recency_weighted=False, and are close but recency-biased when True.

    Returns dict {month_offset: repurchase_rate} where repurchase_rate is
    the fraction of cohort customers who made a purchase in that month.
    """
    _sf = source_filter or 'shopify'
    _cache_key = f"cust_retention_{_sf}_rw{recency_weighted}"
    matrix = _get_cached(
        _cache_key,
        lambda: get_customer_cohort_data(source_filter=_sf)
    )
    if matrix is None or matrix.empty:
        return {}

    # Convert Period index to string for consistent comparison
    str_index = [str(c) for c in matrix.index]
    matrix.index = str_index

    # Only include cohorts from 2020-01 onwards
    all_cohorts = sorted(matrix.index.tolist())
    cohorts = [c for c in all_cohorts if c >= COHORT_START]
    if len(cohorts) < min_cohorts:
        cohorts = all_cohorts
    matrix = matrix.loc[cohorts]

    sorted_cohorts = sorted(matrix.index.tolist())
    n = len(sorted_cohorts)
    weights = _compute_recency_weights(sorted_cohorts, recency_weighted)

    curve = {}
    for col in matrix.columns:
        offset = int(col)
        if offset == 0:
            continue  # skip M0 (always 100%)
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
            curve[offset] = float(values.mean())

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
    """Extend retention curve beyond observed data.

    Uses fixed 0.98 decay per month with 0.50% floor, matching
    the gold standard's Decay Rate and Terminal Floor parameters.
    """
    if not curve:
        return curve

    max_offset = max((o for o in curve if o > 0), default=0)

    last_known = curve.get(max_offset, 0.05)
    for m in range(max_offset + 1, max_offset + 60):
        last_known *= DECAY_RATE
        last_known = max(last_known, TERMINAL_FLOOR)
        curve[m] = last_known

    return curve


def get_aov_and_units(source_filter=None):
    """
    Compute average order value (AOV) and unit-level metrics from Shopify data.

    The source_filter parameter is accepted for API compatibility but
    the repeat model always uses Shopify-only data internally.

    Returns:
        dict with keys:
            aov                        — overall avg order value (all Shopify orders)
            avg_units_per_order        — overall units per order
            units_per_new_customer     — units per new customer in their first month
            units_per_repeat_customer  — units per repeat customer per return month
            new_customer_aov           — AOV for new customer first-month orders
            new_customer_rev_per_unit  — revenue per unit for new customer orders
            repeat_rev_per_unit        — revenue per unit for repeat customer orders
    """
    with get_db() as conn:
        conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        conn.execute("SET LOCAL work_mem = '256MB'")

        source_clause = "AND o.source = ?"
        params = ['shopify']

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

        # CTE: true first order date from orders table
        first_cte = """
            WITH cust_first AS (
                SELECT customer_id, MIN(order_date) AS first_order_date
                FROM orders WHERE source = ?
                GROUP BY customer_id
            )
        """
        cte_params = ['shopify']

        # New customer metrics (last 12 months)
        new_metrics = conn.execute(f"""
            {first_cte}
            SELECT
                SUM(oi.total_price) as total_rev,
                SUM(oi.quantity) as total_units,
                COUNT(DISTINCT o.customer_id) as num_customers,
                COUNT(DISTINCT o.order_id) as num_orders
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            JOIN cust_first cf ON o.customer_id = cf.customer_id
            WHERE strftime('%Y-%m', o.order_date) = strftime('%Y-%m', cf.first_order_date)
              {source_clause}
              AND o.order_date >= date('now', '-12 months')
        """, cte_params + params).fetchone()

        # Repeat customer metrics (last 12 months)
        rep_metrics = conn.execute(f"""
            {first_cte}
            SELECT
                SUM(oi.total_price) as total_rev,
                SUM(oi.quantity) as total_units,
                COUNT(DISTINCT o.customer_id) as num_customers
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            JOIN cust_first cf ON o.customer_id = cf.customer_id
            WHERE strftime('%Y-%m', o.order_date) != strftime('%Y-%m', cf.first_order_date)
              {source_clause}
              AND o.order_date >= date('now', '-12 months')
        """, cte_params + params).fetchone()

        # Units per repeat customer per month
        rep_upc_rows = conn.execute(f"""
            {first_cte}
            SELECT AVG(monthly_upc) as upc FROM (
                SELECT strftime('%Y-%m', o.order_date) as month,
                       CAST(SUM(oi.quantity) AS REAL) / COUNT(DISTINCT o.customer_id) as monthly_upc
                FROM orders o
                JOIN order_items oi ON o.order_id = oi.order_id
                JOIN cust_first cf ON o.customer_id = cf.customer_id
                WHERE strftime('%Y-%m', o.order_date) != strftime('%Y-%m', cf.first_order_date)
                  {source_clause}
                  AND o.order_date >= date('now', '-12 months')
                GROUP BY strftime('%Y-%m', o.order_date)
            )
        """, cte_params + params).fetchone()

    avg_units = float(row["avg_units"] or 1)

    new_rev = float(new_metrics["total_rev"] or 0)
    new_units = float(new_metrics["total_units"] or 0)
    new_custs = float(new_metrics["num_customers"] or 0)
    new_orders = float(new_metrics["num_orders"] or 0)

    new_customer_aov = new_rev / new_orders if new_orders > 0 else float(row["aov"] or 0)
    new_rev_per_unit = new_rev / new_units if new_units > 0 else float(row["aov"] or 0)
    new_upc = new_units / new_custs if new_custs > 0 else avg_units

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
    Count new Shopify customers per month using first_order_date.

    The source_filter parameter is accepted for API compatibility but
    the repeat model always uses Shopify-only data.

    Returns:
        dict: {month_str: count} e.g. {"2025-03": 45, "2025-04": 38}
    """
    with get_db() as conn:
        rows = conn.execute("""
            WITH cust_first AS (
                SELECT customer_id, MIN(order_date) AS first_order_date
                FROM orders WHERE source = ?
                GROUP BY customer_id
            )
            SELECT
                strftime('%Y-%m', first_order_date) as month,
                COUNT(*) as new_customers
            FROM cust_first
            GROUP BY strftime('%Y-%m', first_order_date)
            ORDER BY month
        """, ['shopify']).fetchall()

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
    Revenue-based waterfall forecast matching the gold standard spreadsheet.

    Uses revenue retention: each month's repeat revenue is projected as
        1st_Order_Total * retention_curve[age] * seasonality[calendar_month]

    Three sources of revenue:
    1. Repeat from historical cohorts (already acquired)
    2. Repeat from future cohorts (media-acquired + organic) that compound
    3. New customer first-order revenue (media + organic)

    The source_filter parameter is accepted for API compatibility but
    the repeat model always uses Shopify-only data.

    Args:
        seasonal_indices: optional dict {month_num(1-12): multiplier}.
            Applied to repeat revenue only (not first-order), matching
            the spreadsheet approach.

    Returns:
        DataFrame with columns:
        [month, repeat_units, new_customer_units, total_units,
         new_customers_acquired, repeat_revenue, new_customer_revenue, total_revenue]
    """
    # Revenue-based retention curve (always Shopify)
    retention = _get_cached("retention_shopify", lambda: get_average_retention_curve())
    if not retention:
        return pd.DataFrame()

    metrics = _get_cached("metrics_shopify", lambda: get_aov_and_units())
    new_upc = metrics["units_per_new_customer"]
    new_customer_aov = metrics.get("new_customer_aov") or metrics["aov"] or 25.0
    new_rev_per_unit = metrics.get("new_customer_rev_per_unit") or new_customer_aov
    rep_rev_per_unit = metrics.get("repeat_rev_per_unit") or new_rev_per_unit

    # Historical cohort data
    historical_customers = _get_cached(
        "new_custs_shopify",
        lambda: get_monthly_new_customers()
    )

    rev_data = _get_cached(
        "rev_retention_shopify",
        lambda: get_revenue_retention_data(source_filter='shopify')
    )
    historical_first_order_rev = rev_data.get('first_order_revenue', pd.Series(dtype=float))

    # Organic baseline
    organic_per_month = _get_organic_baseline(historical_customers)

    now = datetime.utcnow()
    current_month = _month_str(now)
    future_months = [_add_months(current_month, i) for i in range(horizon_months)]

    # Parse media plan: spend x ROAS = 1st order revenue for that cohort
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

        # 1) Historical cohorts: project repeat revenue
        for cohort_month in historical_first_order_rev.index:
            fo_rev = historical_first_order_rev.get(cohort_month, 0)
            if fo_rev <= 0:
                continue
            months_since = _month_diff(month, cohort_month)
            if months_since <= 0:
                continue
            rate = retention.get(months_since, 0)
            repeat_revenue += fo_rev * rate

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

        # 3) Apply seasonal adjustment to repeat revenue only
        if seasonal_indices:
            cal_month = _parse_month(month).month
            factor = seasonal_indices.get(cal_month, 1.0)
            repeat_revenue *= factor

        # 4) New customer first-order revenue
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
    Compute SKU % of sales from recent Shopify orders.

    Returns:
        dict: {sku: pct}  -- sums to 1.0
        dict: {sku: variant_name}
    """
    with get_db() as conn:
        # Aggregate by SKU only — product_name varies over time for the same SKU
        qty_rows = conn.execute(f"""
            SELECT oi.sku, SUM(oi.quantity) as qty
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            WHERE o.order_date >= date('now', '-{lookback_months} months')
              AND o.source = 'shopify'
            GROUP BY oi.sku
        """).fetchall()

        # Grab the most common product_name per SKU for the variant label
        name_rows = conn.execute(f"""
            SELECT oi.sku, oi.product_name, SUM(oi.quantity) as qty
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            WHERE o.order_date >= date('now', '-{lookback_months} months')
              AND o.source = 'shopify'
            GROUP BY oi.sku, oi.product_name
            ORDER BY oi.sku, qty DESC
        """).fetchall()

    total = sum(r["qty"] for r in qty_rows) or 1
    mix = {}
    for r in qty_rows:
        mix[r["sku"]] = r["qty"] / total

    # Keep the highest-volume product_name per SKU
    variants = {}
    for r in name_rows:
        if r["sku"] not in variants:
            variants[r["sku"]] = _extract_variant(r["product_name"])

    return mix, variants


def build_sku_forecast_table(waterfall_df, source_filter=None, min_mix_pct=0.005,
                             sku_seasonal_indices=None, global_seasonal_indices=None):
    """
    Allocate the waterfall total units to SKUs using recent % of sales,
    adjusted by per-SKU seasonal indices when available.

    The source_filter parameter is accepted for API compatibility but
    always uses Shopify data internally.

    Returns:
        DataFrame with SKU rows and month columns showing units.
    """
    if waterfall_df.empty:
        return pd.DataFrame()

    sku_mix, sku_variants = _get_sku_mix()
    if not sku_mix:
        return pd.DataFrame()

    months = waterfall_df["month"].tolist()
    total_by_month = dict(zip(waterfall_df["month"], waterfall_df["total_units"]))

    significant_skus = {sku for sku, pct in sku_mix.items() if pct >= min_mix_pct}
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
