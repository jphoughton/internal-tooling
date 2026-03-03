"""
Retention analytics:
1. Customer repurchase cohort analysis
2. SKU lifecycle (sales decay/growth) curves
3. Triple Whale-style cohort matrices (revenue, customers, LTV)
"""
import datetime
import numpy as np
import pandas as pd
import streamlit as st
from db import get_db, read_sql


def get_customer_cohort_data(sku_filter=None, source_filter=None):
    """
    Build customer repurchase cohort matrix.
    Groups customers by their first *paid* purchase month, then tracks what %
    made another purchase in months 1, 2, 3... 12 after.

    Uses the same paid-order cohort definition as ``build_cohort_matrices``.

    Returns:
        DataFrame with index=cohort month, columns=months since first purchase,
        values=retention rate (0-1).
    """
    with get_db() as conn:
        source_clause = ''
        source_params = []
        if source_filter:
            source_clause = ' AND o.source = ?'
            source_params.append(source_filter)

        cohort_cte = f"""
            effective_cohort AS (
                SELECT o.customer_id,
                       MIN(o.order_date) AS first_paid_date
                FROM orders o
                WHERE o.total_amount > 0 {source_clause}
                GROUP BY o.customer_id
            )
        """

        if sku_filter:
            query = f"""
                WITH {cohort_cte}
                SELECT o.customer_id,
                       DATE(o.order_date) AS order_date,
                       ec.first_paid_date AS first_order_date
                FROM orders o
                JOIN effective_cohort ec ON o.customer_id = ec.customer_id
                JOIN order_items oi ON o.order_id = oi.order_id
                WHERE o.total_amount > 0 AND oi.sku = ?
            """
            params = list(source_params) + [sku_filter]
            if source_filter:
                query += ' AND o.source = ?'
                params.append(source_filter)
        else:
            query = f"""
                WITH {cohort_cte}
                SELECT o.customer_id,
                       DATE(o.order_date) AS order_date,
                       ec.first_paid_date AS first_order_date
                FROM orders o
                JOIN effective_cohort ec ON o.customer_id = ec.customer_id
                WHERE o.total_amount > 0
            """
            params = list(source_params)
            if source_filter:
                query += ' AND o.source = ?'
                params.append(source_filter)

        df = read_sql(query, conn, params=params)

    if df.empty:
        return pd.DataFrame()

    df["order_date"] = pd.to_datetime(df["order_date"])
    df["first_order_date"] = pd.to_datetime(df["first_order_date"])

    # Cohort = month of first purchase
    df["cohort"] = df["first_order_date"].dt.to_period("M")
    df["order_month"] = df["order_date"].dt.to_period("M")
    df["months_since_first"] = (df["order_month"] - df["cohort"]).apply(lambda x: x.n)

    # Get unique customers per cohort
    cohort_sizes = df.groupby("cohort")["customer_id"].nunique().rename("cohort_size")

    # Get unique customers per cohort × month offset
    retention = (
        df.groupby(["cohort", "months_since_first"])["customer_id"]
        .nunique()
        .reset_index()
        .rename(columns={"customer_id": "customers"})
    )

    retention = retention.merge(cohort_sizes, on="cohort")
    retention["retention_rate"] = retention["customers"] / retention["cohort_size"]

    # Pivot to matrix form (NaN for months that haven't elapsed yet)
    matrix = retention.pivot_table(
        index="cohort",
        columns="months_since_first",
        values="retention_rate",
    )

    # Convert period index to strings for display
    matrix.index = matrix.index.astype(str)

    return matrix


def get_cohort_sizes(source_filter=None):
    """Get the size of each monthly cohort (paid orders only)."""
    with get_db() as conn:
        source_clause = ''
        params = []
        if source_filter:
            source_clause = ' AND o.source = ?'
            params.append(source_filter)
        query = f"""
            WITH effective_cohort AS (
                SELECT o.customer_id,
                       MIN(o.order_date) AS first_paid_date
                FROM orders o
                WHERE o.total_amount > 0 {source_clause}
                GROUP BY o.customer_id
            )
            SELECT strftime('%Y-%m', first_paid_date) AS cohort,
                   COUNT(*) AS cohort_size
            FROM effective_cohort
            GROUP BY cohort ORDER BY cohort
        """
        df = read_sql(query, conn, params=params)
    return df


