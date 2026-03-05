"""3PL Costs subtab — upload Robo3PL invoices, view WoW cost vs DTC revenue."""
import json
import logging

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from db import get_db, read_sql, upsert_threpl_invoice

log = logging.getLogger(__name__)


def render(ctx, embedded=False):
    """Render the 3PL Costs view with invoice upload and WoW analysis."""
    if not embedded:
        st.header('3PL Costs')

    # ── Upload section ──
    st.subheader('Upload Invoice')
    uploaded = st.file_uploader(
        'Upload a Robo3PL invoice PDF',
        type=['pdf'],
        help='Filename should contain the week range, e.g. "Robo3PL - Hydrant - Invoice 1310 - 2_16_2026-2_22_2026.pdf"',
        key='threpl_invoice_upload',
    )

    if uploaded is not None:
        _handle_upload(uploaded)

    st.divider()

    # ── WoW analysis ──
    st.subheader('Week-over-Week: 3PL Cost vs DTC Revenue')
    _render_wow_analysis()


def _handle_upload(uploaded):
    """Parse uploaded PDF and save to DB."""
    from etl.invoice_parser import parse_robo3pl_invoice

    pdf_bytes = uploaded.read()
    filename = uploaded.name

    try:
        data = parse_robo3pl_invoice(pdf_bytes, filename)
    except Exception as e:
        st.error(f'Failed to parse invoice: {e}')
        return

    if not data['invoice_number']:
        st.error('Could not find invoice number in PDF.')
        return
    if not data['week_start'] or not data['week_end']:
        st.error('Could not extract week range from filename. Expected format: M_D_YYYY-M_D_YYYY')
        return

    # Show preview
    st.success(
        f"Invoice #{data['invoice_number']} — "
        f"Week of {data['week_start']} to {data['week_end']} — "
        f"Total: ${data['total_amount']:,.2f}"
    )

    if data['line_items']:
        li_df = pd.DataFrame(data['line_items'])
        li_df.columns = ['Activity', 'Qty', 'Rate', 'Amount']
        li_df['Rate'] = li_df['Rate'].map('${:,.2f}'.format)
        li_df['Amount'] = li_df['Amount'].map('${:,.2f}'.format)
        li_df['Qty'] = li_df['Qty'].map('{:,}'.format)
        st.dataframe(li_df, use_container_width=True, hide_index=True)

    # Save to DB
    try:
        with get_db() as conn:
            upsert_threpl_invoice(
                conn,
                invoice_number=data['invoice_number'],
                week_start=data['week_start'],
                week_end=data['week_end'],
                invoice_date=data['invoice_date'],
                total_amount=data['total_amount'],
                line_items_json=json.dumps(data['line_items']),
            )
        st.success('Invoice saved to database.')
    except Exception as e:
        st.error(f'Failed to save invoice: {e}')


@st.cache_data(ttl=300)
def _load_invoice_data():
    """Load all 3PL invoices from DB."""
    with get_db() as conn:
        return read_sql(
            'SELECT invoice_number, week_start, week_end, invoice_date, '
            'total_amount, line_items FROM threpl_invoices ORDER BY week_start',
            conn,
        )


@st.cache_data(ttl=300)
def _load_dtc_weekly_revenue():
    """Load DTC revenue aggregated by week (Mon-Sun)."""
    with get_db() as conn:
        return read_sql("""
            SELECT
                DATE_TRUNC('week', sale_date::date)::date::text AS week_start,
                SUM(revenue) AS dtc_revenue
            FROM daily_sku_sales
            WHERE source = 'shopify'
            GROUP BY DATE_TRUNC('week', sale_date::date)
            ORDER BY week_start
        """, conn)


