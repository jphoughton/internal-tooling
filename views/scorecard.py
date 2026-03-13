"""Yearly Scorecard — YTD sales rollup vs prior year."""
import logging
from datetime import date

import streamlit as st
import pandas as pd

from db import get_db, read_sql
from analytics.revenue_model import DEFAULTS as _RM_DEFAULTS

log = logging.getLogger(__name__)

# DTC net-to-gross multiplier (Shopify DB stores net; gross ≈ net × 1.386)
_NET_TO_GROSS = _RM_DEFAULTS.get('dtc_net_to_gross', [1.386])[0]

# 2025 Amazon monthly revenue (not tracked in DB for that year)
_AMAZON_2025 = {
    1: 90_391.00,
    2: 65_382.00,
    3: 0.00,
}


@st.cache_data(ttl=300)
def _load_shopify_orders():
    """All Shopify orders with date and total_amount."""
    with get_db() as conn:
        return read_sql(
            "SELECT order_date, total_amount FROM orders WHERE source = %s",
            conn, params=('shopify',),
        )


@st.cache_data(ttl=300)
def _load_amazon_daily():
    """All Amazon daily_sku_sales rows."""
    with get_db() as conn:
        return read_sql(
            "SELECT sale_date, revenue FROM daily_sku_sales WHERE source = %s",
            conn, params=('amazon',),
        )


def _filter_ytd(df, date_col, cutoff_month, cutoff_day):
    """Filter rows to only include dates up to month/day within each year.

    For completed months (month < cutoff_month), keep all rows.
    For the cutoff month, only keep rows where day <= cutoff_day.
    For months after cutoff, drop them.
    """
    if df.empty:
        return df
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    df['_month'] = df[date_col].dt.month
    df['_day'] = df[date_col].dt.day
    mask = (df['_month'] < cutoff_month) | (
        (df['_month'] == cutoff_month) & (df['_day'] <= cutoff_day)
    )
    return df[mask].drop(columns=['_month', '_day'])


def _aggregate_monthly(df, date_col, value_col):
    """Group by year-month and return DataFrame with year, month, total columns."""
    if df.empty:
        return pd.DataFrame(columns=['year', 'month', 'total'])
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    df['year'] = df[date_col].dt.year
    df['month'] = df[date_col].dt.month
    agg = df.groupby(['year', 'month'])[value_col].sum().reset_index()
    agg.rename(columns={value_col: 'total'}, inplace=True)
    return agg


def _fmt(val):
    """Format dollar value."""
    if val >= 1_000_000:
        return f'${val / 1_000_000:,.1f}M'
    if val >= 1_000:
        return f'${val:,.0f}'
    return f'${val:,.2f}'


def _pct_change(current, prior):
    """Return percentage change string."""
    if prior == 0:
        return 'N/A' if current == 0 else '+100%'
    pct = (current - prior) / prior * 100
    sign = '+' if pct > 0 else ''
    return f'{sign}{pct:.1f}%'


