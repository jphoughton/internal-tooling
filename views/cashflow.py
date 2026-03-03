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
    """Build the main cash balance area chart with confidence bands.

    Answers "are we going to be okay?" in one glance:
    - Actuals: thick solid line with filled markers (trustworthy)
    - Projections: thin dashed line with open markers (estimates)
    - Threshold: prominent dashed red line (danger zone)
    - Zone shading: green/yellow/red background bands
    - Hover: week date + closing balance + net cash flow
    """
    display = df.head(horizon_weeks)
    today = date.today()

    fig = go.Figure()

    # ── Zone shading (green / yellow / red background bands) ──
    # Compute y-axis range for zone rectangles
    y_vals = display['closing_balance'].tolist()
    if 'confidence_upper' in display.columns:
        y_vals += display['confidence_upper'].dropna().tolist()
    y_max = max(y_vals) if y_vals else min_threshold * 3
    y_max = max(y_max, min_threshold * 1.5) * 1.15  # pad top 15%
    yellow_floor = min_threshold * 0.8  # 20% below threshold

    # Red zone: below threshold
    fig.add_hrect(
        y0=0, y1=min_threshold,
        fillcolor='rgba(239, 68, 68, 0.04)',
        line_width=0,
        layer='below',
    )
    # Yellow zone: within 20% above threshold
    fig.add_hrect(
        y0=min_threshold, y1=min_threshold * 1.2,
        fillcolor='rgba(245, 158, 11, 0.04)',
        line_width=0,
        layer='below',
    )

    # ── Confidence band (projected weeks only) ──
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
            fillcolor='rgba(99, 110, 250, 0.08)',
            name='Confidence range',
            showlegend=True,
            hoverinfo='skip',
        ))

    # ── Actual balance line — thick, solid, filled markers ──
    _pos_span = '<span style="color:#22c55e">+'
    _neg_span = '<span style="color:#ef4444">'

    actuals = display[display['is_actual'] == True]  # noqa: E712
    if not actuals.empty:
        hover_actual = [
            f"<b>Week of {ws}</b><br>"
            f"Closing: <b>${cb:,.0f}</b><br>"
            f"Net: {_pos_span if nc >= 0 else _neg_span}"
            f"${abs(nc):,.0f}</span>"
            for ws, cb, nc in zip(
                actuals['week_start'], actuals['closing_balance'], actuals['net_cashflow']
            )
        ]
        fig.add_trace(go.Scatter(
            x=pd.to_datetime(actuals['week_start']),
            y=actuals['closing_balance'],
            mode='lines+markers',
            name='Actual',
            line=dict(color='#16a34a', width=3),
            marker=dict(size=7, symbol='circle', color='#16a34a',
                        line=dict(width=1.5, color='white')),
            hovertemplate='%{customdata}<extra></extra>',
            customdata=hover_actual,
        ))

    # ── Projected balance line — thin, dashed, open markers ──
    projected = display[display['is_actual'] == False]  # noqa: E712
    if not projected.empty:
        # Bridge from last actual so lines connect
        if not actuals.empty:
            bridge = pd.concat([actuals.tail(1), projected])
        else:
            bridge = projected
        hover_proj = [
            f"<b>Week of {ws}</b><br>"
            f"Closing: <b>${cb:,.0f}</b><br>"
            f"Net: {_pos_span if nc >= 0 else _neg_span}"
            f"${abs(nc):,.0f}</span>"
            for ws, cb, nc in zip(
                bridge['week_start'], bridge['closing_balance'], bridge['net_cashflow']
            )
        ]
        fig.add_trace(go.Scatter(
            x=pd.to_datetime(bridge['week_start']),
            y=bridge['closing_balance'],
            mode='lines+markers',
            name='Projected',
            line=dict(color='#818cf8', width=1.5, dash='dash'),
            marker=dict(size=5, symbol='circle-open', color='#818cf8',
                        line=dict(width=1.5, color='#818cf8')),
            hovertemplate='%{customdata}<extra></extra>',
            customdata=hover_proj,
        ))

    # ── Min threshold line — prominent dashed red ──
    fig.add_hline(
        y=min_threshold,
        line_dash='dash',
        line_color='#ef4444',
        line_width=2,
        annotation_text=f'Min Cash ${min_threshold:,.0f}',
        annotation_position='top left',
        annotation_font=dict(color='#ef4444', size=11, family='Arial'),
    )

    # ── Threshold crossing annotation ──
    # Find first projected week that dips below threshold
    if not projected.empty:
        below = projected[projected['closing_balance'] < min_threshold]
        if not below.empty:
            cross_row = below.iloc[0]
            cross_date = pd.to_datetime(cross_row['week_start'])
            cross_val = cross_row['closing_balance']
            # Week number relative to current week
            current_idx = actuals.index[-1] if not actuals.empty else display.index[0]
            cross_idx = cross_row.name
            week_num = cross_idx - current_idx
            fig.add_trace(go.Scatter(
                x=[cross_date],
                y=[cross_val],
                mode='markers',
                marker=dict(size=12, symbol='x', color='#ef4444',
                            line=dict(width=2, color='#ef4444')),
                showlegend=False,
                hoverinfo='skip',
            ))
            fig.add_annotation(
                x=cross_date,
                y=cross_val,
                text=f'Week {week_num}: ${cross_val:,.0f}',
                showarrow=True,
                arrowhead=0,
                arrowcolor='#ef4444',
                ax=0, ay=-35,
                font=dict(color='#ef4444', size=11),
                bgcolor='rgba(255,255,255,0.9)',
                bordercolor='#ef4444',
                borderwidth=1,
                borderpad=3,
            )

    # ── Today marker ──
    fig.add_vline(
        x=str(today),
        line_dash='dash',
        line_color='rgba(15,53,87,0.15)',
        line_width=1,
    )
    fig.add_annotation(
        x=str(today),
        y=1,
        yref='paper',
        text='Today',
        showarrow=False,
        font=dict(color='#94a3b8', size=10),
        yanchor='bottom',
    )

    fig.update_layout(
        height=400,
        margin=dict(l=0, r=0, t=20, b=0),
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#2c3e50', size=11),
        xaxis=dict(
            gridcolor='#E8EDF3',
            showgrid=False,
            tickformat='%b %d',
        ),
        yaxis=dict(
            gridcolor='#E8EDF3',
            showgrid=True,
            griddash='dot',
            tickformat='$,.0f',
            rangemode='tozero',
        ),
        legend=dict(
            orientation='h',
            yanchor='bottom',
            y=1.02,
            xanchor='right',
            x=1,
            font=dict(size=11),
            itemsizing='constant',
        ),
        hovermode='x unified',
        hoverlabel=dict(
            bgcolor='white',
            font_size=12,
            font_color='#2c3e50',
        ),
    )

    # Remove Plotly logo / modebar clutter — applied at render time via config
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
        font=dict(color='#2c3e50', size=11),
        barmode='relative',
        xaxis=dict(gridcolor='#E8EDF3'),
        yaxis=dict(
            gridcolor='#E8EDF3',
            tickformat='$,.0f',
        ),
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
        hovermode='x unified',
    )
    return fig



