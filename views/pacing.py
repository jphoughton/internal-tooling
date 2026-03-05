"""Pacing dashboard — reusable across Overview & Marketing pages."""
import logging
import streamlit as st
import pandas as pd
from db import (
    get_db,
    get_media_spend, get_amazon_revenue_forecast,
)
from analytics.dtc_demand import build_master_dtc_forecast
import analytics.revenue_model as rm
from analytics.metrics import (
    get_channel_revenue, get_amazon_spend, get_amazon_spend_projected, get_nc_stats,
    nc_revenue_fraction, get_total_customers,
    compute_mer, compute_nc_roas, compute_nc_cpa, compute_aov,
)
from analytics.amazon_nc_projector import project_amazon_nc
from ui.pacing_helpers import build_pace_row, style_pace_df, render_white_table
from utils.constants import FORECAST_SKUS
from utils.date_helpers import business_today, business_yesterday
from views.marketing import _load_shopify_daily_metrics, _load_gs_spend

log = logging.getLogger(__name__)


@st.cache_data(ttl=300)
def _get_nc_projection():
    """Cached wrapper around project_amazon_nc() for yesterday."""
    try:
        return project_amazon_nc()
    except Exception as e:
        log.warning('NC projection failed: %s', e)
        return None


def compute_pacing_data(ctx):
    """Compute all pacing data without rendering.

    Returns a dict with all computed MTD/L7D/yesterday values, goals, and
    derived metrics. Returns None if Shopify data is unavailable.
    """
    _cached_waterfall = ctx['cached_waterfall']
    _cached_sku_forecast = ctx['cached_sku_forecast']
    _load_seasonal_json = ctx['load_seasonal_json']
    _load_sku_seasonal_json = ctx['load_sku_seasonal_json']

    # Load Shopify DB metrics (revenue, orders, new/repeat customers)
    _shopify_daily = _load_shopify_daily_metrics()
    if _shopify_daily.empty:
        return None

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
    _mkt_media = []

    # Try precomputed master forecast first
    try:
        from db import get_precomputed
        import json as _json_pac_pre
        with get_db() as _pc_conn:
            _fc_cached = get_precomputed(_pc_conn, 'master_dtc_forecast', max_age_hours=25)
        if _fc_cached:
            _precomputed_fc = _json_pac_pre.loads(_fc_cached)
            if 'summary' in _precomputed_fc:
                _mkt_summary = pd.DataFrame(_precomputed_fc['summary'])
    except Exception:
        pass

    # Always load media spend (needed for spend goals and waterfall NC count)
    try:
        with get_db() as _goals_conn:
            _mkt_media = get_media_spend(_goals_conn, source="All Sources")
            _mkt_amz_media = get_media_spend(_goals_conn, source="Amazon")
    except Exception:
        pass

    # If precomputed miss, fall back to live computation
    if _mkt_summary is None:
        try:
            _seasonal_json = _load_seasonal_json()
            with get_db() as _goals_conn2:
                _amz_rev_f = get_amazon_revenue_forecast(_goals_conn2)
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
    _amz_spend_projected = 0
    _amz_spend_gap_days = 0
    _amz_nc_projected = False
    try:
        with get_db() as _amz_conn:
            _cm_amz_rev = get_channel_revenue(_amz_conn, 'amazon', f"{_cur_month}-01", _yesterday_str)
            _cm_amz_spend, _amz_spend_projected, _amz_spend_gap_days = get_amazon_spend_projected(
                _amz_conn, f"{_cur_month}-01", _yesterday_str)
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

    # Override NC rev + NC-ROAS goals with blended (DTC + Amazon) from revenue model
    _goal_blended_nc_rev = _goal_nc_rev  # fallback to DTC-only
    _goal_blended_nc_roas = 0
    try:
        from db import get_revenue_model
        with get_db() as _rm_conn:
            _rm_data = get_revenue_model(_rm_conn)
        if _rm_data:
            _rm_months = [_cur_month]
            _rm_inputs = rm.merge_with_defaults(_rm_data, _rm_months)
            _rm_calc = rm.compute(_rm_inputs, _rm_months)
            _goal_blended_nc_rev = round(_rm_calc.get('total_new', {}).get(_cur_month, _goal_nc_rev))
            _goal_blended_nc_roas = _rm_calc.get('blended_nc_roas', {}).get(_cur_month, 0)
    except Exception as e:
        log.warning("Failed to load revenue model for blended NC goals: %s", e)

    _goal_total_rev = _goal_nc_rev + _goal_repeat_rev + _goal_amz_rev
    _total_actual_rev = _cm_nc_rev + _cm_ret_rev + _cm_amz_rev
    _remaining_days = max(_days_in_month - _day_of_month, 1)

    # Last 7 days averages
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
            _l7d_amz_spend_total, _, _ = get_amazon_spend_projected(_l7_conn, _l7d_start_str, _yesterday_str)
            _l7d_amz_spend = _l7d_amz_spend_total / 7
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
            _yd_amz_spend_total, _, _ = get_amazon_spend_projected(_yd_conn, _yesterday_str, _yesterday_str)
            _yd_amz_spend = _yd_amz_spend_total
            _nc = get_nc_stats(_yd_conn, 'amazon', _yesterday_str, _yesterday_str)
            _yd_amz_nc = _nc['new_customers']
            _yd_amz_nc_rev = nc_revenue_fraction(_nc['oi_total_rev'], _nc['oi_new_rev'], _yd_amz_rev)
    except Exception as e:
        log.warning("Failed to load Amazon yesterday metrics: %s", e)

    # Inject NC projection for yesterday if actual NC data is missing
    if _yd_amz_nc == 0:
        _proj = _get_nc_projection()
        if _proj and _proj.get('actual_nc_customers') is None and _proj.get('projected_nc_customers', 0) > 0:
            _yd_amz_nc = int(round(_proj['projected_nc_customers']))
            _yd_amz_nc_rev = _proj['projected_nc_revenue']
            _cm_amz_nc += _yd_amz_nc
            _cm_amz_nc_rev += _yd_amz_nc_rev
            _amz_nc_projected = True

    # Repeat customer counts
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

    # Derived totals
    _goal_dtc_rev = _goal_nc_rev + _goal_repeat_rev
    _cm_dtc_rev = _cm_nc_rev + _cm_ret_rev
    _l7d_dtc_rev = _l7d_nc_rev + _l7d_ret_rev
    _yd_dtc_rev = _yd_nc_rev + _yd_ret_rev
    _cm_dtc_spend = _cm_spend
    _l7d_dtc_spend = _l7d_spend
    _yd_dtc_spend = _yd_spend

    _cm_total_spend = _cm_dtc_spend + _cm_amz_spend
    _l7d_total_spend = _l7d_dtc_spend + _l7d_amz_spend
    _yd_total_spend = _yd_dtc_spend + _yd_amz_spend

    _cm_total_nc = _cm_nc + _cm_amz_nc
    _cm_total_nc_rev = _cm_nc_rev + _cm_amz_nc_rev
    _l7d_total_nc = _l7d_nc + _l7d_amz_nc
    _l7d_total_nc_rev = _l7d_nc_rev + _l7d_amz_nc_rev
    _yd_total_nc = _yd_nc + _yd_amz_nc
    _yd_total_nc_rev = _yd_nc_rev + _yd_amz_nc_rev

    _cm_amz_repeat_rev = max(_cm_amz_rev - _cm_amz_nc_rev, 0)
    _cm_total_repeat_rev = _cm_ret_rev + _cm_amz_repeat_rev
    _l7d_amz_repeat_rev = max(_l7d_amz_rev - _l7d_amz_nc_rev, 0)
    _l7d_total_repeat_rev = _l7d_ret_rev + _l7d_amz_repeat_rev
    _yd_amz_repeat_rev = max(_yd_amz_rev - _yd_amz_nc_rev, 0)
    _yd_total_repeat_rev = _yd_ret_rev + _yd_amz_repeat_rev

    _cm_total_repeat_cust = _cm_dtc_repeat_cust + _cm_amz_repeat_cust

    _business_mer = compute_mer(_total_actual_rev, _cm_total_spend)
    _total_nc_roas = compute_nc_roas(_cm_total_nc_rev, _cm_total_spend)
    _total_nc_cpa = compute_nc_cpa(_cm_total_spend, _cm_total_nc)
    _dtc_nc_roas = compute_nc_roas(_cm_nc_rev, _cm_dtc_spend)
    _dtc_nc_aov = compute_aov(_cm_nc_rev, _cm_nc)
    _dtc_cpa = compute_nc_cpa(_cm_dtc_spend, _cm_nc)

    _l7d_total_rev = _l7d_rev + _l7d_amz_rev
    _l7d_mer = compute_mer(_l7d_total_rev, _l7d_total_spend)
    _l7d_total_nc_roas = compute_nc_roas(_l7d_total_nc_rev, _l7d_total_spend)
    _l7d_total_nc_cpa = compute_nc_cpa(_l7d_total_spend, _l7d_total_nc)
    _yd_total_rev = _yd_rev + _yd_amz_rev
    _yd_mer = compute_mer(_yd_total_rev, _yd_total_spend)
    _yd_total_nc_roas = compute_nc_roas(_yd_total_nc_rev, _yd_total_spend)
    _yd_total_nc_cpa = compute_nc_cpa(_yd_total_spend, _yd_total_nc)

    # Spend goals
    _goal_spend = 0
    _spend_plan = [m for m in _mkt_media if m.get("month") == _cur_month]
    if _spend_plan:
        _goal_spend = float(_spend_plan[0].get("spend", 0))

    _goal_amz_spend = 0
    _amz_spend_plan = [m for m in _mkt_amz_media if m.get("month") == _cur_month]
    if _amz_spend_plan:
        _goal_amz_spend = float(_amz_spend_plan[0].get("spend", 0))

    _goal_total_spend = _goal_spend + _goal_amz_spend

    _goal_nc_count = 0
    if _wf is not None and not _wf.empty:
        _wf_nc_row = _wf[_wf['month'] == _cur_month]
        if not _wf_nc_row.empty:
            _goal_nc_count = float(_wf_nc_row.iloc[0].get('new_customers_acquired', 0))
    if _goal_nc_count == 0 and _mkt_summary is not None and not _mkt_summary.empty:
        _sum_nc_row = _mkt_summary[_mkt_summary['month'] == _cur_month]
        if not _sum_nc_row.empty and 'shopify_new_customers' in _sum_nc_row.columns:
            _goal_nc_count = float(_sum_nc_row.iloc[0].get('shopify_new_customers', 0))

    _goal_mer = _goal_total_rev / _goal_total_spend if _goal_total_spend > 0 else 0
    _goal_nc_roas_ratio = _goal_blended_nc_roas if _goal_blended_nc_roas > 0 else (_goal_nc_rev / _goal_spend if _goal_spend > 0 else 0)
    _goal_nc_cpa_target = _goal_spend / _goal_nc_count if _goal_nc_count > 0 else 0

    # Contribution = Revenue - Spend
    _cm_contribution = _total_actual_rev - _cm_total_spend
    _goal_contribution = _goal_total_rev - _goal_total_spend

    return {
        'cur_month_name': _cur_month_name,
        'day_of_month': _day_of_month,
        'days_in_month': _days_in_month,
        'pct_month': _pct_month,
        'remaining_days': _remaining_days,
        'has_goals': _has_goals,
        # MTD actuals
        'total_actual_rev': _total_actual_rev,
        'cm_nc_rev': _cm_nc_rev,
        'cm_ret_rev': _cm_ret_rev,
        'cm_spend': _cm_spend,
        'cm_rev': _cm_rev,
        'cm_orders': _cm_orders,
        'cm_nc': _cm_nc,
        'cm_amz_rev': _cm_amz_rev,
        'cm_amz_spend': _cm_amz_spend,
        'cm_amz_nc': _cm_amz_nc,
        'cm_amz_nc_rev': _cm_amz_nc_rev,
        'amz_spend_projected': _amz_spend_projected,
        'amz_spend_gap_days': _amz_spend_gap_days,
        'amz_nc_projected': _amz_nc_projected,
        # Goals
        'goal_nc_rev': _goal_blended_nc_rev,
        'goal_repeat_rev': _goal_repeat_rev,
        'goal_amz_rev': _goal_amz_rev,
        'goal_total_rev': _goal_total_rev,
        'goal_dtc_rev': _goal_dtc_rev,
        'goal_spend': _goal_spend,
        'goal_amz_spend': _goal_amz_spend,
        'goal_total_spend': _goal_total_spend,
        'goal_nc_count': _goal_nc_count,
        'goal_mer': _goal_mer,
        'goal_nc_roas_ratio': _goal_nc_roas_ratio,
        'goal_nc_cpa_target': _goal_nc_cpa_target,
        # Contribution
        'cm_contribution': _cm_contribution,
        'goal_contribution': _goal_contribution,
        # DTC
        'cm_dtc_rev': _cm_dtc_rev,
        'cm_dtc_spend': _cm_dtc_spend,
        'l7d_dtc_rev': _l7d_dtc_rev,
        'l7d_dtc_spend': _l7d_dtc_spend,
        'yd_dtc_rev': _yd_dtc_rev,
        'yd_dtc_spend': _yd_dtc_spend,
        # Totals
        'cm_total_spend': _cm_total_spend,
        'cm_total_nc': _cm_total_nc,
        'cm_total_nc_rev': _cm_total_nc_rev,
        'cm_total_repeat_rev': _cm_total_repeat_rev,
        'cm_total_repeat_cust': _cm_total_repeat_cust,
        # L7D
        'l7d_total_rev': _l7d_total_rev,
        'l7d_total_spend': _l7d_total_spend,
        'l7d_total_nc': _l7d_total_nc,
        'l7d_total_nc_rev': _l7d_total_nc_rev,
        'l7d_total_repeat_rev': _l7d_total_repeat_rev,
        'l7d_nc_rev': _l7d_nc_rev,
        'l7d_ret_rev': _l7d_ret_rev,
        'l7d_nc': _l7d_nc,
        'l7d_spend': _l7d_spend,
        'l7d_amz_rev': _l7d_amz_rev,
        'l7d_amz_spend': _l7d_amz_spend,
        'l7d_amz_nc': _l7d_amz_nc,
        'l7d_amz_nc_rev': _l7d_amz_nc_rev,
        # Yesterday
        'yd_total_rev': _yd_total_rev,
        'yd_total_spend': _yd_total_spend,
        'yd_total_nc': _yd_total_nc,
        'yd_total_nc_rev': _yd_total_nc_rev,
        'yd_total_repeat_rev': _yd_total_repeat_rev,
        'yd_nc_rev': _yd_nc_rev,
        'yd_ret_rev': _yd_ret_rev,
        'yd_nc': _yd_nc,
        'yd_spend': _yd_spend,
        'yd_amz_rev': _yd_amz_rev,
        'yd_amz_spend': _yd_amz_spend,
        'yd_amz_nc': _yd_amz_nc,
        'yd_amz_nc_rev': _yd_amz_nc_rev,
        # Efficiency metrics
        'business_mer': _business_mer,
        'total_nc_roas': _total_nc_roas,
        'total_nc_cpa': _total_nc_cpa,
        'dtc_nc_roas': _dtc_nc_roas,
        'dtc_nc_aov': _dtc_nc_aov,
        'dtc_cpa': _dtc_cpa,
        'l7d_mer': _l7d_mer,
        'l7d_total_nc_roas': _l7d_total_nc_roas,
        'l7d_total_nc_cpa': _l7d_total_nc_cpa,
        'yd_mer': _yd_mer,
        'yd_total_nc_roas': _yd_total_nc_roas,
        'yd_total_nc_cpa': _yd_total_nc_cpa,
    }


