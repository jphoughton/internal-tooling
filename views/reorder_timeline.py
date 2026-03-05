"""Reorder Timeline — standalone Gantt chart of reorder timing across all SKUs."""
import logging
import json as _json_tl
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime
from dateutil.relativedelta import relativedelta

from db import get_db, get_media_spend, get_planned_inbound_dict
from analytics.sku_flavors import get_flavor, get_sku_sales_rank

log = logging.getLogger(__name__)


def _get_precomputed(key):
    """Load precomputed result from DB, return parsed JSON or None."""
    try:
        from db import get_precomputed
        with get_db() as conn:
            cached = get_precomputed(conn, key, max_age_hours=25)
        if cached:
            return _json_tl.loads(cached)
    except Exception:
        pass
    return None


def _load_reorder_events(ctx):
    """Load or compute reorder events for the timeline."""
    FORECAST_SKUS = ctx['forecast_skus']
    _cached_waterfall = ctx['cached_waterfall']
    _cached_sku_forecast = ctx['cached_sku_forecast']
    _load_seasonal_json = ctx['load_seasonal_json']
    _load_sku_seasonal_json = ctx['load_sku_seasonal_json']

    bv = ctx['biz_vars']
    lt_weeks = bv['lead_time_weeks']
    moq = bv['moq_units']
    safety_wk = bv['safety_buffer_weeks']

    # Try precomputed first
    precomputed = _get_precomputed('reorder_plan')
    if precomputed is not None:
        events = precomputed.get('events', [])
        for ev in events:
            for dk in ['reorder_by', 'arrival_date', 'stockout_date']:
                if dk in ev and isinstance(ev[dk], str):
                    try:
                        ev[dk] = datetime.strptime(ev[dk][:10], '%Y-%m-%d').date()
                    except (ValueError, TypeError):
                        pass
        return events, lt_weeks

    # Live computation
    from analytics.reorder import build_reorder_plan

    try:
        with get_db() as conn:
            existing_spend = get_media_spend(conn, source='All Sources')
        if existing_spend:
            media_plan = existing_spend
        else:
            media_plan = [{'month': (datetime.utcnow() + relativedelta(months=i)).strftime('%Y-%m'),
                           'spend': 0, 'new_customer_roas': 0.7} for i in range(12)]
        media_json = _json_tl.dumps(media_plan, sort_keys=True)
        wf = _cached_waterfall(media_json, None, 12, _load_seasonal_json())

        if wf.empty:
            return [], lt_weeks

        sku_table = _cached_sku_forecast(
            wf.to_json(), None, _load_sku_seasonal_json(), _load_seasonal_json(),
        )
        if sku_table.empty:
            return [], lt_weeks

        sku_table = sku_table[sku_table['SKU'].isin(FORECAST_SKUS)].copy()
        if 'Variant' in sku_table.columns:
            sku_table = sku_table.drop(columns=['Variant'])
        if 'Flavor' not in sku_table.columns:
            sku_table.insert(1, 'Flavor', sku_table['SKU'].map(lambda s: get_flavor(s)))
    except Exception as e:
        log.warning('Could not load demand forecast for timeline: %s', e)
        return [], lt_weeks

    # Get combined inventory
    inv_data = _get_combined_inventory(ctx)

    with get_db() as conn:
        planned_inbound = get_planned_inbound_dict(conn)

    _, events = build_reorder_plan(
        sku_forecast_table=sku_table,
        inventory_data=inv_data,
        lead_time_weeks=lt_weeks,
        moq=moq,
        safety_weeks=safety_wk,
        forecast_skus=FORECAST_SKUS,
        planned_inbound=planned_inbound,
    )
    return events, lt_weeks


