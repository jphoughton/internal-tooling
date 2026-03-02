"""Pacing dashboard — reusable across Overview & Marketing pages."""
import streamlit as st
import pandas as pd
from db import (
    get_db,
    get_media_spend, get_amazon_revenue_forecast,
)
from analytics.dtc_demand import build_master_dtc_forecast
from utils.constants import FORECAST_SKUS
from views.marketing import _load_shopify_daily_metrics, _load_gs_spend


def render_pacing(ctx):
    """Render the current-month pacing dashboard.

    Requires ctx keys: cached_waterfall, cached_sku_forecast, load_seasonal_json, biz_vars.
    Returns True if content was rendered, False if data unavailable.
    """
    _cached_waterfall = ctx['cached_waterfall']
    _cached_sku_forecast = ctx['cached_sku_forecast']
    _load_seasonal_json = ctx['load_seasonal_json']
    _load_sku_seasonal_json = ctx['load_sku_seasonal_json']

    # Load Shopify DB metrics (revenue, orders, new/repeat customers)
    _shopify_daily = _load_shopify_daily_metrics()
    if _shopify_daily.empty:
        return False

    # Merge: Shopify DB for revenue/customers, Google Sheet for spend only
    mkt_df = _shopify_daily.copy()
    _gs_spend = _load_gs_spend()
    if not _gs_spend.empty:
        mkt_df = mkt_df.merge(
            _gs_spend[['_date', '_ad_spend']], on='_date', how='left', suffixes=('', '_gs')
        )
    else:
        mkt_df['_ad_spend'] = 0
    mkt_df = mkt_df.sort_values('_date')

    # ============================================================
    # LOAD REVENUE GOALS from Demand Forecast engine
    # ============================================================
    _mkt_summary = None
    _mkt_amz_media = []
    try:
        _seasonal_json = _load_seasonal_json()
        with get_db() as _goals_conn:
            _mkt_media = get_media_spend(_goals_conn, source="All Sources")
            _mkt_amz_media = get_media_spend(_goals_conn, source="Amazon")
            _amz_rev_f = get_amazon_revenue_forecast(_goals_conn)
        import json as _json_pac
        _wf = _cached_waterfall(_json_pac.dumps(_mkt_media, sort_keys=True), 'shopify', 12, _seasonal_json)
        if _wf is not None and not _wf.empty:
            _sku_table = _cached_sku_forecast(
                _wf.to_json(), None, _load_sku_seasonal_json(), _seasonal_json,
            )
            _amz_rev_dict = {r["month"]: r["revenue"] for r in _amz_rev_f if r.get("revenue", 0) > 0}
            _dtc_fc = build_master_dtc_forecast(
                shopify_waterfall_df=_wf,
                shopify_sku_forecast_df=_sku_table,
                horizon_months=12,
                forecast_skus=FORECAST_SKUS,
                media_plan=_mkt_media,
                amazon_revenue_forecast=_amz_rev_dict if _amz_rev_dict else None,
            )
            _mkt_summary = _dtc_fc["summary"]
    except Exception:
        pass

    # Pre-compute current-month pacing vars
    _now = pd.Timestamp.now()
    _cur_month = _now.strftime("%Y-%m")
    _cur_month_name = _now.strftime("%B %Y")
    _days_in_month = _now.days_in_month
    _day_of_month = _now.day
    _pct_month = _day_of_month / _days_in_month

    # MTD actuals from Shopify DB + GS spend
    _cm_mask = mkt_df["_date"].dt.strftime("%Y-%m") == _cur_month
    _cm_nc_rev = mkt_df.loc[_cm_mask, "_nc_revenue"].sum()
    _cm_ret_rev = mkt_df.loc[_cm_mask, "_ret_revenue"].sum()
    _cm_spend = mkt_df.loc[_cm_mask, "_ad_spend"].sum()
    _cm_rev = mkt_df.loc[_cm_mask, "_revenue"].sum()
    _cm_orders = int(mkt_df.loc[_cm_mask, "_orders"].sum())
    _cm_nc = int(mkt_df.loc[_cm_mask, "_nc_orders"].sum())

    # Amazon MTD from DB
    _cm_amz_rev = 0
    _cm_amz_spend = 0
    _cm_amz_nc = 0
    _cm_amz_nc_rev = 0
    try:
        with get_db() as _amz_conn:
            _amz_mtd = _amz_conn.execute(
                "SELECT SUM(revenue) FROM daily_sku_sales WHERE source = 'amazon' AND sale_date >= ? AND sale_date <= ?",
                (f"{_cur_month}-01", _now.strftime("%Y-%m-%d")),
            ).fetchone()
            _cm_amz_rev = float(_amz_mtd[0] or 0)
            _amz_rollup = _amz_conn.execute(
                "SELECT SUM(spend) AS total_spend, SUM(new_customers) AS total_nc, SUM(new_customer_rev) AS total_nc_rev "
                "FROM amazon_daily_rollup WHERE date >= ? AND date <= ?",
                (f"{_cur_month}-01", _now.strftime("%Y-%m-%d")),
            ).fetchone()
            if _amz_rollup and _amz_rollup["total_spend"] is not None:
                _cm_amz_spend = float(_amz_rollup["total_spend"] or 0)
                _cm_amz_nc = int(float(_amz_rollup["total_nc"] or 0))
                _cm_amz_nc_rev = float(_amz_rollup["total_nc_rev"] or 0)
    except Exception:
        pass

    # Revenue GOALS from the forecast engine
    _goal_nc_rev = 0
    _goal_repeat_rev = 0
    _goal_amz_rev = 0
    _has_goals = False
    if _mkt_summary is not None and not _mkt_summary.empty:
        _plan_row = _mkt_summary[_mkt_summary["month"] == _cur_month]
        if not _plan_row.empty:
            _goal_nc_rev = float(_plan_row.iloc[0].get("shopify_new_rev", 0))
            _goal_repeat_rev = float(_plan_row.iloc[0].get("shopify_repeat_rev", 0))
            _goal_amz_rev = float(_plan_row.iloc[0].get("amazon_rev", 0))
            _has_goals = (_goal_nc_rev + _goal_repeat_rev + _goal_amz_rev) > 0

    _goal_total_rev = _goal_nc_rev + _goal_repeat_rev + _goal_amz_rev

    _total_actual_rev = _cm_nc_rev + _cm_ret_rev + _cm_amz_rev
    _remaining_days = _days_in_month - _day_of_month

    # ============================================================
    # RENDER PACING HEADER
    # ============================================================
    st.header(f"{_cur_month_name} Pacing")
    st.caption(f"Day {_day_of_month} of {_days_in_month}")
    st.progress(_pct_month, text=f"{_pct_month*100:.0f}%")

    if not _has_goals:
        st.info("Set up a media spend plan and Amazon forecast on the **Demand Forecast** page for pacing.")
        st.subheader("MTD Summary")
        ts1, ts2, ts3, ts4 = st.columns(4)
        ts1.metric("NC Revenue (MTD)", f"${_cm_nc_rev:,.0f}")
        ts2.metric("Repeat Revenue (MTD)", f"${_cm_ret_rev:,.0f}")
        ts3.metric("Amazon Revenue (MTD)", f"${_cm_amz_rev:,.0f}")
        _total_mtd = _cm_nc_rev + _cm_ret_rev + _cm_amz_rev
        ts4.metric("Total Revenue (MTD)", f"${_total_mtd:,.0f}")
        return True

    # Last 7 days averages
    _l7d_mask = mkt_df["_date"] >= (_now - pd.Timedelta(days=7))
    _l7d_rev = mkt_df.loc[_l7d_mask, "_revenue"].sum() / 7 if _l7d_mask.any() else 0
    _l7d_nc_rev = mkt_df.loc[_l7d_mask, "_nc_revenue"].sum() / 7 if _l7d_mask.any() else 0
    _l7d_ret_rev = mkt_df.loc[_l7d_mask, "_ret_revenue"].sum() / 7 if _l7d_mask.any() else 0
    _l7d_spend = mkt_df.loc[_l7d_mask, "_ad_spend"].sum() / 7 if _l7d_mask.any() else 0
    _l7d_nc = mkt_df.loc[_l7d_mask, "_nc_orders"].sum() / 7 if _l7d_mask.any() else 0

    # Amazon L7D
    _l7d_amz_rev = 0
    _l7d_amz_spend = 0
    _l7d_amz_nc = 0
    _l7d_amz_nc_rev = 0
    try:
        _l7d_start = (_now - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
        with get_db() as _l7_conn:
            _l7_amz = _l7_conn.execute(
                "SELECT SUM(revenue) FROM daily_sku_sales WHERE source = 'amazon' AND sale_date >= ?",
                (_l7d_start,),
            ).fetchone()
            _l7d_amz_rev = float(_l7_amz[0] or 0) / 7
            _l7_rollup = _l7_conn.execute(
                "SELECT SUM(spend) AS total_spend, SUM(new_customers) AS total_nc, SUM(new_customer_rev) AS total_nc_rev "
                "FROM amazon_daily_rollup WHERE date >= ?",
                (_l7d_start,),
            ).fetchone()
            if _l7_rollup and _l7_rollup["total_spend"] is not None:
                _l7d_amz_spend = float(_l7_rollup["total_spend"] or 0) / 7
                _l7d_amz_nc = float(_l7_rollup["total_nc"] or 0) / 7
                _l7d_amz_nc_rev = float(_l7_rollup["total_nc_rev"] or 0) / 7
    except Exception:
        pass

    # Yesterday actuals
    _yesterday = (_now - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    _yd_mask = mkt_df["_date"].dt.strftime("%Y-%m-%d") == _yesterday
    _yd_rev = mkt_df.loc[_yd_mask, "_revenue"].sum()
    _yd_nc_rev = mkt_df.loc[_yd_mask, "_nc_revenue"].sum()
    _yd_ret_rev = mkt_df.loc[_yd_mask, "_ret_revenue"].sum()
    _yd_spend = mkt_df.loc[_yd_mask, "_ad_spend"].sum()
    _yd_nc = int(mkt_df.loc[_yd_mask, "_nc_orders"].sum())

    # Amazon yesterday
    _yd_amz_rev = 0
    _yd_amz_spend = 0
    _yd_amz_nc = 0
    _yd_amz_nc_rev = 0
    try:
        with get_db() as _yd_conn:
            _yd_amz = _yd_conn.execute(
                "SELECT SUM(revenue) FROM daily_sku_sales WHERE source = 'amazon' AND sale_date = ?",
                (_yesterday,),
            ).fetchone()
            _yd_amz_rev = float(_yd_amz[0] or 0)
            _yd_rollup = _yd_conn.execute(
                "SELECT spend, new_customers, new_customer_rev "
                "FROM amazon_daily_rollup WHERE date = ?",
                (_yesterday,),
            ).fetchone()
            if _yd_rollup:
                _yd_amz_spend = float(_yd_rollup[0] or 0)
                _yd_amz_nc = int(float(_yd_rollup[1] or 0))
                _yd_amz_nc_rev = float(_yd_rollup[2] or 0)
    except Exception:
        pass

    # --- Color helpers ---
    def _pace_color(pct, invert=False):
        if invert:
            if pct <= 0.95:
                return "color: #0a7a3e; font-weight: 700; background-color: rgba(16,185,80,0.12)"
            elif pct <= 1.05:
                return "color: #92700c; font-weight: 700; background-color: rgba(245,166,35,0.12)"
            else:
                return "color: #b91c1c; font-weight: 700; background-color: rgba(220,38,38,0.10)"
        else:
            if pct >= 1.05:
                return "color: #0a7a3e; font-weight: 700; background-color: rgba(16,185,80,0.12)"
            elif pct >= 0.95:
                return "color: #92700c; font-weight: 700; background-color: rgba(245,166,35,0.12)"
            else:
                return "color: #b91c1c; font-weight: 700; background-color: rgba(220,38,38,0.10)"

    def _plus_minus_color(val, invert=False):
        if invert:
            if val > 0:
                return "color: #b91c1c; font-weight: 700; background-color: rgba(220,38,38,0.08)"
            elif val < 0:
                return "color: #0a7a3e; font-weight: 700; background-color: rgba(16,185,80,0.08)"
            return ""
        else:
            if val > 0:
                return "color: #0a7a3e; font-weight: 700; background-color: rgba(16,185,80,0.08)"
            elif val < 0:
                return "color: #b91c1c; font-weight: 700; background-color: rgba(220,38,38,0.08)"
            return ""

    def _build_pace_row(label, mtd_actual, goal, l7d_avg, yd_actual, is_spend=False):
        should_be = goal * _pct_month
        pacing = (mtd_actual / should_be) if should_be > 0 else 0
        plus_minus = mtd_actual - should_be
        remaining = goal - mtd_actual
        adjusted_daily = remaining / _remaining_days if _remaining_days > 0 else 0
        eom_pacing = (mtd_actual / _pct_month) / goal if goal > 0 and _pct_month > 0 else 0
        projection = mtd_actual / _pct_month if _pct_month > 0 else 0
        yd_goal = goal / _days_in_month
        yd_pacing = (yd_actual / yd_goal) if yd_goal > 0 else 0
        return {
            "_label": label,
            "_is_spend": is_spend,
            "_pacing_raw": pacing,
            "_plus_minus_raw": plus_minus,
            "_eom_pacing_raw": eom_pacing,
            "_yd_pacing_raw": yd_pacing,
            "": label,
            "Pacing": f"{pacing:.0%}",
            "MTD Actual": f"${mtd_actual:,.0f}",
            "Should Be": f"${should_be:,.0f}",
            "+/- Pacing": f"${plus_minus:+,.0f}",
            "L7D Avg": f"${l7d_avg:,.0f}",
            "Adj. Daily": f"${adjusted_daily:,.0f}",
            "EOM Pacing": f"{eom_pacing:.0%}",
            "Remaining": f"${remaining:,.0f}",
            "Projection": f"${projection:,.0f}",
            "EOM Goal": f"${goal:,.0f}",
            "Yest. Actual": f"${yd_actual:,.0f}",
            "Yest. Goal": f"${yd_goal:,.0f}",
            "Yest. Pace": f"{yd_pacing:.0%}",
        }

    def _style_pace_df(df_raw):
        display_cols = [c for c in df_raw.columns if not c.startswith("_")]
        style_data = df_raw.copy()
        df_display = df_raw[display_cols].copy()

        def _apply_styles(row_styler):
            idx = row_styler.name
            is_spend = style_data.loc[idx, "_is_spend"]
            pacing_raw = style_data.loc[idx, "_pacing_raw"]
            plus_raw = style_data.loc[idx, "_plus_minus_raw"]
            eom_raw = style_data.loc[idx, "_eom_pacing_raw"]
            yd_raw = style_data.loc[idx, "_yd_pacing_raw"]
            styles = [""] * len(row_styler)
            col_map = {c: i for i, c in enumerate(display_cols)}
            if "" in col_map:
                styles[col_map[""]] = "font-weight: 600; color: #0F3557"
            if "Pacing" in col_map:
                styles[col_map["Pacing"]] = _pace_color(pacing_raw, invert=is_spend)
            if "+/- Pacing" in col_map:
                styles[col_map["+/- Pacing"]] = _plus_minus_color(plus_raw, invert=is_spend)
            if "EOM Pacing" in col_map:
                styles[col_map["EOM Pacing"]] = _pace_color(eom_raw, invert=is_spend)
            if "Yest. Pace" in col_map:
                styles[col_map["Yest. Pace"]] = _pace_color(yd_raw, invert=is_spend)
            if "Projection" in col_map:
                styles[col_map["Projection"]] = _pace_color(eom_raw, invert=is_spend)
            return styles

        styled = (
            df_display.style
            .set_properties(**{
                "font-size": "0.84rem",
                "font-family": "Visby CF, DM Sans, -apple-system, sans-serif",
                "color": "#1e2d3d",
                "background-color": "#ffffff",
            })
            .apply(_apply_styles, axis=1)
            .set_table_styles([
                {"selector": "th", "props": [
                    ("background-color", "#F0F4F8"),
                    ("color", "#0F3557"),
                    ("font-weight", "600"),
                    ("font-size", "0.74rem"),
                    ("text-transform", "uppercase"),
                    ("letter-spacing", "0.05em"),
                    ("border-bottom", "2px solid #D6DEE8"),
                    ("padding", "11px 14px"),
                    ("position", "sticky"),
                    ("top", "0"),
                    ("z-index", "1"),
                ]},
                {"selector": "td", "props": [
                    ("border-bottom", "1px solid #F0F4F8"),
                    ("padding", "9px 14px"),
                ]},
            ])
        )
        return styled

    def _render_white_table(styled_df):
        html = styled_df.hide(axis="index").to_html()
        st.markdown(
            '<div style="background:#ffffff;border-radius:12px;overflow:hidden;'
            'box-shadow:0 2px 12px rgba(15,53,87,0.08);border:1px solid #E8EDF3;'
            'width:100%;">'
            '<style>'
            '.pace-table table { width:100%; border-collapse:collapse; }'
            '.pace-table th, .pace-table td { white-space:nowrap; }'
            '.pace-table th { position:sticky; top:0; z-index:1; background-color:#F0F4F8 !important; }'
            '.pace-table tr:hover td:not([style*="background-color"]) { background:#F7FAFC !important; }'
            '.pace-table td[style*="color"] { -webkit-text-fill-color: unset; }'
            '</style>'
            f'<div class="pace-table" style="overflow-x:auto;max-height:600px;overflow-y:auto;">{html}</div></div>',
            unsafe_allow_html=True,
        )

    # -- DTC actuals --
    _goal_dtc_rev = _goal_nc_rev + _goal_repeat_rev
    _cm_dtc_rev = _cm_nc_rev + _cm_ret_rev
    _l7d_dtc_rev = _l7d_nc_rev + _l7d_ret_rev
    _yd_dtc_rev = _yd_nc_rev + _yd_ret_rev
    _cm_dtc_spend = _cm_spend
    _l7d_dtc_spend = _l7d_spend
    _yd_dtc_spend = _yd_spend

    # -- Combined Roll Up totals (DTC + Amazon) --
    _cm_total_spend = _cm_dtc_spend + _cm_amz_spend
    _l7d_total_spend = _l7d_dtc_spend + _l7d_amz_spend
    _yd_total_spend = _yd_dtc_spend + _yd_amz_spend
    _cm_total_nc_rev = _cm_nc_rev + _cm_amz_nc_rev
    _cm_total_nc = _cm_nc + _cm_amz_nc
    _cm_total_repeat_rev = _cm_ret_rev

    # Efficiency metrics
    _business_mer = _total_actual_rev / _cm_total_spend if _cm_total_spend > 0 else 0
    _total_nc_roas = _cm_total_nc_rev / _cm_total_spend if _cm_total_spend > 0 else 0
    _blended_cpa = _cm_total_spend / _cm_total_nc if _cm_total_nc > 0 else 0

    # DTC efficiency
    _dtc_nc_roas = _cm_nc_rev / _cm_dtc_spend if _cm_dtc_spend > 0 else 0
    _dtc_nc_aov = _cm_nc_rev / _cm_nc if _cm_nc > 0 else 0
    _dtc_cpa = _cm_dtc_spend / _cm_nc if _cm_nc > 0 else 0
    # -- Spend goals from media plan --
    _goal_spend = 0
    _spend_plan = [m for m in _mkt_media if m.get("month") == _cur_month]
    if _spend_plan:
        _goal_spend = float(_spend_plan[0].get("spend", 0))

    _goal_amz_spend = 0
    _amz_spend_plan = [m for m in _mkt_amz_media if m.get("month") == _cur_month]
    if _amz_spend_plan:
        _goal_amz_spend = float(_amz_spend_plan[0].get("spend", 0))

    # ============================================================
    # CHANNEL FILTER
    # ============================================================
    _nav_channel = ctx.get('channel', 'Rollup')
    _chan_sel = {'DTC': 'DTC', 'Amazon': 'Amazon', 'Rollup': 'All'}.get(_nav_channel, 'All')

    # ============================================================
    # ROLL UP — DTC + Amazon combined
    # ============================================================
    if _chan_sel in ("All", "Roll Up"):
        st.subheader("Roll Up")
        st.caption('Combined Shopify + Amazon pacing against monthly goals from the demand forecast engine.')

        eff1, eff2, eff3, eff4 = st.columns(4)
        eff1.metric("Business MER", f"{_business_mer:.2f}x",
                    help="Total Revenue / Total Spend")
        eff2.metric("Total NC ROAS", f"{_total_nc_roas:.2f}x",
                    help="Total NC Revenue / Total Spend")
        eff3.metric("New Customers", f"{_cm_total_nc:,}",
                    help="DTC new customer orders")
        eff4.metric("Blended CPA", f"${_blended_cpa:,.0f}",
                    help="Total Spend / Total New Customers")

        _rollup_rows = []
        _goal_total_spend = _goal_spend + _goal_amz_spend
        if _goal_total_spend > 0:
            _rollup_rows.append(_build_pace_row("Total Spend", _cm_total_spend, _goal_total_spend,
                                                _l7d_total_spend, _yd_total_spend, is_spend=True))
        _rollup_rows.extend([
            _build_pace_row("Total Revenue", _total_actual_rev, _goal_total_rev,
                            _l7d_rev + _l7d_amz_rev, _yd_rev + _yd_amz_rev),
            _build_pace_row("Total NC Rev", _cm_total_nc_rev, _goal_nc_rev,
                            _l7d_nc_rev + _l7d_amz_nc_rev, _yd_nc_rev + _yd_amz_nc_rev),
            _build_pace_row("Repeat Revenue", _cm_total_repeat_rev, _goal_repeat_rev,
                            _l7d_ret_rev, _yd_ret_rev),
        ])
        _rollup_df = pd.DataFrame(_rollup_rows)
        _render_white_table(_style_pace_df(_rollup_df))

    # ============================================================
    # DTC (Shopify)
    # ============================================================
    if _chan_sel in ("All", "DTC"):
        if _chan_sel == "All":
            st.markdown("---")
        st.subheader("DTC (Shopify)")
        st.caption('Shopify-only pacing vs monthly revenue goals from media spend plan.')

        dtc_k1, dtc_k2, dtc_k3, dtc_k4 = st.columns(4)
        dtc_k1.metric("NC ROAS", f"{_dtc_nc_roas:.2f}x")
        dtc_k2.metric("NC Orders", f"{_cm_nc:,}")
        dtc_k3.metric("NC AOV", f"${_dtc_nc_aov:,.0f}")
        dtc_k4.metric("CPA", f"${_dtc_cpa:,.0f}")

        _dtc_rows = []
        if _goal_spend > 0:
            _dtc_rows.append(_build_pace_row("Spend", _cm_dtc_spend, _goal_spend,
                                             _l7d_dtc_spend, _yd_dtc_spend, is_spend=True))
        _dtc_rows.extend([
            _build_pace_row("Revenue", _cm_dtc_rev, _goal_dtc_rev, _l7d_dtc_rev, _yd_dtc_rev),
            _build_pace_row("New Customer Rev", _cm_nc_rev, _goal_nc_rev, _l7d_nc_rev, _yd_nc_rev),
            _build_pace_row("Repeat Customer Rev", _cm_ret_rev, _goal_repeat_rev, _l7d_ret_rev, _yd_ret_rev),
        ])
        _dtc_df = pd.DataFrame(_dtc_rows)
        _render_white_table(_style_pace_df(_dtc_df))

    # ============================================================
    # AMAZON
    # ============================================================
    if _chan_sel in ("All", "Amazon"):
        if _chan_sel == "All":
            st.markdown("---")
        st.subheader("Amazon")
        st.caption('Amazon-only pacing vs revenue goals from amazon_revenue_forecast.')

        _amz_cpa = _cm_amz_spend / _cm_amz_nc if _cm_amz_nc > 0 else 0
        amz_k1, amz_k2, amz_k3, amz_k4 = st.columns(4)
        amz_k1.metric("Revenue MTD", f"${_cm_amz_rev:,.0f}")
        amz_k2.metric("Spend MTD", f"${_cm_amz_spend:,.0f}")
        amz_k3.metric("New Customers", f"{_cm_amz_nc:,}")
        amz_k4.metric("CPA", f"${_amz_cpa:,.0f}")

        _amz_rows = []
        if _goal_amz_spend > 0:
            _amz_rows.append(
                _build_pace_row("Spend", _cm_amz_spend, _goal_amz_spend, _l7d_amz_spend, _yd_amz_spend, is_spend=True),
            )
        _amz_rows.append(
            _build_pace_row("Revenue", _cm_amz_rev, _goal_amz_rev, _l7d_amz_rev, _yd_amz_rev),
        )
        _amz_rows.append(
            _build_pace_row("NC Revenue", _cm_amz_nc_rev, 0, _l7d_amz_nc_rev, _yd_amz_nc_rev),
        )
        _amz_df = pd.DataFrame(_amz_rows)
        _render_white_table(_style_pace_df(_amz_df))

    return True
