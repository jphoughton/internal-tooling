"""Overview page."""
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
from dateutil.relativedelta import relativedelta
from db import (
    get_db, read_sql, get_media_spend,
    get_last_sync_timestamp, get_new_rows_since_yesterday, get_synced_sources,
)
from analytics.sku_flavors import get_flavor
from analytics.retention import get_new_repeat_daily_revenue, get_projected_new_repeat_summary
from ui.components import render_freshness_badge, smart_date_filter
from views.pacing import render_pacing
import json as _json_ov_precompute


def _get_precomputed(key):
    """Load precomputed result from DB, return parsed JSON or None."""
    try:
        from db import get_precomputed
        with get_db() as conn:
            cached = get_precomputed(conn, key, max_age_hours=25)
        if cached:
            return _json_ov_precompute.loads(cached)
    except Exception:
        pass
    return None


@st.cache_data(ttl=300)
def _load_overview_stats(date_start=None, date_end=None):
    date_clause = ''
    date_clause_orders = ''
    params_d = []
    if date_start and date_end:
        date_clause = 'AND sale_date BETWEEN ? AND ?'
        date_clause_orders = 'AND order_date BETWEEN ? AND ?'
        params_d = [str(date_start), str(date_end)]

    with get_db() as conn:
        total_orders = conn.execute(
            f'SELECT COUNT(*) FROM orders WHERE 1=1 {date_clause_orders}', params_d
        ).fetchone()[0]
        total_customers = conn.execute(
            f'SELECT COUNT(DISTINCT customer_id) FROM orders WHERE 1=1 {date_clause_orders}', params_d
        ).fetchone()[0]

        # Revenue by source — use daily_sku_sales (has both Shopify & Amazon)
        # The orders table only has Shopify; Amazon sales go directly to daily_sku_sales
        rev_by_source = {}
        for r in conn.execute(
            f'SELECT source, SUM(revenue) as rev FROM daily_sku_sales WHERE 1=1 {date_clause} GROUP BY source',
            params_d,
        ).fetchall():
            rev_by_source[r['source']] = r['rev'] or 0
        total_revenue = sum(rev_by_source.values())

        total_skus = conn.execute('SELECT COUNT(*) FROM sku_master WHERE is_active = 1').fetchone()[0]

        # Source split — use daily_sku_sales for consistent revenue across channels
        source_split = read_sql(
            f'SELECT source, SUM(order_count) as orders, SUM(revenue) as revenue FROM daily_sku_sales WHERE 1=1 {date_clause} GROUP BY source',
            conn, params=params_d,
        )

        # Top SKUs
        top_skus = read_sql(f"""
            SELECT oi.sku, sm.product_name, sm.category,
                   SUM(oi.quantity) as total_units,
                   SUM(oi.total_price) as total_revenue
            FROM order_items oi
            JOIN sku_master sm ON oi.sku = sm.sku
            JOIN orders o ON oi.order_id = o.order_id
            WHERE 1=1 {date_clause_orders.replace('order_date', 'o.order_date')}
            GROUP BY oi.sku, sm.product_name, sm.category
            ORDER BY total_units DESC
            LIMIT 10
        """, conn, params=params_d)

        # Daily trend by source (for stacked revenue chart)
        daily_trend = read_sql(f"""
            SELECT sale_date, source,
                   SUM(units_sold) as units, SUM(revenue) as revenue
            FROM daily_sku_sales
            WHERE 1=1 {date_clause}
            GROUP BY sale_date, source
            ORDER BY sale_date
        """, conn, params=params_d)

    return {
        'total_orders': total_orders,
        'total_customers': total_customers,
        'total_revenue': total_revenue,
        'total_skus': total_skus,
        'shopify_revenue': rev_by_source.get('shopify', 0),
        'amazon_revenue': rev_by_source.get('amazon', 0),
        'source_split': source_split,
        'top_skus': top_skus,
        'daily_trend': daily_trend,
    }


