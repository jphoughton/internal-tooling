"""
Cash Flow Forecasting dashboard page.

52-week rolling cash flow model with projections from the P&L revenue model.

CFO perspective: This replaces the Google Sheets 13-week cash flow model.
Shows where cash is going, when it's tight, and what levers to pull.
"""
import json as _json_cf
import logging
from datetime import date, timedelta

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

from db import get_db, read_sql, get_cashflow_setting, set_cashflow_setting
from utils.constants import CASHFLOW_CATEGORIES

log = logging.getLogger(__name__)


@st.cache_data(ttl=86400)
def _load_cashflow_overrides():
    """Cached load of cashflow overrides for indicator dots."""
    try:
        with get_db() as conn:
            override_rows = read_sql(
                'SELECT line_item, week_start FROM cashflow_overrides', conn,
            )
            return set(
                (r['line_item'], r['week_start'])
                for _, r in override_rows.iterrows()
            )
    except Exception as e:
        log.warning('Failed to load cashflow overrides: %s', e)
        return set()


def _get_precomputed_cf(key):
    """Load precomputed result from DB, return parsed JSON or None."""
    try:
        from db import get_precomputed
        with get_db() as conn:
            cached = get_precomputed(conn, key, max_age_hours=25)
        if cached:
            return _json_cf.loads(cached)
    except Exception:
        pass
    return None


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
            current_idx = int(actuals.index[-1]) if not actuals.empty else int(display.index[0])
            cross_idx = int(cross_row.name)
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



def _render_kpi_row(kpis, horizon_weeks=13):
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

    # Compute data staleness
    stale_days = None
    if freshness:
        try:
            freshness_date = date.fromisoformat(str(freshness)[:10])
            stale_days = (date.today() - freshness_date).days
        except (ValueError, TypeError):
            pass

    if stale_days is not None and stale_days > 14:
        freshness_html = (
            f'<span style="font-size:0.7rem;color:#d97706;font-weight:600;">'
            f'Data is {stale_days} days old — upload fresh transactions</span>'
        )
    elif freshness:
        freshness_html = (
            f'<span style="font-size:0.7rem;color:#94a3b8;font-weight:400;">'
            f'as of {freshness}</span>'
        )
    else:
        freshness_html = ''

    burn = kpis['monthly_burn']
    burn_label = 'outflow' if burn > 0 else 'inflow'
    burn_color = '#dc2626' if burn > 0 else '#16a34a'

    runway = kpis['runway_weeks']
    runway_text = f'{runway}+ wks' if runway >= 52 else f'{runway} wks'
    runway_color = '#16a34a' if runway >= 26 else '#d97706' if runway >= 13 else '#dc2626'

    # Build horizon KPI (only show when horizon differs from 13 weeks)
    horizon_kpi_html = ''
    if horizon_weeks > 13:
        horizon_kpi_html = f'''<div>
        <div style="font-size:0.65rem;text-transform:uppercase;letter-spacing:0.08em;
                    color:#94a3b8;font-weight:600;margin-bottom:2px;">{horizon_weeks}-Week Projected</div>
        <div style="font-size:clamp(1.1rem, 2vw, 1.5rem);font-weight:700;color:{'#dc2626' if kpis['projected_52w'] < 0 else '#0F3557'};
                    letter-spacing:-0.02em;line-height:1.2;white-space:nowrap;">
          ${kpis['projected_52w']:,.0f}
        </div>
      </div>'''

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
        <div style="font-size:clamp(1.1rem, 2vw, 1.5rem);font-weight:700;color:{'#dc2626' if kpis['projected_13w'] < 0 else '#0F3557'};
                    letter-spacing:-0.02em;line-height:1.2;white-space:nowrap;">
          ${kpis['projected_13w']:,.0f}
        </div>
      </div>
      {horizon_kpi_html}
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

    # Build forecast — try precomputed first for base scenario
    try:
        _cf_precomputed = None
        if scenario_key == 'base':
            _cf_key = 'cashflow_forecast_52w' if horizon_weeks >= 26 else 'cashflow_forecast_13w'
            _cf_precomputed = _get_precomputed_cf(_cf_key)

        if _cf_precomputed is not None:
            forecast_df = pd.DataFrame(_cf_precomputed['df'])
            kpis = _cf_precomputed['kpis']
            if forecast_df.empty:
                st.info('No forecast data available. Check that the revenue model has inputs.')
                return
        else:
            with st.spinner('Building forecast...'):
                with get_db() as conn:
                    from analytics.cashflow import build_cashflow_forecast, get_cashflow_kpis

                    forecast_df = build_cashflow_forecast(
                        conn,
                        start_date=date.today() - timedelta(weeks=4),
                        weeks=horizon_weeks + 4,
                        scenario=scenario_key,
                    )

                    if forecast_df.empty:
                        st.info('No forecast data available. Check that the revenue model has inputs.')
                        return

                    kpis = get_cashflow_kpis(conn, forecast_df)
    except Exception as e:
        log.exception('Cash flow forecast error')
        st.error(
            'Unable to build the cash flow forecast. Check the revenue model '
            'inputs on the Variables page or database connectivity.'
        )
        return

    # 1. KPI row — most prominent, CFO looks here first
    _render_kpi_row(kpis, horizon_weeks=horizon_weeks)

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


