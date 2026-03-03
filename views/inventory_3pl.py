"""3PL Inventory page."""
import streamlit as st
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta

from db import get_db, read_sql, get_media_spend
from ui.components import render_html_table, render_freshness_badge
from analytics.sku_flavors import get_flavor, sort_df_by_best_seller
from utils.date_helpers import business_yesterday
import config


def render(ctx, embedded=False):
    """Render the 3PL Inventory page."""
    FORECAST_SKUS = ctx['forecast_skus']
    _cached_waterfall = ctx['cached_waterfall']
    _cached_sku_forecast = ctx['cached_sku_forecast']
    _load_seasonal_json = ctx['load_seasonal_json']
    _load_sku_seasonal_json = ctx['load_sku_seasonal_json']

    if not embedded:
        _title_col, _badge_col = st.columns([7, 3])
        with _title_col:
            st.title("3PL Inventory")
    if not config.PACKIYO_API_TOKEN:
        if not embedded:
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
                WHERE sale_date >= date('now', '-30 days') AND sale_date <= %s AND source = 'shopify'
                GROUP BY sku ORDER BY units_30d DESC
            """, conn, params=(str(business_yesterday()),))
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
        try:
            inv = ctx['cached_3pl_inventory']()
        except Exception as e:
            inv = None
            st.error(f"Failed to fetch inventory: {e}")

        if not embedded:
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

            # --- Pre-fetch forecast for DoS + comparison table ---
            _3pl_fc_map = {}  # {sku: forecast_3mo}
            _3pl_fc_ok = False
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
                    _sku_fc_raw = _cached_sku_forecast(
                        wf_inv.to_json(), wf_source_inv, _load_sku_seasonal_json(), _load_seasonal_json(),
                    )
                    if not _sku_fc_raw.empty:
                        _sku_fc_core = _sku_fc_raw[_sku_fc_raw["SKU"].isin(FORECAST_SKUS)].copy()
                        if "Variant" in _sku_fc_core.columns:
                            _sku_fc_core = _sku_fc_core.drop(columns=["Variant"])
                        _fc_month_cols = [c for c in _sku_fc_core.columns if c != "SKU"]
                        _sku_fc_core["forecast_3mo"] = _sku_fc_core[_fc_month_cols].sum(axis=1)
                        _3pl_fc_map = dict(zip(_sku_fc_core["SKU"], _sku_fc_core["forecast_3mo"]))
                        _3pl_fc_ok = True
            except Exception:
                pass

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

            # Add Days of Supply column
            _dos_vals = []
            for _, _r in inv_df.iterrows():
                _fc3 = _3pl_fc_map.get(_r["sku"], 0)
                _daily = _fc3 / 91.32 if _fc3 > 0 else 0
                _avail = _r.get("quantity_available", 0) or 0
                _dos_vals.append(round(_avail / _daily) if _daily > 0 else None)
            display_df["DoS"] = _dos_vals
            display_df["DoS"] = display_df["DoS"].apply(
                lambda x: f"{int(x)}d" if pd.notnull(x) and x is not None else "\u2014"
            )

            # Reorder columns: put DoS after Available
            _col_order = ["SKU", "Flavor", "On Hand", "Allocated", "Available", "DoS", "Backordered", "Inbound"]
            display_df = display_df[[c for c in _col_order if c in display_df.columns]]

            def _color_dos_3pl(val):
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

            for _ic in ["On Hand", "Allocated", "Available", "Backordered", "Inbound"]:
                if _ic in display_df.columns:
                    display_df[_ic] = display_df[_ic].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) and isinstance(x, (int, float)) else x)
            st.caption('Live Packiyo 3PL stock levels with days-of-supply based on demand forecast.')
            render_html_table(display_df, max_height=min(len(display_df) * 35 + 38, 700),
                              style_fn=_color_dos_3pl, style_cols=["DoS"],
                              column_groups=[
                                  ('', ['SKU', 'Flavor']),
                                  ('Stock Levels', ['On Hand', 'Allocated', 'Available', 'DoS']),
                                  ('Pipeline', ['Backordered', 'Inbound']),
                              ])

            # Forecast vs Inventory comparison (core SKUs only)
            st.divider()
            st.subheader("Forecast vs. Current Inventory")
            st.caption("Compares your demand forecast with current 3PL stock to flag potential shortfalls.")

            if _3pl_fc_ok:
                try:
                    inv_core = inv_df[inv_df["sku"].isin(FORECAST_SKUS)][["sku", "quantity_available"]].copy()
                    comparison = _sku_fc_core[["SKU", "forecast_3mo"]].merge(
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

                    # Keep raw Months of Stock for styling, format after
                    _mos_raw = comparison["Months of Stock"].copy()

                    for _cc in ["Forecast (3mo)", "Available"]:
                        if _cc in comparison.columns:
                            comparison[_cc] = comparison[_cc].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
                    comparison["Months of Stock"] = _mos_raw.apply(lambda x: f"{x:.1f}" if pd.notnull(x) else "")

                    def _color_mos(val):
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

                    render_html_table(comparison, max_height=min(len(comparison) * 35 + 38, 700),
                                      style_fn=_color_mos, style_cols=["Months of Stock"])
                except Exception as e:
                    st.warning(f"Could not load forecast comparison: {e}")
            else:
                st.info("No forecast data available for comparison.")