def _render_wow_analysis():
    """Render the week-over-week cost analysis."""
    try:
        inv_df = _load_invoice_data()
    except Exception:
        inv_df = pd.DataFrame()

    if inv_df.empty:
        st.info('No invoices uploaded yet. Upload a Robo3PL invoice above to get started.')
        return

    try:
        rev_df = _load_dtc_weekly_revenue()
    except Exception:
        rev_df = pd.DataFrame()

    # Build weekly cost table
    inv_df['week_start'] = pd.to_datetime(inv_df['week_start'])
    cost_weekly = inv_df[['week_start', 'total_amount']].copy()
    cost_weekly = cost_weekly.rename(columns={'total_amount': 'threpl_cost'})

    # Parse line items for category breakdown
    categories = []
    for _, row in inv_df.iterrows():
        try:
            items = json.loads(row['line_items']) if row['line_items'] else []
        except (json.JSONDecodeError, TypeError):
            items = []
        for item in items:
            categories.append({
                'week_start': row['week_start'],
                'activity': item['activity'],
                'amount': item['amount'],
            })
    cat_df = pd.DataFrame(categories) if categories else pd.DataFrame()

    # Merge with DTC revenue
    if not rev_df.empty:
        rev_df['week_start'] = pd.to_datetime(rev_df['week_start'])
        merged = cost_weekly.merge(rev_df, on='week_start', how='left')
    else:
        merged = cost_weekly.copy()
        merged['dtc_revenue'] = 0

    merged = merged.sort_values('week_start')
    merged['cost_pct'] = (merged['threpl_cost'] / merged['dtc_revenue'] * 100).round(2)
    merged['cost_pct'] = merged['cost_pct'].fillna(0)

    # WoW changes
    merged['cost_wow'] = merged['threpl_cost'].pct_change() * 100
    merged['rev_wow'] = merged['dtc_revenue'].pct_change() * 100

    # ── KPI row ──
    latest = merged.iloc[-1]
    prev = merged.iloc[-2] if len(merged) > 1 else None

    cols = st.columns(4)
    with cols[0]:
        delta = f"{latest['cost_wow']:+.1f}%" if prev is not None and pd.notna(latest['cost_wow']) else None
        st.metric('3PL Cost', f"${latest['threpl_cost']:,.0f}", delta=delta, delta_color='inverse')
    with cols[1]:
        delta = f"{latest['rev_wow']:+.1f}%" if prev is not None and pd.notna(latest['rev_wow']) else None
        st.metric('DTC Revenue', f"${latest['dtc_revenue']:,.0f}", delta=delta)
    with cols[2]:
        prev_pct = prev['cost_pct'] if prev is not None else None
        delta = f"{latest['cost_pct'] - prev_pct:+.2f}pp" if prev_pct is not None and prev_pct > 0 else None
        st.metric('Cost as % of Revenue', f"{latest['cost_pct']:.2f}%", delta=delta, delta_color='inverse')
    with cols[3]:
        if latest['dtc_revenue'] > 0:
            cpo = latest['threpl_cost'] / (latest['dtc_revenue'] / 50)  # rough orders estimate
        else:
            cpo = 0
        st.metric('Invoices Loaded', f"{len(merged)}")

    # ── Chart: Cost vs Revenue with % overlay ──
    fig = go.Figure()

    fig.add_trace(go.Bar(
        x=merged['week_start'],
        y=merged['threpl_cost'],
        name='3PL Cost',
        marker_color='#ef4444',
        opacity=0.8,
        yaxis='y',
    ))

    fig.add_trace(go.Bar(
        x=merged['week_start'],
        y=merged['dtc_revenue'],
        name='DTC Revenue',
        marker_color='#3b82f6',
        opacity=0.6,
        yaxis='y',
    ))

    fig.add_trace(go.Scatter(
        x=merged['week_start'],
        y=merged['cost_pct'],
        name='Cost %',
        mode='lines+markers',
        line=dict(color='#f59e0b', width=2.5),
        marker=dict(size=7),
        yaxis='y2',
    ))

    fig.update_layout(
        barmode='group',
        yaxis=dict(title='Dollars ($)', side='left'),
        yaxis2=dict(title='Cost as % of Revenue', side='right', overlaying='y',
                    ticksuffix='%', rangemode='tozero'),
        legend=dict(orientation='h', y=-0.15),
        height=420,
        margin=dict(t=10, b=60),
    )

    st.plotly_chart(fig, use_container_width=True)

    # ── Cost breakdown by category ──
    if not cat_df.empty:
        st.subheader('Cost Breakdown by Category')
        pivot = cat_df.pivot_table(
            index='activity',
            columns=pd.Grouper(key='week_start', freq='W-MON'),
            values='amount',
            aggfunc='sum',
            fill_value=0,
        )
        pivot.columns = [c.strftime('%m/%d') for c in pivot.columns]
        pivot['Total'] = pivot.sum(axis=1)
        pivot = pivot.sort_values('Total', ascending=False)

        # Format for display
        display = pivot.copy()
        for col in display.columns:
            display[col] = display[col].map('${:,.2f}'.format)
        st.dataframe(display, use_container_width=True)

    # ── Detail table ──
    st.subheader('Invoice History')
    display_df = merged[['week_start', 'threpl_cost', 'dtc_revenue', 'cost_pct', 'cost_wow']].copy()
    display_df['week_start'] = display_df['week_start'].dt.strftime('%Y-%m-%d')
    display_df.columns = ['Week Starting', '3PL Cost', 'DTC Revenue', 'Cost %', 'WoW Change %']
    display_df['3PL Cost'] = display_df['3PL Cost'].map('${:,.2f}'.format)
    display_df['DTC Revenue'] = display_df['DTC Revenue'].map('${:,.2f}'.format)
    display_df['Cost %'] = display_df['Cost %'].map('{:.2f}%'.format)
    display_df['WoW Change %'] = display_df['WoW Change %'].apply(
        lambda x: f'{x:+.1f}%' if pd.notna(x) else '—'
    )
    st.dataframe(display_df, use_container_width=True, hide_index=True)
