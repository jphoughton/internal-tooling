"""
Cash Flow Forecasting dashboard page.

52-week rolling cash flow model with auto-calibrated projections,
bank transaction imports, and scenario analysis.

CFO perspective: This replaces the Google Sheets 13-week cash flow model.
Shows where cash is going, when it's tight, and what levers to pull.
Every number is traceable back to actual bank transactions or explicit forecasts.
"""
import logging
from datetime import date, timedelta

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from db import get_db, read_sql, get_cashflow_setting, set_cashflow_setting
from utils.constants import CASHFLOW_CATEGORIES

log = logging.getLogger(__name__)


def _build_balance_chart(df: pd.DataFrame, min_threshold: float, horizon_weeks: int) -> go.Figure:
    """Build the main cash balance area chart with confidence bands."""
    display = df.head(horizon_weeks)
    today = date.today()

    fig = go.Figure()

    # Confidence band (only future weeks)
    future = display[display['is_actual'] == False]  # noqa: E712
    if not future.empty:
        fig.add_trace(go.Scatter(
            x=pd.to_datetime(future['week_start']),
            y=future['confidence_upper'],
            mode='lines',
            line=dict(width=0),
            showlegend=False,
            hoverinfo='skip',
        ))
        fig.add_trace(go.Scatter(
            x=pd.to_datetime(future['week_start']),
            y=future['confidence_lower'],
            mode='lines',
            line=dict(width=0),
            fill='tonexty',
            fillcolor='rgba(99, 110, 250, 0.1)',
            showlegend=False,
            hoverinfo='skip',
        ))

    # Actual balance line
    actuals = display[display['is_actual'] == True]  # noqa: E712
    if not actuals.empty:
        fig.add_trace(go.Scatter(
            x=pd.to_datetime(actuals['week_start']),
            y=actuals['closing_balance'],
            mode='lines+markers',
            name='Actual',
            line=dict(color='#22c55e', width=2.5),
            marker=dict(size=4),
        ))

    # Projected balance line
    projected = display[display['is_actual'] == False]  # noqa: E712
    if not projected.empty:
        # Connect to last actual point
        if not actuals.empty:
            bridge = pd.concat([actuals.tail(1), projected])
        else:
            bridge = projected
        fig.add_trace(go.Scatter(
            x=pd.to_datetime(bridge['week_start']),
            y=bridge['closing_balance'],
            mode='lines+markers',
            name='Projected',
            line=dict(color='#636efa', width=2.5, dash='dash'),
            marker=dict(size=4),
        ))

    # Min threshold line
    fig.add_hline(
        y=min_threshold,
        line_dash='dot',
        line_color='#ef4444',
        annotation_text=f'Min Cash: ${min_threshold:,.0f}',
        annotation_position='top left',
    )

    # Today line
    fig.add_vline(
        x=str(today),
        line_dash='dash',
        line_color='rgba(255,255,255,0.3)',
        annotation_text='Today',
        annotation_position='top',
    )

    fig.update_layout(
        height=400,
        margin=dict(l=0, r=0, t=20, b=0),
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='rgba(255,255,255,0.8)', size=11),
        xaxis=dict(
            gridcolor='rgba(255,255,255,0.05)',
            showgrid=True,
        ),
        yaxis=dict(
            gridcolor='rgba(255,255,255,0.05)',
            showgrid=True,
            tickformat='$,.0f',
        ),
        legend=dict(
            orientation='h',
            yanchor='bottom',
            y=1.02,
            xanchor='right',
            x=1,
        ),
        hovermode='x unified',
    )

    return fig


