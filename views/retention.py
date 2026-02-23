"""Retention page — Triple Whale-style cohort analysis table."""
import json as _json
import calendar
import streamlit as st
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
from db import (
    get_db,
    get_last_sync_timestamp, get_new_rows_since_yesterday, get_synced_sources,
    get_seasonal_indices, get_setting, set_setting, upsert_seasonal_index,
    get_media_spend,
)
from analytics.retention import (
    get_customer_cohort_data,
    get_cohort_sizes,
    build_cohort_matrices,
    get_cohort_summary,
)
from analytics.waterfall import clear_waterfall_cache, get_aov_and_units
from analytics.dtc_demand import build_repeat_customer_sku_month_table
from ui.components import render_freshness_badge, smart_date_filter, render_html_table
from ui.tables import make_cohort_cell_style


@st.cache_data(ttl=300)
def _load_sku_list():
    with get_db() as conn:
        rows = conn.execute(
            'SELECT sku, product_name, category, sources FROM sku_master WHERE is_active = 1 ORDER BY category, sku'
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def _build_cohort_table(matrices, summary, metric, cumulative, start_str, end_str):
    """Build a formatted display DataFrame for the cohort analysis table.

    Returns (display_df, month_col_names) or (None, []) if no data.
    """
    revenue = matrices['revenue']
    customers = matrices['customers']
    cohort_sizes = matrices['cohort_sizes']

    if revenue.empty:
        return None, []

    # Filter to date range
    all_cohorts = sorted(revenue.index)
    filtered = [c for c in all_cohorts if start_str <= c <= end_str]
    if not filtered:
        return None, []

    revenue = revenue.loc[filtered]
    customers = customers.loc[filtered]
    sizes = cohort_sizes.reindex(filtered)

    # Compute metric matrix
    if metric == 'Total Sales':
        if cumulative:
            data = revenue.cumsum(axis=1)
        else:
            data = revenue.copy()
    elif metric == 'Retention Rate':
        retention = customers.div(sizes, axis=0)
        if cumulative:
            data = retention.cumsum(axis=1)
        else:
            data = retention.copy()
    else:  # LTV
        if cumulative:
            data = revenue.cumsum(axis=1).div(sizes, axis=0)
        else:
            data = revenue.div(sizes, axis=0)

    # Build summary columns
    summary_filtered = summary[summary['cohort'].isin(filtered)].set_index('cohort')
    display_rows = []
    for cohort in filtered:
        row = {'Cohort': cohort}
        if cohort in summary_filtered.index:
            s = summary_filtered.loc[cohort]
            row['Customers'] = f'{int(s["customers"]):,}' if pd.notna(s['customers']) else ''
            row['NCPA'] = f'${s["ncpa"]:.2f}' if pd.notna(s['ncpa']) else ''
            row['AOV'] = f'${s["aov"]:.2f}' if pd.notna(s['aov']) else ''
            row['1st Order %'] = f'{s["first_order_pct"]:.2f}%' if pd.notna(s['first_order_pct']) else ''
        else:
            row['Customers'] = ''
            row['NCPA'] = ''
            row['AOV'] = ''
            row['1st Order %'] = ''

        # Month columns
        if cohort in data.index:
            for col in data.columns:
                val = data.loc[cohort, col]
                col_name = f'M{int(col)}'
                if pd.isna(val):
                    row[col_name] = ''
                elif metric == 'Total Sales':
                    row[col_name] = f'${val:,.0f}'
                elif metric == 'Retention Rate':
                    row[col_name] = f'{val * 100:.2f}%'
                else:  # LTV
                    row[col_name] = f'${val:,.2f}'
        display_rows.append(row)

    display_df = pd.DataFrame(display_rows)
    month_cols = [c for c in display_df.columns if c.startswith('M') and c[1:].isdigit()]
    return display_df, month_cols


def render(ctx):
    """Render the Retention page."""
    active_sources = ctx['active_sources']

    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title('Retention')
    with _badge_col:
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, ['shopify'])
            _new = get_new_rows_since_yesterday(conn, ['shopify'])
            _srcs = get_synced_sources(conn, ['shopify'])
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)

    # --- Filters ---
    col1, col2 = st.columns(2)
    skus = _load_sku_list()
    sku_options = skus['sku'].tolist() if not skus.empty else []
    with col1:
        sku_filter = st.selectbox('Filter by SKU', ['All SKUs'] + sku_options)
    with col2:
        source_options = ['All Sources'] + active_sources
        source_filter = st.selectbox('Filter by Source', source_options)

    sku_val = None if sku_filter == 'All SKUs' else sku_filter
    source_val = None if source_filter == 'All Sources' else source_filter

    # --- Cohort Analysis Table ---
    matrices = build_cohort_matrices(sku_filter=sku_val, source_filter=source_val)
    summary = get_cohort_summary(sku_filter=sku_val, source_filter=source_val)

    if matrices['revenue'].empty:
        st.warning('No retention data available with current filters.')
    else:
        # Date range filter
        all_cohorts = sorted(matrices['revenue'].index.tolist())
        earliest = datetime.strptime(all_cohorts[0], '%Y-%m').date()
        _latest_first = datetime.strptime(all_cohorts[-1], '%Y-%m').date()
        latest = _latest_first.replace(day=calendar.monthrange(_latest_first.year, _latest_first.month)[1])

        start_date, end_date = smart_date_filter(earliest, latest, 'ret', default_preset='All Time')
        start_str = start_date.strftime('%Y-%m')
        end_str = end_date.strftime('%Y-%m')

        # Metric selector + cumulative toggle
        _ctrl_col, _toggle_col = st.columns([3, 1])
        with _ctrl_col:
            metric = st.segmented_control(
                'Metric',
                options=['Total Sales', 'Retention Rate', 'LTV'],
                default='Total Sales',
                key='cohort_metric',
            )
        with _toggle_col:
            cumulative = st.toggle('Cumulative', value=True, key='cohort_cumulative')

        if not metric:
            metric = 'Total Sales'

        display_df, month_cols = _build_cohort_table(
            matrices, summary, metric, cumulative, start_str, end_str,
        )

        if display_df is None or display_df.empty:
            st.warning('No cohorts in the selected date range.')
        else:
            st.subheader('Cohort Analysis')
            st.caption(f'{metric} {"(Cumulative)" if cumulative else "(Per Month)"} — by first order month.')

            # Color gradient on month columns
            if month_cols:
                from ui.tables import _parse_perf_num
                flat_vals = []
                for mc in month_cols:
                    for v in display_df[mc]:
                        num = _parse_perf_num(v)
                        if num is not None and num > 0:
                            flat_vals.append(num)
                if flat_vals:
                    vmin = np.percentile(flat_vals, 5)
                    vmax = np.percentile(flat_vals, 95)
                else:
                    vmin, vmax = 0, 1
                style_fn = make_cohort_cell_style(vmin, vmax)
            else:
                style_fn = None

            summary_cols = ['Cohort', 'Customers', 'NCPA', 'AOV', '1st Order %']
            column_groups = [
                ('', summary_cols),
                ('Month', month_cols),
            ]

            render_html_table(
                display_df,
                max_height=min(len(display_df) * 35 + 60, 700),
                style_fn=style_fn,
                style_cols=month_cols,
                column_groups=column_groups,
            )

    # --- Retention Curve ---
    st.divider()
    matrix = get_customer_cohort_data(sku_filter=sku_val, source_filter=source_val)
    if not matrix.empty:
        all_cohorts_curve = sorted(matrix.index.tolist())
        filtered_cohorts = [c for c in all_cohorts_curve if start_str <= c <= end_str] if 'start_str' in dir() else all_cohorts_curve
        matrix_filtered = matrix.loc[[c for c in filtered_cohorts if c in matrix.index]]

        if not matrix_filtered.empty:
            st.subheader('Retention Curve')
            st.caption('Average across selected cohorts.')

            _curve = {}
            for col in matrix_filtered.columns:
                vals = matrix_filtered[col].dropna()
                if len(vals) > 0:
                    _curve[int(col)] = float(vals.mean())

            if _curve:
                max_display = min(max(_curve.keys()), 36)
                curve_months = list(range(1, max_display + 1))
                curve_rates = [_curve.get(m, 0) for m in curve_months]

                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=curve_months,
                    y=curve_rates,
                    mode='lines+markers',
                    name='Retention Rate',
                    line=dict(color='#0F3557', width=2),
                    marker=dict(size=4),
                ))
                fig.update_layout(
                    xaxis_title='Month Offset', yaxis_title='Retention %',
                    yaxis=dict(tickformat='.0%', gridcolor='#E8EDF3'),
                    xaxis=dict(gridcolor='#E8EDF3'),
                    height=320, margin=dict(l=0, r=0, t=10, b=0),
                    plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
                )
                st.plotly_chart(fig, use_container_width=True)

            # Cohort sizes
            st.subheader('Cohort Sizes')
            sizes = get_cohort_sizes()
            if not sizes.empty:
                sizes = sizes[sizes['cohort'].between(start_str, end_str)] if 'start_str' in dir() else sizes
            if not sizes.empty:
                fig = px.bar(sizes, x='cohort', y='cohort_size',
                             color_discrete_sequence=['#0F3557'])
                fig.update_layout(height=280, margin=dict(l=0, r=0, t=10, b=0),
                                  plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
                                  xaxis=dict(gridcolor='#E8EDF3'),
                                  yaxis=dict(gridcolor='#E8EDF3'))
                st.plotly_chart(fig, use_container_width=True)

    # --- Projected 12-Month Revenue ---
    st.divider()
    st.subheader('Projected Revenue (Next 12 Months)')
    st.caption('Repeat revenue from existing cohorts + new customer revenue from media spend plan.')

    _cached_waterfall = ctx.get('cached_waterfall')
    _cached_aov = ctx.get('cached_aov_and_units')
    _load_seasonal_json = ctx.get('load_seasonal_json')

    if _cached_waterfall and _cached_aov:
        with get_db() as conn:
            _media_plan_raw = get_media_spend(conn, source='All Sources')
            _seasonal_json = _load_seasonal_json() if _load_seasonal_json else None

        _media_plan_json = _json.dumps(_media_plan_raw, sort_keys=True)
        _wf = _cached_waterfall(_media_plan_json, None, 12, _seasonal_json)
        _metrics = _cached_aov(None)

        if not _wf.empty and _metrics:
            _repeat_rpu = _metrics.get('repeat_rev_per_unit', 0)
            _new_rpu = _metrics.get('new_customer_rev_per_unit', 0)
            _media_map = {e['month']: e for e in _media_plan_raw} if _media_plan_raw else {}

            _forecast_skus = ctx.get('forecast_skus')
            _rep_sku_table = build_repeat_customer_sku_month_table(
                _wf, horizon_months=12, forecast_skus=_forecast_skus,
            )
            _month_cols = [c for c in _rep_sku_table.columns if c.startswith('20')] if not _rep_sku_table.empty else []
            _rep_units_by_month = {}
            for m in _month_cols:
                _rep_units_by_month[m] = _rep_sku_table[m].sum()

            _rev_rows = []
            for _, row in _wf.iterrows():
                m = row['month']
                repeat_rev = round(_rep_units_by_month.get(m, 0) * _repeat_rpu)
                entry = _media_map.get(m, {})
                spend = entry.get('spend', 0) or 0
                roas = entry.get('new_customer_roas', 0) or 0
                if spend > 0 and roas > 0:
                    new_rev = round(spend * roas)
                else:
                    new_rev = round(row.get('new_customer_units', 0) * _new_rpu)
                total = repeat_rev + new_rev
                _rev_rows.append({
                    'Month': m,
                    'Repeat Revenue': f'${repeat_rev:,.0f}',
                    'New Customer Revenue': f'${new_rev:,.0f}',
                    'Total Revenue': f'${total:,.0f}',
                    '_repeat': repeat_rev,
                    '_new': new_rev,
                    '_total': total,
                })

            _rev_df = pd.DataFrame(_rev_rows)
            _totals = {
                'Month': 'Total',
                'Repeat Revenue': f'${sum(r["_repeat"] for r in _rev_rows):,.0f}',
                'New Customer Revenue': f'${sum(r["_new"] for r in _rev_rows):,.0f}',
                'Total Revenue': f'${sum(r["_total"] for r in _rev_rows):,.0f}',
            }
            _display_df = _rev_df[['Month', 'Repeat Revenue', 'New Customer Revenue', 'Total Revenue']]
            _display_df = pd.concat([_display_df, pd.DataFrame([_totals])], ignore_index=True)
            render_html_table(_display_df)

            _chart_df = _rev_df[['Month', '_repeat', '_new']].rename(columns={'_repeat': 'Repeat', '_new': 'New Customer'})
            _melted = _chart_df.melt(id_vars='Month', var_name='Segment', value_name='Revenue')
            fig_rev = px.bar(
                _melted, x='Month', y='Revenue', color='Segment',
                color_discrete_map={'Repeat': '#0F3557', 'New Customer': '#3B82F6'},
                barmode='stack',
            )
            fig_rev.update_layout(
                height=320, margin=dict(l=0, r=0, t=10, b=0),
                yaxis=dict(tickprefix='$', tickformat=',.0f', gridcolor='#E8EDF3'),
                xaxis=dict(gridcolor='#E8EDF3'),
                plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
                legend=dict(orientation='h', yanchor='bottom', y=1.02),
            )
            st.plotly_chart(fig_rev, use_container_width=True)
        else:
            st.info('No waterfall data available. Configure media spend in the Demand Forecast page.')
    else:
        st.info('Revenue projection requires the waterfall engine. Ensure media spend is configured.')

    # --- Seasonality Factors ---
    st.divider()
    st.subheader('Seasonality')
    st.caption('Monthly multipliers for demand forecasts. >1.0 = increased demand, <1.0 = decreased.')

    with get_db() as conn:
        _seas_enabled_val = get_setting(conn, 'seasonality_enabled', 'true')
        _seas_indices = get_seasonal_indices(conn)

    _seas_enabled = st.toggle('Enable Seasonality', value=(_seas_enabled_val == 'true'), key='seas_toggle')

    _month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    _seas_df = pd.DataFrame({
        'Month': _month_names,
        'Index': [_seas_indices.get(m, 1.0) for m in range(1, 13)],
    })
    _seas_df['Month'] = _seas_df['Month'].astype(str)
    _seas_df['Index'] = pd.to_numeric(_seas_df['Index'], errors='coerce').fillna(1.0)

    col_seas_edit, col_seas_chart = st.columns([1, 2])

    _seas_values = [_seas_indices.get(m, 1.0) for m in range(1, 13)]
    _edited_seas_values = list(_seas_values)

    with col_seas_edit:
        for i, (name, val) in enumerate(zip(_month_names, _seas_values)):
            _lbl_c, _inp_c = st.columns([1, 2])
            with _lbl_c:
                st.markdown(f'**{name}**')
            with _inp_c:
                _edited_seas_values[i] = st.number_input(
                    name, min_value=0.50, max_value=2.00, value=float(val),
                    step=0.01, format='%.2f', key=f'seas_{i}', label_visibility='collapsed',
                )

    edited_seas = pd.DataFrame({'Month': _month_names, 'Index': _edited_seas_values})

    with col_seas_chart:
        fig_seas = go.Figure()
        fig_seas.add_trace(go.Bar(
            x=_month_names,
            y=edited_seas['Index'].tolist(),
            marker_color=['#E05252' if v < 1.0 else '#2DA87E' for v in edited_seas['Index']],
            text=[f'{v:.2f}' for v in edited_seas['Index']],
            textposition='outside',
        ))
        fig_seas.add_shape(type='line', x0=-0.5, x1=11.5, y0=1.0, y1=1.0,
                           line=dict(dash='dash', color='gray', width=1))
        fig_seas.update_layout(
            yaxis_title='Seasonal Index',
            yaxis=dict(range=[0.5, 1.5]),
            height=350,
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(fig_seas, use_container_width=True)

    if st.button('Save Settings', type='primary', key='save_seas'):
        with get_db() as conn:
            set_setting(conn, 'seasonality_enabled', 'true' if _seas_enabled else 'false')
            for i, row in edited_seas.iterrows():
                upsert_seasonal_index(conn, i + 1, float(row['Index']))
        clear_waterfall_cache()
        st.success('Seasonality settings saved! Forecasts will update on next load.')
        st.rerun()