@st.cache_data(ttl=86400)
def _load_transaction_detail():
    """Load actual transactions grouped by category, subcategory, and week.

    Returns dict: {category: {subcategory: {week_start_str: total_amount}}}
    """
    try:
        with get_db() as conn:
            df = read_sql('''
                SELECT category, COALESCE(subcategory, 'other') as subcategory,
                       tx_date, amount, direction
                FROM cashflow_transactions
                WHERE is_duplicate = 0 AND is_transfer = 0
                  AND category IS NOT NULL AND category != 'unmapped'
            ''', conn)
        if df.empty:
            return {}
        df['tx_date'] = pd.to_datetime(df['tx_date'])
        # Compute week_start (Monday) for each transaction
        df['week_start'] = (df['tx_date'] - pd.to_timedelta(df['tx_date'].dt.weekday, unit='d')).dt.strftime('%Y-%m-%d')
        # Sign: debits are positive outflows, credits are positive inflows
        df['signed'] = df.apply(
            lambda r: r['amount'] if r['direction'] == 'debit' else r['amount'], axis=1,
        )
        # Group by category, subcategory, week
        grouped = df.groupby(['category', 'subcategory', 'week_start'])['signed'].sum()
        result = {}
        for (cat, sub, ws), total in grouped.items():
            if cat not in result:
                result[cat] = {}
            if sub not in result[cat]:
                result[cat][sub] = {}
            result[cat][sub][ws] = round(total)
        return result
    except Exception as e:
        log.warning('Failed to load transaction detail: %s', e)
        return {}


