"""Amazon Inventory page."""
import streamlit as st
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta

from db import get_db, read_sql, get_media_spend
from ui.components import render_html_table, render_freshness_badge
from analytics.sku_flavors import get_flavor, sort_df_by_best_seller
import config


def render(ctx):
    """Render the Amazon Inventory page."""
    FORECAST_SKUS = ctx['forecast_skus']
    _cached_waterfall = ctx['cached_waterfall']
    _cached_sku_forecast = ctx['cached_sku_forecast']
    _load_seasonal_json = ctx['load_seasonal_json']
    active_sources = ctx['active_sources']

    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title("Amazon Inventory")

    has_amazon_creds = all(getattr(config, k, "") for k in [
        "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
    ])

    if not has_amazon_creds:
        with _badge_col:
            render_freshness_badge(is_fallback=True, source='Amazon SP-API (no creds)')
        st.caption("Showing cached sales velocity — Amazon SP-API credentials not configured.")
        st.info("Live Amazon inventory requires SP-API credentials. Add them in **Settings** or set the `AMAZON_REFRESH_TOKEN`, `AMAZON_LWA_CLIENT_ID`, and `AMAZON_LWA_CLIENT_SECRET` environment variables.")

        # Fallback: show recent Amazon sales velocity from database
        with get_db() as conn:
            _amz_fb = read_sql("""
                SELECT sku, SUM(units_sold) as units_30d, SUM(revenue) as revenue_30d,
                       ROUND(SUM(units_sold) / 30.0, 1) as daily_velocity
                FROM daily_sku_sales
                WHERE sale_date >= date('now', '-30 days') AND source = 'amazon'
                GROUP BY sku ORDER BY units_30d DESC
            """, conn)
        if not _amz_fb.empty:
            _amz_fb = sort_df_by_best_seller(_amz_fb, sku_col="sku")
            _amz_fb.insert(1, "Flavor", _amz_fb["sku"].apply(lambda s: get_flavor(s, "")))
            st.subheader("Recent Amazon Sales Velocity (last 30 days)")
            st.caption("Showing Amazon sales data as a reference while live FBA inventory is unavailable.")
            _amz_fb_display = _amz_fb.rename(columns={"sku": "SKU", "units_30d": "Units (30d)",
                                                       "revenue_30d": "Revenue (30d)", "daily_velocity": "Daily Avg"})
            for _fc in ["Units (30d)", "Daily Avg"]:
                if _fc in _amz_fb_display.columns:
                    _amz_fb_display[_fc] = _amz_fb_display[_fc].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
            _amz_fb_display["Revenue (30d)"] = _amz_fb_display["Revenue (30d)"].apply(lambda x: f"${x:,.0f}" if pd.notnull(x) else "")
            render_html_table(_amz_fb_display, max_height=min(len(_amz_fb_display) * 35 + 38, 700))
    else:
        from etl.amazon_inventory import get_inventory as get_amz_inventory

        with st.spinner("Fetching FBA inventory from Amazon..."):
            try:
                amz_inv = get_amz_inventory()
            except Exception as e:
                amz_inv = None
                st.error(f"Failed to fetch Amazon inventory: {e}")

        with _badge_col:
            if amz_inv:
                render_freshness_badge(
                    is_live=True,
                    source='Amazon SP-API',
                    last_refreshed_str=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                    new_rows=len(amz_inv),
                )
            else:
                render_freshness_badge(is_fallback=True, source='Amazon SP-API (error)')
        st.caption("Live FBA stock from Seller Central.")

        if amz_inv:
            amz_df = pd.DataFrame(amz_inv)

            # Filter options
            col_f1, col_f2 = st.columns(2)
            with col_f1:
                amz_show = st.radio(
                    "Show",
                    ["All with Stock", "Core Forecast SKUs", "All SKUs"],
                    horizontal=True,
                    key="amz_inv_show",
                )
            with col_f2:
                amz_search = st.text_input("Search SKU, ASIN, or Name", key="amz_inv_search")

            if amz_show == "Core Forecast SKUs":
                amz_df = amz_df[amz_df["sku"].isin(FORECAST_SKUS)]
            elif amz_show == "All with Stock":
                amz_df = amz_df[amz_df["total_quantity"] > 0]

            if amz_search:
                mask = (
                    amz_df["sku"].str.contains(amz_search, case=False, na=False)
                    | amz_df["asin"].str.contains(amz_search, case=False, na=False)
                    | amz_df["product_name"].str.contains(amz_search, case=False, na=False)
                )
                amz_df = amz_df[mask]

            amz_df = sort_df_by_best_seller(amz_df, sku_col="sku")

            # KPI cards
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total SKUs", len(amz_df))
            col2.metric("Fulfillable", f"{amz_df['fulfillable_quantity'].sum():,.0f}")
            col3.metric("Inbound", f"{(amz_df['inbound_working'].sum() + amz_df['inbound_shipped'].sum() + amz_df['inbound_receiving'].sum()):,.0f}")
            col4.metric("Reserved", f"{amz_df['reserved_quantity'].sum():,.0f}")

            st.divider()

            # Main inventory table
            display_amz = amz_df[["sku", "asin", "product_name", "fulfillable_quantity",
                                   "reserved_quantity", "inbound_shipped", "inbound_receiving",
                                   "unfulfillable_quantity", "total_quantity"]].copy()
            display_amz.columns = ["SKU", "ASIN", "Product", "Fulfillable",
                                   "Reserved", "Inbound Shipped", "Inbound Receiving",
                                   "Unfulfillable", "Total"]
            display_amz = display_amz.drop(columns=["Product"])
            display_amz.insert(1, "Flavor", [
                get_flavor(row["sku"], row["product_name"]) for _, row in amz_df[["sku", "product_name"]].iterrows()
            ])

            for _ac in ["Fulfillable", "Reserved", "Inbound Shipped", "Inbound Receiving", "Unfulfillable", "Total"]:
                if _ac in display_amz.columns:
                    display_amz[_ac] = display_amz[_ac].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
            render_html_table(display_amz, max_height=min(len(display_amz) * 35 + 38, 700))

            # Forecast vs Amazon Inventory comparison
            st.divider()
            st.subheader("Forecast vs. Amazon FBA Stock")
            st.caption("Compares Amazon demand forecast with FBA inventory to flag shortfalls.")

            try:
                import json as _json_amz
                with get_db() as conn:
                    existing_spend_amz = get_media_spend(conn, source="All Sources")
                if existing_spend_amz:
                    media_plan_amz = existing_spend_amz
                else:
                    media_plan_amz = [{"month": (datetime.utcnow() + relativedelta(months=i)).strftime("%Y-%m"),
                                       "spend": 0, "new_customer_roas": 0.7} for i in range(3)]
                media_json_amz = _json_amz.dumps(media_plan_amz, sort_keys=True)

                # Try to get Amazon-specific forecast, fall back to combined
                amz_source = "amazon" if "amazon" in active_sources else None
                wf_amz = _cached_waterfall(media_json_amz, amz_source, 3, _load_seasonal_json())

                if not wf_amz.empty:
                    sku_fc_amz = _cached_sku_forecast(wf_amz.to_json(), amz_source)
                    if not sku_fc_amz.empty:
                        sku_fc_amz = sku_fc_amz[sku_fc_amz["SKU"].isin(FORECAST_SKUS)].copy()
                        if "Variant" in sku_fc_amz.columns:
                            sku_fc_amz = sku_fc_amz.drop(columns=["Variant"])

                        month_cols_amz = [c for c in sku_fc_amz.columns if c != "SKU"]
                        sku_fc_amz["forecast_3mo"] = sku_fc_amz[month_cols_amz].sum(axis=1)

                        amz_core = amz_df[amz_df["sku"].isin(FORECAST_SKUS)][["sku", "total_quantity"]].copy()
                        comparison_amz = sku_fc_amz[["SKU", "forecast_3mo"]].merge(
                            amz_core, left_on="SKU", right_on="sku", how="left"
                        ).drop(columns=["sku"])
                        comparison_amz["total_quantity"] = comparison_amz["total_quantity"].fillna(0)
                        comparison_amz["months_of_stock"] = (
                            comparison_amz["total_quantity"] / (comparison_amz["forecast_3mo"] / 3)
                        ).round(1)
                        comparison_amz["months_of_stock"] = comparison_amz["months_of_stock"].replace(
                            [float("inf"), float("-inf")], 0
                        ).fillna(0)
                        comparison_amz.columns = ["SKU", "Forecast (3mo)", "FBA Total", "Months of Stock"]
                        comparison_amz.insert(1, "Flavor", comparison_amz["SKU"].map(lambda s: get_flavor(s)))
                        comparison_amz = sort_df_by_best_seller(comparison_amz, sku_col="SKU")

                        def _color_amz_stock(val):
                            if val < 1:
                                return "background-color: #fee2e2"
                            elif val < 2:
                                return "background-color: #fef3c7"
                            return ""

                        for _cac in ["Forecast (3mo)", "FBA Fulfillable"]:
                            if _cac in comparison_amz.columns:
                                comparison_amz[_cac] = comparison_amz[_cac].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
                        if "Months of Stock" in comparison_amz.columns:
                            comparison_amz["Months of Stock"] = comparison_amz["Months of Stock"].apply(lambda x: f"{x:.1f}" if pd.notnull(x) else "")
                        render_html_table(comparison_amz, max_height=min(len(comparison_amz) * 35 + 38, 700))
                    else:
                        st.info("No SKU forecast data available.")
                else:
                    st.info("No waterfall forecast available.")
            except Exception as e:
                st.warning(f"Could not load forecast comparison: {e}")
