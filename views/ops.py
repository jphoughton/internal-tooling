"""Ops page -- channel-aware sub-tabs for inventory, forecast, and reorder/transfers."""
import streamlit as st


def render(ctx):
    """Render the Ops page with channel-specific sub-tabs."""
    channel = ctx.get('channel', 'Rollup')

    if channel == 'DTC':
        tabs = st.tabs(['Inventory', 'Demand Forecast', 'Reorder'])
        with tabs[0]:
            from views.inventory_3pl import render as render_3pl
            render_3pl(ctx, embedded=True)
        with tabs[1]:
            from views.demand_forecast import render as render_forecast
            render_forecast(ctx, embedded=True)
        with tabs[2]:
            from views.reorder_alerts import render as render_reorder
            render_reorder(ctx, embedded=True)
            st.divider()
            from views.projected_inventory import render as render_proj
            render_proj(ctx, embedded=True)

    elif channel == 'Amazon':
        tabs = st.tabs(['Inventory', 'Demand Forecast', 'Transfers'])
        with tabs[0]:
            from views.inventory_amazon import render as render_amz_inv
            render_amz_inv(ctx, embedded=True)
        with tabs[1]:
            from views.demand_forecast import render as render_forecast
            render_forecast(ctx, embedded=True)
        with tabs[2]:
            from views.fba_transfers import render as render_fba
            render_fba(ctx, embedded=True)

    else:  # Rollup
        tabs = st.tabs(['Inventory', 'Demand Forecast', 'Projected Inventory', 'Reorder'])
        with tabs[0]:
            _render_combined_inventory(ctx)
        with tabs[1]:
            from views.demand_forecast import render as render_forecast
            render_forecast(ctx, embedded=True)
        with tabs[2]:
            from views.projected_inventory import render as render_proj
            render_proj(ctx, embedded=True)
        with tabs[3]:
            from views.reorder_alerts import render as render_reorder
            render_reorder(ctx, embedded=True)


def _render_combined_inventory(ctx):
    """Show combined 3PL + FBA inventory in a unified view."""
    from views.inventory_3pl import render as render_3pl
    from views.inventory_amazon import render as render_amz_inv

    st.subheader('3PL Inventory')
    render_3pl(ctx, embedded=True)
    st.divider()
    st.subheader('Amazon FBA Inventory')
    render_amz_inv(ctx, embedded=True)