def render_hero_bars(data):
    """Render two styled HTML progress bars: Revenue MTD vs Goal + Contribution MTD vs Goal."""
    if not data or not data['has_goals']:
        return

    rev_pct = data['total_actual_rev'] / data['goal_total_rev'] if data['goal_total_rev'] > 0 else 0
    rev_bar_pct = min(rev_pct, 1.0) * 100
    rev_color = '#0a7a3e' if rev_pct >= data['pct_month'] else '#dc2626'

    contrib_pct = data['cm_contribution'] / data['goal_contribution'] if data['goal_contribution'] > 0 else 0
    contrib_bar_pct = min(max(contrib_pct, 0), 1.0) * 100
    contrib_color = '#0a7a3e' if contrib_pct >= data['pct_month'] else '#dc2626'

    pace_marker = data['pct_month'] * 100

    bar_html = f'''
    <div style="display:flex;gap:24px;margin-bottom:8px;">
      <div style="flex:1;">
        <div style="font-size:0.75rem;color:#64748b;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px;">
          Revenue MTD &mdash; ${data['total_actual_rev']:,.0f} / ${data['goal_total_rev']:,.0f}
        </div>
        <div style="position:relative;height:28px;background:#e2e8f0;border-radius:6px;overflow:hidden;">
          <div style="width:{rev_bar_pct:.1f}%;height:100%;background:{rev_color};border-radius:6px;transition:width 0.3s;"></div>
          <div style="position:absolute;left:{pace_marker:.1f}%;top:0;height:100%;width:2px;background:#0F3557;opacity:0.6;"></div>
          <div style="position:absolute;right:8px;top:50%;transform:translateY(-50%);font-size:0.8rem;font-weight:700;color:#0F3557;">{rev_pct:.0%}</div>
        </div>
      </div>
      <div style="flex:1;">
        <div style="font-size:0.75rem;color:#64748b;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px;">
          Contribution MTD &mdash; ${data['cm_contribution']:,.0f} / ${data['goal_contribution']:,.0f}
        </div>
        <div style="position:relative;height:28px;background:#e2e8f0;border-radius:6px;overflow:hidden;">
          <div style="width:{contrib_bar_pct:.1f}%;height:100%;background:{contrib_color};border-radius:6px;transition:width 0.3s;"></div>
          <div style="position:absolute;left:{pace_marker:.1f}%;top:0;height:100%;width:2px;background:#0F3557;opacity:0.6;"></div>
          <div style="position:absolute;right:8px;top:50%;transform:translateY(-50%);font-size:0.8rem;font-weight:700;color:#0F3557;">{contrib_pct:.0%}</div>
        </div>
      </div>
    </div>
    <div style="font-size:0.72rem;color:#94a3b8;margin-bottom:12px;">
      {data['cur_month_name']} &mdash; Day {data['day_of_month']} of {data['days_in_month']} ({data['pct_month']:.0%} elapsed)
    </div>
    '''
    st.markdown(bar_html, unsafe_allow_html=True)


