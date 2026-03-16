"""Centralized daily metric loaders — single source of truth.

Every NC query, daily loader, and revenue split in the codebase calls
through this module. No Streamlit dependency — views wrap with their
own @st.cache_data.
"""
import logging
from datetime import date, timedelta

import pandas as pd

from db import get_db, read_sql

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# NC classification helpers
# ------------------------------------------------------------------

def first_order_cte(source):
    """Return NC classification SQL components for a given source.

    Both channels use MIN(order_date) from orders as the NC definition.
    Shopify: excludes refunded/voided orders.
    Amazon:  uses all orders (365-day retention sync window).

    Returns:
        tuple: (cte_sql, join_sql, first_date_expr, params)
            cte_sql        — WITH clause body (empty string for Amazon)
            join_sql       — JOIN clause to attach first-order info
            first_date_expr — SQL expression for the first order date
            params         — tuple of bind params for the CTE
    """
    if source == 'amazon':
        return (
            ("cust_first AS ("
             "SELECT customer_id, MIN(order_date) AS first_order_date "
             "FROM orders WHERE source = 'amazon' "
             "GROUP BY customer_id)"),
            'JOIN cust_first cf ON o.customer_id = cf.customer_id',
            'cf.first_order_date',
            (),
        )
    # Shopify / DTC — use MIN(order_date) excluding refunded/voided
    return (
        ("cust_first AS ("
         "SELECT customer_id, MIN(order_date) AS first_order_date "
         "FROM orders WHERE source = %s "
         "AND COALESCE(financial_status, 'paid') NOT IN ('refunded', 'voided') "
         "GROUP BY customer_id)"),
        'JOIN cust_first cf ON o.customer_id = cf.customer_id',
        'cf.first_order_date',
        (source,),
    )


def compute_nc_revenue_split(df, revenue_col, oi_total_col, oi_new_col):
    """Fraction-based new/repeat revenue split.

    Uses order-items ground truth (oi_new / oi_total) to split the
    authoritative revenue column into NC and repeat portions.

    Returns:
        (nc_revenue Series, ret_revenue Series)
    """
    oi_total = df[oi_total_col].fillna(0)
    oi_new = df[oi_new_col].fillna(0)
    frac_new = (oi_new / oi_total).fillna(0).clip(0, 1)
    nc_rev = df[revenue_col] * frac_new
    ret_rev = df[revenue_col] * (1 - frac_new)
    return nc_rev, ret_rev


# ------------------------------------------------------------------
# Shopify daily
# ------------------------------------------------------------------

_SHOPIFY_REV_SQL = (
    "SELECT o.order_date AS sale_date, "
    "SUM(o.total_amount - COALESCE(o.total_tax, 0)) AS revenue, "
    "SUM(oi_agg.units) AS units "
    "FROM orders o "
    "LEFT JOIN ("
    "  SELECT order_id, SUM(quantity) AS units "
    "  FROM order_items GROUP BY order_id"
    ") oi_agg ON o.order_id = oi_agg.order_id "
    "WHERE o.source = %s "
    "  AND COALESCE(o.financial_status, 'paid') NOT IN ('refunded', 'voided') "
    "GROUP BY o.order_date ORDER BY sale_date"
)

_SHOPIFY_CUST_SQL = (
    "SELECT o.order_date AS sale_date, "
    "COUNT(DISTINCT o.order_id) AS total_orders, "
    "COUNT(DISTINCT o.customer_id) AS total_customers, "
    "COUNT(DISTINCT CASE WHEN cf.actual_first = o.order_date "
    "THEN o.customer_id END) AS new_customers, "
    "SUM(oi.total_price) AS oi_total_rev, "
    "SUM(CASE WHEN cf.actual_first = o.order_date "
    "THEN oi.total_price ELSE 0 END) AS oi_new_rev "
    "FROM orders o "
    "JOIN (SELECT customer_id, MIN(order_date) AS actual_first "
    "      FROM orders WHERE source = %s "
    "        AND COALESCE(financial_status, 'paid') NOT IN ('refunded', 'voided') "
    "      GROUP BY customer_id) cf "
    "  ON o.customer_id = cf.customer_id "
    "JOIN order_items oi ON o.order_id = oi.order_id "
    "WHERE o.source = %s "
    "  AND COALESCE(o.financial_status, 'paid') NOT IN ('refunded', 'voided') "
    "GROUP BY o.order_date"
)


