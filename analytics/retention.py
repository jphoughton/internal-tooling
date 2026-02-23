"""
Retention analytics:
1. Customer repurchase cohort analysis
2. SKU lifecycle (sales decay/growth) curves
3. Triple Whale-style cohort matrices (revenue, customers, LTV)
"""
import numpy as np
import pandas as pd
import streamlit as st
from db import get_db, read_sql


def get_customer_cohort_data(sku_filter=None, source_filter=None):
    """
    Build customer repurchase cohort matrix.
    Groups customers by their first-purchase month, then tracks what %
    made another purchase in months 1, 2, 3... 12 after.

    Returns:
        DataFrame with index=cohort month, columns=months since first purchase,
        values=retention rate (0-1).
    """
    with get_db() as conn:
        query = """
            SELECT
                o.customer_id,
                DATE(o.order_date) as order_date,
                c.first_order_date
            FROM orders o
            JOIN customers c ON o.customer_id = c.customer_id
            JOIN order_items oi ON o.order_id = oi.order_id
            WHERE 1=1
        """
        params = []
        if sku_filter:
            query += " AND oi.sku = ?"
            params.append(sku_filter)
        if source_filter:
            query += " AND o.source = ?"
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


def get_cohort_sizes():
    """Get the size of each monthly cohort."""
    with get_db() as conn:
        df = read_sql("""
            SELECT
                strftime('%Y-%m', first_order_date) as cohort,
                COUNT(*) as cohort_size
            FROM customers
            GROUP BY strftime('%Y-%m', first_order_date)
            ORDER BY cohort
        """, conn)
    return df


@st.cache_data(ttl=300)
def build_cohort_matrices(sku_filter=None, source_filter=None):
    """Build revenue and customer-count matrices for TW-style cohort analysis.

    Returns dict with:
        revenue:      DataFrame (cohort x month_offset) — total revenue
        customers:    DataFrame (cohort x month_offset) — unique customer count
        cohort_sizes: Series (cohort -> total customers)
    """
    with get_db() as conn:
        query = """
            SELECT
                o.customer_id,
                strftime('%Y-%m', c.first_order_date) as cohort,
                strftime('%Y-%m', o.order_date) as order_month,
                SUM(oi.total_price) as revenue
            FROM orders o
            JOIN customers c ON o.customer_id = c.customer_id
            JOIN order_items oi ON o.order_id = oi.order_id
            WHERE 1=1
        """
        params = []
        if sku_filter:
            query += ' AND oi.sku = ?'
            params.append(sku_filter)
        if source_filter:
            query += ' AND o.source = ?'
            params.append(source_filter)
        query += ' GROUP BY o.customer_id, cohort, order_month'
        df = read_sql(query, conn, params=params)

        # Cohort sizes from customers table (all customers, not just those with
        # orders matching the sku/source filter — matches TW's definition)
        size_query = """
            SELECT strftime('%Y-%m', first_order_date) as cohort,
                   COUNT(*) as cohort_size
            FROM customers
            WHERE 1=1
        """
        size_params = []
        if source_filter:
            size_query += ' AND source = ?'
            size_params.append(source_filter)
        size_query += ' GROUP BY cohort ORDER BY cohort'
        sizes_df = read_sql(size_query, conn, params=size_params)

    if df.empty:
        empty = pd.DataFrame()
        return {'revenue': empty, 'customers': empty,
                'cohort_sizes': pd.Series(dtype=float)}

    # Compute month offsets
    df['cohort_period'] = pd.to_datetime(df['cohort'] + '-01')
    df['order_period'] = pd.to_datetime(df['order_month'] + '-01')
    df['months_since'] = (
        (df['order_period'].dt.year - df['cohort_period'].dt.year) * 12
        + df['order_period'].dt.month - df['cohort_period'].dt.month
    )

    # Revenue matrix: sum revenue per (cohort, month_offset)
    rev_agg = df.groupby(['cohort', 'months_since'])['revenue'].sum()
    revenue_matrix = rev_agg.unstack(fill_value=np.nan)
    # Re-introduce NaN for months that weren't observed (unstack fill_value
    # only fills where the multi-index key existed)
    revenue_matrix = rev_agg.unstack()

    # Customer matrix: unique customers per (cohort, month_offset)
    cust_agg = df.groupby(['cohort', 'months_since'])['customer_id'].nunique()
    customer_matrix = cust_agg.unstack()

    # Cohort sizes as a Series indexed by cohort string
    cohort_sizes = pd.Series(
        sizes_df['cohort_size'].values,
        index=sizes_df['cohort'].values,
        dtype=float,
    )

    # Ensure matrices have the same index as cohort_sizes (fill missing cohorts)
    all_cohorts = sorted(set(cohort_sizes.index) | set(revenue_matrix.index))
    revenue_matrix = revenue_matrix.reindex(all_cohorts)
    customer_matrix = customer_matrix.reindex(all_cohorts)
    cohort_sizes = cohort_sizes.reindex(all_cohorts)

    # Ensure integer column headers sorted
    revenue_matrix.columns = revenue_matrix.columns.astype(int)
    customer_matrix.columns = customer_matrix.columns.astype(int)
    revenue_matrix = revenue_matrix[sorted(revenue_matrix.columns)]
    customer_matrix = customer_matrix[sorted(customer_matrix.columns)]

    return {
        'revenue': revenue_matrix,
        'customers': customer_matrix,
        'cohort_sizes': cohort_sizes,
    }