def _render_kpi_row(kpis):
    """Render the KPI row as the most prominent visual element.

    Current Cash is largest. Color indicator: green above threshold,
    yellow within 20%, red below.
    """
    current = kpis['current_cash']
    threshold = kpis['min_cash_threshold']
    freshness = kpis.get('balance_freshness_date', '')

    # Color indicator for Current Cash
    if current >= threshold * 1.2:
        cash_color = '#16a34a'  # green — healthy
    elif current >= threshold:
        cash_color = '#d97706'  # amber — watch
    else:
        cash_color = '#dc2626'  # red — below threshold

    freshness_html = (
        f'<span style="font-size:0.7rem;color:#94a3b8;font-weight:400;">'
        f'as of {freshness}</span>'
        if freshness else ''
    )

    burn = kpis['monthly_burn']
    burn_label = 'outflow' if burn > 0 else 'inflow'
    burn_color = '#dc2626' if burn > 0 else '#16a34a'

    runway = kpis['runway_weeks']
    runway_text = f'{runway}+ wks' if runway >= 52 else f'{runway} wks'
    runway_color = '#16a34a' if runway >= 26 else '#d97706' if runway >= 13 else '#dc2626'

    st.markdown(f'''
    <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(130px, 1fr));
                gap:16px;margin:8px 0 20px;">
      <div style="grid-column:span 1;">
        <div style="font-size:0.65rem;text-transform:uppercase;letter-spacing:0.08em;
                    color:#94a3b8;font-weight:600;margin-bottom:2px;">Current Cash</div>
        <div style="font-size:clamp(1.4rem, 3vw, 2.2rem);font-weight:800;color:{cash_color};
                    letter-spacing:-0.03em;line-height:1.1;white-space:nowrap;">
          ${current:,.0f}
        </div>
        {freshness_html}
      </div>
      <div>
        <div style="font-size:0.65rem;text-transform:uppercase;letter-spacing:0.08em;
                    color:#94a3b8;font-weight:600;margin-bottom:2px;">13-Week Projected</div>
        <div style="font-size:clamp(1.1rem, 2vw, 1.5rem);font-weight:700;color:#0F3557;
                    letter-spacing:-0.02em;line-height:1.2;white-space:nowrap;">
          ${kpis['projected_13w']:,.0f}
        </div>
      </div>
      <div>
        <div style="font-size:0.65rem;text-transform:uppercase;letter-spacing:0.08em;
                    color:#94a3b8;font-weight:600;margin-bottom:2px;">52-Week Projected</div>
        <div style="font-size:clamp(1.1rem, 2vw, 1.5rem);font-weight:700;color:#0F3557;
                    letter-spacing:-0.02em;line-height:1.2;white-space:nowrap;">
          ${kpis['projected_52w']:,.0f}
        </div>
      </div>
      <div>
        <div style="font-size:0.65rem;text-transform:uppercase;letter-spacing:0.08em;
                    color:#94a3b8;font-weight:600;margin-bottom:2px;">Monthly Burn</div>
        <div style="font-size:clamp(1.1rem, 2vw, 1.5rem);font-weight:700;color:#0F3557;
                    letter-spacing:-0.02em;line-height:1.2;white-space:nowrap;">
          ${abs(burn):,.0f}
        </div>
        <span style="font-size:0.7rem;color:{burn_color};font-weight:600;">{burn_label}</span>
      </div>
      <div>
        <div style="font-size:0.65rem;text-transform:uppercase;letter-spacing:0.08em;
                    color:#94a3b8;font-weight:600;margin-bottom:2px;">Runway</div>
        <div style="font-size:clamp(1.1rem, 2vw, 1.5rem);font-weight:700;color:{runway_color};
                    letter-spacing:-0.02em;line-height:1.2;white-space:nowrap;">
          {runway_text}
        </div>
      </div>
    </div>
    ''', unsafe_allow_html=True)


