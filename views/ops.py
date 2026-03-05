"""Ops page -- channel-aware sub-tabs for inventory, forecast, and reorder/transfers."""
import logging
import streamlit as st

log = logging.getLogger(__name__)


def render(ctx):
    """Render the Ops page with channel-specific sub-tabs."""
    channel = ctx.get('channel', 'Rollup')

    if channel == 'DTC':
        tabs = st.tabs(['Inventory', 'Demand Forecast', 'Reorder'])

        @st.fragment
        def _dtc_inventory():
            from views.inventory_3pl import render as render_3pl
            render_3pl(ctx, embedded=True)

        @st.fragment
        def _dtc_forecast():
            from views.demand_forecast import render as render_forecast
            render_forecast(ctx, embedded=True)

        @st.fragment
        def _dtc_reorder():
            from views.reorder_alerts import render as render_reorder
            render_reorder(ctx, embedded=True)
            st.divider()
            from views.projected_inventory import render as render_proj
            render_proj(ctx, embedded=True)

        with tabs[0]:
            _dtc_inventory()
        with tabs[1]:
            _dtc_forecast()
        with tabs[2]:
            _dtc_reorder()

    elif channel == 'Amazon':
        tabs = st.tabs(['Inventory', 'Demand Forecast', 'Transfers'])

        @st.fragment
        def _amz_inventory():
            from views.inventory_amazon import render as render_amz_inv
            render_amz_inv(ctx, embedded=True)

        @st.fragment
        def _amz_forecast():
            from views.demand_forecast import render as render_forecast
            render_forecast(ctx, embedded=True)

        @st.fragment
        def _amz_transfers():
            from views.fba_transfers import render as render_fba
            render_fba(ctx, embedded=True)

        with tabs[0]:
            _amz_inventory()
        with tabs[1]:
            _amz_forecast()
        with tabs[2]:
            _amz_transfers()

    else:  # Rollup
        tabs = st.tabs(['Inventory', 'Demand Forecast', 'Projected Inventory', 'Reorder', '3PL Costs'])

        @st.fragment
        def _rollup_inventory():
            _render_combined_inventory(ctx)

        @st.fragment
        def _rollup_forecast():
            from views.demand_forecast import render as render_forecast
            render_forecast(ctx, embedded=True)

        @st.fragment
        def _rollup_projected():
            from views.projected_inventory import render as render_proj
            render_proj(ctx, embedded=True)

        @st.fragment
        def _rollup_reorder():
            from views.reorder_alerts import render as render_reorder
            render_reorder(ctx, embedded=True)

        @st.fragment
        def _rollup_threpl_costs():
            try:
                from views.threpl_costs import render as render_costs
                render_costs(ctx, embedded=True)
            except Exception as e:
                log.error('3PL Costs tab failed: %s', e, exc_info=True)
                st.error(f'3PL Costs tab failed: {e}')

        with tabs[0]:
            _rollup_inventory()
        with tabs[1]:
            _rollup_forecast()
        with tabs[2]:
            _rollup_projected()
        with tabs[3]:
            _rollup_reorder()
        with tabs[4]:
            _rollup_threpl_costs()


def _render_combined_inventory(ctx):
    """Show combined 3PL + FBA inventory in a unified view."""
    from views.inventory_3pl import render as render_3pl
    from views.inventory_amazon import render as render_amz_inv

    st.subheader('3PL Inventory')
    render_3pl(ctx, embedded=True)
    st.divider()
    st.subheader('Amazon FBA Inventory')
    render_amz_inv(ctx, embedded=True)