def query_shopify_daily_raw(conn):
    """Execute the two raw Shopify daily queries.

    Returns (rev_df, cust_df) — used by the orchestrator to save
    precomputed intermediate DataFrames.
    """
    rev_df = read_sql(_SHOPIFY_REV_SQL, conn, params=('shopify',))
    cust_df = read_sql(_SHOPIFY_CUST_SQL, conn, params=('shopify', 'shopify'))
    return rev_df, cust_df


def merge_shopify_with_derived(rev_df, cust_df):
    """Merge revenue + customer DataFrames and compute derived columns.

    Adds: _revenue, _units, _orders, _nc_orders, _ret_orders,
          _nc_revenue, _ret_revenue, _date.
    """
    rev_df['sale_date'] = pd.to_datetime(rev_df['sale_date'])
    cust_df['sale_date'] = pd.to_datetime(cust_df['sale_date'])

    df = rev_df.merge(cust_df, on='sale_date', how='left')
    df['total_orders'] = df['total_orders'].fillna(0).astype(int)
    df['new_customers'] = df['new_customers'].fillna(0).astype(int)
    df['total_customers'] = df['total_customers'].fillna(0).astype(int)

    nc_rev, ret_rev = compute_nc_revenue_split(
        df, 'revenue', 'oi_total_rev', 'oi_new_rev')
    df['_revenue'] = df['revenue']
    df['_units'] = df['units']
    df['_orders'] = df['total_orders']
    df['_nc_orders'] = df['new_customers']
    df['_ret_orders'] = df['_orders'] - df['_nc_orders']
    df['_nc_revenue'] = nc_rev
    df['_ret_revenue'] = ret_rev
    df['_date'] = df['sale_date']
    return df


def load_shopify_daily():
    """Load daily DTC metrics from Shopify DB.

    Tries precomputed data first, falls back to live DB queries.
    Returns DataFrame with derived columns (_revenue, _nc_revenue, etc).
    """
    import json as _json

    # Try precomputed data first
    try:
        from db import get_precomputed
        with get_db() as conn:
            rev_cached = get_precomputed(conn, 'shopify_daily_revenue', max_age_hours=25)
            cust_cached = get_precomputed(conn, 'shopify_daily_customers', max_age_hours=25)
        if rev_cached and cust_cached:
            rev_df = pd.DataFrame(_json.loads(rev_cached))
            cust_df = pd.DataFrame(_json.loads(cust_cached))
            if not rev_df.empty and not cust_df.empty:
                return merge_shopify_with_derived(rev_df, cust_df)
    except Exception:
        pass

    # Fall back to live DB queries
    with get_db() as conn:
        rev_df, cust_df = query_shopify_daily_raw(conn)
    if rev_df.empty:
        return pd.DataFrame()
    return merge_shopify_with_derived(rev_df, cust_df)


# ------------------------------------------------------------------
# Amazon daily
# ------------------------------------------------------------------

_AMAZON_CUST_SQL = (
    "WITH cust_first AS ("
    "  SELECT customer_id, MIN(order_date) AS first_order_date "
    "  FROM orders WHERE source = 'amazon' "
    "  GROUP BY customer_id"
    ") "
    "SELECT DATE(o.order_date) AS sale_date, "
    "COUNT(DISTINCT o.customer_id) AS total_customers, "
    "COUNT(DISTINCT CASE WHEN DATE(cf.first_order_date) = DATE(o.order_date) "
    "THEN o.customer_id END) AS new_customers, "
    "SUM(oi.total_price) AS oi_total_rev, "
    "SUM(CASE WHEN DATE(cf.first_order_date) = DATE(o.order_date) "
    "THEN oi.total_price ELSE 0 END) AS oi_new_rev "
    "FROM orders o "
    "JOIN cust_first cf ON o.customer_id = cf.customer_id "
    "JOIN order_items oi ON o.order_id = oi.order_id "
    "WHERE o.source = %s "
    "GROUP BY DATE(o.order_date)"
)