def _build_inflow_outflow_chart(df: pd.DataFrame, horizon_weeks: int) -> go.Figure:
    """Stacked bar chart of inflows vs outflows."""
    display = df.head(horizon_weeks)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=pd.to_datetime(display['week_start']),
        y=display['total_inflows'],
        name='Inflows',
        marker_color='#22c55e',
        opacity=0.8,
    ))
    fig.add_trace(go.Bar(
        x=pd.to_datetime(display['week_start']),
        y=-display['total_outflows'],
        name='Outflows',
        marker_color='#ef4444',
        opacity=0.8,
    ))
    fig.add_trace(go.Scatter(
        x=pd.to_datetime(display['week_start']),
        y=display['net_cashflow'],
        name='Net',
        mode='lines+markers',
        line=dict(color='#fbbf24', width=2),
        marker=dict(size=4),
    ))

    fig.update_layout(
        height=300,
        margin=dict(l=0, r=0, t=20, b=0),
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='rgba(255,255,255,0.8)', size=11),
        barmode='relative',
        xaxis=dict(gridcolor='rgba(255,255,255,0.05)'),
        yaxis=dict(
            gridcolor='rgba(255,255,255,0.05)',
            tickformat='$,.0f',
        ),
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
        hovermode='x unified',
    )
    return fig


def _format_cash_table(df: pd.DataFrame, horizon_weeks: int) -> str:
    """Build an HTML table of the weekly cash flow detail."""
    display = df.head(horizon_weeks)

    revenue_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'revenue']
    expense_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'expense']

    # Build HTML
    html = '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:0.75rem;">'

    # Header
    html += '<thead><tr style="border-bottom:2px solid rgba(255,255,255,0.2);">'
    html += '<th style="text-align:left;padding:6px 8px;position:sticky;left:0;background:#0e1117;z-index:1;">Week</th>'
    for _, row in display.iterrows():
        ws = row['week_start'][:10]
        style = 'font-weight:600;' if row.get('is_actual') else 'font-style:italic;opacity:0.8;'
        html += f'<th style="text-align:right;padding:6px 8px;white-space:nowrap;{style}">{ws}</th>'
    html += '</tr></thead><tbody>'

    def _row_html(label, key, color='inherit', bold=False):
        fw = 'font-weight:700;' if bold else ''
        html_r = f'<tr><td style="padding:4px 8px;white-space:nowrap;position:sticky;left:0;background:#0e1117;{fw}color:{color};">{label}</td>'
        for _, r in display.iterrows():
            val = r.get(key, 0)
            if val is None or pd.isna(val):
                val = 0
            cell_style = f'{fw}color:{color};' if val != 0 else 'opacity:0.3;'
            html_r += f'<td style="text-align:right;padding:4px 8px;{cell_style}">${val:,.0f}</td>'
        html_r += '</tr>'
        return html_r

    # Revenue section
    html += '<tr><td colspan="100" style="padding:8px 8px 2px;font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;color:rgba(255,255,255,0.4);font-weight:700;">Revenue</td></tr>'
    for cat in revenue_cats:
        label = CASHFLOW_CATEGORIES[cat]['label']
        html += _row_html(label, cat, color='#22c55e')
    html += _row_html('Total Inflows', 'total_inflows', color='#22c55e', bold=True)

    # Expense section
    html += '<tr><td colspan="100" style="padding:8px 8px 2px;font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;color:rgba(255,255,255,0.4);font-weight:700;">Expenses</td></tr>'
    for cat in expense_cats:
        label = CASHFLOW_CATEGORIES[cat]['label']
        html += _row_html(label, cat, color='#ef4444')
    html += _row_html('Total Outflows', 'total_outflows', color='#ef4444', bold=True)

    # Summary section
    html += '<tr style="border-top:2px solid rgba(255,255,255,0.2);"><td colspan="100"></td></tr>'
    html += _row_html('Net Cash Flow', 'net_cashflow', color='#fbbf24', bold=True)
    html += _row_html('Closing Balance', 'closing_balance', color='#ffffff', bold=True)

    html += '</tbody></table></div>'
    return html


