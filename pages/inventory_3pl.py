"""3PL Inventory page."""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime
from dateutil.relativedelta import relativedelta

from db import get_db, read_sql, get_media_spend
from ui.components import render_html_table, render_freshness_badge
from analytics.sku_flavors import get_flavor, sort_df_by_best_seller
import config


def render(ctx):
    """Render the 3PL Inventory page."""
    FORECAST_SKUS = ctx['forecast_skus']
    _cached_waterfall = ctx['cached_waterfall']
    _cached_sku_forecast = ctx['cached_sku_forecast']
    _load_seasonal_json = ctx['load_seasonal_json']

    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title("3PL Inventory")
    if not config.PACKIYO_API_TOKEN:
        with _badge_col:
            render_freshness_badge(is_fallback=True, source='Packiyo (no token)')
        st.caption("Showing cached sales velocity — Packiyo API token not configured.")
        st.info("Live 3PL inventory requires a Packiyo API token. Add it in **Settings** or set the `PACKIYO_API_TOKEN` environment variable.")

        # Fallback: show recent sales velocity from database
        with get_db() as conn:
            _fb_df = read_sql("""
                SELECT sku, SUM(units_sold) as units_30d, SUM(revenue) as revenue_30d,
                       ROUND(SUM(units_sold) / 30.0, 1) as daily_velocity
                FROM daily_sku_sales
                WHERE sale_date >= date('now', '-30 days') AND source = 'shopify'
                GROUP BY sku ORDER BY units_30d DESC
            """, conn)
        if not _fb_df.empty:
            _fb_df = sort_df_by_best_seller(_fb_df, sku_col="sku")
            _fb_df.insert(1, "Flavor", _fb_df["sku"].apply(lambda s: get_flavor(s, "")))
            st.subheader("Recent DTC Sales Velocity (last 30 days)")
            st.caption("Showing Shopify sales data as a reference while live 3PL inventory is unavailable.")
            _fb_display = _fb_df.rename(columns={"sku": "SKU", "units_30d": "Units (30d)",
                                                  "revenue_30d": "Revenue (30d)", "daily_velocity": "Daily Avg"})
            for _fc in ["Units (30d)", "Daily Avg"]:
                if _fc in _fb_display.columns:
                    _fb_display[_fc] = _fb_display[_fc].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
            _fb_display["Revenue (30d)"] = _fb_display["Revenue (30d)"].apply(lambda x: f"${x:,.0f}" if pd.notnull(x) else "")
            render_html_table(_fb_display, max_height=min(len(_fb_display) * 35 + 38, 700))
    else:
        from etl.packiyo_client import get_inventory

        with st.spinner("Fetching inventory from Packiyo..."):
            try:
                inv = get_inventory()
            except Exception as e:
                inv = None
                st.error(f"Failed to fetch inventory: {e}")

        with _badge_col:
            if inv:
                render_freshness_badge(
                    is_live=True,
                    source='Packiyo API',
                    last_refreshed_str=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                    new_rows=len(inv),
                )
            else:
                render_freshness_badge(is_fallback=True, source='Packiyo API (error)')
        st.caption("Live stock from Packiyo.")

        if inv:
            inv_df = pd.DataFrame(inv)

            # Filter options
            col_f1, col_f2 = st.columns(2)
            with col_f1:
                show_option = st.radio(
                    "Show",
                    ["Core Forecast SKUs", "All SKUs with Stock", "All SKUs"],
                    horizontal=True,
                    key="inv_show",
                )
            with col_f2:
                search = st.text_input("Search SKU or Name", key="inv_search")

            if show_option == "Core Forecast SKUs":
                inv_df = inv_df[inv_df["sku"].isin(FORECAST_SKUS)]
            elif show_option == "All SKUs with Stock":
                inv_df = inv_df[inv_df["quantity_on_hand"] > 0]

            if search:
                mask = (
                    inv_df["sku"].str.contains(search, case=False, na=False)
                    | inv_df["name"].str.contains(search, case=False, na=False)
                )
                inv_df = inv_df[mask]

            inv_df = sort_df_by_best_seller(inv_df, sku_col="sku")

            # KPI cards
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total SKUs", len(inv_df))
            col2.metric("Total On Hand", f"{inv_df['quantity_on_hand'].sum():,.0f}")
            col3.metric("Total Available", f"{inv_df['quantity_available'].sum():,.0f}")
            col4.metric("Total Allocated", f"{inv_df['quantity_allocated'].sum():,.0f}")

            st.divider()

            # Main inventory table
            display_cols = ["sku", "name", "quantity_on_hand", "quantity_allocated",
                            "quantity_available", "quantity_backordered", "quantity_inbound"]
            display_df = inv_df[display_cols].copy()
            display_df.columns = ["SKU", "Product", "On Hand", "Allocated",
                                  "Available", "Backordered", "Inbound"]
            display_df = display_df.drop(columns=["Product"])
            display_df.insert(1, "Flavor", [
                get_flavor(row["sku"], row["name"]) for _, row in inv_df[["sku", "name"]].iterrows()
            ])

            for _ic in ["On Hand", "Allocated", "Available", "Backordered", "Inbound"]:
                if _ic in display_df.columns:
                    display_df[_ic] = display_df[_ic].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
            render_html_table(display_df, max_height=min(len(display_df) * 35 + 38, 700))

            # Forecast vs Inventory comparison (core SKUs only)
            st.divider()
            st.subheader("Forecast vs. Current Inventory")
            st.caption("Compares your demand forecast with current 3PL stock to flag potential shortfalls.")

            # Get current forecast for the core SKUs
            try:
                import json as _json_inv
                wf_source_inv = None
                with get_db() as conn:
                    existing_spend_inv = get_media_spend(conn, source="All Sources")
                if existing_spend_inv:
                    media_plan_inv = existing_spend_inv
                else:
                    media_plan_inv = [{"month": (datetime.utcnow() + relativedelta(months=i)).strftime("%Y-%m"),
                                       "spend": 0, "new_customer_roas": 0.7} for i in range(12)]
                media_json_inv = _json_inv.dumps(media_plan_inv, sort_keys=True)
                wf_inv = _cached_waterfall(media_json_inv, wf_source_inv, 3, _load_seasonal_json())

                if not wf_inv.empty:
                    sku_fc = _cached_sku_forecast(wf_inv.to_json(), wf_source_inv)
                    if not sku_fc.empty:
                        sku_fc = sku_fc[sku_fc["SKU"].isin(FORECAST_SKUS)].copy()
                        if "Variant" in sku_fc.columns:
                            sku_fc = sku_fc.drop(columns=["Variant"])

                        # Sum next 3 months forecast
                        month_cols = [c for c in sku_fc.columns if c != "SKU"]
                        sku_fc["forecast_3mo"] = sku_fc[month_cols].sum(axis=1)

                        # Merge with inventory
                        inv_core = inv_df[inv_df["sku"].isin(FORECAST_SKUS)][["sku", "quantity_available"]].copy()
                        comparison = sku_fc[["SKU", "forecast_3mo"]].merge(
                            inv_core, left_on="SKU", right_on="sku", how="left"
                        ).drop(columns=["sku"])
                        comparison["quantity_available"] = comparison["quantity_available"].fillna(0)
                        comparison["months_of_stock"] = (
                            comparison["quantity_available"] / (comparison["forecast_3mo"] / 3)
                        ).round(1)
                        comparison["months_of_stock"] = comparison["months_of_stock"].replace(
                            [float("inf"), float("-inf")], 0
                        ).fillna(0)
                        comparison.columns = ["SKU", "Forecast (3mo)", "Available", "Months of Stock"]
                        comparison.insert(1, "Flavor", comparison["SKU"].map(lambda s: get_flavor(s)))
                        comparison = sort_df_by_best_seller(comparison, sku_col="SKU")

                        def _color_stock(val):
                            if val < 1:
                                return "background-color: #fee2e2"  # red -- less than 1 month
                            elif val < 2:
                                return "background-color: #fef3c7"  # yellow -- 1-2 months
                            return ""

                        for _cc in ["Forecast (3mo)", "Available"]:
                            if _cc in comparison.columns:
                                comparison[_cc] = comparison[_cc].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
                        if "Months of Stock" in comparison.columns:
                            comparison["Months of Stock"] = comparison["Months of Stock"].apply(lambda x: f"{x:.1f}" if pd.notnull(x) else "")
                        render_html_table(comparison, max_height=min(len(comparison) * 35 + 38, 700))
                    else:
                        st.info("No SKU forecast data available for comparison.")
                else:
                    st.info("No waterfall forecast available for comparison.")
            except Exception as e:
                st.warning(f"Could not load forecast comparison: {e}")