def get_revenue_retention_data(source_filter=None):
    """
    Build revenue-based retention matrix matching the spreadsheet methodology.

    For each cohort, computes:
        retention[N] = (CumulativeRevenue[N] - CumulativeRevenue[N-1]) / 1st_Order_Revenue

    This measures incremental revenue in month N as a fraction of first-order
    dollars — NOT customer count retention.

    Returns:
        dict with:
            matrix: DataFrame (cohort x month_offset) — incremental revenue retention rates
            first_order_revenue: Series (cohort -> 1st order revenue)
            cohort_sizes: Series (cohort -> number of customers)
    """
    with get_db() as conn:
        source_clause = ''
        source_params = []
        if source_filter:
            source_clause = ' AND o.source = ?'
            source_params.append(source_filter)

        # Effective cohort: first paid order per customer
        cohort_cte = f"""
            effective_cohort AS (
                SELECT o.customer_id,
                       MIN(o.order_date) AS first_paid_date
                FROM orders o
                WHERE o.total_amount > 0 {source_clause}
                GROUP BY o.customer_id
            )
        """

        # Get cumulative revenue per (cohort, month_offset)
        query = f"""
            WITH {cohort_cte}
            SELECT
                strftime('%Y-%m', ec.first_paid_date) AS cohort,
                strftime('%Y-%m', o.order_date) AS order_month,
                SUM(o.total_amount) AS revenue
            FROM orders o
            JOIN effective_cohort ec ON o.customer_id = ec.customer_id
            WHERE o.total_amount > 0 {source_clause}
            GROUP BY cohort, order_month
            ORDER BY cohort, order_month
        """
        params = list(source_params)
        if source_filter:
            params.append(source_filter)
        df = read_sql(query, conn, params=params)

        # First-order-only revenue: strictly the first order per customer
        first_order_query = f"""
            WITH {cohort_cte},
            first_order_ids AS (
                SELECT ec.customer_id,
                       ec.first_paid_date,
                       MIN(o.order_id) AS first_order_id
                FROM orders o
                JOIN effective_cohort ec ON o.customer_id = ec.customer_id
                WHERE o.total_amount > 0
                  AND o.order_date = ec.first_paid_date
                  {source_clause}
                GROUP BY ec.customer_id, ec.first_paid_date
            )
            SELECT strftime('%Y-%m', foi.first_paid_date) AS cohort,
                   SUM(o.total_amount) AS first_order_revenue
            FROM first_order_ids foi
            JOIN orders o ON o.order_id = foi.first_order_id
            GROUP BY cohort ORDER BY cohort
        """
        fo_params = list(source_params)
        if source_filter:
            fo_params.append(source_filter)
        fo_df = read_sql(first_order_query, conn, params=fo_params)

        # Cohort sizes
        size_query = f"""
            WITH {cohort_cte}
            SELECT strftime('%Y-%m', first_paid_date) AS cohort,
                   COUNT(*) AS cohort_size
            FROM effective_cohort
            GROUP BY cohort ORDER BY cohort
        """
        sizes_df = read_sql(size_query, conn, params=list(source_params))

    if df.empty:
        return {'matrix': pd.DataFrame(), 'first_order_revenue': pd.Series(dtype=float),
                'cohort_sizes': pd.Series(dtype=float)}

    # Compute month offsets
    df['cohort_dt'] = pd.to_datetime(df['cohort'] + '-01')
    df['order_dt'] = pd.to_datetime(df['order_month'] + '-01')
    df['month_offset'] = (
        (df['order_dt'].dt.year - df['cohort_dt'].dt.year) * 12
        + df['order_dt'].dt.month - df['cohort_dt'].dt.month
    )
    df['revenue'] = df['revenue'].astype(float)

    # Pivot to cumulative revenue matrix: cohort × month_offset
    rev_matrix = df.pivot_table(
        index='cohort', columns='month_offset', values='revenue', aggfunc='sum'
    )
    rev_matrix.columns = rev_matrix.columns.astype(int)
    rev_matrix = rev_matrix[sorted(rev_matrix.columns)]

    # Make it cumulative across month offsets
    cum_matrix = rev_matrix.cumsum(axis=1)

    # First order revenue series
    first_order_rev = pd.Series(
        fo_df['first_order_revenue'].astype(float).values,
        index=fo_df['cohort'].values,
    )

    # Cohort sizes series
    cohort_sizes = pd.Series(
        sizes_df['cohort_size'].astype(float).values,
        index=sizes_df['cohort'].values,
    )

    # Convert cumulative revenue to incremental retention rates:
    # retention[N] = (Cum[N] - Cum[N-1]) / 1st_Order_Revenue
    # For M0: retention[0] = (Cum[0] - 1st_Order_Rev) / 1st_Order_Rev
    #   (same-month repeat above the first order)
    retention_matrix = pd.DataFrame(index=cum_matrix.index, columns=cum_matrix.columns, dtype=float)

    for cohort in cum_matrix.index:
        fo_rev = first_order_rev.get(cohort, 0)
        if fo_rev <= 0:
            continue
        for col in cum_matrix.columns:
            cum_val = cum_matrix.loc[cohort, col]
            if pd.isna(cum_val):
                continue
            if col == 0:
                # M0 = (total month-0 revenue - 1st order revenue) / 1st order revenue
                retention_matrix.loc[cohort, col] = (cum_val - fo_rev) / fo_rev
            else:
                prev_col = col - 1
                prev_val = cum_matrix.loc[cohort, prev_col] if prev_col in cum_matrix.columns else 0
                if pd.isna(prev_val):
                    prev_val = 0
                retention_matrix.loc[cohort, col] = (cum_val - prev_val) / fo_rev

    return {
        'matrix': retention_matrix,
        'first_order_revenue': first_order_rev,
        'cohort_sizes': cohort_sizes,
    }


