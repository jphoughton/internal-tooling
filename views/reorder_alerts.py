"""Reorder Alerts page."""
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
from dateutil.relativedelta import relativedelta

from db import (
    get_db, read_sql, get_media_spend, get_planned_inbound_dict,
    upsert_planned_inbound, get_last_sync_timestamp,
    get_new_rows_since_yesterday, get_synced_sources,
)
from ui.components import render_html_table, render_freshness_badge
from analytics.sku_flavors import get_flavor, sort_df_by_best_seller
import config


def render(ctx):
    """Render the Reorder Alerts page."""
    FORECAST_SKUS = ctx['forecast_skus']
    _cached_waterfall = ctx['cached_waterfall']
    _cached_sku_forecast = ctx['cached_sku_forecast']
    _load_seasonal_json = ctx['load_seasonal_json']
    _load_sku_seasonal_json = ctx['load_sku_seasonal_json']

    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title("Reorder Alerts")
    with _badge_col:
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, ['shopify', 'amazon'])
            _new = get_new_rows_since_yesterday(conn, ['shopify', 'amazon'])
            _srcs = get_synced_sources(conn, ['shopify', 'amazon'])
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)
    st.caption("Forecast-driven reorder timing based on live 3PL + FBA inventory.")

    from analytics.reorder import build_reorder_plan, build_inventory_runway_chart

    # --- Business Variables (from sidebar panel) ---
    bv = ctx['biz_vars']
    lt_weeks = bv['lead_time_weeks']
    moq = bv['moq_units']
    safety_wk = bv['safety_buffer_weeks']
    st.caption(f"Lead time: {lt_weeks} wks · MOQ: {moq:,} units · Safety: {safety_wk} wks — change in sidebar **Business Variables**")

    # --- Get SKU forecast from waterfall ---
    has_forecast = False
    sku_table = pd.DataFrame()
    try:
        import json as _json_ro
        wf_source_ro = None
        with get_db() as conn:
            existing_spend_ro = get_media_spend(conn, source="All Sources")
        if existing_spend_ro:
            media_plan_ro = existing_spend_ro
        else:
            media_plan_ro = [{"month": (datetime.utcnow() + relativedelta(months=i)).strftime("%Y-%m"),
                               "spend": 0, "new_customer_roas": 0.7} for i in range(12)]
        media_json_ro = _json_ro.dumps(media_plan_ro, sort_keys=True)
        wf_ro = _cached_waterfall(media_json_ro, wf_source_ro, 12, _load_seasonal_json())

        if not wf_ro.empty:
            sku_table = _cached_sku_forecast(
                wf_ro.to_json(), wf_source_ro, _load_sku_seasonal_json(), _load_seasonal_json(),
            )
            if not sku_table.empty:
                sku_table = sku_table[sku_table["SKU"].isin(FORECAST_SKUS)].copy()
                if "Variant" in sku_table.columns:
                    sku_table = sku_table.drop(columns=["Variant"])
                if "Flavor" not in sku_table.columns:
                    sku_table.insert(1, "Flavor", sku_table["SKU"].map(lambda s: get_flavor(s)))
                has_forecast = True
    except Exception as e:
        st.warning(f"Could not load demand forecast: {e}")

    # --- Get combined inventory (3PL + Amazon FBA) ---
    inv_data_3pl = []
    inv_data_amz = []
    has_3pl = False
    has_amz_inv = False

    with st.spinner("Fetching live inventory..."):
        # Packiyo 3PL
        if config.PACKIYO_API_TOKEN:
            try:
                from etl.packiyo_client import get_inventory as get_3pl_inventory
                inv_data_3pl = get_3pl_inventory()
                has_3pl = True
            except Exception as e:
                st.warning(f"Could not fetch 3PL inventory: {e}")

        # Amazon FBA
        has_amazon_creds_ro = all(getattr(config, k, "") for k in [
            "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
        ])
        if has_amazon_creds_ro:
            try:
                from etl.amazon_inventory import get_inventory as get_fba_inventory
                inv_data_amz = get_fba_inventory()
                has_amz_inv = True
            except Exception as e:
                st.warning(f"Could not fetch Amazon FBA inventory: {e}")

    # Merge: combine available stock across both sources per SKU
    combined_inv = {}
    for item in inv_data_3pl:
        sku = item["sku"]
        combined_inv[sku] = {
            "sku": sku,
            "name": item.get("name", ""),
            "quantity_available": item.get("quantity_available", 0) or 0,
            "quantity_on_hand": item.get("quantity_on_hand", 0) or 0,
            "quantity_inbound": item.get("quantity_inbound", 0) or 0,
            "3pl_available": item.get("quantity_available", 0) or 0,
            "fba_fulfillable": 0,
        }
    for item in inv_data_amz:
        sku = item["sku"]
        # Use total_quantity for planning (includes fulfillable + reserved + inbound)
        fba_total = item.get("total_quantity", 0) or 0
        fba_inbound = (item.get("inbound_shipped", 0) or 0) + (item.get("inbound_receiving", 0) or 0)
        if sku in combined_inv:
            combined_inv[sku]["quantity_available"] += fba_total
            combined_inv[sku]["quantity_inbound"] += fba_inbound
            combined_inv[sku]["fba_fulfillable"] = fba_total
        else:
            combined_inv[sku] = {
                "sku": sku,
                "name": item.get("product_name", ""),
                "quantity_available": fba_total,
                "quantity_on_hand": fba_total,
                "quantity_inbound": fba_inbound,
                "3pl_available": 0,
                "fba_fulfillable": fba_total,
            }

    inv_data = list(combined_inv.values())
    has_inventory = has_3pl or has_amz_inv

    if not has_inventory:
        st.info("No inventory sources configured. Connect Packiyo (3PL) or Amazon (FBA) in **Settings** for the full picture.")

    # --- Inventory Rollup ---
    if has_inventory and inv_data:
        st.subheader("Inventory Rollup")
        total_3pl = sum(i.get("3pl_available", 0) for i in inv_data)
        total_fba = sum(i.get("fba_fulfillable", 0) for i in inv_data)
        total_combined = sum(i.get("quantity_available", 0) for i in inv_data)
        total_inbound = sum(i.get("quantity_inbound", 0) for i in inv_data)

        rc1, rc2, rc3, rc4 = st.columns(4)
        rc1.metric("3PL (Packiyo)", f"{total_3pl:,}" if has_3pl else "N/A")
        rc2.metric("FBA (Amazon)", f"{total_fba:,}" if has_amz_inv else "N/A")
        rc3.metric("Total Available", f"{total_combined:,}")
        rc4.metric("Total Inbound", f"{total_inbound:,}")
        st.divider()

    # Load planned inbound (user's already-placed orders) for reorder calculations
    with get_db() as conn:
        ro_planned_inbound = get_planned_inbound_dict(conn)

    if not has_forecast:
        st.warning("No demand forecast available. Go to **Demand Forecast** to set up your media spend plan first.")
    else:
        with st.spinner("Building reorder plan..."):
            reorder_df, reorder_events = build_reorder_plan(
                sku_forecast_table=sku_table,
                inventory_data=inv_data,
                lead_time_weeks=lt_weeks,
                moq=moq,
                safety_weeks=safety_wk,
                forecast_skus=FORECAST_SKUS,
                planned_inbound=ro_planned_inbound,
            )

        if reorder_df.empty:
            st.warning("No reorder data available for the core forecast SKUs.")
        else:
            # --- KPI cards ---
            overdue = reorder_df[reorder_df["Urgency"] == "OVERDUE"]
            order_now = reorder_df[reorder_df["Urgency"] == "ORDER NOW"]
            order_soon = reorder_df[reorder_df["Urgency"] == "ORDER SOON"]
            en_route = reorder_df[reorder_df["Urgency"].str.startswith("EN ROUTE")]
            en_route_warn = reorder_df[reorder_df["Urgency"] == "EN ROUTE \u26a0\ufe0f"]

            _n_ok = len(reorder_df) - len(overdue) - len(order_now) - len(order_soon) - len(en_route)
            _u1, _u2, _u3, _u4, _u5 = st.columns(5)
            _u1.metric("Overdue", len(overdue))
            _u2.metric("Order Now", len(order_now))
            _u3.metric("Order Soon", len(order_soon))
            _u4.metric("En Route", len(en_route))
            _u5.metric("OK", _n_ok)

            st.markdown("")

            # --- Urgency alerts ---
            urgent = reorder_df[reorder_df["Urgency"].isin(["OVERDUE", "EN ROUTE \u26a0\ufe0f", "ORDER NOW", "ORDER SOON"])]
            if not urgent.empty:
                st.subheader("Action Required")
                for _urg_idx, (_, row) in enumerate(urgent.iterrows()):
                    if row["Urgency"] == "OVERDUE":
                        icon = "\U0001f534"
                        color = "error"
                    elif row["Urgency"] == "EN ROUTE \u26a0\ufe0f":
                        icon = "\U0001f69a\u26a0\ufe0f"
                        color = "warning"
                    elif row["Urgency"] == "ORDER NOW":
                        icon = "\U0001f7e0"
                        color = "warning"
                    else:
                        icon = "\U0001f7e1"
                        color = "info"

                    label_parts = [f"{icon} **{row['SKU']}**"]
                    if row["Flavor"]:
                        label_parts.append(f"({row['Flavor']})")
                    label_parts.append(f"— {row['Urgency']}")
                    if row["Urgency"] == "EN ROUTE \u26a0\ufe0f":
                        label_parts.append("(ordered but may stock out before arrival)")
                    elif row["Reorder By"] != "\u2014":
                        label_parts.append(f"| Reorder by **{row['Reorder By']}**")
                    label_parts.append(f"| Order **{row['Order Qty']:,}** units")

                    with st.expander(" ".join(label_parts)):
                        ec1, ec2, ec3, ec4, ec5 = st.columns(5)
                        ec1.metric("3PL Stock", f"{row.get('3PL Stock', 0):,}")
                        ec2.metric("FBA Stock", f"{row.get('FBA Stock', 0):,}")
                        ec3.metric("Total Stock", f"{row['Total Stock']:,}")
                        ec4.metric("Monthly Demand", f"{row['Monthly Demand']:,}")
                        ec5.metric("Order Qty", f"{row['Order Qty']:,}")

                        ec6, ec7, ec8, ec9, ec10 = st.columns(5)
                        ec6.metric("Inbound", f"{row['Inbound']:,}")
                        ec7.metric("Stockout Date", row["Stockout Date"])
                        ec8.metric("Lead Time", f"{lt_weeks} weeks")
                        ec9.metric("MOQ", f"{moq:,}")
                        ec10.metric("Months of Cover", f"{row['Months of Cover']:.1f}")

                        # Show planned inbound if any
                        _sku_planned = ro_planned_inbound.get(row["SKU"], {})
                        if _sku_planned:
                            _planned_str = ", ".join(
                                f"{datetime.strptime(m, '%Y-%m').strftime('%b %Y')}: {u:,}"
                                for m, u in sorted(_sku_planned.items()) if u > 0
                            )
                            st.success(f"\u2705 **Planned inbound:** {_planned_str}")

                        # Mark as Ordered button
                        _mark_key = f"mark_{row['SKU']}_{_urg_idx}"
                        with st.popover(f"\u2705 Mark as Ordered", use_container_width=False):
                            _mk_c1, _mk_c2 = st.columns(2)
                            with _mk_c1:
                                _order_qty = st.number_input(
                                    "Order Qty", min_value=100, value=row["Order Qty"],
                                    step=500, key=f"oq_{_mark_key}",
                                )
                            with _mk_c2:
                                _arrival_options = [
                                    (datetime.utcnow() + relativedelta(months=i)).strftime("%Y-%m")
                                    for i in range(1, 7)
                                ]
                                _arrival_month = st.selectbox(
                                    "Expected Arrival Month", _arrival_options,
                                    key=f"am_{_mark_key}",
                                )
                            if st.button("Save", key=f"save_{_mark_key}", type="primary"):
                                with get_db() as conn:
                                    upsert_planned_inbound(conn, row["SKU"], _arrival_month, _order_qty)
                                st.success(f"Saved: {_order_qty:,} units arriving {_arrival_month}")
                                st.rerun()

                        # Inventory runway chart
                        if has_inventory:
                            inv_item = next((it for it in inv_data if it["sku"] == row["SKU"]), None)
                            if inv_item:
                                meta_cols_r = {"SKU", "Flavor", "Variant"}
                                month_cols_r = [c for c in sku_table.columns if c not in meta_cols_r]
                                fc_row = sku_table[sku_table["SKU"] == row["SKU"]]
                                if not fc_row.empty:
                                    monthly = {m: float(fc_row.iloc[0][m]) for m in month_cols_r if pd.notna(fc_row.iloc[0][m]) and fc_row.iloc[0][m] > 0}
                                    runway_df, ro_info = build_inventory_runway_chart(
                                        row["SKU"], monthly,
                                        inv_item.get("quantity_available", 0),
                                        inv_item.get("quantity_inbound", 0),
                                        lead_time_weeks=lt_weeks, moq=moq, safety_weeks=safety_wk,
                                        planned_inbound_sku=ro_planned_inbound.get(row["SKU"], {}),
                                    )
                                    if not runway_df.empty:
                                        # Convert dates to pd.Timestamp for Plotly compatibility
                                        runway_df["date"] = pd.to_datetime(runway_df["date"])
                                        fig_run = go.Figure()
                                        fig_run.add_trace(go.Scatter(
                                            x=runway_df["date"], y=runway_df["inventory_no_order"],
                                            mode="lines", name="Without Order",
                                            line=dict(color="#E05252", width=2, dash="dash"),
                                        ))
                                        fig_run.add_trace(go.Scatter(
                                            x=runway_df["date"], y=runway_df["inventory_with_order"],
                                            mode="lines", name=f"With Order ({ro_info['order_qty']:,} units)",
                                            line=dict(color="#2DA87E", width=2),
                                        ))
                                        if ro_info.get("arrival_date"):
                                            arrival_str = ro_info["arrival_date"].strftime("%Y-%m-%d")
                                            # Use add_shape + add_annotation instead of
                                            # add_vline(annotation_text=...) to avoid Plotly
                                            # bug where sum() is called on Timestamp x-axis.
                                            fig_run.add_shape(
                                                type="line", x0=arrival_str, x1=arrival_str,
                                                y0=0, y1=1, yref="paper",
                                                line=dict(dash="dot", color="#7ECCE5", width=1),
                                            )
                                            fig_run.add_annotation(
                                                x=arrival_str, y=1, yref="paper",
                                                text=f"Order arrives ({ro_info['arrival_date'].strftime('%b %d')})",
                                                showarrow=False, yanchor="bottom",
                                                font=dict(size=11, color="#7ECCE5"),
                                            )
                                        fig_run.add_hline(y=0, line_dash="solid", line_color="#E05252", line_width=1)
                                        fig_run.update_layout(
                                            height=280,
                                            margin=dict(l=0, r=0, t=30, b=0),
                                            yaxis_title="Units",
                                            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                                        )
                                        st.plotly_chart(fig_run, use_container_width=True)

                st.divider()

            # --- En Route (ordered, on track) ---
            en_route_ok = reorder_df[reorder_df["Urgency"] == "EN ROUTE"]
            if not en_route_ok.empty:
                st.subheader("En Route")
                st.caption("These SKUs have inventory already ordered and arriving soon.")
                for _er_idx, (_, row) in enumerate(en_route_ok.iterrows()):
                    _sku_planned = ro_planned_inbound.get(row["SKU"], {})
                    _planned_str = ", ".join(
                        f"{datetime.strptime(m, '%Y-%m').strftime('%b %Y')}: {u:,}"
                        for m, u in sorted(_sku_planned.items()) if u > 0
                    )
                    flavor_txt = f" ({row['Flavor']})" if row["Flavor"] else ""
                    st.success(
                        f"\U0001f69a **{row['SKU']}**{flavor_txt} \u2014 "
                        f"**{row['Planned Inbound']:,}** units arriving ({_planned_str}) | "
                        f"Current stock: {row['Total Stock']:,} | Monthly demand: {row['Monthly Demand']:,}"
                    )
                st.divider()

            # --- Reorder Timeline ---
            if reorder_events:
                st.subheader("Reorder Timeline")
                st.caption(f"When to place orders based on {lt_weeks}-week lead time. Green = order date, blue = arrival, red = stockout if no order.")

                # Gantt-style timeline
                timeline_rows = []
                for ev in sorted(reorder_events, key=lambda x: x["reorder_by"]):
                    label = f"{ev['flavor'] or ev['sku']}"
                    timeline_rows.append({
                        "SKU": label,
                        "Start": ev["reorder_by"],
                        "End": ev["arrival_date"],
                        "Type": "Lead Time (order \u2192 arrival)",
                    })
                    timeline_rows.append({
                        "SKU": label,
                        "Start": ev["arrival_date"],
                        "End": ev["stockout_date"],
                        "Type": "Runway (arrival \u2192 stockout)",
                    })

                tl_df = pd.DataFrame(timeline_rows)
                # Convert date objects to timestamps for Plotly
                tl_df["Start"] = pd.to_datetime(tl_df["Start"])
                tl_df["End"] = pd.to_datetime(tl_df["End"])
                fig_tl = px.timeline(
                    tl_df, x_start="Start", x_end="End", y="SKU", color="Type",
                    color_discrete_map={
                        "Lead Time (order \u2192 arrival)": "#0F3557",
                        "Runway (arrival \u2192 stockout)": "#F58B3D",
                    },
                )
                fig_tl.update_layout(
                    height=max(250, len(reorder_events) * 50),
                    margin=dict(l=0, r=0, t=10, b=0),
                    xaxis_title="",
                    yaxis_title="",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                )
                # Add today marker (use add_shape + add_annotation to avoid
                # Plotly bug with sum() on Timestamp x-axis data)
                _today_str = datetime.utcnow().strftime("%Y-%m-%d")
                fig_tl.add_shape(
                    type="line", x0=_today_str, x1=_today_str,
                    y0=0, y1=1, yref="paper",
                    line=dict(dash="dash", color="#E05252", width=1),
                )
                fig_tl.add_annotation(
                    x=_today_str, y=1, yref="paper",
                    text="Today", showarrow=False, yanchor="bottom",
                    font=dict(size=11, color="#E05252"),
                )
                st.plotly_chart(fig_tl, use_container_width=True)

                st.divider()

            # --- Full reorder table ---
            st.subheader("Full Reorder Plan")
            st.caption(f"Lead time: {lt_weeks} weeks | MOQ: {moq:,} units | Safety buffer: {safety_wk} weeks")

            display_reorder = reorder_df.copy()
            # Drop Days Until Reorder column (redundant with Reorder By date)
            if "Days Until Reorder" in display_reorder.columns:
                display_reorder = display_reorder.drop(columns=["Days Until Reorder"])

            for _roc in ["3PL Stock", "FBA Stock", "Total Stock", "Inbound", "Monthly Demand", "Order Qty"]:
                if _roc in display_reorder.columns:
                    display_reorder[_roc] = display_reorder[_roc].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
            if "Months of Cover" in display_reorder.columns:
                display_reorder["Months of Cover"] = display_reorder["Months of Cover"].apply(lambda x: f"{x:.1f}" if pd.notnull(x) else "")
            render_html_table(display_reorder, max_height=min(len(display_reorder) * 35 + 38, 700),
                              column_groups=[
                                  ('', ['SKU', 'Flavor', 'Urgency']),
                                  ('Current Stock', ['3PL Stock', 'FBA Stock', 'Total Stock', 'Inbound']),
                                  ('Demand', ['Monthly Demand', 'Months of Cover']),
                                  ('Action', ['Reorder By', 'Stockout Date', 'Order Qty', 'Planned Inbound']),
                              ])

            # --- Export ---
            st.divider()
            csv_ro = reorder_df.to_csv(index=False)
            st.download_button(
                "\U0001f4e5 Export Reorder Plan (CSV)",
                data=csv_ro,
                file_name=f"reorder_plan_{datetime.utcnow().strftime('%Y%m%d')}.csv",
                mime="text/csv",
            )