def render(ctx):
    """Render the Overview page."""
    FORECAST_SKUS = ctx['forecast_skus']
    _cached_waterfall = ctx['cached_waterfall']
    _load_seasonal_json = ctx['load_seasonal_json']

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

    # Show last sync status (compact)
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
    else:
        with get_db() as conn:
            date_range = conn.execute('SELECT MIN(order_date), MAX(order_date) FROM orders').fetchone()
        if date_range and date_range[0] and date_range[1]:
            earliest = datetime.strptime(date_range[0], '%Y-%m-%d')
            latest = datetime.strptime(date_range[1], '%Y-%m-%d')
            data_span_days = (latest - earliest).days
            if data_span_days < 75:
                st.warning(
                    f'Limited history ({data_span_days} days). Request `read_all_orders` scope '
                    f'in your [Shopify Partner Dashboard](https://partners.shopify.com) for full access.'
                )

    # --- Pacing Dashboard (top of page) ---
    render_pacing(ctx)
    st.divider()

    # --- Date Filter ---
    with get_db() as conn:
        _ov_dr = conn.execute('SELECT MIN(order_date), MAX(order_date) FROM orders').fetchone()
        _ov_dr2 = conn.execute('SELECT MIN(sale_date), MAX(sale_date) FROM daily_sku_sales').fetchone()
    # Use whichever source has the widest date range
    _ov_dates = [d for d in [_ov_dr[0] if _ov_dr else None, _ov_dr2[0] if _ov_dr2 else None] if d]
    _ov_dates_max = [d for d in [_ov_dr[1] if _ov_dr else None, _ov_dr2[1] if _ov_dr2 else None] if d]
    _ov_earliest = datetime.strptime(min(_ov_dates), '%Y-%m-%d').date() if _ov_dates else datetime(2020, 1, 1).date()
    _ov_latest = datetime.strptime(max(_ov_dates_max), '%Y-%m-%d').date() if _ov_dates_max else datetime.utcnow().date()

    ov_start, ov_end = smart_date_filter(_ov_earliest, _ov_latest, 'ov')

    stats = _load_overview_stats(date_start=str(ov_start), date_end=str(ov_end))

    # -- KPI row --
    _k1, _k2, _k3, _k4, _k5 = st.columns(5)
    _k5.metric('Total Revenue', f"${stats['total_revenue']:,.0f}")
    _k3.metric('Shopify', f"${stats['shopify_revenue']:,.0f}")
    _k4.metric('Amazon', f"${stats['amazon_revenue']:,.0f}")
    _k1.metric('Orders', f"{stats['total_orders']:,}")
    _k2.metric('Customers', f"{stats['total_customers']:,}")

    st.markdown('')  # spacing

    # -- New vs Repeat Customer Breakdown (with projections) --
    nr_dtc = get_projected_new_repeat_summary(str(ov_start), str(ov_end), 'shopify')
    nr_amz = get_projected_new_repeat_summary(str(ov_start), str(ov_end), 'amazon')

    # Build combined rollup from both channels (source_filter=None defaults to
    # Shopify-only internally, so we sum the per-channel results instead).
    nr_all = {}
    for _k in ['new_customers', 'repeat_customers', 'new_revenue', 'repeat_revenue',
               'new_orders', 'repeat_orders',
               'projected_new_customers', 'projected_new_revenue',
               'projected_repeat_customers', 'projected_repeat_revenue',
               'total_new_customers', 'total_new_revenue',
               'total_repeat_customers', 'total_repeat_revenue']:
        nr_all[_k] = nr_dtc.get(_k, 0) + nr_amz.get(_k, 0)
    # Recalculate AOVs for the combined totals
    nr_all['new_aov'] = round(nr_all['new_revenue'] / nr_all['new_orders'], 2) if nr_all.get('new_orders', 0) > 0 else 0
    nr_all['repeat_aov'] = round(nr_all['repeat_revenue'] / nr_all['repeat_orders'], 2) if nr_all.get('repeat_orders', 0) > 0 else 0
    nr_all['gap_days'] = max(nr_dtc.get('gap_days', 0), nr_amz.get('gap_days', 0))
    nr_all['last_data_date'] = nr_dtc.get('last_data_date') or nr_amz.get('last_data_date')
    nr_all['projection_method'] = nr_dtc.get('projection_method', 'none')

    # Show data-freshness warning if projecting
    _any_gap = max(nr_all.get('gap_days', 0), nr_dtc.get('gap_days', 0), nr_amz.get('gap_days', 0))
    if _any_gap > 0:
        _gap_parts = []
        if nr_amz.get('gap_days', 0) > 0:
            _gap_parts.append(f"Amazon data through {nr_amz['last_data_date']} ({nr_amz['gap_days']}d gap)")
        if nr_dtc.get('gap_days', 0) > 0:
            _gap_parts.append(f"Shopify data through {nr_dtc['last_data_date']} ({nr_dtc['gap_days']}d gap)")
        if _gap_parts:
            st.caption(f"*Projected values include DOW-adjusted estimates for missing days. {'; '.join(_gap_parts)}.*")

    def _fmt_proj(actual, projected, prefix='', suffix=''):
        """Format a metric value, appending ' (est)' when projection is included."""
        total = actual + projected
        if projected > 0:
            return f"{prefix}{total:,.0f}{suffix} (est)"
        return f"{prefix}{total:,.0f}{suffix}"

    # Roll-up KPIs
    _nr1, _nr2, _nr3, _nr4 = st.columns(4)
    _nr1.metric('New Customers', _fmt_proj(nr_all['new_customers'], nr_all.get('projected_new_customers', 0)),
                delta=f"+{nr_all['projected_new_customers']} projected" if nr_all.get('projected_new_customers', 0) > 0 else None)
    _nr2.metric('Repeat Customers', _fmt_proj(nr_all['repeat_customers'], nr_all.get('projected_repeat_customers', 0)),
                delta=f"+{nr_all['projected_repeat_customers']} projected" if nr_all.get('projected_repeat_customers', 0) > 0 else None)
    _nr3.metric('New Customer Revenue', _fmt_proj(nr_all['new_revenue'], nr_all.get('projected_new_revenue', 0), prefix='$'))
    _nr4.metric('Repeat Customer Revenue', _fmt_proj(nr_all['repeat_revenue'], nr_all.get('projected_repeat_revenue', 0), prefix='$'))

    # Per-channel breakdown
    _ch_dtc, _ch_amz = st.columns(2)
    with _ch_dtc:
        st.caption('**DTC (Shopify)**')
        _d1, _d2, _d3 = st.columns(3)
        _d1.metric('New', _fmt_proj(nr_dtc['new_customers'], nr_dtc.get('projected_new_customers', 0)),
                   f"${nr_dtc['total_new_revenue']:,.0f} rev" if nr_dtc.get('projected_new_revenue', 0) > 0 else f"${nr_dtc['new_revenue']:,.0f} rev")
        _d2.metric('Repeat', _fmt_proj(nr_dtc['repeat_customers'], nr_dtc.get('projected_repeat_customers', 0)),
                   f"${nr_dtc['total_repeat_revenue']:,.0f} rev" if nr_dtc.get('projected_repeat_revenue', 0) > 0 else f"${nr_dtc['repeat_revenue']:,.0f} rev")
        _dtc_total_new_rev = nr_dtc.get('total_new_revenue', nr_dtc['new_revenue'])
        _dtc_total_rep_rev = nr_dtc.get('total_repeat_revenue', nr_dtc['repeat_revenue'])
        _dtc_nc_pct = _dtc_total_new_rev / (_dtc_total_new_rev + _dtc_total_rep_rev) * 100 if (_dtc_total_new_rev + _dtc_total_rep_rev) > 0 else 0
        _d3.metric('NC % of Rev', f"{_dtc_nc_pct:.0f}%", f"AOV ${nr_dtc['new_aov']:,.0f} / ${nr_dtc['repeat_aov']:,.0f}")
    with _ch_amz:
        st.caption('**Amazon**')
        _a1, _a2, _a3 = st.columns(3)
        _a1.metric('New', _fmt_proj(nr_amz['new_customers'], nr_amz.get('projected_new_customers', 0)),
                   f"${nr_amz['total_new_revenue']:,.0f} rev" if nr_amz.get('projected_new_revenue', 0) > 0 else f"${nr_amz['new_revenue']:,.0f} rev")
        _a2.metric('Repeat', _fmt_proj(nr_amz['repeat_customers'], nr_amz.get('projected_repeat_customers', 0)),
                   f"${nr_amz['total_repeat_revenue']:,.0f} rev" if nr_amz.get('projected_repeat_revenue', 0) > 0 else f"${nr_amz['repeat_revenue']:,.0f} rev")
        _amz_total_new_rev = nr_amz.get('total_new_revenue', nr_amz['new_revenue'])
        _amz_total_rep_rev = nr_amz.get('total_repeat_revenue', nr_amz['repeat_revenue'])
        _amz_nc_pct = _amz_total_new_rev / (_amz_total_new_rev + _amz_total_rep_rev) * 100 if (_amz_total_new_rev + _amz_total_rep_rev) > 0 else 0
        _a3.metric('NC % of Rev', f"{_amz_nc_pct:.0f}%", f"AOV ${nr_amz['new_aov']:,.0f} / ${nr_amz['repeat_aov']:,.0f}")

    st.markdown('')  # spacing

    # -- Revenue chart + source split --
    col_left, col_right = st.columns([3, 1])

    with col_left:
        st.subheader('Revenue Trend')
        st.caption('7-day moving average of daily revenue from Shopify + Amazon order data.')
        daily = stats['daily_trend']
        if not daily.empty:
            daily['sale_date'] = pd.to_datetime(daily['sale_date'])

            # Pivot to get Shopify and Amazon as separate columns
            daily_pivot = daily.pivot_table(
                index='sale_date', columns='source', values='revenue', aggfunc='sum'
            ).fillna(0).reset_index()

            fig_rev = go.Figure()
            _source_colors = {'shopify': '#0F3557', 'amazon': '#F58B3D'}
            for src in ['shopify', 'amazon']:
                if src in daily_pivot.columns:
                    # Add 7-day moving average for smoother visualization
                    ma_col = daily_pivot[src].rolling(7, min_periods=1).mean()
                    fig_rev.add_trace(go.Scatter(
                        x=daily_pivot['sale_date'], y=ma_col,
                        mode='lines', name=f'{src.title()} (7d avg)',
                        line=dict(color=_source_colors.get(src, '#888'), width=2),
                        stackgroup='one',
                    ))

            # Total combined line
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

    with col_right:
        st.subheader('Channel Mix')
        st.caption('Revenue share by channel from daily_sku_sales.')
        source = stats['source_split']
        if not source.empty:
            fig = px.pie(source, values='revenue', names='source',
                         color_discrete_sequence=['#0F3557', '#F58B3D'],
                         hole=0.55)
            fig.update_layout(height=380, margin=dict(l=10, r=10, t=10, b=10),
                              plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
                              showlegend=True, legend=dict(orientation='h', yanchor='top', y=-0.05))
            fig.update_traces(textposition='inside', textinfo='percent+label',
                              textfont_size=11)
            st.plotly_chart(fig, use_container_width=True)

    # -- New vs Repeat Revenue Trend --
    nr_col_left, nr_col_right = st.columns([3, 1])

    with nr_col_left:
        st.subheader('New vs Repeat Revenue')
        st.caption('New vs returning customer revenue (7-day avg) from Shopify orders.')
        # Try precomputed full dataset, filter client-side
        _nr_daily_cached = _get_precomputed('new_repeat_daily_revenue')
        if _nr_daily_cached is not None:
            nr_daily = pd.DataFrame(_nr_daily_cached)
            if not nr_daily.empty and 'order_date' in nr_daily.columns:
                nr_daily = nr_daily[
                    (nr_daily['order_date'] >= str(ov_start)) &
                    (nr_daily['order_date'] <= str(ov_end))
                ].reset_index(drop=True)
        else:
            nr_daily = get_new_repeat_daily_revenue(str(ov_start), str(ov_end))
        if not nr_daily.empty:
            nr_daily['order_date'] = pd.to_datetime(nr_daily['order_date'])
            nr_daily['new_7d'] = nr_daily['new_revenue'].rolling(7, min_periods=1).mean()
            nr_daily['repeat_7d'] = nr_daily['repeat_revenue'].rolling(7, min_periods=1).mean()

            fig_nr = go.Figure()
            fig_nr.add_trace(go.Scatter(
                x=nr_daily['order_date'], y=nr_daily['repeat_7d'],
                mode='lines', name='Repeat (7d avg)',
                line=dict(color='#0F3557', width=2),
                stackgroup='one',
            ))
            fig_nr.add_trace(go.Scatter(
                x=nr_daily['order_date'], y=nr_daily['new_7d'],
                mode='lines', name='New (7d avg)',
                line=dict(color='#3B82F6', width=2),
                stackgroup='one',
            ))
            fig_nr.update_layout(
                yaxis_title='Revenue',
                height=380,
                margin=dict(l=0, r=0, t=10, b=0),
                legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='left', x=0),
                plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
                yaxis=dict(gridcolor='#E8EDF3'),
                xaxis=dict(gridcolor='#E8EDF3'),
            )
            st.plotly_chart(fig_nr, use_container_width=True)

    with nr_col_right:
        st.subheader('New vs Repeat')
        st.caption('Revenue share between new and repeat customers.')
        _nr_total = nr_all['new_revenue'] + nr_all['repeat_revenue']
        if _nr_total > 0:
            nr_pie_data = pd.DataFrame({
                'Segment': ['New', 'Repeat'],
                'Revenue': [nr_all['new_revenue'], nr_all['repeat_revenue']],
            })
            fig_nr_pie = px.pie(nr_pie_data, values='Revenue', names='Segment',
                                color_discrete_sequence=['#3B82F6', '#0F3557'],
                                hole=0.55)
            fig_nr_pie.update_layout(
                height=380, margin=dict(l=10, r=10, t=10, b=10),
                plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
                showlegend=True,
                legend=dict(orientation='h', yanchor='top', y=-0.05),
            )
            fig_nr_pie.update_traces(textposition='inside', textinfo='percent+label',
                                     textfont_size=11)
            st.plotly_chart(fig_nr_pie, use_container_width=True)

    # -- Top SKUs --
    st.subheader('Top SKUs')
    st.caption('Best-selling SKUs by units sold from Shopify order items.')
    top = stats['top_skus']
    if not top.empty:
        top = top.copy()
        top['label'] = top.apply(
            lambda r: get_flavor(r['sku'], r.get('product_name', '')) or r['sku'],
            axis=1,
        )
        fig = px.bar(top, x='total_units', y='label', orientation='h',
                     text='total_units',
                     color_discrete_sequence=['#0F3557'])
        fig.update_layout(
            height=min(360, len(top) * 36 + 40),
            margin=dict(l=0, r=0, t=10, b=0),
            yaxis=dict(categoryorder='total ascending', title=''),
            xaxis=dict(title='Units Sold', gridcolor='#E8EDF3'),
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        )
        fig.update_traces(textposition='outside', textfont_size=11)
        st.plotly_chart(fig, use_container_width=True)

    # -- Inventory Snapshot --
    st.markdown('')
    st.subheader('Inventory & Demand')

    # Gather inventory from all sources
    _ov_3pl_total = 0
    _ov_fba_total = 0
    _ov_3pl_ok = False
    _ov_fba_ok = False

    try:
        _ov_3pl = ctx['cached_3pl_inventory']()
        if _ov_3pl:
            _ov_3pl_total = sum(i.get('quantity_available', 0) or 0 for i in _ov_3pl)
            _ov_3pl_ok = True
    except Exception:
        pass

    try:
        _ov_fba = ctx['cached_amazon_inventory']()
        if _ov_fba:
            _ov_fba_total = sum(i.get('total_quantity', 0) or 0 for i in _ov_fba)
            _ov_fba_ok = True
    except Exception:
        pass

    # Demand forecast rollup
    _ov_demand_3mo = 0
    _ov_demand_12mo = 0
    try:
        import json as _json_ov
        with get_db() as conn:
            _ov_spend = get_media_spend(conn, source='All Sources')
        if not _ov_spend:
            _ov_spend = [{'month': (datetime.utcnow() + relativedelta(months=i)).strftime('%Y-%m'),
                          'spend': 0, 'new_customer_roas': 0.7} for i in range(12)]
        _ov_wf = _cached_waterfall(_json_ov.dumps(_ov_spend, sort_keys=True), None, 12, _load_seasonal_json())
        if not _ov_wf.empty:
            _ov_demand_3mo = _ov_wf.head(3)['total_units'].sum()
            _ov_demand_12mo = _ov_wf['total_units'].sum()
    except Exception:
        pass

    _iv1, _iv2, _iv3, _iv4, _iv5 = st.columns(5)
    _iv1.metric('3PL Stock', f'{_ov_3pl_total:,.0f}' if _ov_3pl_ok else '\u2014')
    _iv2.metric('FBA Stock', f'{_ov_fba_total:,.0f}' if _ov_fba_ok else '\u2014')
    _total_inv = _ov_3pl_total + _ov_fba_total
    _iv3.metric('Total Inventory', f'{_total_inv:,.0f}' if (_ov_3pl_ok or _ov_fba_ok) else '\u2014')
    _iv4.metric('Demand (3mo)', f'{_ov_demand_3mo:,.0f}')
    _iv5.metric('Demand (12mo)', f'{_ov_demand_12mo:,.0f}')

    if (_ov_3pl_ok or _ov_fba_ok) and _ov_demand_3mo > 0:
        _monthly_demand = _ov_demand_3mo / 3
        _months_supply = _total_inv / _monthly_demand if _monthly_demand > 0 else 0
        _supply_color = '\U0001f7e2' if _months_supply >= 3 else ('\U0001f7e1' if _months_supply >= 1.5 else '\U0001f534')
        st.caption(f'{_supply_color} {_months_supply:.1f} months of supply across all channels')