def load_amazon_daily():
    """Load Amazon daily data with L7D projection for incomplete/missing days.

    Returns DataFrame with columns: sale_date, _date, _amz_revenue,
    _amz_spend, _amz_new_cust, _amz_repeat_cust, _amz_new_rev,
    _amz_repeat_rev, _amz_projected.
    """
    try:
        with get_db() as conn:
            rev_daily = read_sql(
                "SELECT sale_date, SUM(revenue) as _amz_revenue "
                "FROM daily_sku_sales WHERE source = 'amazon' "
                "GROUP BY sale_date ORDER BY sale_date",
                conn,
            )
            spend_daily = read_sql(
                "SELECT date as sale_date, spend as _amz_spend "
                "FROM amazon_daily_rollup ORDER BY date",
                conn,
            )
            cust_daily = read_sql(_AMAZON_CUST_SQL, conn, params=('amazon',))

        if rev_daily.empty:
            return pd.DataFrame()

        df = rev_daily.copy()
        df['_date'] = pd.to_datetime(df['sale_date'])

        # Merge spend (with gap-fill)
        if not spend_daily.empty:
            try:
                from analytics.metrics import project_daily_spend_gaps
                spend_daily = project_daily_spend_gaps(
                    spend_daily, date_col='sale_date', spend_col='_amz_spend')
            except Exception:
                pass
            df = df.merge(
                spend_daily[['sale_date', '_amz_spend']], on='sale_date', how='left')
        if '_amz_spend' not in df.columns:
            df['_amz_spend'] = 0
        df['_amz_spend'] = df['_amz_spend'].fillna(0)

        # Merge customer data
        if not cust_daily.empty:
            cust_daily['sale_date'] = cust_daily['sale_date'].astype(str)
            df = df.merge(
                cust_daily[['sale_date', 'total_customers', 'new_customers',
                            'oi_total_rev', 'oi_new_rev']],
                on='sale_date', how='left',
            )
        for col in ['total_customers', 'new_customers', 'oi_total_rev', 'oi_new_rev']:
            if col not in df.columns:
                df[col] = 0
            df[col] = df[col].fillna(0)

        df['_amz_new_cust'] = df['new_customers'].astype(int)
        df['_amz_repeat_cust'] = (
            df['total_customers'] - df['new_customers']).clip(lower=0).astype(int)

        oi_total = df['oi_total_rev']
        new_frac = (df['oi_new_rev'] / oi_total).where(oi_total > 0, 0)
        df['_amz_new_rev'] = df['_amz_revenue'] * new_frac
        df['_amz_repeat_rev'] = df['_amz_revenue'] * (1 - new_frac)

        # Exclude today
        df = df[df['_date'].dt.date < date.today()]

        # Project incomplete/missing recent days using L7D stable window
        df = _project_amazon_gaps(df)

        if '_amz_projected' not in df.columns:
            df['_amz_projected'] = False
        df['_amz_projected'] = df['_amz_projected'].fillna(False)
        return df

    except Exception as exc:
        log.warning('Amazon daily build failed: %s', exc, exc_info=True)
        return pd.DataFrame()


def _project_amazon_gaps(df):
    """Back-fill incomplete rows and append projected gap rows."""
    if len(df) < 10 or '_amz_new_cust' not in df.columns:
        return df

    sorted_df = df.sort_values('_date')
    stable = sorted_df.iloc[-10:-3]
    if stable.empty:
        return df

    avg = {
        '_amz_new_cust': stable['_amz_new_cust'].mean(),
        '_amz_repeat_cust': stable['_amz_repeat_cust'].mean(),
        '_amz_new_rev': stable['_amz_new_rev'].mean(),
        '_amz_repeat_rev': stable['_amz_repeat_rev'].mean(),
        '_amz_revenue': stable['_amz_revenue'].mean(),
        '_amz_spend': stable['_amz_spend'].mean(),
    }
    threshold = 0.4

    # 1. Back-fill existing rows with incomplete NC data
    nc_keys = ['_amz_new_cust', '_amz_repeat_cust',
               '_amz_new_rev', '_amz_repeat_rev', '_amz_revenue']
    for idx in sorted_df.index[-3:]:
        row_nc = sorted_df.at[idx, '_amz_new_cust']
        if avg['_amz_new_cust'] > 0 and row_nc < avg['_amz_new_cust'] * threshold:
            for k in nc_keys:
                v = avg[k]
                df.at[idx, k] = round(v) if 'cust' in k else round(v, 2)

    # 2. Append projected rows for missing recent days
    yesterday = date.today() - timedelta(days=1)
    max_date = sorted_df['_date'].max().date()
    gap_rows = []
    cur = max_date + timedelta(days=1)
    while cur <= yesterday:
        gap_rows.append({
            'sale_date': str(cur),
            '_date': pd.Timestamp(cur),
            '_amz_revenue': round(avg['_amz_revenue'], 2),
            '_amz_spend': round(avg['_amz_spend'], 2),
            '_amz_new_cust': round(avg['_amz_new_cust']),
            '_amz_repeat_cust': round(avg['_amz_repeat_cust']),
            '_amz_new_rev': round(avg['_amz_new_rev'], 2),
            '_amz_repeat_rev': round(avg['_amz_repeat_rev'], 2),
            '_amz_projected': True,
        })
        cur += timedelta(days=1)
    if gap_rows:
        log.info(
            'Amazon NC projection: adding %d gap rows (max_date=%s, yesterday=%s, avg_nc=%.1f)',
            len(gap_rows), max_date, yesterday, avg['_amz_new_cust'])
        df = pd.concat([df, pd.DataFrame(gap_rows)], ignore_index=True)

    return df


