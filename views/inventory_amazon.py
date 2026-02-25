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

            # --- Pre-fetch forecast for DoS + comparison table ---
            _amz_fc_map = {}  # {sku: forecast_3mo}
            _amz_fc_ok = False
            _amz_sku_fc_core = pd.DataFrame()
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
                amz_source = "amazon" if "amazon" in active_sources else None
                wf_amz = _cached_waterfall(media_json_amz, amz_source, 3, _load_seasonal_json())
                if not wf_amz.empty:
                    _amz_fc_raw = _cached_sku_forecast(wf_amz.to_json(), amz_source)
                    if not _amz_fc_raw.empty:
                        _amz_sku_fc_core = _amz_fc_raw[_amz_fc_raw["SKU"].isin(FORECAST_SKUS)].copy()
                        if "Variant" in _amz_sku_fc_core.columns:
                            _amz_sku_fc_core = _amz_sku_fc_core.drop(columns=["Variant"])
                        _amz_mc = [c for c in _amz_sku_fc_core.columns if c != "SKU"]
                        _amz_sku_fc_core["forecast_3mo"] = _amz_sku_fc_core[_amz_mc].sum(axis=1)
                        _amz_fc_map = dict(zip(_amz_sku_fc_core["SKU"], _amz_sku_fc_core["forecast_3mo"]))
                        _amz_fc_ok = True
            except Exception:
                pass

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

            # Add Days of Supply column
            _amz_dos_vals = []
            for _, _r in amz_df.iterrows():
                _fc3 = _amz_fc_map.get(_r["sku"], 0)
                _daily = _fc3 / 91.32 if _fc3 > 0 else 0
                _total = _r.get("total_quantity", 0) or 0
                _amz_dos_vals.append(round(_total / _daily) if _daily > 0 else None)
            display_amz["DoS"] = _amz_dos_vals
            display_amz["DoS"] = display_amz["DoS"].apply(
                lambda x: f"{int(x)}d" if pd.notnull(x) and x is not None else "\u2014"
            )

            # Reorder columns: put DoS after Total
            _amz_col_order = ["SKU", "Flavor", "ASIN", "Fulfillable", "Reserved",
                              "Inbound Shipped", "Inbound Receiving", "Unfulfillable", "Total", "DoS"]
            display_amz = display_amz[[c for c in _amz_col_order if c in display_amz.columns]]

            def _color_dos_amz(val):
                if isinstance(val, str) and val.endswith('d'):
                    try:
                        days = int(val[:-1])
                        if days < 30:
                            return 'background-color: #fee2e2; color: #991b1b; font-weight: bold'
                        elif days < 60:
                            return 'background-color: #fef3c7; color: #92400e'
                    except ValueError:
                        pass
                return ''

            for _ac in ["Fulfillable", "Reserved", "Inbound Shipped", "Inbound Receiving", "Unfulfillable", "Total"]:
                if _ac in display_amz.columns:
                    display_amz[_ac] = display_amz[_ac].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) and isinstance(x, (int, float)) else x)
            render_html_table(display_amz, max_height=min(len(display_amz) * 35 + 38, 700),
                              style_fn=_color_dos_amz, style_cols=["DoS"],
                              column_groups=[
                                  ('', ['SKU', 'Flavor', 'ASIN']),
                                  ('Available', ['Fulfillable', 'Reserved']),
                                  ('Inbound', ['Inbound Shipped', 'Inbound Receiving']),
                                  ('Summary', ['Unfulfillable', 'Total', 'DoS']),
                              ],
                              n_frozen_cols=3, frozen_col_widths=[195, 145, 110])

            # Forecast vs Amazon Inventory comparison
            st.divider()
            st.subheader("Forecast vs. Amazon FBA Stock")
            st.caption("Compares Amazon demand forecast with FBA inventory to flag shortfalls.")

            if _amz_fc_ok:
                try:
                    amz_core = amz_df[amz_df["sku"].isin(FORECAST_SKUS)][["sku", "total_quantity"]].copy()
                    comparison_amz = _amz_sku_fc_core[["SKU", "forecast_3mo"]].merge(
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

                    _mos_raw_amz = comparison_amz["Months of Stock"].copy()

                    for _cac in ["Forecast (3mo)", "FBA Total"]:
                        if _cac in comparison_amz.columns:
                            comparison_amz[_cac] = comparison_amz[_cac].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
                    comparison_amz["Months of Stock"] = _mos_raw_amz.apply(lambda x: f"{x:.1f}" if pd.notnull(x) else "")

                    def _color_mos_amz(val):
                        if isinstance(val, str):
                            try:
                                v = float(val)
                                if v < 1:
                                    return 'background-color: #fee2e2; color: #991b1b; font-weight: bold'
                                elif v < 2:
                                    return 'background-color: #fef3c7; color: #92400e'
                            except ValueError:
                                pass
                        return ''

                    render_html_table(comparison_amz, max_height=min(len(comparison_amz) * 35 + 38, 700),
                                      style_fn=_color_mos_amz, style_cols=["Months of Stock"],
                                      n_frozen_cols=2, frozen_col_widths=[195, 145])
                except Exception as e:
                    st.warning(f"Could not load forecast comparison: {e}")
            else:
                st.info("No forecast data available for comparison.")
