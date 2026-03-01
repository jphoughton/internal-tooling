"""Ops page -- orchestrates inventory, demand forecast, reorder, and transfer sub-views."""
import streamlit as st
from views import inventory_3pl, inventory_amazon, demand_forecast, projected_inventory, reorder_alerts, fba_transfers


def render(ctx):
    """Render the Ops page with channel-aware sub-tabs."""
    channel = ctx.get('channel', 'Rollup')

    if channel == 'DTC':
        tabs = st.tabs(['Inventory', 'Demand Forecast', 'Reorder'])
        with tabs[0]:
            inventory_3pl.render(ctx, embedded=True)
        with tabs[1]:
            demand_forecast.render(ctx, embedded=True)
        with tabs[2]:
            reorder_alerts.render(ctx, embedded=True)
            projected_inventory.render(ctx, embedded=True)

    elif channel == 'Amazon':
        tabs = st.tabs(['Inventory', 'Demand Forecast', 'Transfers'])
        with tabs[0]:
            inventory_amazon.render(ctx, embedded=True)
        with tabs[1]:
            demand_forecast.render(ctx, embedded=True)
        with tabs[2]:
            fba_transfers.render(ctx, embedded=True)

    else:  # Rollup
        tabs = st.tabs(['Inventory', 'Demand Forecast', 'Reorder'])
        with tabs[0]:
            _render_combined_inventory(ctx)
        with tabs[1]:
            demand_forecast.render(ctx, embedded=True)
        with tabs[2]:
            reorder_alerts.render(ctx, embedded=True)
            projected_inventory.render(ctx, embedded=True)


def _render_combined_inventory(ctx):
    """Render combined 3PL + FBA inventory snapshot."""
    FORECAST_SKUS = ctx['forecast_skus']
    from analytics.sku_flavors import get_flavor, get_sku_sales_rank
    from ui.components import render_html_table

    # Gather inventory from both sources
    inv_3pl = []
    inv_amz = []
    has_3pl = False
    has_amz = False

    try:
        result = ctx['cached_3pl_inventory']()
        if result:
            inv_3pl = result
            has_3pl = True
    except Exception:
        pass

    try:
        result = ctx['cached_amazon_inventory']()
        if result:
            inv_amz = result
            has_amz = True
    except Exception:
        pass

    if not has_3pl and not has_amz:
        st.warning('No inventory sources connected. Connect Packiyo (3PL) or Amazon (FBA) in **Settings**.')
        return

    # Merge inventory
    combined = {}
    for item in inv_3pl:
        sku = item['sku']
        combined[sku] = {
            'sku': sku,
            'name': item.get('name', ''),
            '3pl_available': int(float(item.get('quantity_available', 0) or 0)),
            'fba_fulfillable': 0,
            'inbound': int(float(item.get('quantity_inbound', 0) or 0)),
        }
    for item in inv_amz:
        sku = item['sku']
        fba_total = int(float(item.get('total_quantity', 0) or 0))
        fba_inbound = int(float(item.get('inbound_shipped', 0) or 0)) + int(float(item.get('inbound_receiving', 0) or 0))
        if sku in combined:
            combined[sku]['fba_fulfillable'] = fba_total
            combined[sku]['inbound'] += fba_inbound
        else:
            combined[sku] = {
                'sku': sku,
                'name': item.get('product_name', ''),
                '3pl_available': 0,
                'fba_fulfillable': fba_total,
                'inbound': fba_inbound,
            }

    # KPIs
    total_3pl = sum(v['3pl_available'] for v in combined.values())
    total_fba = sum(v['fba_fulfillable'] for v in combined.values())
    total_inv = total_3pl + total_fba
    total_inbound = sum(v['inbound'] for v in combined.values())

    k1, k2, k3, k4 = st.columns(4)
    k1.metric('3PL Stock', f'{total_3pl:,}' if has_3pl else '\u2014')
    k2.metric('FBA Stock', f'{total_fba:,}' if has_amz else '\u2014')
    k3.metric('Combined', f'{total_inv:,}')
    k4.metric('Inbound', f'{total_inbound:,}')

    # Build table for forecast SKUs
    import pandas as pd
    rank = get_sku_sales_rank()
    max_rank = len(rank)
    rows = []
    for sku in sorted(FORECAST_SKUS, key=lambda s: rank.get(s, max_rank)):
        inv = combined.get(sku)
        if not inv:
            continue
        total = inv['3pl_available'] + inv['fba_fulfillable']
        rows.append({
            'SKU': sku,
            'Flavor': get_flavor(sku),
            '3PL': f"{inv['3pl_available']:,}",
            'FBA': f"{inv['fba_fulfillable']:,}",
            'Total': f'{total:,}',
            'Inbound': f"{inv['inbound']:,}",
        })

    if rows:
        df = pd.DataFrame(rows)
        render_html_table(df, max_height=min(len(df) * 35 + 38, 700),
                          column_groups=[
                              ('', ['SKU', 'Flavor']),
                              ('Stock', ['3PL', 'FBA', 'Total', 'Inbound']),
                          ])
