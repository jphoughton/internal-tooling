"""Shared performance table rendering with gradient period-over-period coloring.

Extracted from views/marketing.py so overview.py and marketing.py can reuse
the same _PERF_COL_DIRECTION, gradient styling, and colored table renderer.
"""
import streamlit as st
import pandas as pd

from ui.tables import gradient_perf_style, _parse_perf_num


# Which direction is "good" for each metric column.
# True = higher is better, False = lower is better (e.g. CPA).
PERF_COL_DIRECTION = {
    # Roll Up columns
    'Spend': True,
    'Revenue': True,
    'Total Rev': True,
    'Total Revenue': True,
    'NC Rev': True,
    'New Rev': True,
    'Repeat Rev': True,
    'NC Orders': True,
    'New Cust': True,
    'Repeat Cust': True,
    'Total Cust': True,
    'New Users': True,
    'NC MER': True,
    'MER': True,
    'NC ROAS': True,
    'NC AOV': True,
    'NC CPA': False,
    'Cost/New User': False,
    # Overview-specific columns
    'CPA': False,
    'NC Rev %': True,
    'Contribution %': True,
}


def render_perf_table_colored(df, period_col, max_height=420, grey_cols=None):
    """Render a performance table with gradient period-over-period color coding.

    Args:
        df: DataFrame with a period column and numeric metric columns.
        period_col: Name of the column containing period labels (e.g. 'Day', 'Week').
        max_height: Max pixel height for the scrollable container.
        grey_cols: Optional set of column names to render in muted grey instead of
                   colored (used for metrics that aren't meaningful at certain granularities).
    """
    if df.empty or len(df) < 2:
        from ui.components import render_html_table
        render_html_table(df, max_height=max_height)
        return

    grey_cols = grey_cols or set()

    # Sort descending for display (most recent first)
    display_df = df.sort_values(period_col, ascending=False).reset_index(drop=True)
    numeric_cols = [c for c in display_df.columns if c != period_col]

    def _apply_display_styles(row):
        idx = row.name
        styles = [''] * len(row)
        col_map = {c: i for i, c in enumerate(display_df.columns)}

        # Bold period column
        if period_col in col_map:
            styles[col_map[period_col]] = 'font-weight: 600; color: #0F3557'

        # Compare with next row (since sorted desc, next row = previous period)
        if idx >= len(display_df) - 1:
            return styles

        for col in numeric_cols:
            if col not in col_map:
                continue

            # Grey-out columns that aren't meaningful at this granularity
            if col in grey_cols:
                styles[col_map[col]] = 'color: #9ca3af; font-weight: 400'
                continue

            curr_val = _parse_perf_num(display_df.iloc[idx][col])
            prev_val = _parse_perf_num(display_df.iloc[idx + 1][col])

            if curr_val is None or prev_val is None or prev_val == 0:
                continue

            pct_change = (curr_val - prev_val) / abs(prev_val)
            higher_is_good = PERF_COL_DIRECTION.get(col, True)

            styles[col_map[col]] = gradient_perf_style(pct_change, higher_is_good)

        return styles

    styled = (
        display_df.style
        .set_properties(**{
            'font-size': '0.84rem',
            'font-family': 'Visby CF, DM Sans, -apple-system, sans-serif',
            'color': '#1e2d3d',
            'background-color': '#ffffff',
        })
        .apply(_apply_display_styles, axis=1)
        .set_table_styles([
            {'selector': 'th', 'props': [
                ('background-color', '#F0F4F8'),
                ('color', '#0F3557'),
                ('font-weight', '600'),
                ('font-size', '0.74rem'),
                ('text-transform', 'uppercase'),
                ('letter-spacing', '0.05em'),
                ('border-bottom', '2px solid #D6DEE8'),
                ('padding', '11px 14px'),
                ('position', 'sticky'),
                ('top', '0'),
                ('z-index', '1'),
            ]},
            {'selector': 'td', 'props': [
                ('border-bottom', '1px solid #F0F4F8'),
                ('padding', '9px 14px'),
                ('white-space', 'nowrap'),
            ]},
        ])
        .hide(axis='index')
    )
    html = styled.to_html()
    height_style = f'max-height:{max_height}px;overflow-y:auto;' if max_height else ''
    st.markdown(
        f'<div style="background:#ffffff;border-radius:12px;'
        f'box-shadow:0 2px 12px rgba(15,53,87,0.08);border:1px solid #E8EDF3;'
        f'width:100%;overflow:hidden;">'
        f'<div class="perf-table" style="overflow-x:auto;{height_style}">'
        '<style>'
        '.perf-table table { width:100%; border-collapse:collapse; }'
        '.perf-table th, .perf-table td { white-space:nowrap; }'
        '.perf-table th { position:sticky; top:0; z-index:1; background-color:#F0F4F8 !important; }'
        '.perf-table tr:hover td:not([style*="background-color"]) { background:#F7FAFC !important; }'
        '</style>'
        f'{html}</div></div>',
        unsafe_allow_html=True,
    )


