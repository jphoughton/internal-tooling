"""FBA Transfers page."""
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime, timedelta

from db import (
    get_db, read_sql, get_last_sync_timestamp,
    get_new_rows_since_yesterday, get_synced_sources,
)
from ui.components import render_html_table, render_freshness_badge
from analytics.sku_flavors import get_flavor, sort_df_by_best_seller
from analytics.dtc_demand import get_amazon_sku_velocity
import config


def render(ctx):
    """Render the FBA Transfers page."""
    FORECAST_SKUS = ctx['forecast_skus']

    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title("FBA Transfers")
    with _badge_col:
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, ['amazon'])
            _new = get_new_rows_since_yesterday(conn, ['amazon'])
            _srcs = get_synced_sources(conn, ['amazon'])
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)
    st.caption("When to ship inventory from your 3PL (Packiyo) to Amazon FBA.")

    # --- Settings ---
    with st.expander("Settings", expanded=False):
        transfer_lt_weeks = st.number_input(
            "DTC\u2192FBA Transfer Lead Time (weeks)", min_value=1, max_value=12, value=4,
            key="fba_transfer_lt",
            help="How many weeks it takes for inventory to ship from your 3PL to Amazon FBA.",
        )

    # --- Fetch inventory from both sources ---
    inv_data_3pl_fba = []
    inv_data_amz_fba = []
    has_amz_inv_fba = False

    with st.spinner("Fetching live inventory..."):
        if config.PACKIYO_API_TOKEN:
            try:
                from etl.packiyo_client import get_inventory as get_3pl_inventory
                inv_data_3pl_fba = get_3pl_inventory()
            except Exception as e:
                st.warning(f"Could not fetch 3PL inventory: {e}")

        has_amazon_creds_fba = all(getattr(config, k, "") for k in [
            "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
        ])
        if has_amazon_creds_fba:
            try:
                from etl.amazon_inventory import get_inventory as get_fba_inventory
                inv_data_amz_fba = get_fba_inventory()
                has_amz_inv_fba = True
            except Exception as e:
                st.warning(f"Could not fetch Amazon FBA inventory: {e}")

    if not has_amz_inv_fba:
        st.info("Live FBA transfer alerts require Amazon SP-API credentials. Add them in **Settings** or set the environment variables.")

        # Fallback: show Amazon sales velocity from database
        with get_db() as conn:
            _fb_amz = read_sql("""
                SELECT sku, SUM(units_sold) as units_30d, SUM(revenue) as revenue_30d,
                       ROUND(SUM(units_sold) / 30.0, 1) as daily_velocity
                FROM daily_sku_sales
                WHERE sale_date >= date('now', '-30 days') AND source = 'amazon'
                GROUP BY sku ORDER BY units_30d DESC
            """, conn)
        if not _fb_amz.empty:
            _fb_amz = sort_df_by_best_seller(_fb_amz, sku_col="sku")
            _fb_amz.insert(1, "Flavor", _fb_amz["sku"].apply(lambda s: get_flavor(s, "")))
            st.subheader("Amazon Sales Velocity (last 30 days)")
            st.caption("Showing Amazon sales data as a reference while live FBA inventory is unavailable.")
            _fb_display = _fb_amz.rename(columns={"sku": "SKU", "units_30d": "Units (30d)",
                                                    "revenue_30d": "Revenue (30d)", "daily_velocity": "Daily Avg"})
            for _fc in ["Units (30d)", "Daily Avg"]:
                if _fc in _fb_display.columns:
                    _fb_display[_fc] = _fb_display[_fc].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
            _fb_display["Revenue (30d)"] = _fb_display["Revenue (30d)"].apply(lambda x: f"${x:,.0f}" if pd.notnull(x) else "")
            render_html_table(_fb_display, max_height=min(len(_fb_display) * 35 + 38, 700))
    elif not inv_data_amz_fba:
        st.info("No Amazon FBA inventory data found.")
    else:
        # Compute Amazon-specific demand from velocity (blended 7d/30d daily rate)
        _amz_velocity = get_amazon_sku_velocity()
        _amz_demand_map = {}
        for _vsku, _vdata in _amz_velocity.items():
            # Blended daily rate: 60% recent 7d + 40% 30d average (matches dtc_demand approach)
            avg_7d = _vdata.get("avg_7d", 0)
            avg_30d = _vdata.get("avg_30d", 0)
            if avg_7d > 0 and avg_30d > 0:
                blended = avg_7d * 0.6 + avg_30d * 0.4
            else:
                blended = _vdata.get("avg_daily", 0)
            _amz_demand_map[_vsku] = blended * 30.44  # daily -> monthly

        # Build transfer alerts for each FBA SKU
        transfer_lt_days = transfer_lt_weeks * 7
        today_t = datetime.utcnow().date()
        transfer_rows = []

        for item in inv_data_amz_fba:
            sku = item["sku"]
            if sku not in FORECAST_SKUS:
                continue
            fba_stock = item.get("total_quantity", 0) or 0
            monthly = _amz_demand_map.get(sku, 0)
            daily_rate = monthly / 30.44 if monthly > 0 else 0

            if daily_rate <= 0:
                continue

            days_of_stock = fba_stock / daily_rate if daily_rate > 0 else 999
            fba_stockout = today_t + timedelta(days=int(days_of_stock)) if days_of_stock < 365 else None
            transfer_by = fba_stockout - timedelta(days=transfer_lt_days) if fba_stockout else None
            days_until_transfer = (transfer_by - today_t).days if transfer_by else 999

            # 3PL stock available for transfer
            _3pl_item = next((i for i in inv_data_3pl_fba if i["sku"] == sku), None)
            _3pl_avail = _3pl_item.get("quantity_available", 0) if _3pl_item else 0

            # Transfer quantity: 2 months of Amazon demand, rounded to 100
            transfer_qty = max(100, round(monthly * 2 / 100) * 100) if monthly > 0 else 0

            if days_until_transfer < 0:
                t_urgency = "OVERDUE"
            elif days_until_transfer <= 7:
                t_urgency = "TRANSFER NOW"
            elif days_until_transfer <= 21:
                t_urgency = "TRANSFER SOON"
            elif days_until_transfer <= 42:
                t_urgency = "UPCOMING"
            else:
                t_urgency = "OK"

            # Compute arrival date (transfer_by + lead time)
            fba_arrival = transfer_by + timedelta(days=transfer_lt_days) if transfer_by else None

            transfer_rows.append({
                "SKU": sku,
                "Flavor": get_flavor(sku),
                "FBA Stock": fba_stock,
                "FBA Monthly Demand": round(monthly),
                "FBA Stockout": fba_stockout.strftime("%Y-%m-%d") if fba_stockout else "Beyond 12mo",
                "Transfer By": transfer_by.strftime("%Y-%m-%d") if transfer_by else "\u2014",
                "Days Until Transfer": days_until_transfer if days_until_transfer < 999 else None,
                "Transfer Qty": transfer_qty,
                "3PL Available": _3pl_avail,
                "Can Fulfill": "\u2705" if _3pl_avail >= transfer_qty else "\u26a0\ufe0f Low",
                "Urgency": t_urgency,
                # Raw dates for timeline chart
                "_transfer_by": transfer_by,
                "_arrival": fba_arrival,
                "_stockout": fba_stockout,
            })

        if transfer_rows:
            transfer_df = pd.DataFrame(transfer_rows)
            transfer_df = sort_df_by_best_seller(transfer_df, sku_col="SKU")

            # --- KPI summary (per-tier urgency counts) ---
            _t_overdue = transfer_df[transfer_df["Urgency"] == "OVERDUE"]
            _t_now = transfer_df[transfer_df["Urgency"] == "TRANSFER NOW"]
            _t_soon = transfer_df[transfer_df["Urgency"] == "TRANSFER SOON"]
            _t_upcoming = transfer_df[transfer_df["Urgency"] == "UPCOMING"]
            _t_ok = transfer_df[transfer_df["Urgency"] == "OK"]
            _t1, _t2, _t3, _t4, _t5 = st.columns(5)
            _t1.metric("Overdue", len(_t_overdue))
            _t2.metric("Transfer Now", len(_t_now))
            _t3.metric("Transfer Soon", len(_t_soon))
            _t4.metric("Upcoming", len(_t_upcoming))
            _t5.metric("OK", len(_t_ok))

            st.markdown("")

            # --- Urgency alerts (expandable detail rows) ---
            _t_urgent = transfer_df[transfer_df["Urgency"].isin(["OVERDUE", "TRANSFER NOW", "TRANSFER SOON"])]
            if not _t_urgent.empty:
                st.subheader("Action Required")
                for _ti, (_, trow) in enumerate(_t_urgent.iterrows()):
                    if trow["Urgency"] == "OVERDUE":
                        icon = "\U0001f534"
                    elif trow["Urgency"] == "TRANSFER NOW":
                        icon = "\U0001f7e0"
                    else:
                        icon = "\U0001f7e1"

                    label_parts = [f"{icon} **{trow['SKU']}**"]
                    if trow["Flavor"]:
                        label_parts.append(f"({trow['Flavor']})")
                    label_parts.append(f"\u2014 {trow['Urgency']}")
                    if trow["Transfer By"] != "\u2014":
                        label_parts.append(f"| Ship by **{trow['Transfer By']}**")
                    label_parts.append(f"| Send **{trow['Transfer Qty']:,}** units")
                    if trow["Can Fulfill"] == "\u26a0\ufe0f Low":
                        label_parts.append("| \u26a0\ufe0f 3PL stock low")

                    with st.expander(" ".join(label_parts)):
                        _tc1, _tc2, _tc3, _tc4, _tc5 = st.columns(5)
                        _tc1.metric("FBA Stock", f"{trow['FBA Stock']:,}")
                        _tc2.metric("3PL Available", f"{trow['3PL Available']:,}")
                        _tc3.metric("FBA Monthly Demand", f"{trow['FBA Monthly Demand']:,}")
                        _tc4.metric("Transfer Qty", f"{trow['Transfer Qty']:,}")
                        _tc5.metric("Can Fulfill", trow["Can Fulfill"])

                        _tc6, _tc7, _tc8 = st.columns(3)
                        _tc6.metric("FBA Stockout", trow["FBA Stockout"])
                        _tc7.metric("Ship By", trow["Transfer By"])
                        _tc8.metric("Transfer Lead Time", f"{transfer_lt_weeks} weeks")

                st.divider()

            # --- Transfer Timeline (Gantt chart) ---
            # Build timeline from rows that have valid dates
            _tl_rows = [r for r in transfer_rows if r["_transfer_by"] and r["_stockout"]]
            if _tl_rows:
                st.subheader("Transfer Timeline")
                st.caption(f"When to ship inventory from 3PL to Amazon FBA based on {transfer_lt_weeks}-week transfer lead time.")

                timeline_data = []
                for ev in sorted(_tl_rows, key=lambda x: x["_transfer_by"]):
                    label = ev["Flavor"] or ev["SKU"]
                    timeline_data.append({
                        "SKU": label,
                        "Start": ev["_transfer_by"],
                        "End": ev["_arrival"],
                        "Type": "Lead Time (ship \u2192 arrives at FBA)",
                    })
                    timeline_data.append({
                        "SKU": label,
                        "Start": ev["_arrival"],
                        "End": ev["_stockout"],
                        "Type": "Runway (arrival \u2192 FBA stockout)",
                    })

                _tl_df = pd.DataFrame(timeline_data)
                _tl_df["Start"] = pd.to_datetime(_tl_df["Start"])
                _tl_df["End"] = pd.to_datetime(_tl_df["End"])
                fig_tl = px.timeline(
                    _tl_df, x_start="Start", x_end="End", y="SKU", color="Type",
                    color_discrete_map={
                        "Lead Time (ship \u2192 arrives at FBA)": "#0F3557",
                        "Runway (arrival \u2192 FBA stockout)": "#F58B3D",
                    },
                )
                fig_tl.update_layout(
                    height=max(250, len(_tl_rows) * 50),
                    margin=dict(l=0, r=0, t=10, b=0),
                    xaxis_title="",
                    yaxis_title="",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                )
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

            # --- Full transfer table ---
            st.subheader("Full Transfer Plan")
            st.caption(f"Transfer lead time: {transfer_lt_weeks} weeks | Transfer qty = 2 months of Amazon demand")

            # Drop internal date columns before display
            display_transfer = transfer_df.drop(columns=["_transfer_by", "_arrival", "_stockout"], errors="ignore").copy()
            for _tc in ["FBA Stock", "FBA Monthly Demand", "Transfer Qty", "3PL Available"]:
                if _tc in display_transfer.columns:
                    display_transfer[_tc] = display_transfer[_tc].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
            if "Days Until Transfer" in display_transfer.columns:
                display_transfer["Days Until Transfer"] = display_transfer["Days Until Transfer"].apply(
                    lambda x: f"{int(x)}" if pd.notnull(x) else "\u2014"
                )
            render_html_table(display_transfer, max_height=min(len(display_transfer) * 35 + 38, 500),
                              column_groups=[
                                  ('', ['SKU', 'Flavor']),
                                  ('FBA Status', ['FBA Stock', 'FBA Monthly Demand', 'FBA Stockout']),
                                  ('Transfer Plan', ['Transfer By', 'Days Until Transfer', 'Transfer Qty']),
                                  ('3PL Supply', ['3PL Available', 'Can Fulfill', 'Urgency']),
                              ])
        else:
            st.info("No Amazon FBA SKUs with measurable demand found.")