def render(ctx):
    """Render the Cash Flow Forecasting page."""
    st.markdown('## Cash Flow Forecast')

    # Controls row
    ctrl_cols = st.columns([1, 1, 2])
    with ctrl_cols[0]:
        scenario = st.segmented_control(
            'Scenario',
            ['Base', 'Conservative', 'Aggressive'],
            default='Base',
            key='cf_scenario',
        )
    with ctrl_cols[1]:
        horizon_label = st.segmented_control(
            'Horizon',
            ['13 weeks', '26 weeks', '52 weeks'],
            default='13 weeks',
            key='cf_horizon',
        )

    scenario_key = (scenario or 'Base').lower()
    horizon_weeks = {'13 weeks': 13, '26 weeks': 26, '52 weeks': 52}.get(horizon_label, 13)

    # Build forecast
    try:
        with get_db() as conn:
            from analytics.cashflow import build_cashflow_forecast, get_cashflow_kpis

            forecast_df = build_cashflow_forecast(
                conn,
                start_date=date.today() - timedelta(weeks=4),  # include 4 weeks of actuals
                weeks=horizon_weeks + 4,
                scenario=scenario_key,
            )

            if forecast_df.empty:
                st.info('No data available. Upload bank transactions to get started.')
                _render_upload_section()
                return

            kpis = get_cashflow_kpis(conn, forecast_df)
    except Exception as e:
        st.error(f'Error building forecast: {e}')
        log.exception('Cash flow forecast error')
        _render_upload_section()
        return

    # Alert banner
    if kpis.get('alert_week'):
        week_num = kpis['alert_week']
        alert_rows = forecast_df[forecast_df['week_num'] == week_num]
        if not alert_rows.empty:
            alert_balance = alert_rows.iloc[0]['closing_balance']
            st.error(
                f"Cash below ${kpis['min_cash_threshold']:,.0f} projected in **Week {week_num}** "
                f"(est. ${alert_balance:,.0f})",
            )

    # KPI row
    kpi_cols = st.columns(5)
    kpi_cols[0].metric('Current Cash', f"${kpis['current_cash']:,.0f}")
    kpi_cols[1].metric('13-Week Projected', f"${kpis['projected_13w']:,.0f}")
    kpi_cols[2].metric('52-Week Projected', f"${kpis['projected_52w']:,.0f}")
    kpi_cols[3].metric(
        'Monthly Burn',
        f"${abs(kpis['monthly_burn']):,.0f}",
        delta='outflow' if kpis['monthly_burn'] > 0 else 'inflow',
        delta_color='inverse',
    )
    runway_text = f"{kpis['runway_weeks']}+ wks" if kpis['runway_weeks'] >= 52 else f"{kpis['runway_weeks']} wks"
    kpi_cols[4].metric('Runway', runway_text)

    st.markdown('---')

    # Cash balance chart
    st.markdown('#### Projected Cash Balance')
    fig = _build_balance_chart(forecast_df, kpis['min_cash_threshold'], horizon_weeks + 4)
    st.plotly_chart(fig, use_container_width=True)

    # Weekly cash flow table
    st.markdown('#### Weekly Detail')
    table_html = _format_cash_table(forecast_df, horizon_weeks + 4)
    st.markdown(table_html, unsafe_allow_html=True)

    # Inflow/Outflow chart in expander
    with st.expander('Inflows vs Outflows', expanded=False):
        fig2 = _build_inflow_outflow_chart(forecast_df, horizon_weeks + 4)
        st.plotly_chart(fig2, use_container_width=True)

    # Model settings expander
    with st.expander('Model Settings', expanded=False):
        _render_settings_section()

    # Upload section
    with st.expander('Upload Transactions', expanded=False):
        _render_upload_section()

    # Mapping link
    with st.expander('Transaction Mappings', expanded=False):
        _render_mapping_link()