def render_perf_table_transposed(df, period_col, max_height=420, grey_cols=None):
    """Render a performance table transposed: periods as columns, metrics as rows.

    Coloring compares each metric across periods (most recent vs prior).
    """
    if df.empty or len(df) < 2:
        from ui.components import render_html_table
        render_html_table(df, max_height=max_height)
        return

    grey_cols = grey_cols or set()

    # Sort periods descending (most recent first)
    sorted_df = df.sort_values(period_col, ascending=False).reset_index(drop=True)
    period_labels = sorted_df[period_col].tolist()
    metric_cols = [c for c in sorted_df.columns if c != period_col]

    # Build transposed: rows=metrics, columns=period labels
    transposed = {'Metric': metric_cols}
    for _, row in sorted_df.iterrows():
        transposed[row[period_col]] = [row[c] for c in metric_cols]
    t_df = pd.DataFrame(transposed)

    from ui.tables import gradient_perf_style, _parse_perf_num

    def _apply_styles(row):
        idx = row.name
        metric = row['Metric']
        styles = ['font-weight:600;color:#0F3557'] + [''] * (len(row) - 1)

        if metric in grey_cols:
            styles = ['font-weight:600;color:#9ca3af'] + ['color:#9ca3af;font-weight:400'] * (len(row) - 1)
            return styles

        # Compare most recent (col index 1) with the next older period (col index 2)
        if len(period_labels) >= 2:
            curr_val = _parse_perf_num(row.iloc[1])
            prev_val = _parse_perf_num(row.iloc[2])
            if curr_val is not None and prev_val is not None and prev_val != 0:
                pct_change = (curr_val - prev_val) / abs(prev_val)
                higher_is_good = PERF_COL_DIRECTION.get(metric, True)
                styles[1] = gradient_perf_style(pct_change, higher_is_good)

        return styles

    styled = (
        t_df.style
        .set_properties(**{
            'font-size': '0.84rem',
            'font-family': 'Visby CF, DM Sans, -apple-system, sans-serif',
            'color': '#1e2d3d',
            'background-color': '#ffffff',
        })
        .apply(_apply_styles, axis=1)
        .set_table_styles([
            {'selector': 'th', 'props': [
                ('background-color', '#F0F4F8'),
                ('color', '#0F3557'),
                ('font-weight', '600'),
                ('font-size', '0.74rem'),
                ('text-transform', 'uppercase'),
                ('letter-spacing', '0.05em'),
                ('border-bottom', '2px solid #D6DEE8'),
                ('padding', '11px 14px'),
                ('position', 'sticky'),
                ('top', '0'),
                ('z-index', '1'),
            ]},
            {'selector': 'td', 'props': [
                ('border-bottom', '1px solid #F0F4F8'),
                ('padding', '9px 14px'),
                ('white-space', 'nowrap'),
            ]},
        ])
        .hide(axis='index')
    )
    html = styled.to_html()
    height_style = f'max-height:{max_height}px;overflow-y:auto;' if max_height else ''
    st.markdown(
        f'<div style="background:#ffffff;border-radius:12px;'
        f'box-shadow:0 2px 12px rgba(15,53,87,0.08);border:1px solid #E8EDF3;'
        f'width:100%;overflow:hidden;">'
        f'<div class="perf-table" style="overflow-x:auto;{height_style}">'
        '<style>'
        '.perf-table table { width:100%; border-collapse:collapse; }'
        '.perf-table th, .perf-table td { white-space:nowrap; }'
        '.perf-table th { position:sticky; top:0; z-index:1; background-color:#F0F4F8 !important; }'
        '.perf-table tr:hover td:not([style*="background-color"]) { background:#F7FAFC !important; }'
        '</style>'
        f'{html}</div></div>',
        unsafe_allow_html=True,
    )


