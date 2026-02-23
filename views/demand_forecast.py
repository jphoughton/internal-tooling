"""Demand Forecast page."""
import json as _json
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime
from dateutil.relativedelta import relativedelta
from db import (
    get_db, read_sql,
    get_media_spend, get_amazon_revenue_forecast,
    get_last_sync_timestamp, get_new_rows_since_yesterday, get_synced_sources,
)
from analytics.forecast import forecast_sku
from analytics.waterfall import clear_waterfall_cache
from analytics.sku_flavors import get_flavor, sort_df_by_best_seller
from analytics.dtc_demand import (
    build_master_dtc_forecast,
    get_amazon_sku_velocity,
    get_current_month_progress,
    compute_remaining_month_demand,
)
from ui.components import render_html_table, render_freshness_badge


@st.cache_data(ttl=300)
def _load_sku_list():
    with get_db() as conn:
        rows = conn.execute(
            'SELECT sku, product_name, category, sources FROM sku_master WHERE is_active = 1 ORDER BY category, sku'
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def _format_sku_month_table(df, pct_col_name, rev_dict=None):
    """
    Format a SKU x Month DataFrame for st.dataframe display.
    If rev_dict is provided, prepend a 'Projected Revenue' row at the top
    with $ formatting and a highlighted background.
    """
    if df is None or df.empty:
        return df, {}, False
    month_cols = [c for c in df.columns if c.startswith('20')]
    short_labels = {}
    for m in month_cols:
        try:
            dt = datetime.strptime(m, '%Y-%m')
            short_labels[m] = dt.strftime("%b '%y")
        except ValueError:
            short_labels[m] = m

    display = df.copy()
    has_rev_row = False

    # Prepend revenue row if provided — values pre-formatted as $ strings
    if rev_dict:
        has_rev_row = True
        rev_row = {'SKU': '\U0001f4b0 PROJECTED REVENUE', 'Flavor': ''}
        if pct_col_name in display.columns:
            rev_row[pct_col_name] = ''
        for m in month_cols:
            val = rev_dict.get(m, 0)
            rev_row[m] = f'${val:,.0f}' if val > 0 else ''
        total_val = sum(rev_dict.get(m, 0) for m in month_cols)
        rev_row['Total'] = f'${total_val:,.0f}' if total_val > 0 else ''
        rev_df = pd.DataFrame([rev_row])
        # Convert columns to object type so strings + numbers can coexist
        for col in month_cols + ['Total']:
            display[col] = display[col].astype(object)
        if pct_col_name in display.columns:
            display[pct_col_name] = display[pct_col_name].astype(object)
        display = pd.concat([rev_df, display], ignore_index=True)

    # Format pct column inline so HTML renderer shows "12.2%"
    if pct_col_name in display.columns:
        display[pct_col_name] = display[pct_col_name].apply(
            lambda v: f'{v:.1f}%' if isinstance(v, (int, float)) and v > 0 else (str(v) if v else '')
        )

    display = display.rename(columns=short_labels)

    # When there's a revenue row, values are mixed (strings for row 0, numbers for rest)
    # Use a lambda formatter that handles both
    if has_rev_row:
        fmt = {}
        for m in month_cols:
            col_name = short_labels[m]
            fmt[col_name] = lambda v: f'{v:,.0f}' if isinstance(v, (int, float)) else (str(v) if v else '')
        fmt['Total'] = lambda v: f'{v:,.0f}' if isinstance(v, (int, float)) else (str(v) if v else '')
        if pct_col_name in display.columns:
            fmt[pct_col_name] = lambda v: f'{v:.1f}%' if isinstance(v, (int, float)) and v > 0 else (str(v) if v else '')
    else:
        fmt = {short_labels[m]: '{:,.0f}' for m in month_cols}
        fmt['Total'] = '{:,.0f}'
        if pct_col_name in display.columns:
            fmt[pct_col_name] = '{:.1f}%'
    return display, fmt, has_rev_row


def _style_with_rev_row(styler, has_rev_row):
    """Apply highlighting + $ formatting to the revenue row (row 0)."""
    if not has_rev_row:
        return styler

    def _highlight_rev(row):
        if row.name == 0:
            return ['background-color: #d1fae5; font-weight: bold; color: #065f46; border-bottom: 2px solid #065f46'] * len(row)
        return [''] * len(row)

    return styler.apply(_highlight_rev, axis=1)


def render(ctx):
    """Render the Demand Forecast page."""
    FORECAST_SKUS = ctx['forecast_skus']
    _cached_waterfall = ctx['cached_waterfall']
    _cached_sku_forecast = ctx['cached_sku_forecast']
    _cached_retention_curve = ctx['cached_retention_curve']
    _cached_aov_and_units = ctx['cached_aov_and_units']
    _load_seasonal_json = ctx['load_seasonal_json']
    active_sources = ctx['active_sources']

    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title('Demand Forecast')
    with _badge_col:
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, ['shopify', 'amazon'])
            _new = get_new_rows_since_yesterday(conn, ['shopify', 'amazon'])
            _srcs = get_synced_sources(conn, ['shopify', 'amazon'])
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)
    st.caption('Shopify retention-based + Amazon velocity-based demand with new/repeat breakdown.')

    # Seasonality status
    _seas_status_json = _load_seasonal_json()
    if _seas_status_json:
        st.caption('Seasonality enabled \u2014 indices from the Retention page.')
    else:
        st.caption('Seasonality disabled \u2014 enable on the Retention page to apply seasonal adjustments.')

    # --- Filters (from sidebar Business Variables) ---
    bv = ctx['biz_vars']
    horizon = bv['forecast_horizon']
    amz_growth = bv['amazon_growth_pct']
    st.caption(f"Horizon: {horizon} months · Amazon growth: {amz_growth:+.0f}%/mo — change in sidebar **Business Variables**")

    st.divider()

    # --- Load media spend and Amazon revenue from DB (edited in sidebar) ---
    with get_db() as conn:
        _db_spend = get_media_spend(conn, source='All Sources')
        _db_amz_rev = get_amazon_revenue_forecast(conn)

    now = datetime.utcnow()
    horizon_months_list = [(now + relativedelta(months=i)).strftime('%Y-%m') for i in range(horizon)]

    if _db_spend:
        edited_spend = pd.DataFrame(_db_spend)
    else:
        edited_spend = pd.DataFrame([
            {'month': m, 'spend': 0.0, 'new_customer_roas': 0.7} for m in horizon_months_list
        ])
    # Ensure all horizon months are present
    _existing_months = set(edited_spend['month'].tolist())
    for m in horizon_months_list:
        if m not in _existing_months:
            edited_spend = pd.concat([edited_spend, pd.DataFrame([{'month': m, 'spend': 5000.0, 'new_customer_roas': 2.0}])], ignore_index=True)
    edited_spend = edited_spend[edited_spend['month'].isin(horizon_months_list)].sort_values('month').reset_index(drop=True)

    amz_rev_lookup = {r['month']: r['revenue'] for r in _db_amz_rev}
    amz_rev_forecast_dict = {m: rev for m, rev in amz_rev_lookup.items() if rev > 0}

    # Shopify waterfall
    media_plan = edited_spend.to_dict('records')
    media_plan_json = _json.dumps(media_plan, sort_keys=True)
    with st.spinner('Computing demand forecast...'):
        _seasonal_json = _load_seasonal_json()
        waterfall_df = _cached_waterfall(media_plan_json, None, horizon, _seasonal_json)
        shopify_sku_table = _cached_sku_forecast(waterfall_df.to_json(), None) if not waterfall_df.empty else pd.DataFrame()

        # Master DTC forecast
        dtc = build_master_dtc_forecast(
            shopify_waterfall_df=waterfall_df,
            shopify_sku_forecast_df=shopify_sku_table,
            amazon_growth_rate=amz_growth / 100.0,
            horizon_months=horizon,
            forecast_skus=FORECAST_SKUS,
            media_plan=media_plan,
            amazon_revenue_forecast=amz_rev_forecast_dict if amz_rev_forecast_dict else None,
        )

    summary_df = dtc['summary']
    new_customer_table = dtc['new_customer_table']
    repeat_customer_table = dtc['repeat_customer_table']
    amazon_table = dtc['amazon_table']
    rollup_table = dtc['rollup_table']
    channel = dtc['channel_split']
    revenue = dtc.get('revenue', {})

    # Sort all SKU tables by best-seller
    if not new_customer_table.empty:
        new_customer_table = sort_df_by_best_seller(new_customer_table, sku_col='SKU')
    if not repeat_customer_table.empty:
        repeat_customer_table = sort_df_by_best_seller(repeat_customer_table, sku_col='SKU')
    if not amazon_table.empty:
        amazon_table = sort_df_by_best_seller(amazon_table, sku_col='SKU')
    if not rollup_table.empty:
        rollup_table = sort_df_by_best_seller(rollup_table, sku_col='SKU')

    # --- Key Metrics ---
    metrics = _cached_aov_and_units(None)
    retention_curve = _cached_retention_curve(None)

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric('DTC Forecast (12mo)', f"{channel['dtc_total']:,} units")
    m2.metric('Shopify', f"{channel['shopify_total']:,}", help=f"{channel['shopify_pct']:.0f}% of DTC")
    m3.metric('Amazon', f"{channel['amazon_total']:,}", help=f"{channel['amazon_pct']:.0f}% of DTC")
    dtc_rev = channel.get('dtc_rev', 0)
    m4.metric('Forecasted Revenue', f'${dtc_rev:,.0f}' if dtc_rev > 0 else '\u2014')
    m5.metric('Units/New Cust', f"{metrics['units_per_new_customer']:.1f}")
    m6.metric('Mo-1 Retention', f'{retention_curve.get(1, 0):.0%}')

    # --- Current Month Drawdown ---
    month_prog = get_current_month_progress()
    if not rollup_table.empty:
        remaining_demand = compute_remaining_month_demand(rollup_table, month_prog)
        total_full = sum(v['full_month'] for v in remaining_demand.values())
        total_consumed = sum(v['already_consumed'] for v in remaining_demand.values())
        total_remaining = sum(v['remaining'] for v in remaining_demand.values())

        st.divider()
        st.subheader(f"Current Month \u2014 {datetime.strptime(month_prog['current_month'], '%Y-%m').strftime('%B %Y')}")
        st.caption(
            f"Day {month_prog['day_of_month']} of {month_prog['days_in_month']} "
            f"({month_prog['pct_elapsed']:.0%} through the month). "
            f'Estimated demand already consumed vs. remaining this month.'
        )
        prog_c1, prog_c2, prog_c3, prog_c4 = st.columns(4)
        prog_c1.metric('Full Month Forecast', f'{total_full:,} units')
        prog_c2.metric('Est. Already Consumed', f'{total_consumed:,} units', help=f"~{month_prog['pct_elapsed']:.0%} of monthly forecast")
        prog_c3.metric('Est. Remaining', f'{total_remaining:,} units', help=f"~{month_prog['pct_remaining']:.0%} of monthly forecast")
        prog_c4.metric('Days Remaining', f"{month_prog['days_remaining']} days")

        # Progress bar
        st.progress(month_prog['pct_elapsed'], text=f"{month_prog['pct_elapsed']:.0%} of month elapsed")

    st.divider()

    # ================================================================
    # SECTION: Master Waterfall Chart (Shopify + Amazon)
    # ================================================================
    st.subheader('Combined Forecast')

    if summary_df.empty or summary_df['total_units'].sum() == 0:
        st.warning('Not enough data to build a demand forecast. Ensure there is sales history.')
    else:
        fig = go.Figure()

        # Stacked bars: Shopify Repeat -> Shopify New -> Amazon
        fig.add_trace(go.Bar(
            x=summary_df['month'],
            y=summary_df['shopify_repeat_units'],
            name='Shopify Repeat',
            marker_color='#0F3557',
        ))
        fig.add_trace(go.Bar(
            x=summary_df['month'],
            y=summary_df['shopify_new_units'],
            name='Shopify New Customer',
            marker_color='#2DA87E',
        ))
        fig.add_trace(go.Bar(
            x=summary_df['month'],
            y=summary_df['amazon_units'],
            name='Amazon',
            marker_color='#F58B3D',
        ))

        # Total line
        fig.add_trace(go.Scatter(
            x=summary_df['month'],
            y=summary_df['total_units'],
            mode='lines+markers',
            name='Total DTC',
            line=dict(color='#E05252', width=2.5),
            marker=dict(size=6),
        ))

        fig.update_layout(
            barmode='stack',
            xaxis_title='Month',
            yaxis_title='Units',
            height=450,
            margin=dict(l=0, r=0, t=10, b=0),
            legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='left', x=0),
        )
        st.plotly_chart(fig, use_container_width=True)

        # --- Monthly Summary Table ---
        disp_summary = summary_df[['month', 'shopify_new_units', 'shopify_repeat_units',
                                     'shopify_total_units', 'amazon_units', 'total_units',
                                     'shopify_new_rev', 'shopify_repeat_rev',
                                     'shopify_total_rev', 'amazon_rev', 'total_rev']].copy()
        disp_summary.columns = ['Month', 'New Units', 'Repeat Units',
                                  'Shopify Units', 'Amazon Units', 'DTC Units',
                                  'New Rev', 'Repeat Rev', 'Shopify Rev', 'Amazon Rev', 'DTC Rev']
        for _uc in ['New Units', 'Repeat Units', 'Shopify Units', 'Amazon Units', 'DTC Units']:
            if _uc in disp_summary.columns:
                disp_summary[_uc] = disp_summary[_uc].apply(lambda x: f'{x:,.0f}' if pd.notnull(x) else '')
        for _rc in ['New Rev', 'Repeat Rev', 'Shopify Rev', 'Amazon Rev', 'DTC Rev']:
            if _rc in disp_summary.columns:
                disp_summary[_rc] = disp_summary[_rc].apply(lambda x: f'${x:,.0f}' if pd.notnull(x) else '')
        render_html_table(disp_summary)

    # ================================================================
    # SECTION: Master DTC Rollup by SKU x Month
    # ================================================================
    st.divider()
    st.subheader('Master Demand by SKU')
    st.caption('Combined Shopify (new + repeat) + Amazon demand per SKU per month. Use this for production planning and reorder decisions.')

    if not rollup_table.empty:
        ru_display, ru_fmt, ru_has_rev = _format_sku_month_table(
            rollup_table, '% of Sales', rev_dict=revenue.get('rollup'),
        )
        render_html_table(ru_display, max_height=min(len(ru_display) * 35 + 38, 700))

        grand_total = rollup_table['Total'].sum()
        dtc_rev_total = channel.get('dtc_rev', 0)
        st.markdown(f'**Grand Total DTC Demand ({horizon}mo):** {grand_total:,.0f} units \u00b7 **Revenue:** ${dtc_rev_total:,.0f}')
    else:
        st.info('No SKU demand data available.')

    # ================================================================
    # SECTION: New Customer Sales by SKU x Month
    # ================================================================
    st.divider()
    st.subheader('New Customer Sales by SKU')
    st.caption('Forecasted new customer demand per SKU per month. Mix is based on what first-time customers actually buy. Driven by media spend plan.')

    if not new_customer_table.empty:
        nc_display, nc_fmt, nc_has_rev = _format_sku_month_table(
            new_customer_table, '% of New Sales', rev_dict=revenue.get('new_customer'),
        )
        render_html_table(nc_display, max_height=min(len(nc_display) * 35 + 38, 600))

        nc_total = new_customer_table['Total'].sum()
        rev_metrics = revenue.get('metrics', {})
        nc_rev_per_unit = rev_metrics.get('new_customer_rev_per_unit', 0)
        st.markdown(f'**Total New Customer Units ({horizon}mo):** {nc_total:,.0f} \u00b7 **Rev/Unit:** ${nc_rev_per_unit:.2f}')
    else:
        st.info('No new customer sales data available.')

    # ================================================================
    # SECTION: Repeat Customer Sales by SKU x Month
    # ================================================================
    st.divider()
    st.subheader('Repeat Customer Sales by SKU')
    st.caption('Forecasted repeat customer demand per SKU per month. Uses retention curve decay applied to each historical cohort, distributed by repeat customer SKU mix.')

    if not repeat_customer_table.empty:
        rc_display, rc_fmt, rc_has_rev = _format_sku_month_table(
            repeat_customer_table, '% of Repeat Sales', rev_dict=revenue.get('repeat_customer'),
        )
        render_html_table(rc_display, max_height=min(len(rc_display) * 35 + 38, 600))

        rc_total = repeat_customer_table['Total'].sum()
        rev_metrics = revenue.get('metrics', {})
        rc_rev_per_unit = rev_metrics.get('repeat_rev_per_unit', 0)
        st.markdown(f'**Total Repeat Customer Units ({horizon}mo):** {rc_total:,.0f} \u00b7 **Rev/Unit:** ${rc_rev_per_unit:.2f}')
    else:
        st.info('No repeat customer sales data available.')

    # ================================================================
    # SECTION: Amazon Sales by SKU x Month
    # ================================================================
    if 'amazon' in active_sources or (not amazon_table.empty):
        st.divider()
        st.subheader('Amazon Sales by SKU')
        st.caption(
            'Amazon demand forecasted from sales velocity (blends 7-day and 30-day averages). '
            "Amazon doesn't provide customer-level data, so this is a trend-based projection."
        )

        if not amazon_table.empty:
            # Amazon metrics (above table)
            amz_total = amazon_table['Total'].sum()
            velocity = get_amazon_sku_velocity()
            total_daily = sum(v['avg_daily'] for v in velocity.values()) if velocity else 0
            rev_metrics = revenue.get('metrics', {})
            amz_rpu = rev_metrics.get('amazon_rev_per_unit', 0)

            amz_c1, amz_c2, amz_c3, amz_c4 = st.columns(4)
            amz_c1.metric('Amazon Daily Run Rate', f'{total_daily:.0f} units/day')
            amz_c2.metric('Amazon Monthly Run Rate', f'{total_daily * 30.44:,.0f} units/month')
            amz_c3.metric(f'Amazon {horizon}mo Forecast', f'{amz_total:,.0f} units')
            amz_c4.metric('Amazon Rev/Unit', f'${amz_rpu:.2f}' if amz_rpu > 0 else '\u2014')

            amz_display, amz_fmt, amz_has_rev = _format_sku_month_table(
                amazon_table, '% of Sales', rev_dict=revenue.get('amazon'),
            )
            render_html_table(amz_display, max_height=min(len(amz_display) * 35 + 38, 600))
        else:
            st.info('No Amazon sales data available for forecasting.')

    # --- Per-SKU Prophet Forecast (kept as expander) ---
    st.divider()
    with st.expander('Per-SKU Forecast (Prophet)', expanded=False):
        skus = _load_sku_list()
        if skus.empty:
            st.info('No SKU data available yet.')
            selected_sku = None
        else:
            selected_sku = st.selectbox('Select SKU', skus['sku'].tolist(), key='sku_forecast')

        if selected_sku:
            with st.spinner(f'Running forecast for {selected_sku}...'):
                result = forecast_sku(selected_sku)

            if result is None:
                st.error('No data available for this SKU.')
            else:
                col1, col2, col3 = st.columns(3)
                col1.metric('Forecast Method', result['method'].title())
                col2.metric('Avg Daily Demand', f"{result['reorder_info']['avg_daily_demand']} units")
                col3.metric('Reorder Point', f"{int(result['reorder_info']['reorder_point'])} units")

                hist = result['history']
                fc = result['forecast']
                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=hist['ds'], y=hist['y'],
                    mode='lines', name='Actual Sales',
                    line=dict(color='#0F3557', width=1.5),
                ))
                fig.add_trace(go.Scatter(
                    x=fc['ds'], y=fc['yhat'],
                    mode='lines', name='Forecast',
                    line=dict(color='#F58B3D', width=2),
                ))
                fig.add_trace(go.Scatter(
                    x=pd.concat([fc['ds'], fc['ds'][::-1]]),
                    y=pd.concat([fc['yhat_upper'], fc['yhat_lower'][::-1]]),
                    fill='toself',
                    fillcolor='rgba(245,158,11,0.15)',
                    line=dict(color='rgba(255,255,255,0)'),
                    name='80% Confidence',
                ))
                fig.update_layout(
                    height=400,
                    margin=dict(l=0, r=0, t=10, b=0),
                    legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='left', x=0),
                )
                st.plotly_chart(fig, use_container_width=True)

                ri = result['reorder_info']
                col1, col2, col3, col4 = st.columns(4)
                col1.metric('Lead Time', f"{ri['lead_time_days']} days")
                col2.metric('Lead Time Demand', f"{int(ri['lead_time_demand'])} units")
                col3.metric('Safety Stock', f"{int(ri['safety_stock'])} units")
                col4.metric('Reorder Point', f"{int(ri['reorder_point'])} units")

                monthly = result['monthly_forecast']
                render_html_table(monthly)
