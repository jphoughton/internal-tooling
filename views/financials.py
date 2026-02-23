"""Financials page — Highbeam/Ramp bank data, cash flow, P&L."""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from db import get_db, read_sql
from ui.components import render_html_table, smart_date_filter


def render(ctx):
    """Render the Financials page."""
    st.title("Financials")

    with get_db() as conn:
        try:
            fin_cnt = conn.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0]
        except Exception:
            fin_cnt = 0

    if fin_cnt == 0:
        st.info("No transaction data yet. Upload a Ramp or Highbeam CSV on the **Settings** page.")
    else:
        with get_db() as conn:
            # Load all data — we'll filter intelligently
            fin_all = read_sql("SELECT * FROM bank_transactions ORDER BY date", conn)

        fin_all["_date"] = pd.to_datetime(fin_all["date"], format="mixed", dayfirst=False)
        fin_all = fin_all.sort_values("_date")
        fin_all["_amount"] = fin_all["amount"].astype(float)

        # Determine data source type
        has_highbeam = (fin_all["source"] == "highbeam").any() if "source" in fin_all.columns else False
        has_ramp = (fin_all["source"] == "ramp").any() if "source" in fin_all.columns else False
        has_direction = "direction" in fin_all.columns and fin_all["direction"].notna().any()

        # Date range filter with smart presets
        fin_start, fin_end = smart_date_filter(
            fin_all["_date"].min().date(), fin_all["_date"].max().date(), "fin"
        )
        fin_all = fin_all[(fin_all["_date"].dt.date >= fin_start) & (fin_all["_date"].dt.date <= fin_end)]

        if fin_all.empty:
            st.warning("No transactions in the selected date range.")
        else:
            # Exclude internal transfers and Ramp CC payments (avoid double-counting)
            _is_transfer = fin_all.get("is_transfer", pd.Series(0, index=fin_all.index)).fillna(0).astype(int)
            _is_ramp_dup = fin_all.get("is_ramp_duplicate", pd.Series(0, index=fin_all.index)).fillna(0).astype(int)
            fin_clean = fin_all[(_is_transfer == 0) & (_is_ramp_dup == 0)].copy()

            excluded_count = len(fin_all) - len(fin_clean)
            if excluded_count > 0:
                st.caption(
                    f"{excluded_count} transactions excluded (internal transfers & CC payments)."
                )

            # Separate inflows and outflows
            if has_direction and has_highbeam:
                # Highbeam data has Credit/Debit direction
                inflows = fin_clean[fin_clean["direction"] == "credit"]
                outflows = fin_clean[fin_clean["direction"] == "debit"]
            else:
                # Ramp data is all outflows (card charges)
                inflows = pd.DataFrame()
                outflows = fin_clean

            total_in = inflows["_amount"].sum() if not inflows.empty else 0
            total_out = outflows["_amount"].sum() if not outflows.empty else 0
            net_cash = total_in - total_out
            days_range = max((fin_clean["_date"].max() - fin_clean["_date"].min()).days, 1)

            # === KPI CARDS ===
            fk1, fk2, fk3, fk4, fk5 = st.columns(5)
            fk1.metric("Cash In", f"${total_in:,.0f}")
            fk2.metric("Cash Out", f"${total_out:,.0f}")
            fk3.metric("Net Cash Flow", f"${net_cash:,.0f}",
                        delta=f"${net_cash/days_range*30:,.0f}/mo",
                        delta_color="normal" if net_cash >= 0 else "inverse")
            fk4.metric("Avg Daily In", f"${total_in/days_range:,.0f}")
            fk5.metric("Avg Daily Out", f"${total_out/days_range:,.0f}")

            st.divider()

            # === CASH FLOW CHART ===
            st.subheader("Cash Flow")
            _cf_group = st.radio("Group by", ["Daily", "Weekly", "Monthly"], horizontal=True, key="cf_group")
            if _cf_group == "Weekly":
                fin_clean["_period"] = fin_clean["_date"].dt.to_period("W").apply(lambda x: x.start_time)
            elif _cf_group == "Monthly":
                fin_clean["_period"] = fin_clean["_date"].dt.to_period("M").apply(lambda x: x.start_time)
            else:
                fin_clean["_period"] = fin_clean["_date"]

            if has_direction and not inflows.empty:
                # Show inflows vs outflows
                inflows["_period"] = fin_clean.loc[inflows.index, "_period"] if not inflows.empty else pd.Series()
                outflows["_period"] = fin_clean.loc[outflows.index, "_period"] if not outflows.empty else pd.Series()

                cf_in = inflows.groupby("_period")["_amount"].sum().reset_index().rename(columns={"_amount": "Cash In"})
                cf_out = outflows.groupby("_period")["_amount"].sum().reset_index().rename(columns={"_amount": "Cash Out"})
                cf_merged = cf_in.merge(cf_out, on="_period", how="outer").fillna(0).sort_values("_period")
                cf_merged["Net"] = cf_merged["Cash In"] - cf_merged["Cash Out"]

                fig_cf = go.Figure()
                fig_cf.add_trace(go.Bar(
                    x=cf_merged["_period"], y=cf_merged["Cash In"],
                    name="Cash In", marker_color="#2DA87E",
                ))
                fig_cf.add_trace(go.Bar(
                    x=cf_merged["_period"], y=-cf_merged["Cash Out"],
                    name="Cash Out", marker_color="#E05252",
                ))
                fig_cf.add_trace(go.Scatter(
                    x=cf_merged["_period"], y=cf_merged["Net"],
                    name="Net Cash Flow", mode="lines+markers",
                    line=dict(color="#0F3557", width=2),
                ))
                fig_cf.update_layout(
                    barmode="relative", height=400,
                    margin=dict(l=0, r=0, t=30, b=0),
                    yaxis_title="$",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                )
                st.plotly_chart(fig_cf, use_container_width=True)
            else:
                # Outflows only (Ramp data)
                cf_out = outflows.groupby("_period")["_amount"].sum().reset_index()
                fig_cf = go.Figure()
                fig_cf.add_trace(go.Bar(
                    x=cf_out["_period"], y=cf_out["_amount"],
                    name="Spend", marker_color="#5BB8D4",
                ))
                if len(cf_out) > 5:
                    cf_out["_ma"] = cf_out["_amount"].rolling(window=min(7, len(cf_out)), min_periods=1).mean()
                    fig_cf.add_trace(go.Scatter(
                        x=cf_out["_period"], y=cf_out["_ma"],
                        name="Moving Avg", mode="lines",
                        line=dict(color="#E05252", width=2),
                    ))
                fig_cf.update_layout(
                    height=400, margin=dict(l=0, r=0, t=30, b=0), yaxis_title="$",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                )
                st.plotly_chart(fig_cf, use_container_width=True)

            st.divider()

            # === INFLOWS BREAKDOWN (if Highbeam data) ===
            if not inflows.empty:
                st.subheader("Cash Inflows")
                in_cats = inflows.groupby("category")["_amount"].sum().sort_values(ascending=False).reset_index()
                in_cats.columns = ["Source", "Total"]

                in_c1, in_c2 = st.columns([2, 1])
                with in_c1:
                    fig_in = go.Figure(data=[go.Pie(
                        labels=in_cats["Source"], values=in_cats["Total"],
                        hole=0.4,
                        marker_colors=["#2DA87E", "#5BB8D4", "#F58B3D", "#6B8FA3", "#F4A3A0"][:len(in_cats)],
                    )])
                    fig_in.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0))
                    st.plotly_chart(fig_in, use_container_width=True)
                with in_c2:
                    in_cats["Pct"] = (in_cats["Total"] / in_cats["Total"].sum() * 100).round(1).apply(lambda x: f"{x:.1f}%")
                    in_cats["Total"] = in_cats["Total"].apply(lambda x: f"${x:,.0f}")
                    render_html_table(in_cats)
                st.divider()

            # === OUTFLOWS BREAKDOWN ===
            if not outflows.empty:
                st.subheader("Cash Outflows")
                out_cats = outflows.groupby("category")["_amount"].sum().sort_values(ascending=False).reset_index()
                out_cats.columns = ["Category", "Total"]

                out_c1, out_c2 = st.columns([2, 1])
                with out_c1:
                    fig_out = go.Figure()
                    _colors = ["#E05252", "#F58B3D", "#5BB8D4", "#6B8FA3", "#F4A3A0",
                               "#0F3557", "#2DA87E", "#FFC857", "#7a8da0", "#C5E0A5"]
                    fig_out.add_trace(go.Bar(
                        x=out_cats["Category"], y=out_cats["Total"],
                        marker_color=_colors[:len(out_cats)],
                    ))
                    fig_out.update_layout(height=350, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="$")
                    st.plotly_chart(fig_out, use_container_width=True)

                with out_c2:
                    out_cats["Pct"] = (out_cats["Total"] / out_cats["Total"].sum() * 100).round(1).apply(lambda x: f"{x:.1f}%")
                    out_cats["Total"] = out_cats["Total"].apply(lambda x: f"${x:,.0f}")
                    render_html_table(out_cats)

                st.divider()

                # --- Top Merchants (outflows only) ---
                st.subheader("Top Vendors")
                merchant_col = "merchant" if "merchant" in outflows.columns else "description"
                merch_spend = outflows.groupby(merchant_col).agg(
                    Total=("_amount", "sum"),
                    Count=("_amount", "count"),
                    Avg=("_amount", "mean"),
                ).sort_values("Total", ascending=False).reset_index().head(20)
                merch_spend.columns = ["Vendor", "Total Spend", "# Payments", "Avg Payment"]
                merch_spend["Total Spend"] = merch_spend["Total Spend"].apply(lambda x: f"${x:,.0f}")
                merch_spend["Avg Payment"] = merch_spend["Avg Payment"].apply(lambda x: f"${x:,.0f}")
                render_html_table(merch_spend)

                st.divider()

            # === MONTHLY P&L SUMMARY ===
            st.subheader("Monthly P&L")
            fin_clean["_month"] = fin_clean["_date"].dt.to_period("M").astype(str)
            if has_direction:
                monthly_in = fin_clean[fin_clean["direction"] == "credit"].groupby("_month")["_amount"].sum()
                monthly_out = fin_clean[fin_clean["direction"] == "debit"].groupby("_month")["_amount"].sum()
                pnl = pd.DataFrame({"Cash In": monthly_in, "Cash Out": monthly_out}).fillna(0)
                pnl["Net"] = pnl["Cash In"] - pnl["Cash Out"]
                pnl = pnl.sort_index()
                pnl_display = pnl.copy()
                for c in pnl_display.columns:
                    pnl_display[c] = pnl_display[c].apply(lambda x: f"${x:,.0f}")
                render_html_table(pnl_display)
            else:
                # Outflows only — show by category
                monthly_cat = outflows.pivot_table(
                    index="category", columns="_month" if "_month" in outflows.columns else outflows["_date"].dt.to_period("M").astype(str),
                    values="_amount", aggfunc="sum", fill_value=0,
                ).round(0)
                monthly_cat.loc["TOTAL"] = monthly_cat.sum()
                monthly_display = monthly_cat.map(lambda x: f"${x:,.0f}")
                render_html_table(monthly_display)

            st.divider()

            # --- Spend by Department (Ramp data) ---
            if "department" in fin_clean.columns:
                depts = outflows[outflows["department"].notna() & (outflows["department"] != "") & (outflows["department"] != "nan")] if not outflows.empty else pd.DataFrame()
                if not depts.empty:
                    st.subheader("Spend by Department")
                    dept_spend = depts.groupby("department")["_amount"].sum().sort_values(ascending=False).reset_index()
                    dept_spend.columns = ["Department", "Total"]
                    fig_dept = go.Figure(data=[go.Pie(
                        labels=dept_spend["Department"], values=dept_spend["Total"], hole=0.4,
                    )])
                    fig_dept.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0))
                    st.plotly_chart(fig_dept, use_container_width=True)
                    st.divider()

            # --- Spend by User (Ramp data) ---
            if "user_name" in fin_clean.columns:
                users_df = outflows[outflows["user_name"].notna() & (outflows["user_name"] != "") & (outflows["user_name"] != "nan")] if not outflows.empty else pd.DataFrame()
                if not users_df.empty:
                    st.subheader("Spend by Team Member")
                    user_spend = users_df.groupby("user_name")["_amount"].agg(["sum", "count"]).sort_values("sum", ascending=False).reset_index()
                    user_spend.columns = ["Team Member", "Total Spend", "Transactions"]
                    user_spend["Total Spend"] = user_spend["Total Spend"].apply(lambda x: f"${x:,.0f}")
                    render_html_table(user_spend)
                    st.divider()

            # --- Raw Transaction Table ---
            with st.expander("All Transactions"):
                _show_cols = ["_date", "direction", "merchant", "_amount", "category"]
                if "department" in fin_clean.columns:
                    _show_cols.append("department")
                if "user_name" in fin_clean.columns:
                    _show_cols.append("user_name")
                display_df = fin_clean[[c for c in _show_cols if c in fin_clean.columns]].copy()
                display_df.columns = [c.replace("_", "").title() for c in display_df.columns]
                if "Amount" in display_df.columns:
                    display_df["Amount"] = display_df["Amount"].apply(lambda x: f"${x:,.2f}")
                if "Date" in display_df.columns:
                    display_df["Date"] = pd.to_datetime(display_df["Date"]).dt.strftime("%Y-%m-%d")
                render_html_table(display_df.sort_values("Date", ascending=False))
