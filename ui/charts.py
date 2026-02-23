"""Reusable Plotly chart factories for the Hydrant dashboard."""
import pandas as pd
import plotly.express as px
import streamlit as st
from datetime import datetime


def render_timeline_chart(events, lead_label='Lead Time', runway_label='Runway',
                          lead_color='#0F3557', runway_color='#F58B3D',
                          height_per_item=50, min_height=250):
    """Render a Gantt-style timeline chart with today marker.

    Used by Reorder Alerts and FBA Transfers pages.  Each event
    produces two horizontal bars: *lead time* (order/ship -> arrival)
    and *runway* (arrival -> stockout).

    Args:
        events: list of dicts, each with keys:
            - label: str  (SKU or flavor name for y-axis)
            - lead_start: date  (order/ship date)
            - lead_end: date  (arrival date)
            - runway_start: date  (same as lead_end in most cases)
            - runway_end: date  (projected stockout date)
        lead_label: legend text for the lead-time bars.
        runway_label: legend text for the runway bars.
        lead_color: hex colour for lead-time bars.
        runway_color: hex colour for runway bars.
        height_per_item: pixels per row.
        min_height: minimum chart height in pixels.
    """
    if not events:
        st.info('No timeline data to display.')
        return

    timeline_rows = []
    for ev in sorted(events, key=lambda x: x['lead_start']):
        label = ev['label']
        timeline_rows.append({
            'SKU': label,
            'Start': ev['lead_start'],
            'End': ev['lead_end'],
            'Type': lead_label,
        })
        if ev.get('runway_end') and ev.get('runway_start'):
            timeline_rows.append({
                'SKU': label,
                'Start': ev['runway_start'],
                'End': ev['runway_end'],
                'Type': runway_label,
            })

    tl_df = pd.DataFrame(timeline_rows)
    # Convert date objects to timestamps for Plotly
    tl_df['Start'] = pd.to_datetime(tl_df['Start'])
    tl_df['End'] = pd.to_datetime(tl_df['End'])

    fig_tl = px.timeline(
        tl_df, x_start='Start', x_end='End', y='SKU', color='Type',
        color_discrete_map={
            lead_label: lead_color,
            runway_label: runway_color,
        },
    )
    fig_tl.update_layout(
        height=max(min_height, len(events) * height_per_item),
        margin=dict(l=0, r=0, t=10, b=0),
        xaxis_title='',
        yaxis_title='',
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='left', x=0),
    )

    # Today marker  (use add_shape + add_annotation to avoid
    # Plotly bug with sum() on Timestamp x-axis data)
    _today_str = datetime.utcnow().strftime('%Y-%m-%d')
    fig_tl.add_shape(
        type='line', x0=_today_str, x1=_today_str,
        y0=0, y1=1, yref='paper',
        line=dict(dash='dash', color='#E05252', width=1),
    )
    fig_tl.add_annotation(
        x=_today_str, y=1, yref='paper',
        text='Today', showarrow=False, yanchor='bottom',
        font=dict(size=11, color='#E05252'),
    )
    st.plotly_chart(fig_tl, use_container_width=True)