@st.cache_data(ttl=300)
def get_cohort_summary(sku_filter=None, source_filter=None):
    """Compute per-cohort summary metrics: customers, NCPA, AOV, 1st order %.

    Returns DataFrame with columns: cohort, customers, ncpa, aov, first_order_pct.
    """
    with get_db() as conn:
        # Cohort sizes
        size_query = """
            SELECT strftime('%Y-%m', first_order_date) as cohort,
                   COUNT(*) as customers
            FROM customers WHERE 1=1
        """
        size_params = []
        if source_filter:
            size_query += ' AND source = ?'
            size_params.append(source_filter)
        size_query += ' GROUP BY cohort ORDER BY cohort'
        sizes = read_sql(size_query, conn, params=size_params)

        # First-month AOV: revenue and order count for orders in the cohort's first month
        aov_query = """
            SELECT
                strftime('%Y-%m', c.first_order_date) as cohort,
                SUM(oi.total_price) as first_month_revenue,
                COUNT(DISTINCT o.order_id) as first_month_orders
            FROM orders o
            JOIN customers c ON o.customer_id = c.customer_id
            JOIN order_items oi ON o.order_id = oi.order_id
            WHERE strftime('%Y-%m', o.order_date) = strftime('%Y-%m', c.first_order_date)
        """
        aov_params = []
        if sku_filter:
            aov_query += ' AND oi.sku = ?'
            aov_params.append(sku_filter)
        if source_filter:
            aov_query += ' AND o.source = ?'
            aov_params.append(source_filter)
        aov_query += ' GROUP BY cohort'
        aov_df = read_sql(aov_query, conn, params=aov_params)

        # Total ordering customers per month (for 1st order %)
        total_cust_query = """
            SELECT strftime('%Y-%m', o.order_date) as month,
                   COUNT(DISTINCT o.customer_id) as total_customers
            FROM orders o WHERE 1=1
        """
        tc_params = []
        if source_filter:
            total_cust_query += ' AND o.source = ?'
            tc_params.append(source_filter)
        total_cust_query += ' GROUP BY month'
        total_cust = read_sql(total_cust_query, conn, params=tc_params)

        # Media spend for NCPA
        try:
            from db import get_media_spend
            media = get_media_spend(conn, source='All Sources')
        except Exception:
            media = []

    if sizes.empty:
        return pd.DataFrame(columns=['cohort', 'customers', 'ncpa', 'aov', 'first_order_pct'])

    result = sizes.copy()

    # AOV
    if not aov_df.empty:
        aov_map = dict(zip(aov_df['cohort'], aov_df['first_month_revenue'] / aov_df['first_month_orders']))
        result['aov'] = result['cohort'].map(aov_map)
    else:
        result['aov'] = np.nan

    # 1st order %: new customers / total ordering customers that month
    if not total_cust.empty:
        tc_map = dict(zip(total_cust['month'], total_cust['total_customers']))
        result['first_order_pct'] = result.apply(
            lambda r: r['customers'] / tc_map.get(r['cohort'], 1) * 100, axis=1
        )
    else:
        result['first_order_pct'] = np.nan

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

    return result[['cohort', 'customers', 'ncpa', 'aov', 'first_order_pct']]


def get_sku_lifecycle_data(sku=None, normalize=True):
    """
    Track sales volume over time since each SKU's first sale.

    Args:
        sku: Optional specific SKU to return. If None, returns all.
        normalize: If True, normalize to weeks-since-launch.

    Returns:
        DataFrame with columns: sku, period (week number or date), units_sold
    """
    with get_db() as conn:
        query = """
            SELECT
                ds.sku,
                ds.sale_date,
                sm.first_sale_date,
                SUM(ds.units_sold) as units_sold,
                SUM(ds.revenue) as revenue
            FROM daily_sku_sales ds
            JOIN sku_master sm ON ds.sku = sm.sku
        """
        params = []
        if sku:
            query += " WHERE ds.sku = ?"
            params.append(sku)

        query += " GROUP BY ds.sku, ds.sale_date, sm.first_sale_date ORDER BY ds.sku, ds.sale_date"

        df = read_sql(query, conn, params=params)

    if df.empty:
        return pd.DataFrame()

    df["sale_date"] = pd.to_datetime(df["sale_date"])
    df["first_sale_date"] = pd.to_datetime(df["first_sale_date"])

    if normalize:
        df["days_since_launch"] = (df["sale_date"] - df["first_sale_date"]).dt.days
        df["week"] = df["days_since_launch"] // 7

        # Aggregate by week
        weekly = (
            df.groupby(["sku", "week"])
            .agg({"units_sold": "sum", "revenue": "sum"})
            .reset_index()
        )
        return weekly
    else:
        return df


def classify_sku_trend(sku):
    """
    Classify a SKU as 'growing', 'declining', or 'stable' based on
    recent vs. earlier sales velocity.
    """
    lifecycle = get_sku_lifecycle_data(sku=sku, normalize=True)
    if lifecycle.empty or len(lifecycle) < 4:
        return "insufficient_data"

    midpoint = len(lifecycle) // 2
    first_half_avg = lifecycle.iloc[:midpoint]["units_sold"].mean()
    second_half_avg = lifecycle.iloc[midpoint:]["units_sold"].mean()

    if first_half_avg == 0:
        return "new"

    ratio = second_half_avg / first_half_avg
    if ratio > 1.15:
        return "growing"
    elif ratio < 0.85:
        return "declining"
    else:
        return "stable"