@st.cache_data(ttl=1800)
def build_cohort_matrices(sku_filter=None, source_filter=None):
    """Build revenue, order-count, and customer-count matrices for TW-style
    cohort analysis.

    Cohort assignment uses each customer's first *paid* order (total_amount > 0)
    so that $0 orders (cancelled, test, subscription activations) don't inflate
    cohort sizes.  Revenue comes from ``orders.total_amount`` (order-level total
    including shipping/taxes) to match Triple Whale's methodology, except when
    a SKU filter is applied — in that case line-item revenue is used since
    order-level totals can't be attributed to a single SKU.

    Returns dict with:
        revenue:            DataFrame (cohort x month_offset) — total revenue
        orders:             DataFrame (cohort x month_offset) — distinct order count
        customers:          DataFrame (cohort x month_offset) — unique customer count
        cohort_sizes:       Series   (cohort -> total customers)
        first_order_revenue: Series  (cohort -> revenue from first order only)
    """
    with get_db() as conn:
        # -- Effective cohort assignment: first paid order per customer ------
        cohort_cte = """
            effective_cohort AS (
                SELECT o.customer_id,
                       MIN(o.order_date) AS first_paid_date
                FROM orders o
                WHERE o.total_amount > 0
        """
        cohort_params = []
        if source_filter:
            cohort_cte += ' AND o.source = ?'
            cohort_params.append(source_filter)
        cohort_cte += ' GROUP BY o.customer_id)'

        if sku_filter:
            # SKU filter: join order_items, use line-item revenue
            query = f"""
                WITH {cohort_cte}
                SELECT
                    o.customer_id,
                    o.order_id,
                    strftime('%Y-%m', ec.first_paid_date) AS cohort,
                    strftime('%Y-%m', o.order_date) AS order_month,
                    SUM(oi.total_price) AS revenue
                FROM orders o
                JOIN effective_cohort ec ON o.customer_id = ec.customer_id
                JOIN order_items oi ON o.order_id = oi.order_id
                WHERE o.total_amount > 0
                  AND oi.sku = ?
            """
            params = list(cohort_params) + [sku_filter]
            if source_filter:
                query += ' AND o.source = ?'
                params.append(source_filter)
            query += ' GROUP BY o.customer_id, o.order_id, cohort, order_month'
        else:
            # No SKU filter: use order-level total_amount (no order_items join)
            query = f"""
                WITH {cohort_cte}
                SELECT
                    o.customer_id,
                    o.order_id,
                    strftime('%Y-%m', ec.first_paid_date) AS cohort,
                    strftime('%Y-%m', o.order_date) AS order_month,
                    o.total_amount AS revenue
                FROM orders o
                JOIN effective_cohort ec ON o.customer_id = ec.customer_id
                WHERE o.total_amount > 0
            """
            params = list(cohort_params)
            if source_filter:
                query += ' AND o.source = ?'
                params.append(source_filter)

        df = read_sql(query, conn, params=params)

        # -- Cohort sizes: customers with at least one paid order -----------
        size_query = f"""
            WITH {cohort_cte}
            SELECT strftime('%Y-%m', ec.first_paid_date) AS cohort,
                   COUNT(*) AS cohort_size
            FROM effective_cohort ec
            GROUP BY cohort ORDER BY cohort
        """
        sizes_df = read_sql(size_query, conn, params=list(cohort_params))

    if df.empty:
        empty = pd.DataFrame()
        return {
            'revenue': empty, 'orders': empty, 'customers': empty,
            'cohort_sizes': pd.Series(dtype=float),
            'first_order_revenue': pd.Series(dtype=float),
        }

    df['revenue'] = df['revenue'].astype(float)

    # Compute month offsets
    df['cohort_period'] = pd.to_datetime(df['cohort'] + '-01')
    df['order_period'] = pd.to_datetime(df['order_month'] + '-01')
    df['months_since'] = (
        (df['order_period'].dt.year - df['cohort_period'].dt.year) * 12
        + df['order_period'].dt.month - df['cohort_period'].dt.month
    )

    # Revenue matrix: sum revenue per (cohort, month_offset)
    rev_agg = df.groupby(['cohort', 'months_since'])['revenue'].sum()
    revenue_matrix = rev_agg.unstack()

    # Order-count matrix: distinct orders per (cohort, month_offset)
    order_agg = df.groupby(['cohort', 'months_since'])['order_id'].nunique()
    order_matrix = order_agg.unstack()

    # Customer matrix: unique customers per (cohort, month_offset)
    cust_agg = df.groupby(['cohort', 'months_since'])['customer_id'].nunique()
    customer_matrix = cust_agg.unstack()

    # First-order-only revenue per cohort
    first_order_idx = df.groupby('customer_id')['order_period'].idxmin()
    first_orders = df.loc[first_order_idx]
    first_order_revenue = first_orders.groupby('cohort')['revenue'].sum()

    # Cohort sizes as a Series indexed by cohort string
    cohort_sizes = pd.Series(
        sizes_df['cohort_size'].values,
        index=sizes_df['cohort'].values,
        dtype=float,
    )

    # Ensure matrices have the same index as cohort_sizes (fill missing cohorts)
    all_cohorts = sorted(set(cohort_sizes.index) | set(revenue_matrix.index))
    revenue_matrix = revenue_matrix.reindex(all_cohorts)
    order_matrix = order_matrix.reindex(all_cohorts)
    customer_matrix = customer_matrix.reindex(all_cohorts)
    cohort_sizes = cohort_sizes.reindex(all_cohorts)
    first_order_revenue = first_order_revenue.reindex(all_cohorts)

    # Ensure integer column headers sorted
    for m in (revenue_matrix, order_matrix, customer_matrix):
        m.columns = m.columns.astype(int)
    revenue_matrix = revenue_matrix[sorted(revenue_matrix.columns)]
    order_matrix = order_matrix[sorted(order_matrix.columns)]
    customer_matrix = customer_matrix[sorted(customer_matrix.columns)]

    return {
        'revenue': revenue_matrix,
        'orders': order_matrix,
        'customers': customer_matrix,
        'cohort_sizes': cohort_sizes,
        'first_order_revenue': first_order_revenue,
    }