def _render_editable_table(forecast_df: pd.DataFrame, horizon_weeks: int):
    """Render unified cash flow table with expandable transaction detail.

    One table with section headers, month/week/date header rows,
    alternating row colors, muted zeroes, and color-coded subtotals.
    Each line item row is expandable to show vendor/subcategory breakdown.
    Editing is available in a collapsed expander below.
    """
    display = forecast_df.head(horizon_weeks)
    today = date.today()

    revenue_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'revenue']
    expense_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'expense']
    cogs_debt_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'cogs_debt']
    all_cats = revenue_cats + expense_cats + cogs_debt_cats
    cat_labels = {k: v['label'] for k, v in CASHFLOW_CATEGORIES.items() if k in all_cats}

    week_starts = display['week_start'].tolist()
    is_actual_map = dict(zip(display['week_start'], display['is_actual']))

    # Identify current week
    current_week_col = None
    for ws in week_starts:
        ws_date = date.fromisoformat(ws[:10])
        we_date = ws_date + timedelta(days=6)
        if ws_date <= today <= we_date:
            current_week_col = ws[:10]
            break

    # Parse dates for header rows
    parsed_dates = []
    for ws in week_starts:
        d = date.fromisoformat(ws[:10])
        parsed_dates.append(d)

    # Group columns by month for colspan
    month_groups = []
    for d in parsed_dates:
        month_key = d.strftime('%B %Y')
        if month_groups and month_groups[-1][0] == month_key:
            month_groups[-1] = (month_key, month_groups[-1][1] + 1)
        else:
            month_groups.append((month_key, 1))

    # Build pivot data
    pivot = {}
    for cat in all_cats:
        row = {}
        for _, r in display.iterrows():
            ws = r['week_start']
            val = r.get(cat, 0)
            if val is None or (isinstance(val, float) and np.isnan(val)):
                val = 0
            row[ws[:10]] = round(val)
        pivot[cat] = row

    # Summary rows from forecast_df
    summary_keys = ['total_inflows', 'total_expenses', 'total_cogs_debt',
                    'total_outflows', 'net_cashflow', 'closing_balance']
    if 'loc_balance' in display.columns:
        summary_keys.insert(3, 'loc_balance')
    summary_data = {}
    for key in summary_keys:
        row = {}
        for _, r in display.iterrows():
            ws = r['week_start'][:10]
            val = r.get(key, 0)
            if val is None or (isinstance(val, float) and np.isnan(val)):
                val = 0
            row[ws] = round(val)
        summary_data[key] = row

    col_keys = [ws[:10] for ws in week_starts]
    n_cols = len(col_keys)

    # Load overrides for indicator dots (cached)
    existing_overrides = _load_cashflow_overrides()
    overridden_cat_keys = set()
    for cat in all_cats:
        for ws in week_starts:
            if (cat, ws[:10]) in existing_overrides or (cat, ws) in existing_overrides:
                overridden_cat_keys.add(cat)
                break

    # Load transaction detail for expandable rows
    tx_detail = _load_transaction_detail()

    # ── CSS — exact mockup styling, all !important to beat Streamlit ──
    # Streamlit dark theme bg is #0e1117. We use that + #111827 for stripes.
    css = '''<style>
    .cf-wrap{overflow-x:auto!important;-webkit-overflow-scrolling:touch!important;
      border:1px solid #1e2a3a!important;border-radius:8px!important;background:#0e1117!important;}
    .cf-tbl{border-collapse:collapse!important;font-size:0.78rem!important;
      width:100%!important;min-width:1200px!important;background:#0e1117!important;}
    .cf-tbl th,.cf-tbl td{font-variant-numeric:tabular-nums!important;border:none!important;}

    /* ── Header rows: month / week / date ── */
    .cf-month th{padding:6px 0!important;font-size:0.7rem!important;font-weight:700!important;
      text-transform:uppercase!important;letter-spacing:0.06em!important;color:#64748b!important;
      border-bottom:1px solid #1e2a3a!important;text-align:center!important;background:#0a0e14!important;}
    .cf-month th.ms{border-left:1px solid #253347!important;}
    .cf-wk th{padding:3px 0!important;font-size:0.65rem!important;font-weight:600!important;
      color:#475569!important;text-align:center!important;background:#0a0e14!important;}
    .cf-wk th.cf-ms{border-left:1px solid #253347!important;}
    .cf-dt th{padding:4px 10px!important;font-size:0.68rem!important;font-weight:500!important;
      color:#64748b!important;text-align:right!important;border-bottom:2px solid #1e2a3a!important;
      white-space:nowrap!important;background:#0a0e14!important;}
    .cf-dt th.cw{font-weight:700!important;color:#818cf8!important;}
    .cf-dt th.aw{color:#4ade80!important;}
    .cf-dt th.cf-ms{border-left:1px solid #253347!important;}

    /* ── Label column (sticky left) ── */
    .cf-lbl{text-align:left!important;padding-left:14px!important;font-weight:500!important;
      position:sticky!important;left:0!important;z-index:2!important;min-width:160px!important;
      background:#0a0e14!important;}
    td.cf-lbl{color:#e2e8f0!important;background:#0e1117!important;}

    /* ── Data cells ── */
    .cf-tbl td{padding:7px 10px!important;text-align:right!important;white-space:nowrap!important;
      border-bottom:1px solid #111827!important;color:#cbd5e1!important;}

    /* ── Alternating rows ── */
    .cf-even td{background:#0e1117!important;}
    .cf-odd td{background:#111827!important;}
    .cf-even td.cf-lbl{background:#0e1117!important;}
    .cf-odd td.cf-lbl{background:#111827!important;}

    /* ── Muted zero values ── */
    .cf-z{color:#334155!important;}

    /* ── Current week highlight ── */
    .cf-even td.cf-cw{background:rgba(99,110,250,0.06)!important;}
    .cf-odd td.cf-cw{background:rgba(99,110,250,0.08)!important;}

    /* ── Month boundaries ── */
    td.cf-ms{border-left:1px solid #253347!important;}

    /* ── Section headers (Revenue, Expenses, COGS & Debt) ── */
    .cf-sec td{padding:10px 14px 4px!important;font-size:0.7rem!important;text-transform:uppercase!important;
      letter-spacing:0.06em!important;font-weight:700!important;color:#64748b!important;
      border-bottom:none!important;text-align:left!important;background:#0e1117!important;}

    /* ── Subtotal rows ── */
    .cf-sub td{font-weight:700!important;border-top:1px solid #1e2a3a!important;
      border-bottom:1px solid #1e2a3a!important;padding:8px 10px!important;background:#111827!important;}
    .cf-sub td.cf-lbl{background:#111827!important;}
    .cf-sub-g td{color:#4ade80!important;}
    .cf-sub-r td{color:#f87171!important;}
    .cf-sub-a td{color:#fbbf24!important;}
    .cf-sub-m td{color:#94a3b8!important;}

    /* ── Summary rows (Net Cash Flow, Closing Balance) ── */
    .cf-sum td{font-weight:800!important;font-size:0.82rem!important;padding:9px 10px!important;
      border-bottom:1px solid #1e2a3a!important;background:#141c2b!important;}
    .cf-sum td.cf-lbl{background:#141c2b!important;}
    .cf-sum-net td{color:#fbbf24!important;}
    .cf-sum-bal td{color:#60a5fa!important;}

    /* ── Separator before summary ── */
    .cf-sep td{border-top:2px solid #334155!important;padding:0!important;height:2px!important;
      background:transparent!important;}

    /* ── Expandable parent rows ── */
    .cf-parent td.cf-lbl{cursor:pointer!important;}
    .cf-parent td.cf-lbl:hover{color:#818cf8!important;}
    .cf-arrow{display:inline-block;font-size:0.55rem;margin-right:6px;transition:transform 0.15s;
      color:#475569;vertical-align:middle;}
    .cf-parent.cf-open .cf-arrow{transform:rotate(90deg);}

    /* ── Child (vendor) rows — hidden by default ── */
    .cf-child{display:none;}
    .cf-child td{padding:5px 10px!important;font-size:0.72rem!important;color:#94a3b8!important;
      border-bottom:1px solid rgba(17,24,39,0.4)!important;}
    .cf-child td.cf-lbl{padding-left:30px!important;font-weight:400!important;color:#94a3b8!important;
      font-style:italic!important;}
    .cf-child.cf-even td{background:#0e1117!important;}
    .cf-child.cf-odd td{background:rgba(17,24,39,0.5)!important;}
    .cf-child.cf-even td.cf-lbl{background:#0e1117!important;}
    .cf-child.cf-odd td.cf-lbl{background:rgba(17,24,39,0.5)!important;}
    .cf-child .cf-z{color:rgba(51,65,85,0.3)!important;}
    </style>
    <script>
    function cfToggle(catId){
      var parent=document.getElementById('cf-p-'+catId);
      var children=document.querySelectorAll('.cf-ch-'+catId);
      if(parent.classList.contains('cf-open')){
        parent.classList.remove('cf-open');
        children.forEach(function(c){c.style.display='none';});
      }else{
        parent.classList.add('cf-open');
        children.forEach(function(c){c.style.display='';});
      }
    }
    </script>'''

    # ── Helper to format a value cell ──
    def _cell(val, ws, is_summary=False):
        classes = []
        if ws == current_week_col:
            classes.append('cf-cw')
        # Month boundary
        d = date.fromisoformat(ws)
        if d.day <= 7 and parsed_dates.index(d) > 0:
            prev_d = parsed_dates[parsed_dates.index(d) - 1]
            if prev_d.month != d.month:
                classes.append('cf-ms')
        if val == 0 and not is_summary:
            classes.append('cf-z')
        cls = f' class="{" ".join(classes)}"' if classes else ''
        if val < 0:
            formatted = f'-${abs(val):,.0f}'
        else:
            formatted = f'${val:,.0f}'
        return f'<td{cls}>{formatted}</td>'

    def _month_boundary_class(idx):
        """Return ' cf-ms' if this column is the first of a new month."""
        if idx > 0 and parsed_dates[idx].month != parsed_dates[idx - 1].month:
            return ' cf-ms'
        return ''

    # ── Build HTML ──
    h = [css, '<div class="cf-wrap"><table class="cf-tbl">']

    # Month header row
    h.append('<thead><tr class="cf-month"><th class="cf-lbl"></th>')
    first_in_group = True
    for month_name, colspan in month_groups:
        ms = ' ms' if not first_in_group else ''
        h.append(f'<th class="{ms}" colspan="{colspan}">{month_name}</th>')
        first_in_group = False
    h.append('</tr>')

    # Week number row
    h.append('<tr class="cf-wk"><th class="cf-lbl"></th>')
    for i, d in enumerate(parsed_dates):
        wk = d.isocalendar()[1]
        mc = _month_boundary_class(i)
        h.append(f'<th class="{mc}">W{wk}</th>')
    h.append('</tr>')

    # Date row
    h.append('<tr class="cf-dt"><th class="cf-lbl"></th>')
    for i, d in enumerate(parsed_dates):
        ws = col_keys[i]
        mc = _month_boundary_class(i)
        label = d.strftime('%b %-d')
        if ws == current_week_col:
            h.append(f'<th class="cw{mc}">{label}</th>')
        elif is_actual_map.get(week_starts[i], False):
            h.append(f'<th class="aw{mc}">{label}</th>')
        else:
            h.append(f'<th class="{mc}">{label}</th>')
    h.append('</tr></thead><tbody>')

    row_idx = 0  # for alternating colors

    def _data_row(cat_key, label, override_dot=False):
        nonlocal row_idx
        stripe = 'cf-even' if row_idx % 2 == 0 else 'cf-odd'
        dot = ' <span style="color:#818cf8;font-size:0.6rem;">&#9679;</span>' if override_dot else ''
        cells = ''.join(_cell(pivot[cat_key].get(ws, 0), ws) for ws in col_keys)
        row_idx += 1

        # Check if this category has transaction detail
        cat_subs = tx_detail.get(cat_key, {})
        if not cat_subs:
            return f'<tr class="{stripe}"><td class="cf-lbl">{label}{dot}</td>{cells}</tr>'

        # Parent row with toggle arrow
        safe_id = cat_key.replace(' ', '_').replace('/', '_')
        arrow = '<span class="cf-arrow">&#9654;</span>'
        parent = (f'<tr id="cf-p-{safe_id}" class="{stripe} cf-parent" '
                  f'onclick="cfToggle(\'{safe_id}\')">'
                  f'<td class="cf-lbl">{arrow}{label}{dot}</td>{cells}</tr>')

        # Child rows for each subcategory
        children = []
        child_idx = 0
        for sub_name in sorted(cat_subs.keys()):
            sub_data = cat_subs[sub_name]
            c_stripe = 'cf-even' if child_idx % 2 == 0 else 'cf-odd'
            sub_label = sub_name.replace('_', ' ').title()
            c_cells = []
            for ws in col_keys:
                val = sub_data.get(ws, 0)
                classes = []
                if ws == current_week_col:
                    classes.append('cf-cw')
                d = date.fromisoformat(ws)
                idx = parsed_dates.index(d)
                if idx > 0 and parsed_dates[idx - 1].month != d.month:
                    classes.append('cf-ms')
                if val == 0:
                    classes.append('cf-z')
                cls = f' class="{" ".join(classes)}"' if classes else ''
                formatted = f'-${abs(val):,.0f}' if val < 0 else f'${val:,.0f}'
                c_cells.append(f'<td{cls}>{formatted}</td>')
            children.append(
                f'<tr class="cf-child {c_stripe} cf-ch-{safe_id}">'
                f'<td class="cf-lbl">{sub_label}</td>{"".join(c_cells)}</tr>'
            )
            child_idx += 1

        return parent + ''.join(children)

    def _subtotal_row(key, label, color_class):
        cells = ''.join(_cell(summary_data[key].get(ws, 0), ws, is_summary=True) for ws in col_keys)
        return f'<tr class="cf-sub {color_class}"><td class="cf-lbl">{label}</td>{cells}</tr>'

    def _summary_row(key, label, cls):
        cells = ''.join(_cell(summary_data[key].get(ws, 0), ws, is_summary=True) for ws in col_keys)
        return f'<tr class="cf-sum {cls}"><td class="cf-lbl">{label}</td>{cells}</tr>'

    def _section_header(icon, label, color):
        return (f'<tr class="cf-sec"><td colspan="{n_cols + 1}">'
                f'<span style="color:{color};">{icon}</span> {label}</td></tr>')

    # Revenue section
    h.append(_section_header('&#9650;', 'Revenue', '#22c55e'))
    for cat in revenue_cats:
        h.append(_data_row(cat, cat_labels[cat], cat in overridden_cat_keys))
    h.append(_subtotal_row('total_inflows', 'Total Inflows', 'cf-sub-g'))

    # Expenses section
    h.append(_section_header('&#9660;', 'Expenses', '#ef4444'))
    row_idx = 0  # reset alternation per section
    for cat in expense_cats:
        h.append(_data_row(cat, cat_labels[cat], cat in overridden_cat_keys))
    h.append(_subtotal_row('total_expenses', 'Total Expenses', 'cf-sub-r'))

    # COGS & Debt section
    h.append(_section_header('&#9670;', 'COGS & Debt', '#f59e0b'))
    row_idx = 0
    for cat in cogs_debt_cats:
        h.append(_data_row(cat, cat_labels[cat], cat in overridden_cat_keys))
    h.append(_subtotal_row('total_cogs_debt', 'Total COGS & Debt', 'cf-sub-a'))

    # LOC Balance
    if 'loc_balance' in summary_data:
        h.append(_subtotal_row('loc_balance', 'LOC Balance', 'cf-sub-m'))

    # Separator + summary
    h.append(f'<tr class="cf-sep"><td colspan="{n_cols + 1}"></td></tr>')
    h.append(_subtotal_row('total_outflows', 'Total Outflows', 'cf-sub-r'))
    h.append(_summary_row('net_cashflow', 'Net Cash Flow', 'cf-sum-net'))
    h.append(_summary_row('closing_balance', 'Closing Balance', 'cf-sum-bal'))

    h.append('</tbody></table></div>')
    st.markdown(''.join(h), unsafe_allow_html=True)

    # Editing expander — keeps override editing functional
    _render_edit_expander(display, all_cats, cat_labels, revenue_cats, expense_cats,
                          cogs_debt_cats, week_starts, is_actual_map,
                          existing_overrides, overridden_cat_keys, current_week_col)


