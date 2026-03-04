"""Pacing dashboard — reusable across Overview & Marketing pages."""
import logging
import streamlit as st
import pandas as pd
from db import (
    get_db,
    get_media_spend, get_amazon_revenue_forecast,
)
from analytics.dtc_demand import build_master_dtc_forecast
from analytics.metrics import (
    get_channel_revenue, get_amazon_spend, get_nc_stats,
    nc_revenue_fraction, get_total_customers,
    compute_mer, compute_nc_roas, compute_nc_cpa, compute_aov,
)
from ui.pacing_helpers import build_pace_row, style_pace_df, render_white_table
from utils.constants import FORECAST_SKUS
from utils.date_helpers import business_today, business_yesterday
from views.marketing import _load_shopify_daily_metrics, _load_gs_spend

log = logging.getLogger(__name__)


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
    _wf = None
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

    # Pre-compute current-month pacing vars (data through yesterday only)
    _today = business_today()
    _yesterday = business_yesterday()
    _yesterday_str = str(_yesterday)
    _cur_month = _today.strftime("%Y-%m")
    _cur_month_name = _today.strftime("%B %Y")
    import calendar as _cal
    _days_in_month = _cal.monthrange(_today.year, _today.month)[1]
    # elapsed = yesterday's day if in same month, else 0 (first day of month)
    _day_of_month = _yesterday.day if _yesterday.month == _today.month else 0
    _pct_month = _day_of_month / _days_in_month if _days_in_month > 0 else 0

    # MTD actuals from Shopify DB + GS spend (through yesterday)
    _cm_mask = (mkt_df["_date"].dt.strftime("%Y-%m") == _cur_month) & (mkt_df["_date"].dt.date <= _yesterday)
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
            _cm_amz_rev = get_channel_revenue(_amz_conn, 'amazon', f"{_cur_month}-01", _yesterday_str)
            _cm_amz_spend = get_amazon_spend(_amz_conn, f"{_cur_month}-01", _yesterday_str)
            _nc = get_nc_stats(_amz_conn, 'amazon', f"{_cur_month}-01", _yesterday_str)
            _cm_amz_nc = _nc['new_customers']
            _cm_amz_nc_rev = nc_revenue_fraction(_nc['oi_total_rev'], _nc['oi_new_rev'], _cm_amz_rev)
    except Exception as e:
        log.warning("Failed to load Amazon MTD metrics: %s", e)

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
    _remaining_days = max(_days_in_month - _day_of_month, 1)

    # ============================================================
    # RENDER PACING HEADER
    # ============================================================
    st.header(f"{_cur_month_name} Pacing")
    st.caption(f"Day {_day_of_month} of {_days_in_month}")
    st.progress(_pct_month, text=f"{_pct_month*100:.0f}%")

    _nav_channel = ctx.get('channel', 'Rollup')

    if not _has_goals:
        st.info("Set up a media spend plan and Amazon forecast on the **Demand Forecast** page for pacing.")
        st.subheader("MTD Summary")
        if _nav_channel == 'DTC':
            ts1, ts2, ts3 = st.columns(3)
            ts1.metric("NC Revenue (MTD)", f"${_cm_nc_rev:,.0f}")
            ts2.metric("Repeat Revenue (MTD)", f"${_cm_ret_rev:,.0f}")
            _total_mtd = _cm_nc_rev + _cm_ret_rev
            ts3.metric("Total Revenue (MTD)", f"${_total_mtd:,.0f}")
        elif _nav_channel == 'Amazon':
            ts1, ts2 = st.columns(2)
            ts1.metric("Amazon Revenue (MTD)", f"${_cm_amz_rev:,.0f}")
            ts2.metric("Amazon Spend (MTD)", f"${_cm_amz_spend:,.0f}")
        else:
            ts1, ts2, ts3, ts4 = st.columns(4)
            ts1.metric("NC Revenue (MTD)", f"${_cm_nc_rev:,.0f}")
            ts2.metric("Repeat Revenue (MTD)", f"${_cm_ret_rev:,.0f}")
            ts3.metric("Amazon Revenue (MTD)", f"${_cm_amz_rev:,.0f}")
            _total_mtd = _cm_nc_rev + _cm_ret_rev + _cm_amz_rev
            ts4.metric("Total Revenue (MTD)", f"${_total_mtd:,.0f}")
        return True

    # Last 7 days averages (7 complete days ending at yesterday)
    _l7d_start = _yesterday - pd.Timedelta(days=6)
    _l7d_mask = (mkt_df["_date"].dt.date >= _l7d_start) & (mkt_df["_date"].dt.date <= _yesterday)
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
        _l7d_start_str = str(_l7d_start)
        with get_db() as _l7_conn:
            _l7d_amz_rev = get_channel_revenue(_l7_conn, 'amazon', _l7d_start_str, _yesterday_str) / 7
            _l7d_amz_spend = get_amazon_spend(_l7_conn, _l7d_start_str, _yesterday_str) / 7
            _nc = get_nc_stats(_l7_conn, 'amazon', _l7d_start_str, _yesterday_str)
            _l7d_amz_nc = _nc['new_customers'] / 7
            _l7d_amz_nc_rev = nc_revenue_fraction(_nc['oi_total_rev'], _nc['oi_new_rev'], _l7d_amz_rev * 7) / 7
    except Exception as e:
        log.warning("Failed to load Amazon L7D metrics: %s", e)

    # Yesterday actuals
    _yd_mask = mkt_df["_date"].dt.strftime("%Y-%m-%d") == _yesterday_str
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
            _yd_amz_rev = get_channel_revenue(_yd_conn, 'amazon', _yesterday_str, _yesterday_str)
            _yd_amz_spend = get_amazon_spend(_yd_conn, _yesterday_str, _yesterday_str)
            _nc = get_nc_stats(_yd_conn, 'amazon', _yesterday_str, _yesterday_str)
            _yd_amz_nc = _nc['new_customers']
            _yd_amz_nc_rev = nc_revenue_fraction(_nc['oi_total_rev'], _nc['oi_new_rev'], _yd_amz_rev)
    except Exception as e:
        log.warning("Failed to load Amazon yesterday metrics: %s", e)

    # Repeat customer counts (DTC + Amazon)
    _cm_dtc_total_cust = 0
    _cm_amz_total_cust = 0
    try:
        with get_db() as _rc_conn:
            _cm_dtc_total_cust = get_total_customers(_rc_conn, 'shopify', f"{_cur_month}-01", _yesterday_str)
            _cm_amz_total_cust = get_total_customers(_rc_conn, 'amazon', f"{_cur_month}-01", _yesterday_str)
    except Exception as e:
        log.warning("Failed to load repeat customer counts: %s", e)
    _cm_dtc_repeat_cust = max(_cm_dtc_total_cust - _cm_nc, 0)
    _cm_amz_repeat_cust = max(_cm_amz_total_cust - _cm_amz_nc, 0)

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

    # NC totals (DTC + Amazon)
    _cm_total_nc = _cm_nc + _cm_amz_nc
    _cm_total_nc_rev = _cm_nc_rev + _cm_amz_nc_rev
    _l7d_total_nc = _l7d_nc + _l7d_amz_nc
    _l7d_total_nc_rev = _l7d_nc_rev + _l7d_amz_nc_rev
    _yd_total_nc = _yd_nc + _yd_amz_nc
    _yd_total_nc_rev = _yd_nc_rev + _yd_amz_nc_rev

    # Repeat totals (DTC + Amazon implied repeat)
    _cm_amz_repeat_rev = max(_cm_amz_rev - _cm_amz_nc_rev, 0)
    _cm_total_repeat_rev = _cm_ret_rev + _cm_amz_repeat_rev
    _l7d_amz_repeat_rev = max(_l7d_amz_rev - _l7d_amz_nc_rev, 0)
    _l7d_total_repeat_rev = _l7d_ret_rev + _l7d_amz_repeat_rev
    _yd_amz_repeat_rev = max(_yd_amz_rev - _yd_amz_nc_rev, 0)
    _yd_total_repeat_rev = _yd_ret_rev + _yd_amz_repeat_rev

    # Total repeat customers
    _cm_total_repeat_cust = _cm_dtc_repeat_cust + _cm_amz_repeat_cust

    # Efficiency metrics
    _business_mer = compute_mer(_total_actual_rev, _cm_total_spend)
    _total_nc_roas = compute_nc_roas(_cm_total_nc_rev, _cm_total_spend)
    _total_nc_cpa = compute_nc_cpa(_cm_total_spend, _cm_total_nc)
    _dtc_nc_roas = compute_nc_roas(_cm_nc_rev, _cm_dtc_spend)
    _dtc_nc_aov = compute_aov(_cm_nc_rev, _cm_nc)
    _dtc_cpa = compute_nc_cpa(_cm_dtc_spend, _cm_nc)

    # L7D / Yesterday ratio metrics
    _l7d_total_rev = _l7d_rev + _l7d_amz_rev
    _l7d_mer = compute_mer(_l7d_total_rev, _l7d_total_spend)
    _l7d_total_nc_roas = compute_nc_roas(_l7d_total_nc_rev, _l7d_total_spend)
    _l7d_total_nc_cpa = compute_nc_cpa(_l7d_total_spend, _l7d_total_nc)
    _yd_total_rev = _yd_rev + _yd_amz_rev
    _yd_mer = compute_mer(_yd_total_rev, _yd_total_spend)
    _yd_total_nc_roas = compute_nc_roas(_yd_total_nc_rev, _yd_total_spend)
    _yd_total_nc_cpa = compute_nc_cpa(_yd_total_spend, _yd_total_nc)

    # -- Spend goals from media plan --
    _goal_spend = 0
    _spend_plan = [m for m in _mkt_media if m.get("month") == _cur_month]
    if _spend_plan:
        _goal_spend = float(_spend_plan[0].get("spend", 0))

    _goal_amz_spend = 0
    _amz_spend_plan = [m for m in _mkt_amz_media if m.get("month") == _cur_month]
    if _amz_spend_plan:
        _goal_amz_spend = float(_amz_spend_plan[0].get("spend", 0))

    _goal_total_spend = _goal_spend + _goal_amz_spend

    # NC count goal from waterfall
    _goal_nc_count = 0
    if _wf is not None and not _wf.empty:
        _wf_nc_row = _wf[_wf['month'] == _cur_month]
        if not _wf_nc_row.empty:
            _goal_nc_count = float(_wf_nc_row.iloc[0].get('new_customers_acquired', 0))

    # Derived ratio goals
    _goal_mer = _goal_total_rev / _goal_total_spend if _goal_total_spend > 0 else 0
    _goal_nc_roas_ratio = _goal_nc_rev / _goal_spend if _goal_spend > 0 else 0
    _goal_nc_cpa_target = _goal_spend / _goal_nc_count if _goal_nc_count > 0 else 0

    # ============================================================
    # CHANNEL FILTER
    # ============================================================
    _chan_sel = {'DTC': 'DTC', 'Amazon': 'Amazon', 'Rollup': 'Rollup'}.get(_nav_channel, 'Rollup')

    # ============================================================
    # ROLL UP — DTC + Amazon combined
    # ============================================================
    if _chan_sel in ("All", "Rollup"):
        st.subheader("Roll Up")
        st.caption('Combined Shopify + Amazon pacing against monthly goals from the demand forecast engine.')

        # Row 1: Efficiency ratios
        eff1, eff2, eff3 = st.columns(3)
        eff1.metric("Business MER", f"{_business_mer:.2f}x",
                    help="Total Revenue / Total Spend")
        eff2.metric("Total NC ROAS", f"{_total_nc_roas:.2f}x",
                    help="Total NC Revenue / Total Spend")
        eff3.metric("Total NC CPA", f"${_total_nc_cpa:,.0f}",
                    help="Total Spend / Total New Customers")

        # Row 2: Revenue & Spend
        rev1, rev2, rev3 = st.columns(3)
        rev1.metric("Total Spend", f"${_cm_total_spend:,.0f}")
        rev2.metric("Total Revenue", f"${_total_actual_rev:,.0f}")
        rev3.metric("Total New Customers", f"{_cm_total_nc:,}")

        # Row 3: NC/Repeat split
        nr1, nr2, nr3 = st.columns(3)
        nr1.metric("Total NC Revenue", f"${_cm_total_nc_rev:,.0f}")
        nr2.metric("Total Repeat Revenue", f"${_cm_total_repeat_rev:,.0f}")
        nr3.metric("Total Repeat Customers", f"{_cm_total_repeat_cust:,}")

        _rollup_rows = []
        if _goal_total_spend > 0:
            _rollup_rows.append(build_pace_row("Total Spend", _cm_total_spend, _goal_total_spend,
                                               _l7d_total_spend, _yd_total_spend, _pct_month, _days_in_month, _remaining_days, is_spend=True))
        _rollup_rows.extend([
            build_pace_row("Total Revenue", _total_actual_rev, _goal_total_rev,
                           _l7d_total_rev, _yd_total_rev, _pct_month, _days_in_month, _remaining_days),
            build_pace_row("Total NC Revenue", _cm_total_nc_rev, _goal_nc_rev,
                           _l7d_total_nc_rev, _yd_total_nc_rev, _pct_month, _days_in_month, _remaining_days),
            build_pace_row("Total Repeat Revenue", _cm_total_repeat_rev, _goal_repeat_rev,
                           _l7d_total_repeat_rev, _yd_total_repeat_rev, _pct_month, _days_in_month, _remaining_days),
        ])
        if _goal_nc_count > 0:
            _rollup_rows.append(build_pace_row("Total New Customers", _cm_total_nc, _goal_nc_count,
                                               _l7d_total_nc, _yd_total_nc, _pct_month, _days_in_month, _remaining_days, fmt='count'))
        if _goal_mer > 0:
            _rollup_rows.append(build_pace_row("Business MER", _business_mer, _goal_mer,
                                               _l7d_mer, _yd_mer, _pct_month, _days_in_month, _remaining_days, fmt='ratio'))
        if _goal_nc_roas_ratio > 0:
            _rollup_rows.append(build_pace_row("Total NC ROAS", _total_nc_roas, _goal_nc_roas_ratio,
                                               _l7d_total_nc_roas, _yd_total_nc_roas, _pct_month, _days_in_month, _remaining_days, fmt='ratio'))
        if _goal_nc_cpa_target > 0:
            _rollup_rows.append(build_pace_row("Total NC CPA", _total_nc_cpa, _goal_nc_cpa_target,
                                               _l7d_total_nc_cpa, _yd_total_nc_cpa, _pct_month, _days_in_month, _remaining_days, is_spend=True))
        _rollup_df = pd.DataFrame(_rollup_rows)
        render_white_table(style_pace_df(_rollup_df))

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
            _dtc_rows.append(build_pace_row("Spend", _cm_dtc_spend, _goal_spend,
                                            _l7d_dtc_spend, _yd_dtc_spend, _pct_month, _days_in_month, _remaining_days, is_spend=True))
        _dtc_rows.extend([
            build_pace_row("Revenue", _cm_dtc_rev, _goal_dtc_rev, _l7d_dtc_rev, _yd_dtc_rev, _pct_month, _days_in_month, _remaining_days),
            build_pace_row("New Customer Rev", _cm_nc_rev, _goal_nc_rev, _l7d_nc_rev, _yd_nc_rev, _pct_month, _days_in_month, _remaining_days),
            build_pace_row("Repeat Customer Rev", _cm_ret_rev, _goal_repeat_rev, _l7d_ret_rev, _yd_ret_rev, _pct_month, _days_in_month, _remaining_days),
        ])
        _dtc_df = pd.DataFrame(_dtc_rows)
        render_white_table(style_pace_df(_dtc_df))

    # ============================================================
    # AMAZON
    # ============================================================
    if _chan_sel in ("All", "Amazon"):
        if _chan_sel == "All":
            st.markdown("---")
        st.subheader("Amazon")
        st.caption('Amazon-only pacing vs revenue goals from amazon_revenue_forecast.')

        _amz_roas = _cm_amz_rev / _cm_amz_spend if _cm_amz_spend > 0 else 0
        amz_k1, amz_k2, amz_k3 = st.columns(3)
        amz_k1.metric("Revenue MTD", f"${_cm_amz_rev:,.0f}")
        amz_k2.metric("Spend MTD", f"${_cm_amz_spend:,.0f}")
        amz_k3.metric("ROAS", f"{_amz_roas:.2f}x",
                       help="Amazon Revenue / Amazon Spend")

        _amz_rows = []
        if _goal_amz_spend > 0:
            _amz_rows.append(
                build_pace_row("Spend", _cm_amz_spend, _goal_amz_spend, _l7d_amz_spend, _yd_amz_spend, _pct_month, _days_in_month, _remaining_days, is_spend=True),
            )
        _amz_rows.append(
            build_pace_row("Revenue", _cm_amz_rev, _goal_amz_rev, _l7d_amz_rev, _yd_amz_rev, _pct_month, _days_in_month, _remaining_days),
        )
        _amz_df = pd.DataFrame(_amz_rows)
        render_white_table(style_pace_df(_amz_df))

    return True