@st.cache_data(ttl=1800)
def get_cohort_summary(source_filter=None):
    """Compute per-cohort summary metrics: customers, NCPA, RPR.

    Uses the same paid-order cohort definition as ``build_cohort_matrices``.

    Returns DataFrame with columns: cohort, customers, ncpa, rpr.
    """
    with get_db() as conn:
        # Effective cohort: first paid order
        source_clause = ''
        source_params = []
        if source_filter:
            source_clause = ' AND o.source = ?'
            source_params.append(source_filter)

        # Cohort sizes from paid orders
        size_query = f"""
            WITH effective_cohort AS (
                SELECT o.customer_id,
                       MIN(o.order_date) AS first_paid_date
                FROM orders o
                WHERE o.total_amount > 0 {source_clause}
                GROUP BY o.customer_id
            )
            SELECT strftime('%Y-%m', first_paid_date) AS cohort,
                   COUNT(*) AS customers
            FROM effective_cohort
            GROUP BY cohort ORDER BY cohort
        """
        sizes = read_sql(size_query, conn, params=list(source_params))

        # RPR: repeat purchase rate (% of cohort with 2+ distinct paid orders)
        rpr_query = f"""
            WITH effective_cohort AS (
                SELECT o.customer_id,
                       MIN(o.order_date) AS first_paid_date
                FROM orders o
                WHERE o.total_amount > 0 {source_clause}
                GROUP BY o.customer_id
            ),
            order_counts AS (
                SELECT ec.customer_id,
                       strftime('%Y-%m', ec.first_paid_date) AS cohort,
                       COUNT(DISTINCT o.order_id) AS order_count
                FROM effective_cohort ec
                JOIN orders o ON ec.customer_id = o.customer_id
                WHERE o.total_amount > 0 {source_clause}
                GROUP BY ec.customer_id, cohort
            )
            SELECT cohort,
                   COUNT(*) AS total,
                   SUM(CASE WHEN order_count >= 2 THEN 1 ELSE 0 END) AS repeaters
            FROM order_counts
            GROUP BY cohort ORDER BY cohort
        """
        rpr_df = read_sql(rpr_query, conn, params=list(source_params) + list(source_params))

        # Media spend for NCPA
        try:
            from db import get_media_spend
            media = get_media_spend(conn, source='All Sources')
        except Exception:
            media = []

    if sizes.empty:
        return pd.DataFrame(columns=['cohort', 'customers', 'ncpa', 'rpr'])

    result = sizes.copy()

    # RPR
    if not rpr_df.empty:
        rpr_map = dict(zip(
            rpr_df['cohort'],
            rpr_df['repeaters'].astype(float) / rpr_df['total'].astype(float),
        ))
        result['rpr'] = result['cohort'].map(rpr_map).fillna(0)
    else:
        result['rpr'] = 0.0

    # NCPA: media spend / new customers
    if media:
        spend_map = {e['month']: e['spend'] for e in media}
        result['ncpa'] = result.apply(
            lambda r: spend_map.get(r['cohort'], 0) / r['customers']
            if r['customers'] > 0 and spend_map.get(r['cohort'], 0) > 0
            else np.nan,
            axis=1,
        )
    else:
        result['ncpa'] = np.nan

    return result[['cohort', 'customers', 'ncpa', 'rpr']]