def _render_edit_expander(display, all_cats, cat_labels, revenue_cats, expense_cats,
                          cogs_debt_cats, week_starts, is_actual_map,
                          existing_overrides, overridden_cat_keys, current_week_col):
    """Collapsed expander for editing overrides and reset buttons."""
    # Build pivot for editors
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

    with st.expander('Edit Overrides', expanded=False):
        for group_cats, group_label, key_prefix in [
            (revenue_cats, 'Revenue', 'rev'),
            (expense_cats, 'Expenses', 'exp'),
            (cogs_debt_cats, 'COGS & Debt', 'cd'),
        ]:
            group_labels = [cat_labels[c] for c in group_cats]
            section_df = pivot_df.loc[pivot_df.index.isin(group_labels)].copy()
            _render_category_editor(section_df, group_cats, cat_labels, week_starts,
                                    is_actual_map, existing_overrides, overridden_cat_keys,
                                    current_week_col, key_prefix)


def _render_category_editor(section_df, cats, cat_labels, week_starts,
                            is_actual_map, existing_overrides, overridden_cat_keys,
                            current_week_col, key_prefix):
    """Render a data_editor for a group of categories inside the edit expander."""
    label_to_cat = {v: k for k, v in cat_labels.items() if k in cats}
    display_labels = {}
    for cat in cats:
        lbl = cat_labels[cat]
        if cat in overridden_cat_keys:
            display_labels[lbl] = f'{lbl}  \u25cf'
        else:
            display_labels[lbl] = lbl
    editor_df = section_df.rename(index=display_labels).astype(int)

    col_config = {}
    for ws in week_starts:
        col_key = ws[:10]
        is_actual = is_actual_map.get(ws, False)
        d = date.fromisoformat(col_key)
        label = d.strftime('%b %-d')
        if col_key == current_week_col:
            label = f'\u25b8 {label}'
        elif is_actual:
            label = f'{label} \u2713'
        col_config[col_key] = st.column_config.NumberColumn(
            label,
            format='dollar',
            step=1,
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
    reverse_display = {v: k for k, v in display_labels.items()}
    original_label_to_cat = {}
    for disp_label in editor_df.index:
        orig_label = reverse_display.get(disp_label, disp_label)
        cat = label_to_cat.get(orig_label)
        if cat:
            original_label_to_cat[disp_label] = cat

    if edited is not None and not edited.equals(editor_df):
        _save_edits(editor_df, edited, original_label_to_cat, week_starts, is_actual_map)

    # Reset buttons
    overridden = [c for c in cats if c in overridden_cat_keys]
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
                    _load_cashflow_overrides.clear()
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
            if pd.isna(new_val):
                continue
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
        _load_cashflow_overrides.clear()
        st.toast(f'Saved {len(changes)} override(s)')
        st.rerun()  # rebuild forecast with new overrides so totals update
    except Exception as e:
        st.error(f'Failed to save overrides: {e}')



def _render_settings_section():
    """Render cash flow model settings."""
    st.markdown('##### Model Parameters')

    try:
        with get_db() as conn:
            cogs_pct = get_cashflow_setting(conn, 'cogs_pct', '0.25')
            fulfill_pct = get_cashflow_setting(conn, 'fulfillment_pct', '0.18')
            min_cash = get_cashflow_setting(conn, 'min_cash_threshold', '100000')
            loc_balance = get_cashflow_setting(conn, 'loc_balance', '510000')
            loc_apr = get_cashflow_setting(conn, 'loc_apr', '0.1164')
            po_cost = get_cashflow_setting(conn, 'production_cost_per_unit', '40.00')
            payroll_a = get_cashflow_setting(conn, 'payroll_amount_a', '13000')
            payroll_b = get_cashflow_setting(conn, 'payroll_amount_b', '15500')
            loan_principal = get_cashflow_setting(conn, 'loan_default_principal', '30000')
            media_split = get_cashflow_setting(conn, 'media_split_early_pct', '0.40')
            sales_tax_rate = get_cashflow_setting(conn, 'sales_tax_rate', '0.043')
            sales_tax_default = get_cashflow_setting(conn, 'sales_tax_default', '13000')
            shipping_pct = get_cashflow_setting(conn, 'shipping_pct', '0.02')
            consulting = get_cashflow_setting(conn, 'consulting_monthly', '0')
            opening_cash = get_cashflow_setting(conn, 'opening_cash_balance', '153000')
    except Exception as e:
        log.warning('Failed to load cashflow settings, using defaults: %s', e)
        cogs_pct, fulfill_pct = '0.25', '0.18'
        min_cash, loc_balance, loc_apr, po_cost = '100000', '510000', '0.1164', '40.00'
        payroll_a, payroll_b, loan_principal = '13000', '15500', '30000'
        media_split, sales_tax_rate, sales_tax_default = '0.40', '0.043', '13000'
        shipping_pct, consulting = '0.02', '0'
        opening_cash = '153000'

    with st.form('cf_settings_form'):
        # Row 1: COGS, Fulfillment, Production Cost
        cols = st.columns(3)
        new_cogs = cols[0].number_input(
            'COGS % (fallback)', value=float(cogs_pct) * 100,
            min_value=5.0, max_value=60.0, step=1.0, format='%.0f',
        )
        new_fulfill = cols[1].number_input(
            'Fulfillment % of DTC (fallback)', value=float(fulfill_pct) * 100,
            min_value=5.0, max_value=50.0, step=1.0, format='%.0f',
        )
        new_po_cost = cols[2].number_input(
            'Production Cost / Unit ($)', value=float(po_cost),
            min_value=1.0, max_value=200.0, step=1.0, format='%.2f',
        )

        # Row 2: Min Cash, LOC Balance, Loan APR
        cols2 = st.columns(3)
        new_min = cols2[0].number_input(
            'Min Cash Threshold ($)', value=float(min_cash),
            min_value=0.0, step=10000.0, format='%.0f',
        )
        new_loc = cols2[1].number_input(
            'LOC Balance ($)', value=float(loc_balance),
            min_value=0.0, step=10000.0, format='%.0f',
        )
        new_apr = cols2[2].number_input(
            'Loan APR %', value=float(loc_apr) * 100,
            min_value=0.0, max_value=30.0, step=0.5, format='%.1f',
        )

        # Row 3: Payroll A, Payroll B, Loan Principal
        cols3 = st.columns(3)
        new_payroll_a = cols3[0].number_input(
            'Payroll A ($)', value=float(payroll_a),
            min_value=0.0, step=500.0, format='%.0f',
            help='Biweekly smaller payroll (e.g. Justworks base)',
        )
        new_payroll_b = cols3[1].number_input(
            'Payroll B ($)', value=float(payroll_b),
            min_value=0.0, step=500.0, format='%.0f',
            help='Biweekly larger payroll (e.g. Justworks + VAs)',
        )
        new_loan_principal = cols3[2].number_input(
            'Loan Principal Default ($)', value=float(loan_principal),
            min_value=0.0, step=5000.0, format='%.0f',
            help='Monthly loan principal when no specific amount is set',
        )

        # Row 4: Media Split, Sales Tax Rate, Shipping %
        cols4 = st.columns(3)
        new_media_split = cols4[0].number_input(
            'Media Split Early %', value=float(media_split) * 100,
            min_value=0.0, max_value=100.0, step=5.0, format='%.0f',
            help='How much of monthly media spend hits early in month',
        )
        new_sales_tax_rate = cols4[1].number_input(
            'Sales Tax Rate %', value=float(sales_tax_rate) * 100,
            min_value=0.0, max_value=15.0, step=0.1, format='%.1f',
            help='Sales tax as % of net sales (variable)',
        )
        new_shipping_pct = cols4[2].number_input(
            'Shipping % of DTC Rev', value=float(shipping_pct) * 100,
            min_value=0.0, max_value=20.0, step=0.5, format='%.1f',
            help='Shipping cost as % of DTC revenue (variable)',
        )

        # Row 5: Opening Cash, Consulting, Sales Tax Default
        cols5 = st.columns(3)
        new_opening_cash = cols5[0].number_input(
            'Opening Cash Balance ($)', value=float(opening_cash),
            min_value=0.0, step=10000.0, format='%.0f',
            help='Starting cash balance for the forecast',
        )
        new_consulting = cols5[1].number_input(
            'Consulting Monthly ($)', value=float(consulting),
            min_value=0.0, step=500.0, format='%.0f',
            help='Monthly consulting expense (set to 0 when not active)',
        )
        new_sales_tax = cols5[2].number_input(
            'Sales Tax Fallback ($)', value=float(sales_tax_default),
            min_value=0.0, step=1000.0, format='%.0f',
            help='Flat monthly fallback if sales tax rate is 0',
        )

        if st.form_submit_button('Save Settings', type='primary'):
            with get_db() as conn:
                set_cashflow_setting(conn, 'cogs_pct', str(new_cogs / 100))
                set_cashflow_setting(conn, 'fulfillment_pct', str(new_fulfill / 100))
                set_cashflow_setting(conn, 'production_cost_per_unit', str(new_po_cost))
                set_cashflow_setting(conn, 'min_cash_threshold', str(new_min))
                set_cashflow_setting(conn, 'loc_balance', str(new_loc))
                set_cashflow_setting(conn, 'loc_apr', str(new_apr / 100))
                set_cashflow_setting(conn, 'payroll_amount_a', str(new_payroll_a))
                set_cashflow_setting(conn, 'payroll_amount_b', str(new_payroll_b))
                set_cashflow_setting(conn, 'loan_default_principal', str(new_loan_principal))
                set_cashflow_setting(conn, 'media_split_early_pct', str(new_media_split / 100))
                set_cashflow_setting(conn, 'sales_tax_rate', str(new_sales_tax_rate / 100))
                set_cashflow_setting(conn, 'sales_tax_default', str(new_sales_tax))
                set_cashflow_setting(conn, 'shipping_pct', str(new_shipping_pct / 100))
                set_cashflow_setting(conn, 'consulting_monthly', str(new_consulting))
                set_cashflow_setting(conn, 'opening_cash_balance', str(new_opening_cash))
            st.success('Settings saved!')
            st.rerun()