def render(ctx):
    """Render the Cash Flow Forecasting page."""
    st.markdown('## Cash Flow Forecast')

    # Build forecast first (need KPIs before rendering anything)
    # Controls row — secondary, filter-style
    ctrl_cols = st.columns([1, 1, 4])
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

    # 1. KPI row — most prominent, CFO looks here first
    _render_kpi_row(kpis)

    # 2. Alert banner — only when something needs attention
    if kpis.get('alert_week'):
        week_num = kpis['alert_week']
        alert_rows = forecast_df[forecast_df['week_num'] == week_num]
        if not alert_rows.empty:
            alert_balance = alert_rows.iloc[0]['closing_balance']
            st.warning(
                'Cash projected below \\$%s in **Week %d** (est. \\$%s)'
                % (f"{kpis['min_cash_threshold']:,.0f}", week_num, f"{alert_balance:,.0f}"),
                icon='\u26a0\ufe0f',
            )

    # 3. Chart — tells the story at a glance
    st.markdown(
        '<p style="font-size:0.72rem;text-transform:uppercase;letter-spacing:0.05em;'
        'color:#94a3b8;font-weight:600;margin:24px 0 4px;">Projected Cash Balance</p>',
        unsafe_allow_html=True,
    )
    fig = _build_balance_chart(forecast_df, kpis['min_cash_threshold'], horizon_weeks + 4)
    st.plotly_chart(
        fig, use_container_width=True,
        config={'displaylogo': False, 'modeBarButtonsToRemove': ['lasso2d', 'select2d']},
    )

    # Breathing room
    st.markdown('<div style="margin-top:20px;"></div>', unsafe_allow_html=True)

    # 4. Table — detail layer, reference material
    st.markdown(
        '<p style="font-size:0.72rem;text-transform:uppercase;letter-spacing:0.05em;'
        'color:#94a3b8;font-weight:600;margin:0 0 4px;">Weekly Detail</p>',
        unsafe_allow_html=True,
    )
    _render_editable_table(forecast_df, horizon_weeks + 4)

    # Secondary sections — collapsed by default
    st.markdown('<div style="margin-top:24px;"></div>', unsafe_allow_html=True)
    with st.expander('Inflows vs Outflows', expanded=False):
        fig2 = _build_inflow_outflow_chart(forecast_df, horizon_weeks + 4)
        st.plotly_chart(fig2, use_container_width=True)

    with st.expander('Model Settings', expanded=False):
        _render_settings_section()

    with st.expander('Upload Transactions', expanded=False):
        _render_upload_section()

    with st.expander('Transaction Mappings', expanded=False):
        _render_mapping_link()