def _get_combined_inventory(ctx):
    """Fetch and merge 3PL + FBA inventory."""
    combined = {}

    try:
        items_3pl = ctx['cached_3pl_inventory']()
        for item in (items_3pl or []):
            sku = item['sku']
            combined[sku] = {
                'sku': sku,
                'name': item.get('name', ''),
                'quantity_available': item.get('quantity_available', 0) or 0,
                'quantity_on_hand': item.get('quantity_on_hand', 0) or 0,
                'quantity_inbound': item.get('quantity_inbound', 0) or 0,
                '3pl_available': item.get('quantity_available', 0) or 0,
                'fba_fulfillable': 0,
            }
    except Exception:
        pass

    try:
        items_amz = ctx['cached_amazon_inventory']()
        for item in (items_amz or []):
            sku = item['sku']
            fba_total = item.get('total_quantity', 0) or 0
            fba_inbound = (item.get('inbound_shipped', 0) or 0) + (item.get('inbound_receiving', 0) or 0)
            if sku in combined:
                combined[sku]['quantity_available'] += fba_total
                combined[sku]['quantity_inbound'] += fba_inbound
                combined[sku]['fba_fulfillable'] = fba_total
            else:
                combined[sku] = {
                    'sku': sku,
                    'name': item.get('product_name', ''),
                    'quantity_available': fba_total,
                    'quantity_on_hand': fba_total,
                    'quantity_inbound': fba_inbound,
                    '3pl_available': 0,
                    'fba_fulfillable': fba_total,
                }
    except Exception:
        pass

    return list(combined.values())


def render(ctx, embedded=False):
    """Render the Reorder Timeline Gantt chart."""
    if not embedded:
        st.title('Reorder Timeline')

    events, lt_weeks = _load_reorder_events(ctx)

    if not events:
        st.info('No reorder events to display. All SKUs have sufficient stock or no demand forecast is available.')
        return

    st.caption(f'When to place orders based on {lt_weeks}-week lead time. '
               'Dark blue = lead time (order to arrival), orange = runway (arrival to stockout).')

    # Sort by stockout urgency (earliest stockout first)
    events_sorted = sorted(events, key=lambda x: x['stockout_date'] if x.get('stockout_date') else datetime.max.date())

    # Build Gantt rows
    timeline_rows = []
    for ev in events_sorted:
        label = ev.get('flavor') or ev['sku']
        timeline_rows.append({
            'SKU': label,
            'Start': ev['reorder_by'],
            'End': ev['arrival_date'],
            'Type': 'Lead Time (order \u2192 arrival)',
        })
        timeline_rows.append({
            'SKU': label,
            'Start': ev['arrival_date'],
            'End': ev['stockout_date'],
            'Type': 'Runway (arrival \u2192 stockout)',
        })

    tl_df = pd.DataFrame(timeline_rows)
    tl_df['Start'] = pd.to_datetime(tl_df['Start'])
    tl_df['End'] = pd.to_datetime(tl_df['End'])

    fig = px.timeline(
        tl_df, x_start='Start', x_end='End', y='SKU', color='Type',
        color_discrete_map={
            'Lead Time (order \u2192 arrival)': '#0F3557',
            'Runway (arrival \u2192 stockout)': '#F58B3D',
        },
    )

    fig.update_layout(
        height=max(300, len(events_sorted) * 50),
        margin=dict(l=0, r=0, t=10, b=0),
        xaxis_title='',
        yaxis_title='',
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='left', x=0),
    )

    # Today marker
    _today_str = datetime.utcnow().strftime('%Y-%m-%d')
    fig.add_shape(
        type='line', x0=_today_str, x1=_today_str,
        y0=0, y1=1, yref='paper',
        line=dict(dash='dash', color='#E05252', width=1),
    )
    fig.add_annotation(
        x=_today_str, y=1, yref='paper',
        text='Today', showarrow=False, yanchor='bottom',
        font=dict(size=11, color='#E05252'),
    )

    st.plotly_chart(fig, use_container_width=True)

    # Summary table below the chart
    summary_rows = []
    for ev in events_sorted:
        summary_rows.append({
            'SKU': ev['sku'],
            'Flavor': ev.get('flavor') or '',
            'Urgency': ev.get('urgency', ''),
            '3PL Stock': f"{ev.get('3pl_stock', 0):,}",
            'FBA Stock': f"{ev.get('fba_stock', 0):,}",
            'Monthly Demand': f"{ev.get('monthly_demand', 0):,}",
            'Order By': ev['reorder_by'].strftime('%Y-%m-%d') if hasattr(ev['reorder_by'], 'strftime') else str(ev['reorder_by']),
            'Arrival': ev['arrival_date'].strftime('%Y-%m-%d') if hasattr(ev['arrival_date'], 'strftime') else str(ev['arrival_date']),
            'Stockout': ev['stockout_date'].strftime('%Y-%m-%d') if hasattr(ev['stockout_date'], 'strftime') else str(ev['stockout_date']),
            'Order Qty': f"{ev.get('order_qty', 0):,}",
        })

    if summary_rows:
        from ui.components import render_html_table
        with st.expander('Reorder Details', expanded=False):
            render_html_table(pd.DataFrame(summary_rows))