def render_pacing_detail_table(data, source_filter=None):
    """Render combined revenue + spend pacing table, optionally filtered by channel.

    Args:
        data: Pacing data dict from compute_pacing_data().
        source_filter: 'shopify', 'amazon', or None (Rollup — show all rows).
    """
    if not data or not data['has_goals']:
        return

    d = data
    rows = []
    _has_spend_goals = d.get('goal_total_spend', 0) > 0

    # --- REVENUE section ---
    if not source_filter:
        rows.append(build_pace_row("Total Revenue", d['total_actual_rev'], d['goal_total_rev'],
                                   d['l7d_total_rev'], d['yd_total_rev'],
                                   d['pct_month'], d['days_in_month'], d['remaining_days'],
                                   section='revenue'))
    if source_filter in (None, 'shopify'):
        _indent = '\u00a0\u00a0\u00a0\u00a0' if not source_filter else ''
        rows.append(build_pace_row(f"{_indent}DTC Revenue", d['cm_dtc_rev'], d['goal_dtc_rev'],
                                   d['l7d_dtc_rev'], d['yd_dtc_rev'],
                                   d['pct_month'], d['days_in_month'], d['remaining_days'],
                                   section='revenue'))
    if source_filter in (None, 'amazon'):
        _indent = '\u00a0\u00a0\u00a0\u00a0' if not source_filter else ''
        rows.append(build_pace_row(f"{_indent}Amazon Revenue", d['cm_amz_rev'], d['goal_amz_rev'],
                                   d['l7d_amz_rev'], d['yd_amz_rev'],
                                   d['pct_month'], d['days_in_month'], d['remaining_days'],
                                   section='revenue'))
    if not source_filter:
        if d.get('goal_nc_rev', 0) > 0:
            rows.append(build_pace_row("\u00a0\u00a0\u00a0\u00a0New Customer Rev", d['cm_total_nc_rev'], d['goal_nc_rev'],
                                       d['l7d_total_nc_rev'], d['yd_total_nc_rev'],
                                       d['pct_month'], d['days_in_month'], d['remaining_days'],
                                       section='revenue'))
        if d.get('goal_repeat_rev', 0) > 0:
            rows.append(build_pace_row("\u00a0\u00a0\u00a0\u00a0Repeat Rev", d['cm_total_repeat_rev'], d['goal_repeat_rev'],
                                       d['l7d_total_repeat_rev'], d['yd_total_repeat_rev'],
                                       d['pct_month'], d['days_in_month'], d['remaining_days'],
                                       section='revenue'))

    # --- SPEND section ---
    if _has_spend_goals:
        if not source_filter:
            rows.append(build_pace_row("Total Spend", d['cm_total_spend'], d['goal_total_spend'],
                                       d['l7d_total_spend'], d['yd_total_spend'],
                                       d['pct_month'], d['days_in_month'], d['remaining_days'],
                                       is_spend=True, section='spend'))
        if source_filter in (None, 'shopify') and d.get('goal_spend', 0) > 0:
            _indent = '\u00a0\u00a0\u00a0\u00a0' if not source_filter else ''
            rows.append(build_pace_row(f"{_indent}DTC Spend", d['cm_dtc_spend'], d['goal_spend'],
                                       d['l7d_dtc_spend'], d['yd_dtc_spend'],
                                       d['pct_month'], d['days_in_month'], d['remaining_days'],
                                       is_spend=True, section='spend'))
        if source_filter in (None, 'amazon') and d.get('goal_amz_spend', 0) > 0:
            _indent = '\u00a0\u00a0\u00a0\u00a0' if not source_filter else ''
            rows.append(build_pace_row(f"{_indent}Amazon Spend", d['cm_amz_spend'], d['goal_amz_spend'],
                                       d['l7d_amz_spend'], d['yd_amz_spend'],
                                       d['pct_month'], d['days_in_month'], d['remaining_days'],
                                       is_spend=True, section='spend'))

    # --- EFFICIENCY section ---
    if not source_filter and d.get('goal_nc_roas_ratio', 0) > 0:
        rows.append(build_pace_row("NC-ROAS", d['total_nc_roas'], d['goal_nc_roas_ratio'],
                                   d['l7d_total_nc_roas'], d['yd_total_nc_roas'],
                                   d['pct_month'], d['days_in_month'], d['remaining_days'],
                                   fmt='ratio', section='efficiency'))

    if rows:
        render_white_table(style_pace_df(pd.DataFrame(rows)))