def _render_upload_section():
    """Render the bank transaction CSV upload widget."""
    st.markdown('##### Upload Bank Transactions')
    st.caption('Upload a Highbeam CSV export. Re-uploads are safe -- duplicates are skipped.')

    uploaded = st.file_uploader(
        'Highbeam CSV',
        type=['csv'],
        key='cf_csv_upload',
        label_visibility='collapsed',
    )

    if uploaded:
        try:
            from etl.cashflow_csv import parse_highbeam_csv, import_transactions

            df = parse_highbeam_csv(uploaded)
            st.info(f'Parsed {len(df)} transactions from CSV')

            if st.button('Import Transactions', type='primary', key='cf_import_btn'):
                with st.spinner('Importing...'):
                    with get_db() as conn:
                        result = import_transactions(conn, df)

                st.success(
                    f"Import complete: **{result['inserted']}** new, "
                    f"**{result['skipped']}** skipped (duplicates), "
                    f"**{result['unmapped']}** need mapping"
                )
                if result['unmapped'] > 0:
                    st.info('Go to **Transaction Mappings** to categorize unmapped transactions.')
                st.rerun()
        except Exception as e:
            st.error(f'Import failed: {e}')


def _render_settings_section():
    """Render cash flow model settings."""
    st.markdown('##### Model Parameters')

    try:
        with get_db() as conn:
            dtc_ratio = get_cashflow_setting(conn, 'dtc_payout_ratio', '0.94')
            amz_ratio = get_cashflow_setting(conn, 'amazon_payout_ratio', '0.62')
            cogs_pct = get_cashflow_setting(conn, 'cogs_pct', '0.25')
            min_cash = get_cashflow_setting(conn, 'min_cash_threshold', '100000')
            loc_balance = get_cashflow_setting(conn, 'loc_balance', '510000')
    except Exception:
        dtc_ratio, amz_ratio, cogs_pct = '0.94', '0.62', '0.25'
        min_cash, loc_balance = '100000', '510000'

    with st.form('cf_settings_form'):
        cols = st.columns(3)
        new_dtc = cols[0].number_input(
            'DTC Payout Ratio', value=float(dtc_ratio),
            min_value=0.5, max_value=1.0, step=0.01, format='%.2f',
        )
        new_amz = cols[1].number_input(
            'Amazon Payout Ratio', value=float(amz_ratio),
            min_value=0.3, max_value=0.9, step=0.01, format='%.2f',
        )
        new_cogs = cols[2].number_input(
            'COGS %', value=float(cogs_pct) * 100,
            min_value=5.0, max_value=60.0, step=1.0, format='%.0f',
        )

        cols2 = st.columns(2)
        new_min = cols2[0].number_input(
            'Min Cash Threshold ($)', value=float(min_cash),
            min_value=0.0, step=10000.0, format='%.0f',
        )
        new_loc = cols2[1].number_input(
            'Line of Credit ($)', value=float(loc_balance),
            min_value=0.0, step=10000.0, format='%.0f',
        )

        if st.form_submit_button('Save Settings', type='primary'):
            with get_db() as conn:
                set_cashflow_setting(conn, 'dtc_payout_ratio', str(new_dtc))
                set_cashflow_setting(conn, 'amazon_payout_ratio', str(new_amz))
                set_cashflow_setting(conn, 'cogs_pct', str(new_cogs / 100))
                set_cashflow_setting(conn, 'min_cash_threshold', str(new_min))
                set_cashflow_setting(conn, 'loc_balance', str(new_loc))
            st.success('Settings saved!')
            st.rerun()


def _render_mapping_link():
    """Render link to transaction mapping page."""
    try:
        with get_db() as conn:
            from analytics.cashflow import get_mapping_stats
            stats = get_mapping_stats(conn)

        unmapped = stats.get('unmapped_tx_count', 0)
        total = stats.get('total_tx_count', 0)
        mapped_pct = ((total - unmapped) / total * 100) if total > 0 else 0

        st.markdown(
            f'**{mapped_pct:.0f}%** of transactions categorized '
            f'({total - unmapped:,} of {total:,})'
        )
        if unmapped > 0:
            st.warning(f'{unmapped:,} transactions need mapping.')

        st.caption('Navigate to **Transaction Mapping** page in the sidebar to manage category mappings.')
    except Exception:
        st.caption('Upload transactions first, then use the Transaction Mapping page to categorize them.')
