"""Overview page — compacted layout with hero pacing, trend tables, and key charts."""
import logging
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime
from dateutil.relativedelta import relativedelta
from db import (
    get_db, read_sql, get_media_spend, get_cashflow_setting,
    get_last_sync_timestamp, get_new_rows_since_yesterday, get_synced_sources,
)
from analytics.sku_flavors import get_flavor
from analytics.metrics import compute_cac_payback, compute_nc_roas, compute_nc_cpa
from ui.components import render_freshness_badge, render_html_table
from ui.perf_tables import render_perf_table_colored, build_overview_trend_rows
from views.pacing import compute_pacing_data, render_hero_bars, render_pacing_detail_table
from views.marketing import _load_shopify_daily_metrics, _load_gs_spend

log = logging.getLogger(__name__)


@st.cache_data(ttl=300)
def _load_amazon_daily():
    """Load Amazon daily data for trend tables (revenue, spend, new/repeat)."""
    try:
        with get_db() as conn:
            rev = read_sql(
                "SELECT sale_date, SUM(revenue) as _amz_revenue "
                "FROM daily_sku_sales WHERE source = 'amazon' "
                "GROUP BY sale_date ORDER BY sale_date", conn,
            )
            spend = read_sql(
                "SELECT date as sale_date, spend as _amz_spend "
                "FROM amazon_daily_rollup ORDER BY date", conn,
            )
            cust = read_sql(
                "SELECT DATE(o.order_date) AS sale_date, "
                "COUNT(DISTINCT o.customer_id) AS total_customers, "
                "COUNT(DISTINCT CASE WHEN DATE(c.first_order_date) = DATE(o.order_date) "
                "THEN o.customer_id END) AS new_customers, "
                "SUM(oi.total_price) AS oi_total_rev, "
                "SUM(CASE WHEN DATE(c.first_order_date) = DATE(o.order_date) "
                "THEN oi.total_price ELSE 0 END) AS oi_new_rev "
                "FROM orders o "
                "JOIN customers c ON o.customer_id = c.customer_id "
                "JOIN order_items oi ON o.order_id = oi.order_id "
                "WHERE o.source = %s "
                "GROUP BY DATE(o.order_date)", conn, params=('amazon',),
            )
        if rev.empty:
            return pd.DataFrame()
        df = rev
        if not spend.empty:
            df = df.merge(spend, on='sale_date', how='left')
        if '_amz_spend' not in df.columns:
            df['_amz_spend'] = 0
        df['_amz_spend'] = df['_amz_spend'].fillna(0)

        if not cust.empty:
            cust['sale_date'] = cust['sale_date'].astype(str)
            df = df.merge(
                cust[['sale_date', 'total_customers', 'new_customers', 'oi_total_rev', 'oi_new_rev']],
                on='sale_date', how='left',
            )
        for col in ['total_customers', 'new_customers', 'oi_total_rev', 'oi_new_rev']:
            if col not in df.columns:
                df[col] = 0
            df[col] = df[col].fillna(0)

        df['_amz_new_cust'] = df['new_customers'].astype(int)
        df['_amz_repeat_cust'] = (df['total_customers'] - df['new_customers']).clip(lower=0).astype(int)
        _oi_total = df['oi_total_rev']
        _new_frac = (df['oi_new_rev'] / _oi_total).where(_oi_total > 0, 0)
        df['_amz_new_rev'] = df['_amz_revenue'] * _new_frac
        df['_amz_repeat_rev'] = df['_amz_revenue'] * (1 - _new_frac)
        df['_date'] = pd.to_datetime(df['sale_date'])
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def _load_overview_daily_trend():
    """Load 90-day daily revenue trend by source for the stacked area chart."""
    with get_db() as conn:
        return read_sql(
            "SELECT sale_date, source, SUM(units_sold) as units, SUM(revenue) as revenue "
            "FROM daily_sku_sales "
            "WHERE sale_date >= date('now', '-90 days') "
            "GROUP BY sale_date, source ORDER BY sale_date", conn,
        )


@st.cache_data(ttl=300)
def _load_top_skus():
    """Load top 10 SKUs by units sold."""
    with get_db() as conn:
        return read_sql("""
            SELECT oi.sku, sm.product_name, sm.category,
                   SUM(oi.quantity) as total_units,
                   SUM(oi.total_price) as total_revenue
            FROM order_items oi
            JOIN sku_master sm ON oi.sku = sm.sku
            JOIN orders o ON oi.order_id = o.order_id
            GROUP BY oi.sku, sm.product_name, sm.category
            ORDER BY total_units DESC
            LIMIT 10
        """, conn)