def render_pacing(ctx):
    """Render the current-month pacing dashboard.

    Requires ctx keys: cached_waterfall, cached_sku_forecast, load_seasonal_json, biz_vars.
    Returns True if content was rendered, False if data unavailable.
    """
    data = compute_pacing_data(ctx)
    if data is None:
        return False

    _nav_channel = ctx.get('channel', 'Rollup')

    # ============================================================
    # RENDER PACING HEADER
    # ============================================================
    st.header(f"{data['cur_month_name']} Pacing")
    st.caption(f"Day {data['day_of_month']} of {data['days_in_month']}")
    st.progress(data['pct_month'], text=f"{data['pct_month']*100:.0f}%")

    if not data['has_goals']:
        st.info("Set up a media spend plan and Amazon forecast on the **Demand Forecast** page for pacing.")
        st.subheader("MTD Summary")
        if _nav_channel == 'DTC':
            ts1, ts2, ts3 = st.columns(3)
            ts1.metric("NC Revenue (MTD)", f"${data['cm_nc_rev']:,.0f}")
            ts2.metric("Repeat Revenue (MTD)", f"${data['cm_ret_rev']:,.0f}")
            _total_mtd = data['cm_nc_rev'] + data['cm_ret_rev']
            ts3.metric("Total Revenue (MTD)", f"${_total_mtd:,.0f}")
        elif _nav_channel == 'Amazon':
            ts1, ts2 = st.columns(2)
            ts1.metric("Amazon Revenue (MTD)", f"${data['cm_amz_rev']:,.0f}")
            ts2.metric("Amazon Spend (MTD)", f"${data['cm_amz_spend']:,.0f}")
        else:
            ts1, ts2, ts3, ts4 = st.columns(4)
            ts1.metric("NC Revenue (MTD)", f"${data['cm_nc_rev']:,.0f}")
            ts2.metric("Repeat Revenue (MTD)", f"${data['cm_ret_rev']:,.0f}")
            ts3.metric("Amazon Revenue (MTD)", f"${data['cm_amz_rev']:,.0f}")
            _total_mtd = data['cm_nc_rev'] + data['cm_ret_rev'] + data['cm_amz_rev']
            ts4.metric("Total Revenue (MTD)", f"${_total_mtd:,.0f}")
        return True

    d = data
    _chan_sel = {'DTC': 'DTC', 'Amazon': 'Amazon', 'Rollup': 'Rollup'}.get(_nav_channel, 'Rollup')

    # ============================================================
    # ROLL UP
    # ============================================================
    if _chan_sel in ("All", "Rollup"):
        st.subheader("Roll Up")
        st.caption('Combined Shopify + Amazon pacing against monthly goals from the demand forecast engine.')

        eff1, eff2, eff3 = st.columns(3)
        eff1.metric("Business MER", f"{d['business_mer']:.2f}x", help="Total Revenue / Total Spend")
        eff2.metric("Total NC ROAS", f"{d['total_nc_roas']:.2f}x", help="Total NC Revenue / Total Spend")
        eff3.metric("Total NC CPA", f"${d['total_nc_cpa']:,.0f}", help="Total Spend / Total New Customers")

        _nc_est = d.get('amz_nc_projected', False)
        _nc_label_ru = "Total New Customers (est)" if _nc_est else "Total New Customers"
        rev1, rev2, rev3 = st.columns(3)
        rev1.metric("Total Spend", f"${d['cm_total_spend']:,.0f}")
        rev2.metric("Total Revenue", f"${d['total_actual_rev']:,.0f}")
        rev3.metric(_nc_label_ru, f"{d['cm_total_nc']:,}",
                    help='Includes projected Amazon NC for yesterday' if _nc_est else None)

        nr1, nr2, nr3 = st.columns(3)
        nr1.metric("Total NC Revenue", f"${d['cm_total_nc_rev']:,.0f}")
        nr2.metric("Total Repeat Revenue", f"${d['cm_total_repeat_rev']:,.0f}")
        nr3.metric("Total Repeat Customers", f"{d['cm_total_repeat_cust']:,}")

        _rollup_rows = []
        if d['goal_total_spend'] > 0:
            _rollup_rows.append(build_pace_row("Total Spend", d['cm_total_spend'], d['goal_total_spend'],
                                               d['l7d_total_spend'], d['yd_total_spend'], d['pct_month'], d['days_in_month'], d['remaining_days'], is_spend=True))
        _rollup_rows.extend([
            build_pace_row("Total Revenue", d['total_actual_rev'], d['goal_total_rev'],
                           d['l7d_total_rev'], d['yd_total_rev'], d['pct_month'], d['days_in_month'], d['remaining_days']),
            build_pace_row("Total NC Revenue", d['cm_total_nc_rev'], d['goal_nc_rev'],
                           d['l7d_total_nc_rev'], d['yd_total_nc_rev'], d['pct_month'], d['days_in_month'], d['remaining_days']),
            build_pace_row("Total Repeat Revenue", d['cm_total_repeat_rev'], d['goal_repeat_rev'],
                           d['l7d_total_repeat_rev'], d['yd_total_repeat_rev'], d['pct_month'], d['days_in_month'], d['remaining_days']),
        ])
        if d['goal_nc_count'] > 0:
            _rollup_rows.append(build_pace_row("Total New Customers", d['cm_total_nc'], d['goal_nc_count'],
                                               d['l7d_total_nc'], d['yd_total_nc'], d['pct_month'], d['days_in_month'], d['remaining_days'], fmt='count'))
        if d['goal_mer'] > 0:
            _rollup_rows.append(build_pace_row("Business MER", d['business_mer'], d['goal_mer'],
                                               d['l7d_mer'], d['yd_mer'], d['pct_month'], d['days_in_month'], d['remaining_days'], fmt='ratio'))
        if d['goal_nc_roas_ratio'] > 0:
            _rollup_rows.append(build_pace_row("Total NC ROAS", d['total_nc_roas'], d['goal_nc_roas_ratio'],
                                               d['l7d_total_nc_roas'], d['yd_total_nc_roas'], d['pct_month'], d['days_in_month'], d['remaining_days'], fmt='ratio'))
        if d['goal_nc_cpa_target'] > 0:
            _rollup_rows.append(build_pace_row("Total NC CPA", d['total_nc_cpa'], d['goal_nc_cpa_target'],
                                               d['l7d_total_nc_cpa'], d['yd_total_nc_cpa'], d['pct_month'], d['days_in_month'], d['remaining_days'], is_spend=True))
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
        dtc_k1.metric("NC ROAS", f"{d['dtc_nc_roas']:.2f}x")
        dtc_k2.metric("NC Orders", f"{d['cm_nc']:,}")
        dtc_k3.metric("NC AOV", f"${d['dtc_nc_aov']:,.0f}")
        dtc_k4.metric("CPA", f"${d['dtc_cpa']:,.0f}")

        _dtc_rows = []
        if d['goal_spend'] > 0:
            _dtc_rows.append(build_pace_row("Spend", d['cm_dtc_spend'], d['goal_spend'],
                                            d['l7d_dtc_spend'], d['yd_dtc_spend'], d['pct_month'], d['days_in_month'], d['remaining_days'], is_spend=True))
        _dtc_rows.extend([
            build_pace_row("Revenue", d['cm_dtc_rev'], d['goal_dtc_rev'], d['l7d_dtc_rev'], d['yd_dtc_rev'], d['pct_month'], d['days_in_month'], d['remaining_days']),
            build_pace_row("New Customer Rev", d['cm_nc_rev'], d['goal_nc_rev'], d['l7d_nc_rev'], d['yd_nc_rev'], d['pct_month'], d['days_in_month'], d['remaining_days']),
            build_pace_row("Repeat Customer Rev", d['cm_ret_rev'], d['goal_repeat_rev'], d['l7d_ret_rev'], d['yd_ret_rev'], d['pct_month'], d['days_in_month'], d['remaining_days']),
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
        _amz_nc_est_note = ' NC data includes yesterday estimate.' if d.get('amz_nc_projected', False) else ''
        st.caption(f'Amazon-only pacing vs revenue goals from amazon_revenue_forecast.{_amz_nc_est_note}')

        _amz_roas = d['cm_amz_rev'] / d['cm_amz_spend'] if d['cm_amz_spend'] > 0 else 0
        amz_k1, amz_k2, amz_k3 = st.columns(3)
        amz_k1.metric("Revenue MTD", f"${d['cm_amz_rev']:,.0f}")
        amz_k2.metric("Spend MTD", f"${d['cm_amz_spend']:,.0f}")
        amz_k3.metric("ROAS", f"{_amz_roas:.2f}x", help="Amazon Revenue / Amazon Spend")

        _amz_rows = []
        if d['goal_amz_spend'] > 0:
            _amz_rows.append(
                build_pace_row("Spend", d['cm_amz_spend'], d['goal_amz_spend'], d['l7d_amz_spend'], d['yd_amz_spend'], d['pct_month'], d['days_in_month'], d['remaining_days'], is_spend=True),
            )
        _amz_rows.append(
            build_pace_row("Revenue", d['cm_amz_rev'], d['goal_amz_rev'], d['l7d_amz_rev'], d['yd_amz_rev'], d['pct_month'], d['days_in_month'], d['remaining_days']),
        )
        _amz_df = pd.DataFrame(_amz_rows)
        render_white_table(style_pace_df(_amz_df))

    return True