@st.cache_data(ttl=1800)
def get_repeat_rate_summary(source_filter=None):
    """Compute per-cohort repeat purchase metrics.

    Groups customers by first-purchase month, counts how many placed 2+
    distinct orders.  Revenue is derived from order_items.total_price
    (not orders.total_amount) to handle multi-line-item Amazon orders.

    Returns DataFrame: cohort, total_customers, repeat_customers,
                       repeat_rate, first_order_revenue, repeat_revenue.
    """
    first_source = source_filter or 'shopify'
    with get_db() as conn:
        # --- customer-level order counts ---
        cust_query = """
            WITH cust_first AS (
                SELECT customer_id, MIN(order_date) AS first_order_date
                FROM orders WHERE source = ?
                GROUP BY customer_id
            )
            SELECT
                sub.cohort,
                COUNT(*)                                          AS total_customers,
                SUM(CASE WHEN sub.order_count >= 2 THEN 1 ELSE 0 END) AS repeat_customers
            FROM (
                SELECT
                    cf.customer_id,
                    strftime('%Y-%m', cf.first_order_date) AS cohort,
                    COUNT(DISTINCT o.order_id)            AS order_count
                FROM cust_first cf
                JOIN orders o ON cf.customer_id = o.customer_id
                WHERE 1=1
        """
        params = [first_source]
        if source_filter:
            cust_query += " AND o.source = ?"
            params.append(source_filter)
        cust_query += """
                GROUP BY cf.customer_id, cohort
            ) sub
            GROUP BY sub.cohort
            ORDER BY sub.cohort
        """
        cust_df = read_sql(cust_query, conn, params=params)

        # --- revenue split: first-order vs repeat ---
        rev_query = """
            WITH cust_first AS (
                SELECT customer_id, MIN(order_date) AS first_order_date
                FROM orders WHERE source = ?
                GROUP BY customer_id
            )
            SELECT
                strftime('%Y-%m', cf.first_order_date) AS cohort,
                SUM(CASE WHEN strftime('%Y-%m', o.order_date)
                              = strftime('%Y-%m', cf.first_order_date)
                         THEN oi.total_price ELSE 0 END) AS first_order_revenue,
                SUM(CASE WHEN strftime('%Y-%m', o.order_date)
                              != strftime('%Y-%m', cf.first_order_date)
                         THEN oi.total_price ELSE 0 END) AS repeat_revenue
            FROM orders o
            JOIN cust_first cf  ON o.customer_id = cf.customer_id
            JOIN order_items oi ON o.order_id    = oi.order_id
            WHERE 1=1
        """
        rev_params = [first_source]
        if source_filter:
            rev_query += " AND o.source = ?"
            rev_params.append(source_filter)
        rev_query += " GROUP BY cohort ORDER BY cohort"
        rev_df = read_sql(rev_query, conn, params=rev_params)

    if cust_df.empty:
        return pd.DataFrame(columns=[
            'cohort', 'total_customers', 'repeat_customers',
            'repeat_rate', 'first_order_revenue', 'repeat_revenue',
        ])

    result = cust_df.copy()
    result['repeat_rate'] = result['repeat_customers'] / result['total_customers']

    if not rev_df.empty:
        rev_map_first = dict(zip(rev_df['cohort'], rev_df['first_order_revenue']))
        rev_map_repeat = dict(zip(rev_df['cohort'], rev_df['repeat_revenue']))
        result['first_order_revenue'] = result['cohort'].map(rev_map_first).fillna(0)
        result['repeat_revenue'] = result['cohort'].map(rev_map_repeat).fillna(0)
    else:
        result['first_order_revenue'] = 0
        result['repeat_revenue'] = 0

    return result