def render(ctx):
    """Render the Yearly Scorecard page."""
    st.title('Yearly Scorecard')
    today = date.today()
    current_year = today.year
    prior_year = current_year - 1
    current_month = today.month
    current_day = today.day

    st.caption(f'Through {today.strftime("%B %d, %Y")} — prior year uses same date range')

    # ── Load data & filter to YTD (up to today's month/day in each year) ──
    shopify_raw = _filter_ytd(_load_shopify_orders(), 'order_date', current_month, current_day)
    amazon_raw = _filter_ytd(_load_amazon_daily(), 'sale_date', current_month, current_day)

    shopify_monthly = _aggregate_monthly(shopify_raw, 'order_date', 'total_amount')
    # Convert Shopify net to gross sales
    shopify_monthly['total'] = shopify_monthly['total'] * _NET_TO_GROSS
    amazon_monthly = _aggregate_monthly(amazon_raw, 'sale_date', 'revenue')

    # ── Build month-by-month comparison ──
    months = list(range(1, current_month + 1))
    month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                   'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

    rows = []
    ytd_cy_dtc = ytd_py_dtc = 0
    ytd_cy_amz = ytd_py_amz = 0

    for m in months:
        # Shopify current year
        cy_dtc = shopify_monthly[
            (shopify_monthly['year'] == current_year) & (shopify_monthly['month'] == m)
        ]['total'].sum()

        # Shopify prior year
        py_dtc = shopify_monthly[
            (shopify_monthly['year'] == prior_year) & (shopify_monthly['month'] == m)
        ]['total'].sum()

        # Amazon current year (from DB)
        cy_amz = amazon_monthly[
            (amazon_monthly['year'] == current_year) & (amazon_monthly['month'] == m)
        ]['total'].sum()

        # Amazon prior year (hardcoded for months we have, else DB)
        if m in _AMAZON_2025 and prior_year == 2025:
            py_amz = _AMAZON_2025[m]
        else:
            py_amz = amazon_monthly[
                (amazon_monthly['year'] == prior_year) & (amazon_monthly['month'] == m)
            ]['total'].sum()

        cy_total = cy_dtc + cy_amz
        py_total = py_dtc + py_amz

        ytd_cy_dtc += cy_dtc
        ytd_py_dtc += py_dtc
        ytd_cy_amz += cy_amz
        ytd_py_amz += py_amz

        rows.append({
            'Month': month_names[m - 1],
            f'DTC {current_year}': cy_dtc,
            f'DTC {prior_year}': py_dtc,
            'DTC YoY': _pct_change(cy_dtc, py_dtc),
            f'Amazon {current_year}': cy_amz,
            f'Amazon {prior_year}': py_amz,
            'Amazon YoY': _pct_change(cy_amz, py_amz),
            f'Total {current_year}': cy_total,
            f'Total {prior_year}': py_total,
            'Total YoY': _pct_change(cy_total, py_total),
        })

    ytd_cy_total = ytd_cy_dtc + ytd_cy_amz
    ytd_py_total = ytd_py_dtc + ytd_py_amz

    # ── Hero KPIs ──
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric(
            f'YTD Total ({current_year})',
            _fmt(ytd_cy_total),
            delta=_pct_change(ytd_cy_total, ytd_py_total),
        )
    with c2:
        st.metric(
            f'YTD DTC ({current_year})',
            _fmt(ytd_cy_dtc),
            delta=_pct_change(ytd_cy_dtc, ytd_py_dtc),
        )
    with c3:
        st.metric(
            f'YTD Amazon ({current_year})',
            _fmt(ytd_cy_amz),
            delta=_pct_change(ytd_cy_amz, ytd_py_amz),
        )

    st.divider()

    # ── Monthly breakdown table ──
    st.subheader('Monthly Breakdown')

    df = pd.DataFrame(rows)

    # Add YTD totals row
    ytd_row = {
        'Month': 'YTD Total',
        f'DTC {current_year}': ytd_cy_dtc,
        f'DTC {prior_year}': ytd_py_dtc,
        'DTC YoY': _pct_change(ytd_cy_dtc, ytd_py_dtc),
        f'Amazon {current_year}': ytd_cy_amz,
        f'Amazon {prior_year}': ytd_py_amz,
        'Amazon YoY': _pct_change(ytd_cy_amz, ytd_py_amz),
        f'Total {current_year}': ytd_cy_total,
        f'Total {prior_year}': ytd_py_total,
        'Total YoY': _pct_change(ytd_cy_total, ytd_py_total),
    }
    df = pd.concat([df, pd.DataFrame([ytd_row])], ignore_index=True)

    # Format dollar columns
    dollar_cols = [c for c in df.columns if c not in ('Month', 'DTC YoY', 'Amazon YoY', 'Total YoY')]
    for col in dollar_cols:
        df[col] = df[col].apply(lambda v: _fmt(v) if pd.notna(v) else '$0')

    # Style: bold the YTD row
    def _style_table(styler):
        styler.set_properties(
            subset=pd.IndexSlice[len(df) - 1, :],
            **{'font-weight': '700', 'border-top': '2px solid #0F3557'}
        )
        # Color YoY columns
        for yoy_col in ['DTC YoY', 'Amazon YoY', 'Total YoY']:
            styler.map(
                lambda v: 'color: #16a34a' if isinstance(v, str) and v.startswith('+')
                else ('color: #dc2626' if isinstance(v, str) and v.startswith('-') else ''),
                subset=[yoy_col],
            )
        return styler

    styled = df.style.pipe(_style_table).hide(axis='index')
    st.dataframe(df, use_container_width=True, hide_index=True)
