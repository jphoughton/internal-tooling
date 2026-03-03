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
import numpy as np
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
    )
    fig.add_annotation(
        x=str(today),
        y=1,
        yref='paper',
        text='Today',
        showarrow=False,
        font=dict(color='rgba(255,255,255,0.5)', size=10),
        yanchor='bottom',
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
                'Cash below \\$%s projected in **Week %d** (est. \\$%s)'
                % (f"{kpis['min_cash_threshold']:,.0f}", week_num, f"{alert_balance:,.0f}"),
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

    # Weekly cash flow table (editable)
    st.markdown('#### Weekly Detail')
    _render_editable_table(forecast_df, horizon_weeks + 4)

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


def _render_editable_table(forecast_df: pd.DataFrame, horizon_weeks: int):
    """Render an editable cash flow table with per-cell persistence.

    Past weeks (actuals) are read-only. Future weeks are editable.
    Edits persist to cashflow_overrides and are used in projections.
    Each category row has a 'Reset to Smart Projection' action.
    """
    display = forecast_df.head(horizon_weeks)
    today = date.today()

    revenue_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'revenue']
    expense_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'expense']
    all_cats = revenue_cats + expense_cats
    cat_labels = {k: v['label'] for k, v in CASHFLOW_CATEGORIES.items() if k in all_cats}

    week_starts = display['week_start'].tolist()
    is_actual_map = dict(zip(display['week_start'], display['is_actual']))

    # Build pivot: rows = categories, columns = week_start dates
    pivot_data = {}
    for cat in all_cats:
        row_vals = {}
        for _, r in display.iterrows():
            ws = r['week_start']
            val = r.get(cat, 0)
            if val is None or (isinstance(val, float) and np.isnan(val)):
                val = 0
            row_vals[ws] = round(val)
        pivot_data[cat_labels[cat]] = row_vals

    pivot_df = pd.DataFrame(pivot_data).T
    pivot_df.columns = [ws[:10] for ws in week_starts]

    # Load existing overrides to show which cells have manual values
    existing_overrides = set()
    try:
        with get_db() as conn:
            override_rows = read_sql(
                'SELECT line_item, week_start FROM cashflow_overrides', conn,
            )
            for _, r in override_rows.iterrows():
                existing_overrides.add((r['line_item'], r['week_start']))
    except Exception:
        pass

    # Section headers
    rev_labels = [cat_labels[c] for c in revenue_cats]
    exp_labels = [cat_labels[c] for c in expense_cats]

    # Render revenue section
    st.markdown(
        '<p style="font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;'
        'color:rgba(255,255,255,0.4);font-weight:700;margin:12px 0 4px;">Revenue</p>',
        unsafe_allow_html=True,
    )
    rev_df = pivot_df.loc[pivot_df.index.isin(rev_labels)].copy()
    _render_category_section(rev_df, revenue_cats, cat_labels, week_starts,
                             is_actual_map, existing_overrides, '#22c55e', 'rev')

    # Total inflows row
    _render_total_row(display, 'total_inflows', 'Total Inflows', '#22c55e')

    # Render expense section
    st.markdown(
        '<p style="font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;'
        'color:rgba(255,255,255,0.4);font-weight:700;margin:12px 0 4px;">Expenses</p>',
        unsafe_allow_html=True,
    )
    exp_df = pivot_df.loc[pivot_df.index.isin(exp_labels)].copy()
    _render_category_section(exp_df, expense_cats, cat_labels, week_starts,
                             is_actual_map, existing_overrides, '#ef4444', 'exp')

    # Total outflows row
    _render_total_row(display, 'total_outflows', 'Total Outflows', '#ef4444')

    # Summary rows
    st.markdown('---')
    _render_total_row(display, 'net_cashflow', 'Net Cash Flow', '#fbbf24')
    _render_total_row(display, 'closing_balance', 'Closing Balance', '#ffffff')