def _render_editable_table(forecast_df: pd.DataFrame, horizon_weeks: int):
    """Render an editable cash flow table with per-cell persistence.

    Past weeks (actuals) are read-only. Future weeks are editable.
    Edits persist to cashflow_overrides and are used in projections.
    Overridden rows show an inline reset link.
    """
    display = forecast_df.head(horizon_weeks)
    today = date.today()

    revenue_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'revenue']
    expense_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'expense']
    all_cats = revenue_cats + expense_cats
    cat_labels = {k: v['label'] for k, v in CASHFLOW_CATEGORIES.items() if k in all_cats}

    week_starts = display['week_start'].tolist()
    is_actual_map = dict(zip(display['week_start'], display['is_actual']))

    # Identify current week (the week containing today)
    current_week_col = None
    for ws in week_starts:
        ws_date = date.fromisoformat(ws[:10])
        we_date = ws_date + timedelta(days=6)
        if ws_date <= today <= we_date:
            current_week_col = ws[:10]
            break

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
    except Exception as e:
        log.warning('Failed to load cashflow overrides in view: %s', e)

    # Build set of categories that have overrides (for row labels)
    overridden_cat_keys = set()
    for cat in all_cats:
        for ws in week_starts:
            if (cat, ws[:10]) in existing_overrides or (cat, ws) in existing_overrides:
                overridden_cat_keys.add(cat)
                break

    # Section headers
    rev_labels = [cat_labels[c] for c in revenue_cats]
    exp_labels = [cat_labels[c] for c in expense_cats]

    # Render revenue section
    st.markdown(
        '<p style="font-size:0.72rem;text-transform:uppercase;letter-spacing:0.05em;'
        'color:#6b7c93;font-weight:700;margin:12px 0 4px;">'
        '<span style="color:#22c55e;">&#9650;</span> Revenue</p>',
        unsafe_allow_html=True,
    )
    rev_df = pivot_df.loc[pivot_df.index.isin(rev_labels)].copy()
    _render_category_section(rev_df, revenue_cats, cat_labels, week_starts,
                             is_actual_map, existing_overrides, overridden_cat_keys,
                             current_week_col, '#22c55e', 'rev')

    # Total inflows row
    _render_total_row(display, 'total_inflows', 'Total Inflows', '#16a34a',
                      current_week_col)

    # Render expense section
    st.markdown(
        '<p style="font-size:0.72rem;text-transform:uppercase;letter-spacing:0.05em;'
        'color:#6b7c93;font-weight:700;margin:12px 0 4px;">'
        '<span style="color:#ef4444;">&#9660;</span> Expenses</p>',
        unsafe_allow_html=True,
    )
    exp_df = pivot_df.loc[pivot_df.index.isin(exp_labels)].copy()
    _render_category_section(exp_df, expense_cats, cat_labels, week_starts,
                             is_actual_map, existing_overrides, overridden_cat_keys,
                             current_week_col, '#ef4444', 'exp')

    # Total outflows row
    _render_total_row(display, 'total_outflows', 'Total Outflows', '#dc2626',
                      current_week_col)

    # Summary separator
    st.markdown(
        '<div style="border-top:2px solid #e2e8f0;margin:12px 0 8px;"></div>',
        unsafe_allow_html=True,
    )
    _render_total_row(display, 'net_cashflow', 'Net Cash Flow', '#d97706',
                      current_week_col, bold_bg=True)
    _render_total_row(display, 'closing_balance', 'Closing Balance', '#0F3557',
                      current_week_col, bold_bg=True)


