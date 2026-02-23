"""Retention page."""
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
from db import (
    get_db,
    get_last_sync_timestamp, get_new_rows_since_yesterday, get_synced_sources,
    get_seasonal_indices, get_setting, set_setting, upsert_seasonal_index,
)
from analytics.retention import (
    get_customer_cohort_data,
    get_cohort_sizes,
)
from analytics.waterfall import clear_waterfall_cache
from analytics.sku_flavors import get_flavor
from ui.components import render_freshness_badge, smart_date_filter


@st.cache_data(ttl=300)
def _load_sku_list():
    with get_db() as conn:
        rows = conn.execute(
            'SELECT sku, product_name, category, sources FROM sku_master WHERE is_active = 1 ORDER BY category, sku'
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


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

    matrix = get_customer_cohort_data(sku_filter=sku_val, source_filter=source_val)

    if matrix.empty:
        st.warning('No retention data available with current filters.')
    else:
        # Date range filter — scoped to this page only
        all_cohorts = sorted(matrix.index.tolist())
        earliest = datetime.strptime(all_cohorts[0], '%Y-%m').date()
        latest = datetime.strptime(all_cohorts[-1], '%Y-%m').date()

        start_date, end_date = smart_date_filter(earliest, latest, 'ret')

        # Filter matrix to selected date range
        start_str = start_date.strftime('%Y-%m')
        end_str = end_date.strftime('%Y-%m')
        filtered_cohorts = [c for c in all_cohorts if start_str <= c <= end_str]
        matrix = matrix.loc[filtered_cohorts]

        if matrix.empty:
            st.warning('No cohorts in the selected date range.')
        else:
            st.subheader('Cohort Heatmap')
            st.caption('Each row is a monthly cohort. Values = % of customers returning at each month offset.')

            # Create heatmap
            fig = px.imshow(
                matrix.values,
                labels=dict(x='Months Since First Purchase', y='Cohort', color='Retention %'),
                x=[str(c) for c in matrix.columns],
                y=matrix.index.tolist(),
                color_continuous_scale='Blues',
                aspect='auto',
                zmin=0, zmax=1,
            )
            fig.update_layout(height=max(300, len(matrix) * 40))
            st.plotly_chart(fig, use_container_width=True)

            # Average retention curve — computed from the date-filtered matrix
            st.subheader('Retention Curve')
            st.caption('Average across selected cohorts.')

            # Build curve from the filtered matrix directly
            _curve = {}
            for col in matrix.columns:
                vals = matrix[col].dropna()
                if len(vals) >= 2:
                    _curve[int(col)] = float(vals.mean())
                elif len(vals) > 0:
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
                sizes = sizes[sizes['cohort'].between(start_str, end_str)]
            if not sizes.empty:
                fig = px.bar(sizes, x='cohort', y='cohort_size',
                             color_discrete_sequence=['#0F3557'])
                fig.update_layout(height=280, margin=dict(l=0, r=0, t=10, b=0),
                                  plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
                                  xaxis=dict(gridcolor='#E8EDF3'),
                                  yaxis=dict(gridcolor='#E8EDF3'))
                st.plotly_chart(fig, use_container_width=True)

    # --- Seasonality Factors ---
    st.divider()
    st.subheader('Seasonality')
    st.caption('Monthly multipliers for demand forecasts. >1.0 = increased demand, <1.0 = decreased.')

    with get_db() as conn:
        _seas_enabled_val = get_setting(conn, 'seasonality_enabled', 'true')
        _seas_indices = get_seasonal_indices(conn)

    _seas_enabled = st.toggle('Enable Seasonality', value=(_seas_enabled_val == 'true'), key='seas_toggle')

    # Build editable table
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
        # 2-column grid: Month label | Number input
        for i, (name, val) in enumerate(zip(_month_names, _seas_values)):
            _lbl_c, _inp_c = st.columns([1, 2])
            with _lbl_c:
                st.markdown(f'**{name}**')
            with _inp_c:
                _edited_seas_values[i] = st.number_input(
                    name, min_value=0.50, max_value=2.00, value=float(val),
                    step=0.01, format='%.2f', key=f'seas_{i}', label_visibility='collapsed',
                )

    # Build edited DataFrame for chart + save
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