def _render_category_section(section_df, cats, cat_labels, week_starts,
                             is_actual_map, existing_overrides, color, key_prefix):
    """Render an editable data_editor section for a group of categories."""
    # Create the editor DataFrame with integer values
    editor_df = section_df.astype(int)

    # Configure columns: actuals are disabled, future are editable
    col_config = {}
    for ws in week_starts:
        col_key = ws[:10]
        is_actual = is_actual_map.get(ws, False)
        col_config[col_key] = st.column_config.NumberColumn(
            col_key,
            format='$%d',
            disabled=is_actual,
            width='small',
        )

    edited = st.data_editor(
        editor_df,
        column_config=col_config,
        use_container_width=True,
        key=f'cf_edit_{key_prefix}',
        height=min(35 * (len(editor_df) + 1), 400),
    )

    # Detect changes and save overrides
    label_to_cat = {v: k for k, v in cat_labels.items() if k in cats}

    if edited is not None and not edited.equals(editor_df):
        _save_edits(editor_df, edited, label_to_cat, week_starts, is_actual_map)

    # Smart projection reset buttons
    _render_smart_buttons(cats, cat_labels, existing_overrides, week_starts, key_prefix)


def _render_total_row(display, col_key, label, color):
    """Render a read-only summary total row as HTML."""
    cells = ''
    for _, r in display.iterrows():
        val = r.get(col_key, 0)
        if val is None or (isinstance(val, float) and np.isnan(val)):
            val = 0
        cells += f'<td style="text-align:right;padding:4px 8px;font-weight:700;color:{color};">${val:,.0f}</td>'

    html = (
        f'<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:0.75rem;">'
        f'<tr><td style="padding:4px 8px;font-weight:700;color:{color};white-space:nowrap;">{label}</td>'
        f'{cells}</tr></table></div>'
    )
    st.markdown(html, unsafe_allow_html=True)


def _render_smart_buttons(cats, cat_labels, existing_overrides, week_starts, key_prefix):
    """Render 'Use Smart Projection' buttons for categories with manual overrides."""
    overridden_cats = set()
    for cat in cats:
        for ws in week_starts:
            if (cat, ws[:10]) in existing_overrides or (cat, ws) in existing_overrides:
                overridden_cats.add(cat)
                break

    if not overridden_cats:
        return

    st.caption('Manual overrides active:')
    cols = st.columns(min(len(overridden_cats), 4))
    for i, cat in enumerate(overridden_cats):
        col_idx = i % min(len(overridden_cats), 4)
        if cols[col_idx].button(
            f'Reset {cat_labels[cat]}',
            key=f'cf_reset_{key_prefix}_{cat}',
            type='secondary',
        ):
            try:
                with get_db() as conn:
                    conn.execute(
                        'DELETE FROM cashflow_overrides WHERE line_item = %s',
                        (cat,),
                    )
                st.success(f'Cleared overrides for {cat_labels[cat]}')
                st.rerun()
            except Exception as e:
                st.error(f'Failed to clear overrides: {e}')


def _save_edits(original_df, edited_df, label_to_cat, week_starts, is_actual_map):
    """Save changed cells to cashflow_overrides table."""
    changes = []
    for label in edited_df.index:
        cat = label_to_cat.get(label)
        if not cat:
            continue
        for ws in week_starts:
            col_key = ws[:10]
            if is_actual_map.get(ws, False):
                continue  # skip actuals
            orig_val = original_df.at[label, col_key] if label in original_df.index else 0
            new_val = edited_df.at[label, col_key]
            if orig_val != new_val:
                changes.append((cat, col_key, int(new_val)))

    if not changes:
        return

    try:
        with get_db() as conn:
            for cat, ws, amount in changes:
                # Upsert override
                conn.execute(
                    """INSERT INTO cashflow_overrides (line_item, week_start, override_amount)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (line_item, week_start)
                       DO UPDATE SET override_amount = %s""",
                    (cat, ws, amount, amount),
                )
        st.toast(f'Saved {len(changes)} override(s)')
    except Exception as e:
        st.error(f'Failed to save overrides: {e}')


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