def _render_category_section(section_df, cats, cat_labels, week_starts,
                             is_actual_map, existing_overrides, overridden_cat_keys,
                             current_week_col, color, key_prefix):
    """Render an editable data_editor section for a group of categories."""
    # Modify index to show override indicator on rows with manual overrides
    label_to_cat = {v: k for k, v in cat_labels.items() if k in cats}
    display_labels = {}
    for cat in cats:
        lbl = cat_labels[cat]
        if cat in overridden_cat_keys:
            display_labels[lbl] = f'{lbl}  ●'
        else:
            display_labels[lbl] = lbl
    editor_df = section_df.rename(index=display_labels).astype(int)

    # Configure columns: actuals are disabled, future are editable
    # Current week gets a "▸" indicator
    col_config = {}
    for ws in week_starts:
        col_key = ws[:10]
        is_actual = is_actual_map.get(ws, False)
        if col_key == current_week_col:
            label = f'▸ {col_key}'
        elif is_actual:
            label = f'{col_key} ✓'
        else:
            label = col_key
        col_config[col_key] = st.column_config.NumberColumn(
            label,
            format='$,.0f',
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

    # Detect changes and save overrides — map display labels back to original
    reverse_display = {v: k for k, v in display_labels.items()}
    original_label_to_cat = {}
    for disp_label in editor_df.index:
        orig_label = reverse_display.get(disp_label, disp_label)
        cat = label_to_cat.get(orig_label)
        if cat:
            original_label_to_cat[disp_label] = cat

    if edited is not None and not edited.equals(editor_df):
        _save_edits(editor_df, edited, original_label_to_cat, week_starts, is_actual_map)

    # Per-row reset buttons — inline, compact, only for rows with overrides
    _render_inline_resets(cats, cat_labels, overridden_cat_keys, key_prefix)


def _render_total_row(display, col_key, label, color, current_week_col=None,
                      bold_bg=False):
    """Render a read-only summary total row as HTML.

    bold_bg: If True, adds a subtle background to make summary rows
    (Net Cash Flow, Closing Balance) feel like results, not inputs.
    current_week_col: Highlights the current week's cell with a subtle stripe.
    """
    bg_style = 'background:#f1f5f9;border-radius:4px;' if bold_bg else ''
    label_bg = '#f1f5f9' if bold_bg else '#ffffff'
    cells = ''
    for _, r in display.iterrows():
        val = r.get(col_key, 0)
        if val is None or (isinstance(val, float) and np.isnan(val)):
            val = 0
        formatted = f'-${abs(val):,.0f}' if val < 0 else f'${val:,.0f}'
        ws = str(r.get('week_start', ''))[:10]
        cell_bg = 'background:rgba(99,110,250,0.06);' if ws == current_week_col else ''
        cells += (
            f'<td style="text-align:right;padding:6px 8px;font-weight:700;'
            f'color:{color};white-space:nowrap;{cell_bg}">{formatted}</td>'
        )

    html = (
        f'<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;{bg_style}margin:2px 0;">'
        f'<table style="border-collapse:collapse;font-size:0.75rem;min-width:max-content;">'
        f'<tr><td style="padding:6px 8px;font-weight:700;color:{color};'
        f'white-space:nowrap;position:sticky;left:0;background:{label_bg};'
        f'z-index:1;min-width:120px;">{label}</td>'
        f'{cells}</tr></table></div>'
    )
    st.markdown(html, unsafe_allow_html=True)


def _render_inline_resets(cats, cat_labels, overridden_cat_keys, key_prefix):
    """Render compact per-row reset links for categories with manual overrides.

    Only renders for categories that have active overrides. Each reset link
    appears on a single line with the category name, making the connection
    between the row and its reset action obvious.
    """
    overridden = [c for c in cats if c in overridden_cat_keys]
    if not overridden:
        return

    for cat in overridden:
        cols = st.columns([6, 1])
        with cols[1]:
            if st.button(
                f'Reset {cat_labels[cat]}',
                key=f'cf_reset_{key_prefix}_{cat}',
                type='tertiary',
                use_container_width=True,
            ):
                try:
                    with get_db() as conn:
                        conn.execute(
                            'DELETE FROM cashflow_overrides WHERE line_item = %s',
                            (cat,),
                        )
                    st.toast(f'Reset {cat_labels[cat]} to auto')
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
        st.rerun()  # rebuild forecast with new overrides so totals update
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
    except Exception as e:
        log.warning('Failed to load cashflow settings, using defaults: %s', e)
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