def build_overview_trend_rows(shopify_daily, gs_spend, amz_daily, period):
    """Build the 6-column, 3-row (Rollup/DTC/Amazon) trend table for Overview.

    Args:
        shopify_daily: DataFrame from _load_shopify_daily_metrics() with _date, _revenue,
                       _nc_orders, _ad_spend, _nc_revenue, _ret_revenue columns.
        gs_spend: DataFrame from _load_gs_spend() with _date, _ad_spend columns.
        amz_daily: DataFrame with _date, _amz_revenue, _amz_spend, _amz_new_cust,
                   _amz_new_rev, _amz_repeat_rev columns.
        period: 'dod' or 'wow'.

    Returns:
        DataFrame with columns [Period, Revenue, New Customers, CPA, MER, NC Rev %, Contribution %]
        and 3 rows per period: Rollup, DTC, Amazon.
    """
    from utils.date_helpers import business_yesterday
    from analytics.metrics import compute_mer, compute_nc_cpa

    yesterday = business_yesterday()

    # Merge Shopify with spend
    dtc = shopify_daily.copy()
    if not gs_spend.empty:
        dtc = dtc.merge(gs_spend[['_date', '_ad_spend']], on='_date', how='left', suffixes=('', '_gs'))
    if '_ad_spend' not in dtc.columns:
        dtc['_ad_spend'] = 0
    dtc['_ad_spend'] = dtc['_ad_spend'].fillna(0)
    dtc['_date'] = pd.to_datetime(dtc['_date'])

    rows = []

    if period == 'dod':
        # DoD: 3-day avg vs same 3 days prior week
        # Current 3 days: yesterday, day before, day before that
        end = yesterday
        start = pd.Timestamp(end) - pd.Timedelta(days=2)
        prev_end = pd.Timestamp(end) - pd.Timedelta(days=7)
        prev_start = pd.Timestamp(start) - pd.Timedelta(days=7)

        periods = [
            ('Current (3d)', start, pd.Timestamp(end)),
            ('Prior Week (3d)', prev_start, prev_end),
        ]
    else:
        # WoW: current week vs prior week (show 4 weeks)
        # Week ends on yesterday, 7-day windows
        periods = []
        for i in range(4):
            end_d = pd.Timestamp(yesterday) - pd.Timedelta(days=7 * i)
            start_d = end_d - pd.Timedelta(days=6)
            label = start_d.strftime('%m/%d') + '-' + end_d.strftime('%m/%d')
            periods.append((label, start_d, end_d))
        periods.reverse()

    for label, p_start, p_end in periods:
        # DTC metrics
        mask_dtc = (dtc['_date'] >= p_start) & (dtc['_date'] <= p_end)
        dtc_rev = dtc.loc[mask_dtc, '_revenue'].sum()
        dtc_nc = int(dtc.loc[mask_dtc, '_nc_orders'].sum())
        dtc_spend = dtc.loc[mask_dtc, '_ad_spend'].sum()
        dtc_nc_rev = dtc.loc[mask_dtc, '_nc_revenue'].sum()
        dtc_ret_rev = dtc.loc[mask_dtc, '_ret_revenue'].sum()

        # Amazon metrics
        amz_rev = 0
        amz_nc = 0
        amz_spend = 0
        amz_nc_rev = 0
        if not amz_daily.empty and '_date' in amz_daily.columns:
            amz_d = amz_daily.copy()
            amz_d['_date'] = pd.to_datetime(amz_d['_date'])
            mask_amz = (amz_d['_date'] >= p_start) & (amz_d['_date'] <= p_end)
            amz_rev = amz_d.loc[mask_amz, '_amz_revenue'].sum() if '_amz_revenue' in amz_d.columns else 0
            amz_spend = amz_d.loc[mask_amz, '_amz_spend'].sum() if '_amz_spend' in amz_d.columns else 0
            amz_nc = int(amz_d.loc[mask_amz, '_amz_new_cust'].sum()) if '_amz_new_cust' in amz_d.columns else 0
            amz_nc_rev = amz_d.loc[mask_amz, '_amz_new_rev'].sum() if '_amz_new_rev' in amz_d.columns else 0

        # Rollup
        total_rev = dtc_rev + amz_rev
        total_nc = dtc_nc + amz_nc
        total_spend = dtc_spend + amz_spend
        total_nc_rev = dtc_nc_rev + amz_nc_rev
        total_mer = compute_mer(total_rev, total_spend)
        total_cpa = compute_nc_cpa(total_spend, total_nc)
        nc_rev_pct = (total_nc_rev / total_rev * 100) if total_rev > 0 else 0
        contribution = ((total_rev - total_spend) / total_rev * 100) if total_rev > 0 else 0

        dtc_mer = compute_mer(dtc_rev, dtc_spend)
        dtc_cpa = compute_nc_cpa(dtc_spend, dtc_nc)
        dtc_nc_pct = (dtc_nc_rev / dtc_rev * 100) if dtc_rev > 0 else 0
        dtc_contribution = ((dtc_rev - dtc_spend) / dtc_rev * 100) if dtc_rev > 0 else 0

        amz_mer = compute_mer(amz_rev, amz_spend)
        amz_nc_pct = (amz_nc_rev / amz_rev * 100) if amz_rev > 0 else 0
        amz_contribution = ((amz_rev - amz_spend) / amz_rev * 100) if amz_rev > 0 else 0

        rows.append({
            'Period': label, 'Channel': 'Rollup',
            'Revenue': f'${total_rev:,.0f}', 'New Customers': total_nc,
            'CPA': f'${total_cpa:,.0f}', 'MER': f'{total_mer:.2f}x',
            'NC Rev %': f'{nc_rev_pct:.0f}%', 'Contribution %': f'{contribution:.0f}%',
        })
        rows.append({
            'Period': label, 'Channel': 'DTC',
            'Revenue': f'${dtc_rev:,.0f}', 'New Customers': dtc_nc,
            'CPA': f'${dtc_cpa:,.0f}', 'MER': f'{dtc_mer:.2f}x',
            'NC Rev %': f'{dtc_nc_pct:.0f}%', 'Contribution %': f'{dtc_contribution:.0f}%',
        })
        rows.append({
            'Period': label, 'Channel': 'Amazon',
            'Revenue': f'${amz_rev:,.0f}', 'New Customers': '\u2014',
            'CPA': '\u2014', 'MER': f'{amz_mer:.2f}x',
            'NC Rev %': f'{amz_nc_pct:.0f}%', 'Contribution %': f'{amz_contribution:.0f}%',
        })

    return pd.DataFrame(rows)