# ---------------------------------------------------------------------------
# New vs Repeat customer summary helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=1800)
def get_new_repeat_summary(start_date, end_date, source_filter=None):
    """Compute new vs repeat customer summary for a date range.

    A customer is 'new' if their first_order_date falls within
    [start_date, end_date]. A customer is 'repeat' if they placed an order
    in that range but their first_order_date is before start_date.

    Args:
        start_date: YYYY-MM-DD string
        end_date:   YYYY-MM-DD string
        source_filter: 'shopify' (DTC), 'amazon', or None (roll-up)

    Returns dict with: new_customers, repeat_customers, new_revenue,
        repeat_revenue, new_orders, repeat_orders, new_aov, repeat_aov
    """
    first_source = source_filter or 'shopify'
    source_clause = ""
    first_source_params = [first_source]
    params_cust = [start_date, end_date, start_date, start_date, end_date]
    params_rev = [start_date, end_date, start_date, start_date, end_date, start_date, start_date, end_date]

    if source_filter:
        source_clause = " AND o.source = ?"
        params_cust.append(source_filter)
        params_rev.append(source_filter)

    with get_db() as conn:
        # Customer counts
        row = conn.execute(f"""
            WITH cust_first AS (
                SELECT customer_id, MIN(order_date) AS first_order_date
                FROM orders WHERE source = ?
                GROUP BY customer_id
            )
            SELECT
                SUM(CASE WHEN cf.first_order_date >= ? AND cf.first_order_date <= ?
                         THEN 1 ELSE 0 END) AS new_customers,
                SUM(CASE WHEN cf.first_order_date < ?
                         THEN 1 ELSE 0 END) AS repeat_customers
            FROM (
                SELECT DISTINCT o.customer_id
                FROM orders o
                WHERE o.order_date BETWEEN ? AND ?
                {source_clause}
            ) active
            JOIN cust_first cf ON active.customer_id = cf.customer_id
        """, first_source_params + params_cust).fetchone()

        new_customers = int(row['new_customers'] or 0)
        repeat_customers = int(row['repeat_customers'] or 0)

        # Revenue and order counts
        row2 = conn.execute(f"""
            WITH cust_first AS (
                SELECT customer_id, MIN(order_date) AS first_order_date
                FROM orders WHERE source = ?
                GROUP BY customer_id
            )
            SELECT
                SUM(CASE WHEN cf.first_order_date >= ? AND cf.first_order_date <= ?
                         THEN oi.total_price ELSE 0 END) AS new_revenue,
                SUM(CASE WHEN cf.first_order_date < ?
                         THEN oi.total_price ELSE 0 END) AS repeat_revenue,
                COUNT(DISTINCT CASE WHEN cf.first_order_date >= ? AND cf.first_order_date <= ?
                                    THEN o.order_id END) AS new_orders,
                COUNT(DISTINCT CASE WHEN cf.first_order_date < ?
                                    THEN o.order_id END) AS repeat_orders
            FROM orders o
            JOIN cust_first cf  ON o.customer_id = cf.customer_id
            JOIN order_items oi ON o.order_id    = oi.order_id
            WHERE o.order_date BETWEEN ? AND ?
            {source_clause}
        """, first_source_params + params_rev).fetchone()

        new_revenue = float(row2['new_revenue'] or 0)
        repeat_revenue = float(row2['repeat_revenue'] or 0)
        new_orders = int(row2['new_orders'] or 0)
        repeat_orders = int(row2['repeat_orders'] or 0)

    return {
        'new_customers': new_customers,
        'repeat_customers': repeat_customers,
        'new_revenue': round(new_revenue, 2),
        'repeat_revenue': round(repeat_revenue, 2),
        'new_orders': new_orders,
        'repeat_orders': repeat_orders,
        'new_aov': round(new_revenue / new_orders, 2) if new_orders else 0,
        'repeat_aov': round(repeat_revenue / repeat_orders, 2) if repeat_orders else 0,
    }