def render(ctx):
    """Render the Overview page."""
    # ================================================================
    # Header + freshness badge
    # ================================================================
    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title('Overview')
    with _badge_col:
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, ['shopify', 'amazon'])
            _new = get_new_rows_since_yesterday(conn, ['shopify', 'amazon'])
            _srcs = get_synced_sources(conn, ['shopify', 'amazon'])
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)

    # Empty state check
    with get_db() as conn:
        last_sync = conn.execute(
            'SELECT source, sync_date, records_fetched, status, error_message, created_at '
            'FROM sync_log ORDER BY created_at DESC LIMIT 1'
        ).fetchone()
        total_orders_check = conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]

    if total_orders_check == 0:
        st.warning('No sales data yet. Click **Refresh Data** in the sidebar, or connect a store in **Settings**.')
        if last_sync and last_sync['status'] != 'success':
            st.error(f"Last sync failed ({last_sync['source'].title()}): {last_sync['error_message'] or 'Unknown error'}")
        return

    # ================================================================
    # Section 1: Hero Pacing Bars + Alert Banners
    # ================================================================
    pacing_data = compute_pacing_data(ctx)

    if pacing_data and pacing_data['has_goals']:
        render_hero_bars(pacing_data)

    # Show OVERDUE/ORDER NOW alerts if any
    global_alerts = ctx.get('global_alerts', {})
    _urgent_alerts = [a for a in global_alerts.get('reorder', [])
                      if a.get('urgency') in ('OVERDUE', 'ORDER NOW')]
    if _urgent_alerts:
        for a in _urgent_alerts[:3]:
            flavor = a.get('flavor') or a.get('sku', '')
            days = a.get('days')
            if a['urgency'] == 'OVERDUE':
                st.error(f"**{flavor}** reorder is {abs(days)}d overdue", icon='\u26A0\uFE0F')
            else:
                st.warning(f"**{flavor}** — {days}d until reorder deadline", icon='\U0001F514')

    # ================================================================
    # Section 2: Hero Numbers
    # ================================================================
    if pacing_data:
        d = pacing_data
        _pct = d['pct_month']  # fraction of month elapsed (e.g. 0.10 for day 3/31)

        # Pro-rated goals: where we should be RIGHT NOW
        _rev_pace_goal = d['goal_total_rev'] * _pct if d['has_goals'] else 0
        _nc_pace_goal = d['goal_nc_count'] * _pct if d.get('goal_nc_count', 0) > 0 else 0

        h1, h2, h3 = st.columns(3)
        h1.metric('Total Rev MTD', f"${d['total_actual_rev']:,.0f}",
                   delta=f"Pace: ${_rev_pace_goal:,.0f}" if _rev_pace_goal > 0 else None,
                   delta_color='off')
        h2.metric('New Customers MTD', f"{d['cm_total_nc']:,}",
                   delta=f"Pace: {_nc_pace_goal:,.0f}" if _nc_pace_goal > 0 else None,
                   delta_color='off')

        # CAC Payback — contribution-margin model with COGS, fulfillment, retention
        biz = ctx.get('biz_vars', {})
        _cogs_pct = float(biz.get('cogs_pct', 25)) / 100
        try:
            with get_db() as _cf_conn:
                _fulfill_pct = float(get_cashflow_setting(_cf_conn, 'fulfillment_pct', '0.18'))
        except Exception:
            _fulfill_pct = 0.18

        _ret_curve = None
        try:
            _ret_curve = ctx['cached_retention_curve']('shopify')
        except Exception:
            pass

        _cac_payback = compute_cac_payback(
            d['cm_total_spend'], d['cm_total_nc'], d['cm_total_nc_rev'],
            cogs_pct=_cogs_pct, fulfillment_pct=_fulfill_pct,
            retention_curve=_ret_curve,
        )
        _goal_cac_payback = None
        if d.get('goal_nc_count', 0) > 0 and d.get('goal_total_spend', 0) > 0:
            _goal_cac_payback = compute_cac_payback(
                d['goal_total_spend'], d['goal_nc_count'], d['goal_nc_rev'],
                cogs_pct=_cogs_pct, fulfillment_pct=_fulfill_pct,
                retention_curve=_ret_curve,
            )
        h3.metric('CAC Payback',
                   f'{_cac_payback:.1f}mo' if _cac_payback > 0 else '\u2014',
                   delta=f"Target: {_goal_cac_payback:.1f}mo" if _goal_cac_payback and _goal_cac_payback > 0 else None,
                   delta_color='off',
                   help='Months until contribution margin (rev \u2212 COGS \u2212 fulfillment) covers CAC, including repeat revenue')

        # CAC Payback math breakdown
        if d['cm_total_nc'] > 0 and d['cm_total_spend'] > 0:
            _pb_cac = d['cm_total_spend'] / d['cm_total_nc']
            _pb_aov = d['cm_total_nc_rev'] / d['cm_total_nc']
            _pb_margin = 1 - _cogs_pct - _fulfill_pct
            with st.expander('CAC Payback Breakdown', expanded=False):
                # Show input variables with sources
                v1, v2, v3, v4 = st.columns(4)
                v1.metric('AOV (NC Rev / NC Count)', f'${_pb_aov:,.0f}')
                v2.metric('CPA (Spend / NC Count)', f'${_pb_cac:,.0f}')
                v3.metric('COGS % (Settings)', f'{_cogs_pct:.0%}')
                v4.metric('Fulfillment % (Cash Flow)', f'{_fulfill_pct:.0%}')

                st.caption(f'CPA to recover: **${_pb_cac:,.2f}** per customer | '
                           f'Margin rate: **{_pb_margin:.0%}** (1 \u2212 {_cogs_pct:.0%} COGS \u2212 {_fulfill_pct:.0%} Fulfill) | '
                           f'Retention: **DTC only** (Amazon has no customer data)')

                # Build month-by-month columns (transposed: months across top, calcs down)
                _max_show = 12
                _payback_found = False
                _month_data = {}  # keyed by column label
                _cumul = 0

                for _m in range(0, _max_show + 1):
                    if _m == 0:
                        _rev = _pb_aov
                        _ret_val = 1.0
                        _col = 'M0'
                    else:
                        _ret_val = _ret_curve.get(_m, 0) if _ret_curve else 0
                        _rev = _pb_aov * _ret_val
                        _col = f'M{_m}'

                    _cogs_amt = _rev * _cogs_pct
                    _ful_amt = _rev * _fulfill_pct
                    _contrib = _rev * _pb_margin
                    _cpa_hit = _pb_cac if _m == 0 else 0
                    _net = _contrib - _cpa_hit
                    _cumul += _net

                    _month_data[_col] = {
                        'DTC Retention': '\u2014' if _m == 0 else f'{_ret_val:.1%}',
                        'Revenue': f'${_rev:,.2f}',
                        f'COGS ({_cogs_pct:.0%})': f'(${_cogs_amt:,.2f})',
                        f'Fulfillment ({_fulfill_pct:.0%})': f'(${_ful_amt:,.2f})',
                        'CPA': f'(${_cpa_hit:,.2f})' if _m == 0 else '\u2014',
                        'Net': f'${_net:,.2f}' if _net >= 0 else f'(${abs(_net):,.2f})',
                        'Cumulative': f'${_cumul:,.2f}' if _cumul >= 0 else f'(${abs(_cumul):,.2f})',
                    }

                    if _cumul >= 0 and _m > 0 and not _payback_found:
                        _payback_found = True
                        if _m < _max_show:
                            _max_show = _m + 1

                # Build transposed DataFrame: rows = metric names, columns = M0..MN
                _cols_to_show = [f'M{i}' for i in range(_max_show + 1) if f'M{i}' in _month_data]
                _metric_names = list(next(iter(_month_data.values())).keys())
                _transposed = {' ': _metric_names}
                for _col in _cols_to_show:
                    _transposed[_col] = [_month_data[_col][k] for k in _metric_names]
                render_html_table(pd.DataFrame(_transposed))

    # ================================================================
    # Section 3: Pacing Detail (expander)
    # ================================================================
    if pacing_data and pacing_data['has_goals']:
        with st.expander('Pacing Detail', expanded=False):
            render_pacing_detail_table(pacing_data)

    # ================================================================
    # Section 4: Performance Trends (expander with DoD/WoW tabs)
    # ================================================================
    _shopify_daily = _load_shopify_daily_metrics()
    _gs_spend = _load_gs_spend()
    _amz_daily = _load_amazon_daily()

    with st.expander('Performance Trends', expanded=True):
        _dod_tab, _wow_tab = st.tabs(['Day over Day', 'Week over Week'])

        with _dod_tab:
            if not _shopify_daily.empty:
                dod_df = build_overview_trend_rows(_shopify_daily, _gs_spend, _amz_daily, 'dod')
                if not dod_df.empty:
                    # Pivot: one row per channel, columns = metrics
                    for channel in ['Rollup', 'DTC', 'Amazon']:
                        ch_df = dod_df[dod_df['Channel'] == channel].drop(columns=['Channel'])
                        if channel == 'Rollup':
                            st.caption(f'**{channel}**')
                        else:
                            st.caption(channel)
                        render_perf_table_colored(
                            ch_df, 'Period', max_height=200,
                            grey_cols={'NC Rev %', 'Contribution %'},
                        )
                else:
                    st.caption('Not enough data for DoD comparison.')
            else:
                st.caption('No Shopify data available.')

        with _wow_tab:
            if not _shopify_daily.empty:
                wow_df = build_overview_trend_rows(_shopify_daily, _gs_spend, _amz_daily, 'wow')
                if not wow_df.empty:
                    for channel in ['Rollup', 'DTC', 'Amazon']:
                        ch_df = wow_df[wow_df['Channel'] == channel].drop(columns=['Channel'])
                        if channel == 'Rollup':
                            st.caption(f'**{channel}**')
                        else:
                            st.caption(channel)
                        render_perf_table_colored(ch_df, 'Period', max_height=300)
                else:
                    st.caption('Not enough data for WoW comparison.')
            else:
                st.caption('No Shopify data available.')

    # ================================================================
    # Section 5: Marketing Performance Card
    # ================================================================
    if pacing_data:
        d = pacing_data
        _l7d_nc_roas = compute_nc_roas(d['l7d_total_nc_rev'] * 7, d['l7d_total_spend'] * 7)
        _l7d_nc_count = d['l7d_total_nc'] * 7

        st.markdown(
            '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;'
            'padding:16px 20px;margin:8px 0 16px;">'
            '<div style="font-weight:700;color:#0F3557;font-size:0.9rem;margin-bottom:6px;">'
            'Marketing Signal (L7D)</div>'
            f'<div style="display:flex;gap:32px;font-size:0.84rem;color:#334155;">'
            f'<span>NC ROAS: <b>{_l7d_nc_roas:.2f}x</b></span>'
            f'<span>New Customers: <b>{_l7d_nc_count:.0f}</b></span>'
            f'<span>MER: <b>{d["l7d_mer"]:.2f}x</b></span>'
            f'<span>CPA: <b>${d["l7d_total_nc_cpa"]:,.0f}</b></span>'
            '</div>'
            '<div style="font-size:0.72rem;color:#94a3b8;margin-top:4px;">'
            'See Marketing page for full breakdown &rarr;</div>'
            '</div>',
            unsafe_allow_html=True,
        )

    # ================================================================
    # Section 6: Revenue Trend Chart (90-day stacked area, full width)
    # ================================================================
    st.subheader('Revenue Trend')
    st.caption('90-day moving average of daily revenue (Shopify + Amazon).')
    daily = _load_overview_daily_trend()
    if not daily.empty:
        daily['sale_date'] = pd.to_datetime(daily['sale_date'])
        daily_pivot = daily.pivot_table(
            index='sale_date', columns='source', values='revenue', aggfunc='sum'
        ).fillna(0).reset_index()

        fig_rev = go.Figure()
        _source_colors = {'shopify': '#0F3557', 'amazon': '#F58B3D'}
        for src in ['shopify', 'amazon']:
            if src in daily_pivot.columns:
                ma_col = daily_pivot[src].rolling(7, min_periods=1).mean()
                fig_rev.add_trace(go.Scatter(
                    x=daily_pivot['sale_date'], y=ma_col,
                    mode='lines', name=f'{src.title()} (7d avg)',
                    line=dict(color=_source_colors.get(src, '#888'), width=2),
                    stackgroup='one',
                ))

        daily_total = daily.groupby('sale_date')['revenue'].sum().reset_index()
        daily_total['revenue_7d'] = daily_total['revenue'].rolling(7, min_periods=1).mean()
        fig_rev.add_trace(go.Scatter(
            x=daily_total['sale_date'], y=daily_total['revenue_7d'],
            mode='lines', name='Total (7d avg)',
            line=dict(color='#111827', width=2, dash='dot'),
        ))

        fig_rev.update_layout(
            yaxis_title='Revenue',
            height=380,
            margin=dict(l=0, r=0, t=10, b=0),
            legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='left', x=0),
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            yaxis=dict(gridcolor='#E8EDF3'),
            xaxis=dict(gridcolor='#E8EDF3'),
        )
        st.plotly_chart(fig_rev, use_container_width=True)

    # ================================================================
    # Section 7: Top SKUs bar chart
    # ================================================================
    st.subheader('Top SKUs')
    st.caption('Best-selling SKUs by units sold.')
    top = _load_top_skus()
    if not top.empty:
        top = top.copy()
        top['label'] = top.apply(
            lambda r: get_flavor(r['sku'], r.get('product_name', '')) or r['sku'],
            axis=1,
        )
        fig = go.Figure(go.Bar(
            x=top['total_units'], y=top['label'], orientation='h',
            text=top['total_units'], textposition='outside',
            marker_color='#0F3557',
        ))
        fig.update_layout(
            height=min(360, len(top) * 36 + 40),
            margin=dict(l=0, r=0, t=10, b=0),
            yaxis=dict(categoryorder='total ascending', title=''),
            xaxis=dict(title='Units Sold', gridcolor='#E8EDF3'),
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        )
        fig.update_traces(textfont_size=11)
        st.plotly_chart(fig, use_container_width=True)

    # ================================================================
    # Section 8: Inventory footer (single HTML table)
    # ================================================================
    st.subheader('Inventory & Demand')

    _3pl_total = 0
    _fba_total = 0
    _3pl_ok = False
    _fba_ok = False

    try:
        _3pl = ctx['cached_3pl_inventory']()
        if _3pl:
            _3pl_total = sum(i.get('quantity_available', 0) or 0 for i in _3pl)
            _3pl_ok = True
    except Exception:
        pass

    try:
        _fba = ctx['cached_amazon_inventory']()
        if _fba:
            _fba_total = sum(i.get('total_quantity', 0) or 0 for i in _fba)
            _fba_ok = True
    except Exception:
        pass

    # Demand forecast rollup
    _demand_3mo = 0
    _demand_12mo = 0
    FORECAST_SKUS = ctx['forecast_skus']
    _cached_waterfall = ctx['cached_waterfall']
    _load_seasonal_json = ctx['load_seasonal_json']
    try:
        import json as _json_ov
        with get_db() as conn:
            _ov_spend = get_media_spend(conn, source='All Sources')
        if not _ov_spend:
            _ov_spend = [{'month': (datetime.utcnow() + relativedelta(months=i)).strftime('%Y-%m'),
                          'spend': 0, 'new_customer_roas': 0.7} for i in range(12)]
        _ov_wf = _cached_waterfall(_json_ov.dumps(_ov_spend, sort_keys=True), None, 12, _load_seasonal_json())
        if not _ov_wf.empty:
            _demand_3mo = _ov_wf.head(3)['total_units'].sum()
            _demand_12mo = _ov_wf['total_units'].sum()
    except Exception:
        pass

    _total_inv = _3pl_total + _fba_total
    inv_rows = [{
        '3PL Stock': f'{_3pl_total:,.0f}' if _3pl_ok else '\u2014',
        'FBA Stock': f'{_fba_total:,.0f}' if _fba_ok else '\u2014',
        'Total Inventory': f'{_total_inv:,.0f}' if (_3pl_ok or _fba_ok) else '\u2014',
        'Demand (3mo)': f'{_demand_3mo:,.0f}',
        'Demand (12mo)': f'{_demand_12mo:,.0f}',
    }]
    render_html_table(pd.DataFrame(inv_rows))

    if (_3pl_ok or _fba_ok) and _demand_3mo > 0:
        _monthly_demand = _demand_3mo / 3
        _months_supply = _total_inv / _monthly_demand if _monthly_demand > 0 else 0
        _supply_icon = '\U0001f7e2' if _months_supply >= 3 else ('\U0001f7e1' if _months_supply >= 1.5 else '\U0001f534')
        st.caption(f'{_supply_icon} {_months_supply:.1f} months of supply across all channels')
