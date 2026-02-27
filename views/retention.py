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
    get_seasonal_indices, get_sku_seasonal_indices,
    get_setting, set_setting, upsert_seasonal_index,
    get_media_spend,
    get_model_runs,
)
from analytics.retention import (
    get_customer_cohort_data,
    get_cohort_sizes,
    build_cohort_matrices,
    get_cohort_summary,
    get_repeat_rate_summary,
)
from analytics.waterfall import clear_waterfall_cache
from analytics.dtc_demand import build_repeat_customer_sku_month_table
from ui.components import render_freshness_badge, render_model_freshness, smart_date_filter, render_html_table
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
    orders = matrices['orders']
    cohort_sizes = matrices['cohort_sizes']
    first_order_revenue = matrices['first_order_revenue']

    if revenue.empty:
        return None, []

    # Filter to date range
    all_cohorts = sorted(revenue.index)
    filtered = [c for c in all_cohorts if start_str <= c <= end_str]
    if not filtered:
        return None, []

    revenue = revenue.loc[filtered]
    orders = orders.loc[filtered]
    sizes = cohort_sizes.reindex(filtered)
    fo_rev = first_order_revenue.reindex(filtered)

    # Compute metric matrix (Month 0, Month 1, ... columns)
    if metric == 'Total Sales':
        if cumulative:
            data = revenue.cumsum(axis=1)
        else:
            data = revenue.copy()
    elif metric == 'Retention Rate':
        # Retention = cumulative order count / cohort size (matches TW)
        retention = orders.div(sizes, axis=0)
        if cumulative:
            data = retention.cumsum(axis=1)
        else:
            data = retention.copy()
    else:  # LTV
        if cumulative:
            data = revenue.cumsum(axis=1).div(sizes, axis=0)
        else:
            data = revenue.div(sizes, axis=0)

    # Compute "1st order" column value per cohort
    first_order_vals = {}
    for cohort in filtered:
        fo = fo_rev.get(cohort, np.nan)
        sz = sizes.get(cohort, np.nan)
        if pd.isna(fo) or pd.isna(sz) or sz == 0:
            first_order_vals[cohort] = np.nan
            continue
        if metric == 'Total Sales':
            first_order_vals[cohort] = fo
        elif metric == 'Retention Rate':
            first_order_vals[cohort] = 1.0  # 100% — every customer has a 1st order
        else:  # LTV
            first_order_vals[cohort] = fo / sz

    # Build summary columns
    summary_filtered = summary[summary['cohort'].isin(filtered)].set_index('cohort')
    display_rows = []
    for cohort in filtered:
        row = {'Cohort': cohort}
        if cohort in summary_filtered.index:
            s = summary_filtered.loc[cohort]
            row['Customers'] = f'{int(s["customers"]):,}' if pd.notna(s['customers']) else ''
            row['NCPA'] = f'${s["ncpa"]:.2f}' if pd.notna(s['ncpa']) else ''
            row['RPR'] = f'{s["rpr"]:.1%}' if pd.notna(s['rpr']) else ''
        else:
            row['Customers'] = ''
            row['NCPA'] = ''
            row['RPR'] = ''

        # "1st order" column (before month columns)
        fo_val = first_order_vals.get(cohort, np.nan)
        if pd.isna(fo_val):
            row['1st order'] = ''
        elif metric == 'Total Sales':
            row['1st order'] = f'${fo_val:,.0f}'
        elif metric == 'Retention Rate':
            row['1st order'] = f'{fo_val * 100:.2f}%'
        else:
            row['1st order'] = f'${fo_val:,.2f}'

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
    month_cols = ['1st order'] + [c for c in display_df.columns if c.startswith('M') and c[1:].isdigit()]
    return display_df, month_cols