@st.cache_data(ttl=1800)
def get_new_repeat_daily_revenue(start_date, end_date, source_filter=None):
    """Daily new vs repeat revenue for charting.

    Args:
        start_date: YYYY-MM-DD string
        end_date:   YYYY-MM-DD string
        source_filter: 'shopify', 'amazon', or None (roll-up)

    Returns DataFrame with columns: order_date, new_revenue, repeat_revenue
    """
    first_source = source_filter or 'shopify'
    source_clause = ""
    first_source_params = [first_source]
    params = [start_date, end_date, start_date, start_date, end_date]

    if source_filter:
        source_clause = " AND o.source = ?"
        params.append(source_filter)

    with get_db() as conn:
        df = read_sql(f"""
            WITH cust_first AS (
                SELECT customer_id, MIN(order_date) AS first_order_date
                FROM orders WHERE source = ?
                GROUP BY customer_id
            )
            SELECT
                o.order_date,
                SUM(CASE WHEN cf.first_order_date >= ? AND cf.first_order_date <= ?
                         THEN oi.total_price ELSE 0 END) AS new_revenue,
                SUM(CASE WHEN cf.first_order_date < ?
                         THEN oi.total_price ELSE 0 END) AS repeat_revenue
            FROM orders o
            JOIN cust_first cf  ON o.customer_id = cf.customer_id
            JOIN order_items oi ON o.order_id    = oi.order_id
            WHERE o.order_date BETWEEN ? AND ?
            {source_clause}
            GROUP BY o.order_date
            ORDER BY o.order_date
        """, conn, params=first_source_params + params)

    return df


# ---------------------------------------------------------------------------
# Data-freshness detection + DOW-adjusted projection
# ---------------------------------------------------------------------------

@st.cache_data(ttl=1800)
def get_last_order_date(source_filter=None):
    """Return the most recent order_date in the DB for a given channel."""
    clause = ""
    params = []
    if source_filter:
        clause = " WHERE source = ?"
        params.append(source_filter)
    with get_db() as conn:
        row = conn.execute(
            f"SELECT MAX(order_date) AS last_date FROM orders{clause}", params
        ).fetchone()
    val = row['last_date'] if row else None
    if val is None:
        return None
    if isinstance(val, str):
        return datetime.date.fromisoformat(val)
    if isinstance(val, datetime.datetime):
        return val.date()
    return val


@st.cache_data(ttl=1800)
def _get_dow_daily_new_customers(start_date, end_date, source_filter=None):
    """Return a DataFrame with first_order_date and new_customer count per day,
    plus dow (0=Mon … 6=Sun) for building DOW indices."""
    first_source = source_filter or 'shopify'
    params = [first_source, start_date, end_date]
    with get_db() as conn:
        df = read_sql("""
            WITH cust_first AS (
                SELECT customer_id, MIN(order_date) AS first_order_date
                FROM orders WHERE source = ?
                GROUP BY customer_id
            )
            SELECT first_order_date, COUNT(*) AS new_customers
            FROM cust_first
            WHERE first_order_date BETWEEN ? AND ?
            GROUP BY first_order_date
            ORDER BY first_order_date
        """, conn, params=params)
    if df.empty:
        return df
    df['first_order_date'] = pd.to_datetime(df['first_order_date'])
    df['dow'] = df['first_order_date'].dt.dayofweek  # 0=Mon
    return df