# ------------------------------------------------------------------
# Google Sheet spend
# ------------------------------------------------------------------

def _clean_num(val):
    """Parse a currency/percent string to float."""
    if pd.isna(val):
        return 0.0
    s = str(val).replace('$', '').replace(',', '').replace('%', '').strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _parse_spend_df(df):
    """Convert raw google_sheet_data DataFrame to cleaned spend DataFrame."""
    df['_date'] = pd.to_datetime(df['date'], format='mixed', dayfirst=False)
    df['_ad_spend'] = df['blended_ad_spend'].apply(_clean_num)
    sub_cols = []
    for sub_col in ['subscriptions', 'subscription_revenue', 'active_subscriptions',
                     'total_active_subscriptions', 'total_new_subscriptions',
                     'total_cancelled_subscriptions', 'total_subscription_order_revenue']:
        if sub_col in df.columns:
            df[f'_{sub_col}'] = df[sub_col].apply(_clean_num)
            sub_cols.append(f'_{sub_col}')
    return df[['_date', '_ad_spend'] + sub_cols].copy()


def load_gs_spend():
    """Load ad spend from Google Sheet with 3-path fallback.

    Path 1: precomputed cache (fastest)
    Path 2: DB google_sheet_data table
    Path 3: direct Google Sheets API fetch
    """
    # Path 1: precomputed cache
    try:
        import json as _json
        from db import get_precomputed
        with get_db() as conn:
            cached = get_precomputed(conn, 'gs_spend_cleaned', max_age_hours=25)
        if cached:
            result = pd.DataFrame(_json.loads(cached))
            if '_date' in result.columns and not result.empty:
                result['_date'] = pd.to_datetime(result['_date'], unit='ms', errors='coerce')
                result = result.dropna(subset=['_date'])
            if '_ad_spend' in result.columns and not result.empty:
                log.info('gs_spend loaded from precomputed cache (%d rows, spend sum=%.0f)',
                         len(result), result['_ad_spend'].sum())
                return result
            log.warning('gs_spend precomputed cache had no usable data')
    except Exception as exc:
        log.warning('gs_spend precomputed cache failed: %s', exc)

    # Path 2: DB table
    try:
        with get_db() as conn:
            df = read_sql("SELECT * FROM google_sheet_data ORDER BY id", conn)
            if not df.empty and 'date' in df.columns and 'blended_ad_spend' in df.columns:
                log.info('gs_spend loaded from DB table (%d rows)', len(df))
                return _parse_spend_df(df)
            elif not df.empty:
                log.warning('google_sheet_data missing columns: have %s', list(df.columns)[:5])
            else:
                log.warning('google_sheet_data table is empty')
    except Exception as exc:
        log.warning('gs_spend DB query failed: %s', exc)

    # Path 3: direct fetch from Google Sheets
    try:
        from etl.google_sheets import fetch_daily_data_tab
        import re as _re
        df = fetch_daily_data_tab()
        if not df.empty:
            df.columns = [
                _re.sub(r'[^a-z0-9_]', '', c.strip().lower().replace(' ', '_').replace('-', '_'))
                for c in df.columns
            ]
            if 'date' in df.columns and 'blended_ad_spend' in df.columns:
                log.info('gs_spend loaded via direct sheet fetch (%d rows)', len(df))
                return _parse_spend_df(df)
    except Exception as exc:
        log.warning('gs_spend direct sheet fetch failed: %s', exc)

    return pd.DataFrame()