def render(ctx):
    """Render the Retention page."""
    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title('Retention')

    # --- DTC / Amazon toggle ---
    _toggle_col, _sku_col = st.columns([1, 2])
    with _toggle_col:
        _channel = st.segmented_control(
            'Channel',
            options=['DTC', 'Amazon'],
            default='DTC',
            key='retention_channel',
        )
    if not _channel:
        _channel = 'DTC'
    _is_amazon = _channel == 'Amazon'
    source_val = 'amazon' if _is_amazon else 'shopify'

    with _badge_col:
        _badge_source = ['amazon'] if _is_amazon else ['shopify']
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, _badge_source)
            _new = get_new_rows_since_yesterday(conn, _badge_source)
            _srcs = get_synced_sources(conn, _badge_source)
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)
        with get_db() as _mr_conn:
            _mr = get_model_runs(_mr_conn)
        render_model_freshness(_mr, ['retention_cohorts', 'repeat_forecast'])

    # --- SKU filter ---
    skus = _load_sku_list()
    sku_options = skus['sku'].tolist() if not skus.empty else []
    with _sku_col:
        sku_filter = st.selectbox('Filter by SKU', ['All SKUs'] + sku_options)

    sku_val = None if sku_filter == 'All SKUs' else sku_filter

    # --- Cohort Analysis Table ---
    matrices = build_cohort_matrices(sku_filter=sku_val, source_filter=source_val)
    summary = get_cohort_summary(source_filter=source_val)

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

            summary_cols = ['Cohort', 'Customers', 'NCPA', 'RPR']
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
            sizes = get_cohort_sizes(source_filter=source_val)
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

    if _is_amazon:
        # --- Amazon Repeat Purchase Summary ---
        st.divider()
        st.subheader('Repeat Purchase Summary')
        st.caption('Repeat rate by cohort — customers with 2+ distinct Amazon orders (keyed by hashed buyer email).')

        _rr = get_repeat_rate_summary(source_filter='amazon')
        if not _rr.empty:
            _rr_filtered = _rr.copy()
            if 'start_str' in dir() and 'end_str' in dir():
                _rr_filtered = _rr_filtered[_rr_filtered['cohort'].between(start_str, end_str)]

            if not _rr_filtered.empty:
                # Top-level metrics
                _total_cust = int(_rr_filtered['total_customers'].sum())
                _total_repeat = int(_rr_filtered['repeat_customers'].sum())
                _overall_rate = _total_repeat / _total_cust if _total_cust > 0 else 0
                _total_repeat_rev = _rr_filtered['repeat_revenue'].sum()

                _m1, _m2, _m3, _m4 = st.columns(4)
                with _m1:
                    st.metric('Total Customers', f'{_total_cust:,}')
                with _m2:
                    st.metric('Repeat Customers', f'{_total_repeat:,}')
                with _m3:
                    st.metric('Overall Repeat Rate', f'{_overall_rate:.1%}')
                with _m4:
                    st.metric('Repeat Revenue', f'${_total_repeat_rev:,.0f}')

                # Formatted table
                _rr_display = _rr_filtered.copy()
                _rr_display['Cohort'] = _rr_display['cohort']
                _rr_display['Customers'] = _rr_display['total_customers'].apply(lambda x: f'{int(x):,}')
                _rr_display['Repeat'] = _rr_display['repeat_customers'].apply(lambda x: f'{int(x):,}')
                _rr_display['Repeat Rate'] = _rr_display['repeat_rate'].apply(lambda x: f'{x:.1%}')
                _rr_display['1st Order Rev'] = _rr_display['first_order_revenue'].apply(lambda x: f'${x:,.0f}')
                _rr_display['Repeat Rev'] = _rr_display['repeat_revenue'].apply(lambda x: f'${x:,.0f}')
                _rr_display = _rr_display[['Cohort', 'Customers', 'Repeat', 'Repeat Rate', '1st Order Rev', 'Repeat Rev']]
                render_html_table(_rr_display, max_height=min(len(_rr_display) * 35 + 60, 500))

                # Repeat rate trend chart
                st.subheader('Repeat Rate Trend')
                fig_rr = go.Figure()
                fig_rr.add_trace(go.Scatter(
                    x=_rr_filtered['cohort'].tolist(),
                    y=_rr_filtered['repeat_rate'].tolist(),
                    mode='lines+markers',
                    name='Repeat Rate',
                    line=dict(color='#0F3557', width=2),
                    marker=dict(size=5),
                ))
                fig_rr.update_layout(
                    xaxis_title='Cohort', yaxis_title='Repeat Rate',
                    yaxis=dict(tickformat='.0%', gridcolor='#E8EDF3'),
                    xaxis=dict(gridcolor='#E8EDF3'),
                    height=320, margin=dict(l=0, r=0, t=10, b=0),
                    plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
                )
                st.plotly_chart(fig_rr, use_container_width=True)

                # Revenue split chart
                st.subheader('Revenue Split by Cohort')
                st.caption('First-order vs repeat revenue per acquisition cohort.')
                _rev_chart = pd.DataFrame({
                    'Cohort': _rr_filtered['cohort'],
                    'First Order': _rr_filtered['first_order_revenue'],
                    'Repeat': _rr_filtered['repeat_revenue'],
                })
                _rev_melted = _rev_chart.melt(id_vars='Cohort', var_name='Segment', value_name='Revenue')
                fig_rev = px.bar(
                    _rev_melted, x='Cohort', y='Revenue', color='Segment',
                    color_discrete_map={'First Order': '#3B82F6', 'Repeat': '#0F3557'},
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
                st.info('No Amazon repeat data in the selected date range.')
        else:
            st.info('No Amazon customer data available. Run a sync to pull fulfillment data.')

    else:
        # --- DTC: Projected 12-Month Revenue ---
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
            _wf = _cached_waterfall(_media_plan_json, 'shopify', 12, _seasonal_json)
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

        # --- Seasonality Factors (DTC only) ---
        st.divider()
        st.subheader('Seasonality')
        st.caption('Monthly multipliers for demand forecasts. >1.0 = increased demand, <1.0 = decreased.')

        with get_db() as conn:
            _seas_enabled_val = get_setting(conn, 'seasonality_enabled', 'true')
            _seas_mode = get_setting(conn, 'seasonality_mode', 'auto')
            _seas_indices = get_seasonal_indices(conn)
            _sku_seas_indices = get_sku_seasonal_indices(conn)

        _seas_enabled = st.toggle('Enable Seasonality', value=(_seas_enabled_val == 'true'), key='seas_toggle')

        _mode_options = ['Auto (data-driven)', 'Manual']
        _mode_idx = 1 if _seas_mode == 'manual' else 0
        _selected_mode = st.segmented_control(
            'Seasonality Mode', _mode_options, default=_mode_options[_mode_idx], key='seas_mode_ctrl',
        )
        _is_manual = (_selected_mode == 'Manual')

        if not _is_manual:
            st.caption(
                'Indices are computed daily from actual sales data. '
                'Each SKU gets its own seasonal shape. The global index shown '
                'below is a sales-weighted average across all SKUs.'
            )
        else:
            st.caption('Manually set seasonal multipliers for all SKUs.')

        _month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

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
                        disabled=(not _is_manual),
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
                name='Global',
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
                set_setting(conn, 'seasonality_mode', 'manual' if _is_manual else 'auto')
                if _is_manual:
                    for i, row in edited_seas.iterrows():
                        upsert_seasonal_index(conn, i + 1, float(row['Index']))
            clear_waterfall_cache()
            st.success('Seasonality settings saved! Forecasts will update on next load.')
            st.rerun()

        # --- Per-SKU Seasonal Indices (read-only) ---
        if _sku_seas_indices and not _is_manual:
            from analytics.sku_flavors import get_flavor
            st.divider()
            st.subheader('Per-SKU Seasonal Indices')
            st.caption('Data-driven indices computed from actual sales history. Updated daily.')

            _sku_rows = []
            for sku, months in sorted(_sku_seas_indices.items()):
                row = {'SKU': sku, 'Flavor': get_flavor(sku)}
                for m in range(1, 13):
                    row[_month_names[m - 1]] = months.get(m, 1.0)
                _sku_rows.append(row)

            if _sku_rows:
                _sku_seas_df = pd.DataFrame(_sku_rows)

                def _seas_color(val):
                    if isinstance(val, (int, float)):
                        if val < 0.95:
                            return 'color: #E05252'
                        elif val > 1.05:
                            return 'color: #2DA87E; font-weight: 600'
                    return ''

                styled = _sku_seas_df.style.format(
                    {m: '{:.2f}' for m in _month_names}, na_rep='-',
                ).map(_seas_color, subset=_month_names)
                st.dataframe(styled, use_container_width=True, hide_index=True)