def _project_missing_days(actuals_df, last_data_date, projection_end, nc_aov):
    """Use DOW-adjusted trailing average to project new customers + revenue
    for each missing day between last_data_date+1 and projection_end.

    Returns (projected_customers, projected_revenue, gap_days, method).
    Returns (0, 0, 0, None) when no projection is needed.
    """
    if last_data_date is None or projection_end <= last_data_date:
        return 0, 0.0, 0, None

    gap_days = (projection_end - last_data_date).days
    if gap_days > 14:
        return 0, 0.0, gap_days, 'stale'

    if actuals_df.empty or len(actuals_df) < 3:
        return 0, 0.0, gap_days, 'insufficient'

    overall_avg = actuals_df['new_customers'].mean()

    # Build DOW indices with shrinkage toward mean
    SHRINKAGE = 0.6 if len(actuals_df) >= 7 else 0.4
    dow_avg = actuals_df.groupby('dow')['new_customers'].mean()
    dow_indices = {}
    for d in range(7):
        if d in dow_avg.index:
            blended = SHRINKAGE * dow_avg[d] + (1 - SHRINKAGE) * overall_avg
            dow_indices[d] = blended / overall_avg if overall_avg > 0 else 1.0
        else:
            dow_indices[d] = 1.0

    # Project each missing day
    projected_nc = 0.0
    for offset in range(1, gap_days + 1):
        day = last_data_date + datetime.timedelta(days=offset)
        dow = day.weekday()
        projected_nc += overall_avg * dow_indices.get(dow, 1.0)

    projected_nc = round(projected_nc)
    projected_rev = round(projected_nc * nc_aov, 2) if nc_aov > 0 else 0.0
    return projected_nc, projected_rev, gap_days, 'dow_adjusted'


@st.cache_data(ttl=1800)
def get_projected_new_repeat_summary(start_date, end_date, source_filter=None):
    """Like get_new_repeat_summary but adds projected values when data is stale.

    Returns the same 8-key dict plus:
        projected_new_customers, projected_new_revenue,
        total_new_customers, total_new_revenue,
        gap_days, projection_method, last_data_date
    """
    actuals = get_new_repeat_summary(start_date, end_date, source_filter)

    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    end_dt = datetime.date.fromisoformat(end_date) if isinstance(end_date, str) else end_date

    # Only project up to yesterday or end_date, whichever is earlier
    projection_end = min(yesterday, end_dt)

    last_date = get_last_order_date(source_filter)

    # Get daily actuals for DOW model
    daily_df = _get_dow_daily_new_customers(start_date, end_date, source_filter)

    nc_aov = actuals['new_aov'] if actuals['new_aov'] > 0 else 0

    proj_nc, proj_rev, gap, method = _project_missing_days(
        daily_df, last_date, projection_end, nc_aov
    )

    # Also project repeat customers using same approach but with repeat ratio
    total_actual_cust = actuals['new_customers'] + actuals['repeat_customers']
    if total_actual_cust > 0 and actuals['repeat_customers'] > 0:
        repeat_ratio = actuals['repeat_customers'] / total_actual_cust
        # Rough estimate: if we project X new, proportionally project repeat
        proj_repeat_nc = round(proj_nc * repeat_ratio / (1 - repeat_ratio)) if repeat_ratio < 1 else 0
        proj_repeat_rev = round(proj_repeat_nc * actuals['repeat_aov'], 2) if actuals['repeat_aov'] > 0 else 0.0
    else:
        proj_repeat_nc = 0
        proj_repeat_rev = 0.0

    return {
        **actuals,
        'projected_new_customers': proj_nc,
        'projected_new_revenue': proj_rev,
        'projected_repeat_customers': proj_repeat_nc,
        'projected_repeat_revenue': proj_repeat_rev,
        'total_new_customers': actuals['new_customers'] + proj_nc,
        'total_new_revenue': round(actuals['new_revenue'] + proj_rev, 2),
        'total_repeat_customers': actuals['repeat_customers'] + proj_repeat_nc,
        'total_repeat_revenue': round(actuals['repeat_revenue'] + proj_repeat_rev, 2),
        'gap_days': gap,
        'projection_method': method,
        'last_data_date': str(last_date) if last_date else None,
    }
