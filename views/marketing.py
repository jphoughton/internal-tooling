"""Marketing page — Klaviyo + Google Sheet analytics, pacing, DoD/WoW/MoM performance."""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from db import (
    get_db, read_sql,
    get_media_spend, get_amazon_revenue_forecast,
    get_setting, set_setting,
    get_last_sync_timestamp, get_new_rows_since_yesterday, get_synced_sources,
    get_klaviyo_campaigns, get_klaviyo_flows, get_klaviyo_lists,
    upsert_klaviyo_campaign, upsert_klaviyo_flow, upsert_klaviyo_list,
    update_klaviyo_campaign_metrics, update_klaviyo_flow_metrics,
)
from analytics.dtc_demand import build_master_dtc_forecast
from analytics.retention import get_new_repeat_summary, get_projected_new_repeat_summary
from analytics.sku_flavors import get_flavor, sort_df_by_best_seller
from ui.components import render_html_table, render_freshness_badge, smart_date_filter
from utils.constants import FORECAST_SKUS
from utils.date_helpers import business_today, business_yesterday


@st.cache_data(ttl=300)
def _load_shopify_daily_metrics():
    """Load daily DTC metrics from Shopify DB (revenue, orders, new/repeat customers).

    Uses MIN(order_date) from orders table for accurate first-order classification
    (matches Shopify admin's new vs returning definition).
    """
    with get_db() as conn:
        rev_df = read_sql(
            "SELECT sale_date, SUM(revenue) AS revenue, SUM(units_sold) AS units "
            "FROM daily_sku_sales WHERE source = %s "
            "GROUP BY sale_date ORDER BY sale_date",
            conn, params=('shopify',),
        )
        cust_df = read_sql(
            "SELECT DATE(o.order_date) AS sale_date, "
            "COUNT(DISTINCT o.order_id) AS total_orders, "
            "COUNT(DISTINCT o.customer_id) AS total_customers, "
            "COUNT(DISTINCT CASE WHEN DATE(cf.actual_first) = DATE(o.order_date) "
            "THEN o.customer_id END) AS new_customers, "
            "SUM(oi.total_price) AS oi_total_rev, "
            "SUM(CASE WHEN DATE(cf.actual_first) = DATE(o.order_date) "
            "THEN oi.total_price ELSE 0 END) AS oi_new_rev "
            "FROM orders o "
            "JOIN (SELECT customer_id, MIN(order_date) AS actual_first "
            "      FROM orders WHERE source = %s GROUP BY customer_id) cf "
            "  ON o.customer_id = cf.customer_id "
            "JOIN order_items oi ON o.order_id = oi.order_id "
            "WHERE o.source = %s "
            "GROUP BY DATE(o.order_date)",
            conn, params=('shopify', 'shopify'),
        )
    if rev_df.empty:
        return pd.DataFrame()

    rev_df['sale_date'] = pd.to_datetime(rev_df['sale_date'])
    cust_df['sale_date'] = pd.to_datetime(cust_df['sale_date'])

    df = rev_df.merge(cust_df, on='sale_date', how='left')
    df['total_orders'] = df['total_orders'].fillna(0).astype(int)
    df['new_customers'] = df['new_customers'].fillna(0).astype(int)
    df['total_customers'] = df['total_customers'].fillna(0).astype(int)

    # Derive new/repeat revenue using order_items fraction applied to daily_sku_sales revenue
    oi_total = df['oi_total_rev'].fillna(0)
    oi_new = df['oi_new_rev'].fillna(0)
    frac_new = (oi_new / oi_total).fillna(0).clip(0, 1)
    df['_revenue'] = df['revenue']
    df['_units'] = df['units']
    df['_orders'] = df['total_orders']
    df['_nc_orders'] = df['new_customers']
    df['_ret_orders'] = df['_orders'] - df['_nc_orders']
    df['_nc_revenue'] = df['_revenue'] * frac_new
    df['_ret_revenue'] = df['_revenue'] * (1 - frac_new)
    df['_date'] = df['sale_date']
    return df


@st.cache_data(ttl=300)
def _load_gs_spend():
    """Load ad spend and subscription data from Google Sheet."""
    with get_db() as conn:
        try:
            cols = [d["column_name"] for d in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'google_sheet_data'"
            ).fetchall()]
            if "date" not in cols:
                return pd.DataFrame()
        except Exception:
            return pd.DataFrame()
        df = read_sql("SELECT * FROM google_sheet_data ORDER BY id", conn)
    if df.empty:
        return df

    def _clean_num(val):
        if pd.isna(val):
            return 0.0
        s = str(val).replace("$", "").replace(",", "").replace("%", "").strip()
        try:
            return float(s)
        except (ValueError, TypeError):
            return 0.0

    df['_date'] = pd.to_datetime(df['date'], format='mixed', dayfirst=False)
    df['_ad_spend'] = df['blended_ad_spend'].apply(_clean_num)

    # Keep subscription columns if present
    sub_cols = []
    for sub_col in ['subscriptions', 'subscription_revenue', 'active_subscriptions',
                     'total_active_subscriptions', 'total_new_subscriptions',
                     'total_cancelled_subscriptions', 'total_subscription_order_revenue']:
        if sub_col in df.columns:
            df[f'_{sub_col}'] = df[sub_col].apply(_clean_num)
            sub_cols.append(f'_{sub_col}')

    return df[['_date', '_ad_spend'] + sub_cols].copy()


def render(ctx):
    """Render the Marketing page."""
    _cached_waterfall = ctx['cached_waterfall']
    _cached_sku_forecast = ctx['cached_sku_forecast']
    _load_seasonal_json = ctx['load_seasonal_json']
    _load_sku_seasonal_json = ctx['load_sku_seasonal_json']
    _bv = ctx.get('biz_vars', {})
    _mkt_horizon = _bv.get('forecast_horizon', 12)
    _mkt_amz_growth = _bv.get('amazon_growth_pct', 0.0)

    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title("Marketing")
    with _badge_col:
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, ['shopify', 'amazon'])
            _new = get_new_rows_since_yesterday(conn, ['shopify', 'amazon'])
            _srcs = get_synced_sources(conn, ['shopify', 'amazon'])
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)

    with get_db() as conn:
        _mkt_klaviyo_key = get_setting(conn, "klaviyo_api_key", "")

    # Load Shopify DB metrics (revenue, orders, new/repeat customers)
    _shopify_daily = _load_shopify_daily_metrics()
    _gs_spend = _load_gs_spend()
    _has_shopify_data = not _shopify_daily.empty

    # Merge: Shopify DB for revenue/customers, Google Sheet for spend/sessions
    if _has_shopify_data:
        mkt_df = _shopify_daily.copy()
        if not _gs_spend.empty:
            mkt_df = mkt_df.merge(
                _gs_spend, on='_date', how='left', suffixes=('', '_gs')
            )
        else:
            mkt_df['_ad_spend'] = 0
        mkt_df = mkt_df.sort_values('_date')

        if not mkt_df.empty:
            # Date range with smart presets — default to MTD
            mkt_start, mkt_end = smart_date_filter(
                mkt_df["_date"].min().date(), mkt_df["_date"].max().date(), "mkt",
                default_preset="MTD",
            )
            mkt_df = mkt_df[(mkt_df["_date"].dt.date >= mkt_start) & (mkt_df["_date"].dt.date <= mkt_end)]

            total_spend = mkt_df["_ad_spend"].fillna(0).sum()
            total_rev = mkt_df["_revenue"].sum()
            total_orders = int(mkt_df["_orders"].sum())
            total_nc = int(mkt_df["_nc_orders"].sum())
            total_ret = int(mkt_df["_ret_orders"].sum())
            nc_rev = mkt_df["_nc_revenue"].sum()
            ret_rev = mkt_df["_ret_revenue"].sum()

            # ============================================================
            # LOAD REVENUE GOALS from Demand Forecast engine
            # ============================================================
            _mkt_summary = None
            _mkt_revenue = {}
            _mkt_amz_media = []
            try:
                _seasonal_json_mkt = _load_seasonal_json()
                with get_db() as _goals_conn:
                    _mkt_media = get_media_spend(_goals_conn, source="All Sources")
                    _mkt_amz_media = get_media_spend(_goals_conn, source="Amazon")
                    _amz_rev_f = get_amazon_revenue_forecast(_goals_conn)
                import json as _json_mkt
                _mkt_wf = _cached_waterfall(_json_mkt.dumps(_mkt_media, sort_keys=True), 'shopify', _mkt_horizon, _seasonal_json_mkt)
                if _mkt_wf is not None and not _mkt_wf.empty:
                    _mkt_sku_table = _cached_sku_forecast(
                        _mkt_wf.to_json(), None, _load_sku_seasonal_json(), _seasonal_json_mkt,
                    )
                    _amz_rev_dict = {r["month"]: r["revenue"] for r in _amz_rev_f if r.get("revenue", 0) > 0}
                    _dtc_mkt = build_master_dtc_forecast(
                        shopify_waterfall_df=_mkt_wf,
                        shopify_sku_forecast_df=_mkt_sku_table,
                        amazon_growth_rate=_mkt_amz_growth / 100.0,
                        horizon_months=_mkt_horizon,
                        forecast_skus=FORECAST_SKUS,
                        media_plan=_mkt_media,
                        amazon_revenue_forecast=_amz_rev_dict if _amz_rev_dict else None,
                    )
                    _mkt_summary = _dtc_mkt["summary"]
                    _mkt_revenue = _dtc_mkt.get("revenue", {})
            except Exception as _mkt_err:
                st.caption(f"Could not load revenue goals: {_mkt_err}")

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

            # MTD actuals from Google Sheet (through yesterday)
            _cm_mask = (mkt_df["_date"].dt.strftime("%Y-%m") == _cur_month) & (mkt_df["_date"].dt.date <= _yesterday)
            _cm_nc_rev = mkt_df.loc[_cm_mask, "_nc_revenue"].sum()
            _cm_ret_rev = mkt_df.loc[_cm_mask, "_ret_revenue"].sum()
            _cm_spend = mkt_df.loc[_cm_mask, "_ad_spend"].sum()
            _cm_rev = mkt_df.loc[_cm_mask, "_revenue"].sum()
            _cm_orders = int(mkt_df.loc[_cm_mask, "_orders"].sum())
            _cm_nc = int(mkt_df.loc[_cm_mask, "_nc_orders"].sum())
            _cm_ret_orders = int(mkt_df.loc[_cm_mask, "_ret_orders"].sum()) if _cm_mask.any() else 0

            # Amazon MTD: Revenue from daily_sku_sales, Spend from amazon_daily_rollup
            _cm_amz_rev = 0
            _cm_amz_spend = 0
            try:
                with get_db() as _amz_conn:
                    # Revenue from daily_sku_sales
                    _amz_mtd = _amz_conn.execute(
                        "SELECT SUM(revenue) FROM daily_sku_sales WHERE source = 'amazon' AND sale_date >= ? AND sale_date <= ?",
                        (f"{_cur_month}-01", _yesterday_str),
                    ).fetchone()
                    _cm_amz_rev = float(_amz_mtd[0] or 0)

                    # Spend from amazon_daily_rollup
                    _amz_rollup = _amz_conn.execute(
                        "SELECT SUM(spend) AS total_spend "
                        "FROM amazon_daily_rollup WHERE date >= ? AND date <= ?",
                        (f"{_cur_month}-01", _yesterday_str),
                    ).fetchone()
                    if _amz_rollup and _amz_rollup["total_spend"] is not None:
                        _cm_amz_spend = float(_amz_rollup["total_spend"] or 0)
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

            # Last month actuals (for fallback comparison)
            _last_month = (pd.Timestamp(_today) - pd.DateOffset(months=1)).strftime("%Y-%m")
            _lm_mask = mkt_df["_date"].dt.strftime("%Y-%m") == _last_month
            _lm_nc_rev = mkt_df.loc[_lm_mask, "_nc_revenue"].sum()
            _lm_ret_rev = mkt_df.loc[_lm_mask, "_ret_revenue"].sum()
            _lm_spend = mkt_df.loc[_lm_mask, "_ad_spend"].sum()

            # ============================================================
            # PACING DASHBOARD — matches Revenue Model "Automated Pacing" tab
            # ============================================================
            st.header(f"{_cur_month_name} Pacing")
            st.caption(f"Day {_day_of_month} of {_days_in_month}")
            st.progress(_pct_month, text=f"{_pct_month*100:.0f}%")

            # Last 7 days averages from Google Sheet (7 complete days ending at yesterday)
            _l7d_start = _yesterday - pd.Timedelta(days=6)
            _l7d_mask = (mkt_df["_date"].dt.date >= _l7d_start) & (mkt_df["_date"].dt.date <= _yesterday)
            _l7d_rev = mkt_df.loc[_l7d_mask, "_revenue"].sum() / 7 if _l7d_mask.any() else 0
            _l7d_nc_rev = mkt_df.loc[_l7d_mask, "_nc_revenue"].sum() / 7 if _l7d_mask.any() else 0
            _l7d_ret_rev = mkt_df.loc[_l7d_mask, "_ret_revenue"].sum() / 7 if _l7d_mask.any() else 0
            _l7d_spend = mkt_df.loc[_l7d_mask, "_ad_spend"].sum() / 7 if _l7d_mask.any() else 0
            _l7d_nc = mkt_df.loc[_l7d_mask, "_nc_orders"].sum() / 7 if _l7d_mask.any() else 0

            # Amazon L7D from daily_sku_sales (revenue) + amazon_daily_rollup (spend)
            _l7d_amz_rev = 0
            _l7d_amz_spend = 0
            try:
                _l7d_start_str = str(_l7d_start)
                with get_db() as _l7_conn:
                    _l7_amz = _l7_conn.execute(
                        "SELECT SUM(revenue) FROM daily_sku_sales WHERE source = 'amazon' AND sale_date >= ? AND sale_date <= ?",
                        (_l7d_start_str, _yesterday_str),
                    ).fetchone()
                    _l7d_amz_rev = float(_l7_amz[0] or 0) / 7
                    # Spend from amazon_daily_rollup
                    _l7_rollup = _l7_conn.execute(
                        "SELECT SUM(spend) AS total_spend "
                        "FROM amazon_daily_rollup WHERE date >= ? AND date <= ?",
                        (_l7d_start_str, _yesterday_str),
                    ).fetchone()
                    if _l7_rollup and _l7_rollup["total_spend"] is not None:
                        _l7d_amz_spend = float(_l7_rollup["total_spend"] or 0) / 7
            except Exception:
                pass

            # Yesterday actuals from Google Sheet
            _yd_mask = mkt_df["_date"].dt.strftime("%Y-%m-%d") == _yesterday_str
            _yd_rev = mkt_df.loc[_yd_mask, "_revenue"].sum()
            _yd_nc_rev = mkt_df.loc[_yd_mask, "_nc_revenue"].sum()
            _yd_ret_rev = mkt_df.loc[_yd_mask, "_ret_revenue"].sum()
            _yd_spend = mkt_df.loc[_yd_mask, "_ad_spend"].sum()
            _yd_nc = int(mkt_df.loc[_yd_mask, "_nc_orders"].sum())

            # Amazon yesterday from daily_sku_sales (revenue) + amazon_daily_rollup (spend)
            _yd_amz_rev = 0
            _yd_amz_spend = 0
            try:
                with get_db() as _yd_conn:
                    _yd_amz = _yd_conn.execute(
                        "SELECT SUM(revenue) FROM daily_sku_sales WHERE source = 'amazon' AND sale_date = ?",
                        (_yesterday_str,),
                    ).fetchone()
                    _yd_amz_rev = float(_yd_amz[0] or 0)
                    _yd_rollup = _yd_conn.execute(
                        "SELECT spend FROM amazon_daily_rollup WHERE date = ?",
                        (_yesterday_str,),
                    ).fetchone()
                    if _yd_rollup:
                        _yd_amz_spend = float(_yd_rollup[0] or 0)
            except Exception:
                pass

            _remaining_days = _days_in_month - _day_of_month
            _total_actual_rev = _cm_nc_rev + _cm_ret_rev + _cm_amz_rev

            # Channel from dashboard router context
            _nav_channel = ctx.get('channel', 'Rollup')

            if _has_goals:
                # Build pacing row helper
                # --- Color helpers ---
                # For revenue: above pace = green, below = red
                # For spend: above pace = red (overspending), below = green
                def _pace_color(pct, invert=False):
                    """Return CSS for pacing cells with colored bg tint + bold text. invert=True for spend."""
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
                    """Color for +/- column. Positive = green for rev, red for spend."""
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
                    """Apply red/yellow/green styling to a pacing DataFrame."""
                    # Extract hidden columns for styling, then drop them
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
                        # Bold label column
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
                    """Render a Pandas Styler as an HTML table with white bg, preserving cell-level colors."""
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

                # Use global HTML table renderer
                _render_df_as_html = render_html_table

                # -- DTC actuals (Google + Meta spend, Shopify revenue) --
                _goal_dtc_rev = _goal_nc_rev + _goal_repeat_rev
                _cm_dtc_rev = _cm_nc_rev + _cm_ret_rev
                _l7d_dtc_rev = _l7d_nc_rev + _l7d_ret_rev
                _yd_dtc_rev = _yd_nc_rev + _yd_ret_rev
                _cm_dtc_spend = _cm_spend
                _l7d_dtc_spend = _l7d_spend
                _yd_dtc_spend = _yd_spend

                # -- Amazon data from amazon_daily_rollup (spend already loaded above) --
                # _cm_amz_spend set above from DB
                # _l7d_amz_spend set above from DB
                # _yd_amz_spend set above from DB

                # -- Combined Roll Up totals (DTC + Amazon) --
                _cm_total_spend = _cm_dtc_spend + _cm_amz_spend
                _l7d_total_spend = _l7d_dtc_spend + _l7d_amz_spend
                _yd_total_spend = _yd_dtc_spend + _yd_amz_spend
                _cm_total_repeat_rev = _cm_ret_rev  # Amazon repeat not tracked in this source

                # Efficiency metrics
                _business_mer = _total_actual_rev / _cm_total_spend if _cm_total_spend > 0 else 0

                # DTC efficiency (NC metrics from Shopify orders DB only)
                _dtc_nc_roas = _cm_nc_rev / _cm_dtc_spend if _cm_dtc_spend > 0 else 0
                _dtc_nc_aov = _cm_nc_rev / _cm_nc if _cm_nc > 0 else 0
                _dtc_cpa = _cm_dtc_spend / _cm_nc if _cm_nc > 0 else 0

                # -- Spend goal from media plan --
                _goal_spend = 0
                _spend_plan = [m for m in _mkt_media if m.get("month") == _cur_month]
                if _spend_plan:
                    _goal_spend = float(_spend_plan[0].get("spend", 0))

                _goal_amz_spend = 0
                _amz_spend_plan = [m for m in _mkt_amz_media if m.get("month") == _cur_month]
                if _amz_spend_plan:
                    _goal_amz_spend = float(_amz_spend_plan[0].get("spend", 0))

                # ============================================================
                # ROLL UP — DTC + Amazon combined
                # ============================================================
                if _nav_channel == 'Rollup':
                    st.subheader("Roll Up")
                    st.caption('Combined Shopify + Amazon pacing from Google Sheets + Shopify/Amazon data.')

                    # Roll Up KPIs (above table)
                    eff1, eff2, eff3, eff4 = st.columns(4)
                    eff1.metric("Business MER", f"{_business_mer:.2f}x",
                                help="Total Revenue / Total Spend")
                    eff2.metric("Total Revenue", f"${_total_actual_rev:,.0f}",
                                help="DTC + Amazon revenue MTD")
                    eff3.metric("Total Spend", f"${_cm_total_spend:,.0f}",
                                help="DTC + Amazon spend MTD")
                    eff4.metric("DTC NC Orders", f"{_cm_nc:,}",
                                help="Shopify new customer orders MTD")

                    _rollup_rows = []
                    _goal_total_spend = _goal_spend + _goal_amz_spend
                    if _goal_total_spend > 0:
                        _rollup_rows.append(_build_pace_row("Total Spend", _cm_total_spend, _goal_total_spend,
                                                            _l7d_total_spend, _yd_total_spend, is_spend=True))
                    _rollup_rows.extend([
                        _build_pace_row("Total Revenue", _total_actual_rev, _goal_total_rev,
                                        _l7d_rev + _l7d_amz_rev, _yd_rev + _yd_amz_rev),
                        _build_pace_row("Repeat Revenue", _cm_total_repeat_rev, _goal_repeat_rev,
                                        _l7d_ret_rev, _yd_ret_rev),
                    ])
                    _rollup_df = pd.DataFrame(_rollup_rows)
                    _render_white_table(_style_pace_df(_rollup_df))

                # ============================================================
                # DTC (Shopify) — Google + Meta spend
                # ============================================================
                if _nav_channel in ('Rollup', 'DTC'):
                    if _nav_channel == 'Rollup':
                        st.markdown("---")
                    st.subheader("DTC (Shopify)")
                    st.caption('Shopify-only pacing vs revenue and spend goals from media plan.')

                    # DTC KPIs (above table)
                    dtc_k1, dtc_k2, dtc_k3, dtc_k4 = st.columns(4)
                    dtc_k1.metric("NC ROAS", f"{_dtc_nc_roas:.2f}x")
                    dtc_k2.metric("NC Orders", f"{_cm_nc:,}")
                    dtc_k3.metric("NC AOV", f"${_dtc_nc_aov:,.0f}")
                    dtc_k4.metric("CPA", f"${_dtc_cpa:,.0f}")

                    _dtc_rows = []
                    if _goal_spend > 0:
                        _goal_dtc_spend = _goal_spend  # DTC spend goal from media plan
                        _dtc_rows.append(_build_pace_row("Spend", _cm_dtc_spend, _goal_dtc_spend,
                                                         _l7d_dtc_spend, _yd_dtc_spend, is_spend=True))
                    _dtc_rows.extend([
                        _build_pace_row("Revenue", _cm_dtc_rev, _goal_dtc_rev, _l7d_dtc_rev, _yd_dtc_rev),
                        _build_pace_row("New Customer Rev", _cm_nc_rev, _goal_nc_rev, _l7d_nc_rev, _yd_nc_rev),
                        _build_pace_row("Repeat Customer Rev", _cm_ret_rev, _goal_repeat_rev, _l7d_ret_rev, _yd_ret_rev),
                    ])
                    _dtc_df = pd.DataFrame(_dtc_rows)
                    _render_white_table(_style_pace_df(_dtc_df))

                # ============================================================
                # AMAZON — from amazon_daily_rollup
                # ============================================================
                if _nav_channel in ('Rollup', 'Amazon'):
                    if _nav_channel == 'Rollup':
                        st.markdown("---")
                    st.subheader("Amazon")
                    st.caption('Amazon-only pacing from daily_sku_sales + amazon_daily_rollup.')

                    # Amazon KPIs (above table)
                    _amz_roas = _cm_amz_rev / _cm_amz_spend if _cm_amz_spend > 0 else 0
                    amz_k1, amz_k2, amz_k3 = st.columns(3)
                    amz_k1.metric("Revenue MTD", f"${_cm_amz_rev:,.0f}")
                    amz_k2.metric("Spend MTD", f"${_cm_amz_spend:,.0f}")
                    amz_k3.metric("ROAS", f"{_amz_roas:.2f}x",
                                  help="Amazon Revenue / Amazon Spend")

                    _amz_rows = []
                    if _goal_amz_spend > 0:
                        _amz_rows.append(
                            _build_pace_row("Spend", _cm_amz_spend, _goal_amz_spend, _l7d_amz_spend, _yd_amz_spend, is_spend=True),
                        )
                    _amz_rows.append(
                        _build_pace_row("Revenue", _cm_amz_rev, _goal_amz_rev, _l7d_amz_rev, _yd_amz_rev),
                    )
                    _amz_df = pd.DataFrame(_amz_rows)
                    _render_white_table(_style_pace_df(_amz_df))

            else:
                # No goals — show MTD summary with last month comparison
                st.info("Set up a media spend plan and Amazon forecast in the sidebar **Business Variables** panel for pacing.")
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

            st.divider()

            # ============================================================
            # DoD / WoW / MoM PERFORMANCE TABS
            # ============================================================
            st.header("Performance")
            _perf_tab_dod, _perf_tab_wow, _perf_tab_mom = st.tabs(
                ["Daily", "Weekly", "Monthly"]
            )

            # --- Helper: build a performance summary table from aggregated data ---
            def _build_perf_table(agg_df, period_col, amz_agg=None):
                """Build DTC / Roll Up / Amazon summary tables from aggregated Google Sheet data.

                agg_df: DataFrame grouped by period with summed metrics.
                period_col: column name for the period label.
                amz_agg: optional DataFrame of Amazon revenue/spend grouped by the same period.
                Returns (dtc_df, rollup_df, amz_df) formatted for display.
                """
                rows_dtc = []
                rows_rollup = []
                rows_amz = []

                for _, r in agg_df.iterrows():
                    period_label = r[period_col]
                    spend = r.get("_ad_spend", 0)
                    total_rev = r.get("_revenue", 0)
                    nc_orders = r.get("_nc_orders", 0)
                    nc_rev = r.get("_nc_revenue", 0)
                    orders = r.get("_orders", 0)
                    nc_aov = nc_rev / nc_orders if nc_orders > 0 else 0
                    nc_cpa = spend / nc_orders if nc_orders > 0 else 0
                    nc_roas = nc_rev / spend if spend > 0 else 0
                    cost_per_nc = spend / nc_orders if nc_orders > 0 else 0

                    # DTC row
                    rows_dtc.append({
                        period_col: period_label,
                        "Spend": f"${spend:,.0f}",
                        "Total Rev": f"${total_rev:,.0f}",
                        "New Users": int(nc_orders),
                        "Cost/New User": f"${cost_per_nc:,.0f}",
                        "NC Rev": f"${nc_rev:,.0f}",
                        "NC Orders": int(nc_orders),
                        "NC AOV": f"${nc_aov:,.0f}",
                        "NC ROAS": f"{nc_roas:.2f}x",
                        "NC CPA": f"${nc_cpa:,.0f}",
                    })

                    # Roll Up: combine with Amazon if available
                    amz_rev_val = 0
                    amz_spend_val = 0
                    amz_new_cust = 0
                    amz_repeat_cust = 0
                    amz_new_rev_val = 0
                    amz_repeat_rev_val = 0
                    if amz_agg is not None and period_col in amz_agg.columns:
                        amz_match = amz_agg[amz_agg[period_col] == period_label]
                        if not amz_match.empty:
                            amz_rev_val = float(amz_match.iloc[0].get("_amz_revenue", 0))
                            amz_spend_val = float(amz_match.iloc[0].get("_amz_spend", 0))
                            amz_new_cust = int(amz_match.iloc[0].get("_amz_new_cust", 0))
                            amz_repeat_cust = int(amz_match.iloc[0].get("_amz_repeat_cust", 0))
                            amz_new_rev_val = float(amz_match.iloc[0].get("_amz_new_rev", 0))
                            amz_repeat_rev_val = float(amz_match.iloc[0].get("_amz_repeat_rev", 0))

                    combined_rev = total_rev + amz_rev_val
                    combined_spend = spend + amz_spend_val
                    combined_new_cust = int(nc_orders) + amz_new_cust
                    combined_new_rev = nc_rev + amz_new_rev_val
                    combined_total_cust = int(nc_orders) + amz_new_cust + amz_repeat_cust
                    nc_mer = combined_new_rev / combined_spend if combined_spend > 0 else 0
                    mer = combined_rev / combined_spend if combined_spend > 0 else 0
                    combined_nc_cpa = combined_spend / combined_new_cust if combined_new_cust > 0 else 0

                    rows_rollup.append({
                        period_col: period_label,
                        "Spend": f"${combined_spend:,.0f}",
                        "Revenue": f"${combined_rev:,.0f}",
                        "New Cust": combined_new_cust,
                        "Repeat Cust": amz_repeat_cust,
                        "Total Cust": combined_total_cust,
                        "New Rev": f"${combined_new_rev:,.0f}",
                        "NC MER": f"{nc_mer:.2f}x",
                        "NC CPA": f"${combined_nc_cpa:,.0f}",
                        "MER": f"{mer:.2f}x",
                    })

                    # Amazon row
                    rows_amz.append({
                        period_col: period_label,
                        "Spend": f"${amz_spend_val:,.0f}",
                        "Total Revenue": f"${amz_rev_val:,.0f}",
                        "New Cust": amz_new_cust,
                        "Repeat Cust": amz_repeat_cust,
                        "New Rev": f"${amz_new_rev_val:,.0f}",
                        "Repeat Rev": f"${amz_repeat_rev_val:,.0f}",
                    })

                return (
                    pd.DataFrame(rows_dtc),
                    pd.DataFrame(rows_rollup),
                    pd.DataFrame(rows_amz),
                )

            # --- Period-over-period color helper for DoD/WoW/MoM tables ---
            # For each numeric column, compare to previous period and color green/red
            # "higher_is_good" = True: increase = green, decrease = red
            # "higher_is_good" = False: increase = red, decrease = green (e.g. CPA)
            _PERF_COL_DIRECTION = {
                # Roll Up columns
                "Spend": True,          # spending more = investing = good
                "Revenue": True,        # more revenue = good
                "Total Rev": True,
                "Total Revenue": True,
                "NC Rev": True,         # more NC revenue = good
                "New Rev": True,        # more new revenue = good
                "Repeat Rev": True,     # more repeat revenue = good
                "NC Orders": True,      # more new customers = good
                "New Cust": True,       # more new customers = good
                "Repeat Cust": True,    # more repeat customers = good
                "Total Cust": True,     # more total customers = good
                "New Users": True,
                "NC MER": True,         # higher efficiency = good
                "MER": True,            # higher efficiency = good
                "NC ROAS": True,        # higher return = good
                "NC AOV": True,         # higher AOV = good
                "NC CPA": False,        # lower CPA = good
                "Cost/New User": False,  # lower cost = good
            }

            def _parse_perf_num(val):
                """Extract numeric value from formatted string like '$1,234' or '1.50x' or '2.3%'."""
                if isinstance(val, (int, float)):
                    return float(val)
                if not isinstance(val, str):
                    return None
                s = val.replace("$", "").replace(",", "").replace("x", "").replace("%", "").strip()
                try:
                    return float(s)
                except (ValueError, TypeError):
                    return None

            def _gradient_perf_style(pct_change, higher_is_good):
                """Return CSS for a cell based on gradient intensity of change.

                Maps pct_change to a smooth red <-> yellow <-> green gradient:
                  - Deep green   (+20%+ good change)
                  - Light green  (+10% good change)
                  - Yellow/amber (~0% or small change, within +/-3%)
                  - Light red    (-10% bad change)
                  - Deep red     (-20%+ bad change)
                """
                # Determine if the change is favorable
                signed = pct_change if higher_is_good else -pct_change

                # Clamp to +/-30% for gradient mapping
                t = max(-0.30, min(0.30, signed))

                if abs(t) < 0.03:
                    # Neutral zone — soft amber/yellow
                    return "color: #92700c; font-weight: 500; background-color: rgba(245,166,35,0.10)"

                if t > 0:
                    # Positive (good) — interpolate from light green to deep green
                    # t ranges 0.03 -> 0.30, map to intensity 0.0 -> 1.0
                    intensity = min(1.0, (t - 0.03) / 0.27)
                    # Text color: light green #15803d -> deep green #065f26
                    r = int(21 - 11 * intensity)      # 21 -> 6
                    g = int(128 - 33 * intensity)     # 128 -> 95
                    b = int(61 - 23 * intensity)      # 61 -> 38
                    # Background opacity: 0.06 -> 0.18
                    bg_alpha = 0.06 + 0.12 * intensity
                    return (
                        f"color: rgb({r},{g},{b}); font-weight: 600; "
                        f"background-color: rgba(16,185,80,{bg_alpha:.2f})"
                    )
                else:
                    # Negative (bad) — interpolate from light red to deep red
                    intensity = min(1.0, (abs(t) - 0.03) / 0.27)
                    # Text color: light red #dc2626 -> deep red #991b1b
                    r = int(220 - 67 * intensity)     # 220 -> 153
                    g = int(38 - 11 * intensity)      # 38 -> 27
                    b = int(38 - 11 * intensity)      # 38 -> 27
                    # Background opacity: 0.05 -> 0.16
                    bg_alpha = 0.05 + 0.11 * intensity
                    return (
                        f"color: rgb({r},{g},{b}); font-weight: 600; "
                        f"background-color: rgba(220,38,38,{bg_alpha:.2f})"
                    )

            def _render_perf_table_colored(df, period_col, max_height=420):
                """Render a performance table with gradient period-over-period color coding."""
                if df.empty or len(df) < 2:
                    _render_df_as_html(df, max_height=max_height)
                    return

                # Sort descending for display (most recent first)
                display_df = df.sort_values(period_col, ascending=False).reset_index(drop=True)
                numeric_cols = [c for c in display_df.columns if c != period_col]

                def _apply_display_styles(row):
                    idx = row.name
                    styles = [""] * len(row)
                    col_map = {c: i for i, c in enumerate(display_df.columns)}

                    # Bold period column
                    if period_col in col_map:
                        styles[col_map[period_col]] = "font-weight: 600; color: #0F3557"

                    # Compare with next row (since sorted desc, next row = previous period)
                    if idx >= len(display_df) - 1:
                        return styles

                    for col in numeric_cols:
                        if col not in col_map:
                            continue
                        curr_val = _parse_perf_num(display_df.iloc[idx][col])
                        prev_val = _parse_perf_num(display_df.iloc[idx + 1][col])

                        if curr_val is None or prev_val is None or prev_val == 0:
                            continue

                        pct_change = (curr_val - prev_val) / abs(prev_val)
                        higher_is_good = _PERF_COL_DIRECTION.get(col, True)

                        styles[col_map[col]] = _gradient_perf_style(pct_change, higher_is_good)

                    return styles

                styled = (
                    display_df.style
                    .set_properties(**{
                        "font-size": "0.84rem",
                        "font-family": "Visby CF, DM Sans, -apple-system, sans-serif",
                        "color": "#1e2d3d",
                        "background-color": "#ffffff",
                    })
                    .apply(_apply_display_styles, axis=1)
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
                            ("white-space", "nowrap"),
                        ]},
                    ])
                    .hide(axis="index")
                )
                html = styled.to_html()
                height_style = f"max-height:{max_height}px;overflow-y:auto;" if max_height else ""
                st.markdown(
                    f'<div style="background:#ffffff;border-radius:12px;'
                    f'box-shadow:0 2px 12px rgba(15,53,87,0.08);border:1px solid #E8EDF3;'
                    f'width:100%;overflow:hidden;">'
                    f'<div class="perf-table" style="overflow-x:auto;{height_style}">'
                    '<style>'
                    '.perf-table table { width:100%; border-collapse:collapse; }'
                    '.perf-table th, .perf-table td { white-space:nowrap; }'
                    '.perf-table th { position:sticky; top:0; z-index:1; background-color:#F0F4F8 !important; }'
                    '.perf-table tr:hover td:not([style*="background-color"]) { background:#F7FAFC !important; }'
                    '</style>'
                    f'{html}</div></div>',
                    unsafe_allow_html=True,
                )

            # --- Aggregate helpers ---
            # We use the full (unfiltered by date range) dataset for these tabs
            # Shopify DB for revenue/customers, Google Sheet for spend/sessions
            _perf_df = _shopify_daily.copy()
            if not _gs_spend.empty:
                _perf_df = _perf_df.merge(
                    _gs_spend, on='_date', how='left', suffixes=('', '_gs')
                )
            else:
                _perf_df['_ad_spend'] = 0
            _perf_df = _perf_df.sort_values('_date')
            _perf_df['_ad_spend'] = _perf_df['_ad_spend'].fillna(0)

            # Amazon daily data: revenue from daily_sku_sales, spend from amazon_daily_rollup,
            # new/repeat customer counts and revenue from orders/customers/order_items
            _amz_daily = pd.DataFrame()
            try:
                with get_db() as _amz_perf_conn:
                    _amz_rev_daily = read_sql(
                        "SELECT sale_date, SUM(revenue) as _amz_revenue "
                        "FROM daily_sku_sales WHERE source = 'amazon' "
                        "GROUP BY sale_date ORDER BY sale_date",
                        _amz_perf_conn,
                    )
                    _amz_spend_daily = read_sql(
                        "SELECT date as sale_date, spend as _amz_spend "
                        "FROM amazon_daily_rollup ORDER BY date",
                        _amz_perf_conn,
                    )
                    # New/repeat customer counts and order_items revenue by day
                    _amz_cust_daily = read_sql(
                        "SELECT DATE(o.order_date) AS sale_date, "
                        "COUNT(DISTINCT o.customer_id) AS total_customers, "
                        "COUNT(DISTINCT CASE WHEN DATE(c.first_order_date) = DATE(o.order_date) "
                        "THEN o.customer_id END) AS new_customers, "
                        "SUM(oi.total_price) AS oi_total_rev, "
                        "SUM(CASE WHEN DATE(c.first_order_date) = DATE(o.order_date) "
                        "THEN oi.total_price ELSE 0 END) AS oi_new_rev "
                        "FROM orders o "
                        "JOIN customers c ON o.customer_id = c.customer_id "
                        "JOIN order_items oi ON o.order_id = oi.order_id "
                        "WHERE o.source = %s "
                        "GROUP BY DATE(o.order_date)",
                        _amz_perf_conn,
                        params=('amazon',),
                    )
                if not _amz_rev_daily.empty:
                    _amz_daily = _amz_rev_daily
                    if not _amz_spend_daily.empty:
                        _amz_daily = _amz_daily.merge(_amz_spend_daily, on="sale_date", how="left")
                    if "_amz_spend" not in _amz_daily.columns:
                        _amz_daily["_amz_spend"] = 0
                    _amz_daily["_amz_spend"] = _amz_daily["_amz_spend"].fillna(0)

                    # Merge customer data and derive new/repeat counts + proportionally scaled revenue
                    if not _amz_cust_daily.empty:
                        _amz_cust_daily["sale_date"] = _amz_cust_daily["sale_date"].astype(str)
                        _amz_daily = _amz_daily.merge(
                            _amz_cust_daily[["sale_date", "total_customers", "new_customers",
                                             "oi_total_rev", "oi_new_rev"]],
                            on="sale_date", how="left",
                        )
                    for _col in ["total_customers", "new_customers", "oi_total_rev", "oi_new_rev"]:
                        if _col not in _amz_daily.columns:
                            _amz_daily[_col] = 0
                        _amz_daily[_col] = _amz_daily[_col].fillna(0)

                    _amz_daily["_amz_new_cust"] = _amz_daily["new_customers"].astype(int)
                    _amz_daily["_amz_repeat_cust"] = (_amz_daily["total_customers"] - _amz_daily["new_customers"]).clip(lower=0).astype(int)

                    # Scale new/repeat revenue proportionally from daily_sku_sales total
                    _oi_total = _amz_daily["oi_total_rev"]
                    _new_frac = (_amz_daily["oi_new_rev"] / _oi_total).where(_oi_total > 0, 0)
                    _amz_daily["_amz_new_rev"] = _amz_daily["_amz_revenue"] * _new_frac
                    _amz_daily["_amz_repeat_rev"] = _amz_daily["_amz_revenue"] * (1 - _new_frac)

                    _amz_daily["_date"] = pd.to_datetime(_amz_daily["sale_date"])
            except Exception:
                pass

            # --- DAY OVER DAY ---
            with _perf_tab_dod:
                _dod_df = _perf_df.copy()
                _dod_df["Day"] = _dod_df["_date"].dt.strftime("%Y-%m-%d")
                _dod_agg_cols = {"_ad_spend": "sum", "_revenue": "sum", "_nc_orders": "sum",
                                 "_nc_revenue": "sum", "_ret_revenue": "sum",
                                 "_orders": "sum", "_units": "sum"}
                _dod_agg = _dod_df.groupby("Day", sort=True).agg(_dod_agg_cols).reset_index()

                # Amazon daily
                _amz_dod = pd.DataFrame(columns=["Day", "_amz_revenue", "_amz_spend",
                                                  "_amz_new_cust", "_amz_repeat_cust",
                                                  "_amz_new_rev", "_amz_repeat_rev"])
                if not _amz_daily.empty:
                    _amz_dod = _amz_daily.copy()
                    _amz_dod["Day"] = _amz_dod["_date"].dt.strftime("%Y-%m-%d")
                    _amz_dod_agg = {
                        "_amz_revenue": ("_amz_revenue", "sum"),
                        "_amz_spend": ("_amz_spend", "sum"),
                    }
                    if "_amz_new_cust" in _amz_dod.columns:
                        _amz_dod_agg.update({
                            "_amz_new_cust": ("_amz_new_cust", "sum"),
                            "_amz_repeat_cust": ("_amz_repeat_cust", "sum"),
                            "_amz_new_rev": ("_amz_new_rev", "sum"),
                            "_amz_repeat_rev": ("_amz_repeat_rev", "sum"),
                        })
                    _amz_dod = _amz_dod.groupby("Day", sort=True).agg(**_amz_dod_agg).reset_index()

                _dtc_dod, _rollup_dod, _amz_tbl_dod = _build_perf_table(_dod_agg, "Day", _amz_dod)

                st.caption('Day-over-day performance comparison from Shopify orders + Google Sheets daily data.')
                if _nav_channel == 'DTC':
                    _render_perf_table_colored(_dtc_dod, "Day", max_height=420)
                elif _nav_channel == 'Amazon':
                    _render_perf_table_colored(_amz_tbl_dod, "Day", max_height=420)
                else:
                    st.subheader("Roll Up")
                    _render_perf_table_colored(_rollup_dod, "Day", max_height=420)
                    st.subheader("DTC (Shopify)")
                    _render_perf_table_colored(_dtc_dod, "Day", max_height=420)
                    st.subheader("Amazon")
                    _render_perf_table_colored(_amz_tbl_dod, "Day", max_height=420)

            # --- WEEK OVER WEEK ---
            with _perf_tab_wow:
                _wow_df = _perf_df.copy()
                _wow_df["_week_start"] = _wow_df["_date"].dt.to_period("W-SAT").apply(lambda x: x.start_time)
                _wow_df["Week"] = _wow_df["_week_start"].dt.strftime("%Y-%m-%d")
                _wow_df["Wk #"] = _wow_df["_date"].dt.isocalendar().week.astype(int)
                _wow_agg = _wow_df.groupby("Week", sort=True).agg(
                    _ad_spend=("_ad_spend", "sum"), _revenue=("_revenue", "sum"),
                    _nc_orders=("_nc_orders", "sum"), _nc_revenue=("_nc_revenue", "sum"),
                    _ret_revenue=("_ret_revenue", "sum"),
                    _orders=("_orders", "sum"), _units=("_units", "sum"),
                    **{"Wk #": ("Wk #", "first")},
                ).reset_index()

                # Amazon weekly
                _amz_wow = pd.DataFrame(columns=["Week", "_amz_revenue", "_amz_spend",
                                                  "_amz_new_cust", "_amz_repeat_cust",
                                                  "_amz_new_rev", "_amz_repeat_rev"])
                if not _amz_daily.empty:
                    _amz_wow_tmp = _amz_daily.copy()
                    _amz_wow_tmp["_week_start"] = _amz_wow_tmp["_date"].dt.to_period("W-SAT").apply(lambda x: x.start_time)
                    _amz_wow_tmp["Week"] = _amz_wow_tmp["_week_start"].dt.strftime("%Y-%m-%d")
                    _amz_wow_agg = {
                        "_amz_revenue": ("_amz_revenue", "sum"),
                        "_amz_spend": ("_amz_spend", "sum"),
                    }
                    if "_amz_new_cust" in _amz_wow_tmp.columns:
                        _amz_wow_agg.update({
                            "_amz_new_cust": ("_amz_new_cust", "sum"),
                            "_amz_repeat_cust": ("_amz_repeat_cust", "sum"),
                            "_amz_new_rev": ("_amz_new_rev", "sum"),
                            "_amz_repeat_rev": ("_amz_repeat_rev", "sum"),
                        })
                    _amz_wow = _amz_wow_tmp.groupby("Week", sort=True).agg(**_amz_wow_agg).reset_index()

                _dtc_wow, _rollup_wow, _amz_tbl_wow = _build_perf_table(_wow_agg, "Week", _amz_wow)
                st.caption('Week-over-week performance comparison from Shopify orders + Google Sheets data.')

                # Insert week number after the Week column
                _wk_map = _wow_agg.set_index("Week")["Wk #"]
                for _tbl in [_dtc_wow, _rollup_wow, _amz_tbl_wow]:
                    if not _tbl.empty and "Week" in _tbl.columns:
                        _tbl.insert(1, "Wk #", _tbl["Week"].map(_wk_map).fillna(0).astype(int))

                if _nav_channel == 'DTC':
                    _render_perf_table_colored(_dtc_wow, "Week", max_height=420)
                elif _nav_channel == 'Amazon':
                    _render_perf_table_colored(_amz_tbl_wow, "Week", max_height=420)
                else:
                    st.subheader("Roll Up")
                    _render_perf_table_colored(_rollup_wow, "Week", max_height=420)
                    st.subheader("DTC (Shopify)")
                    _render_perf_table_colored(_dtc_wow, "Week", max_height=420)
                    st.subheader("Amazon")
                    _render_perf_table_colored(_amz_tbl_wow, "Week", max_height=420)

            # --- MONTH OVER MONTH ---
            with _perf_tab_mom:
                _mom_df = _perf_df.copy()
                _mom_df["Month"] = _mom_df["_date"].dt.to_period("M").astype(str)
                _mom_agg = _mom_df.groupby("Month", sort=True).agg(
                    _ad_spend=("_ad_spend", "sum"), _revenue=("_revenue", "sum"),
                    _nc_orders=("_nc_orders", "sum"), _nc_revenue=("_nc_revenue", "sum"),
                    _ret_revenue=("_ret_revenue", "sum"),
                    _orders=("_orders", "sum"), _units=("_units", "sum"),
                ).reset_index()

                # Amazon monthly
                _amz_mom = pd.DataFrame(columns=["Month", "_amz_revenue", "_amz_spend",
                                                  "_amz_new_cust", "_amz_repeat_cust",
                                                  "_amz_new_rev", "_amz_repeat_rev"])
                if not _amz_daily.empty:
                    _amz_mom_tmp = _amz_daily.copy()
                    _amz_mom_tmp["Month"] = _amz_mom_tmp["_date"].dt.to_period("M").astype(str)
                    _amz_mom_agg = {
                        "_amz_revenue": ("_amz_revenue", "sum"),
                        "_amz_spend": ("_amz_spend", "sum"),
                    }
                    if "_amz_new_cust" in _amz_mom_tmp.columns:
                        _amz_mom_agg.update({
                            "_amz_new_cust": ("_amz_new_cust", "sum"),
                            "_amz_repeat_cust": ("_amz_repeat_cust", "sum"),
                            "_amz_new_rev": ("_amz_new_rev", "sum"),
                            "_amz_repeat_rev": ("_amz_repeat_rev", "sum"),
                        })
                    _amz_mom = _amz_mom_tmp.groupby("Month", sort=True).agg(**_amz_mom_agg).reset_index()

                _dtc_mom, _rollup_mom, _amz_tbl_mom = _build_perf_table(_mom_agg, "Month", _amz_mom)
                st.caption('Month-over-month performance comparison from Shopify orders + Google Sheets data.')

                if _nav_channel == 'DTC':
                    _render_perf_table_colored(_dtc_mom, "Month")
                elif _nav_channel == 'Amazon':
                    _render_perf_table_colored(_amz_tbl_mom, "Month")
                else:
                    st.subheader("Roll Up")
                    _render_perf_table_colored(_rollup_mom, "Month")
                    st.subheader("DTC (Shopify)")
                    _render_perf_table_colored(_dtc_mom, "Month")
                    st.subheader("Amazon")
                    _render_perf_table_colored(_amz_tbl_mom, "Month")

            st.divider()

            # ============================================================
            # SECTION 1: NEW CUSTOMER ACQUISITION (Period Detail)
            # ============================================================
            st.header("New Customer Acquisition")

            if _nav_channel in ('DTC', 'Rollup'):
                nc_cpa_avg = total_spend / total_nc if total_nc > 0 else 0
                nc_roas = nc_rev / total_spend if total_spend > 0 else 0
                nc_aov_avg = nc_rev / total_nc if total_nc > 0 else 0

                nc1, nc2, nc3, nc4, nc5 = st.columns(5)
                nc1.metric("New Customers", f"{total_nc:,}")
                nc2.metric("NC Revenue", f"${nc_rev:,.0f}")
                nc3.metric("Ad Spend", f"${total_spend:,.0f}")
                nc4.metric("NC CPA", f"${nc_cpa_avg:,.0f}", help="Total ad spend / new customer orders")
                nc5.metric("NC ROAS", f"{nc_roas:.2f}x", help="New customer revenue / ad spend")

            # DB Ground Truth — per-channel new/repeat from order history (with projections)
            _db_nr_all = get_projected_new_repeat_summary(str(mkt_start), str(mkt_end))
            _db_nr_dtc = get_projected_new_repeat_summary(str(mkt_start), str(mkt_end), 'shopify')
            _db_nr_amz = get_projected_new_repeat_summary(str(mkt_start), str(mkt_end), 'amazon')

            def _mkt_fmt(actual, projected, prefix='', suffix=''):
                total = actual + projected
                if projected > 0:
                    return f"{prefix}{total:,.0f}{suffix} (est)"
                return f"{prefix}{total:,.0f}{suffix}"

            if _db_nr_all['new_customers'] > 0 or _db_nr_all['repeat_customers'] > 0:
                _mkt_gap_parts = []
                if _db_nr_amz.get('gap_days', 0) > 0:
                    _mkt_gap_parts.append(f"Amazon through {_db_nr_amz['last_data_date']}")
                if _db_nr_dtc.get('gap_days', 0) > 0:
                    _mkt_gap_parts.append(f"Shopify through {_db_nr_dtc['last_data_date']}")
                _mkt_gap_note = f" — *{'; '.join(_mkt_gap_parts)}, DOW-adjusted est. for missing days*" if _mkt_gap_parts else ""
                st.caption(f"**DB Ground Truth** (from Shopify + Amazon order history){_mkt_gap_note}")
                _nc_gt_cols = []
                if _nav_channel == 'Rollup':
                    _nc_gt_cols.append("rollup")
                if _nav_channel in ('Rollup', 'DTC'):
                    _nc_gt_cols.append("dtc")
                if _nav_channel in ('Rollup', 'Amazon'):
                    _nc_gt_cols.append("amz")
                _nc_gt_st_cols = st.columns(len(_nc_gt_cols))
                for _gt_idx, _gt_key in enumerate(_nc_gt_cols):
                    with _nc_gt_st_cols[_gt_idx]:
                        if _gt_key == "rollup":
                            st.markdown("**Roll Up**")
                            _dbru1, _dbru2, _dbru3 = st.columns(3)
                            _dbru1.metric("New Cust", _mkt_fmt(_db_nr_all['new_customers'], _db_nr_all.get('projected_new_customers', 0)))
                            _dbru2.metric("NC Rev", _mkt_fmt(_db_nr_all['new_revenue'], _db_nr_all.get('projected_new_revenue', 0), prefix='$'))
                            _ru_tot_new = _db_nr_all.get('total_new_revenue', _db_nr_all['new_revenue'])
                            _ru_tot_rep = _db_nr_all.get('total_repeat_revenue', _db_nr_all['repeat_revenue'])
                            _db_nc_pct_all = _ru_tot_new / (_ru_tot_new + _ru_tot_rep) * 100 if (_ru_tot_new + _ru_tot_rep) > 0 else 0
                            _dbru3.metric("NC % Rev", f"{_db_nc_pct_all:.0f}%")
                        elif _gt_key == "dtc":
                            st.markdown("**DTC (Shopify)**")
                            _dbdtc1, _dbdtc2, _dbdtc3 = st.columns(3)
                            _dbdtc1.metric("New Cust", _mkt_fmt(_db_nr_dtc['new_customers'], _db_nr_dtc.get('projected_new_customers', 0)))
                            _dbdtc2.metric("NC Rev", _mkt_fmt(_db_nr_dtc['new_revenue'], _db_nr_dtc.get('projected_new_revenue', 0), prefix='$'))
                            _dtc_tot_new = _db_nr_dtc.get('total_new_revenue', _db_nr_dtc['new_revenue'])
                            _dtc_tot_rep = _db_nr_dtc.get('total_repeat_revenue', _db_nr_dtc['repeat_revenue'])
                            _db_nc_pct_dtc = _dtc_tot_new / (_dtc_tot_new + _dtc_tot_rep) * 100 if (_dtc_tot_new + _dtc_tot_rep) > 0 else 0
                            _dbdtc3.metric("NC % Rev", f"{_db_nc_pct_dtc:.0f}%")
                        elif _gt_key == "amz":
                            st.markdown("**Amazon**")
                            _dbamz1, _dbamz2, _dbamz3 = st.columns(3)
                            _dbamz1.metric("New Cust", _mkt_fmt(_db_nr_amz['new_customers'], _db_nr_amz.get('projected_new_customers', 0)))
                            _dbamz2.metric("NC Rev", _mkt_fmt(_db_nr_amz['new_revenue'], _db_nr_amz.get('projected_new_revenue', 0), prefix='$'))
                            _amz_tot_new = _db_nr_amz.get('total_new_revenue', _db_nr_amz['new_revenue'])
                            _amz_tot_rep = _db_nr_amz.get('total_repeat_revenue', _db_nr_amz['repeat_revenue'])
                            _db_nc_pct_amz = _amz_tot_new / (_amz_tot_new + _amz_tot_rep) * 100 if (_amz_tot_new + _amz_tot_rep) > 0 else 0
                            _dbamz3.metric("NC % Rev", f"{_db_nc_pct_amz:.0f}%")

            if _nav_channel in ('DTC', 'Rollup'):
                st.divider()

                # NC Revenue & Spend over time
                st.subheader("NC Revenue vs Ad Spend")
                st.caption('New customer revenue vs ad spend from Shopify orders + Google Sheets spend data.')
                fig_nc = go.Figure()
                fig_nc.add_trace(go.Bar(
                    x=mkt_df["_date"], y=mkt_df["_nc_revenue"],
                    name="NC Revenue", marker_color="#F58B3D",
                ))
                fig_nc.add_trace(go.Bar(
                    x=mkt_df["_date"], y=mkt_df["_ad_spend"],
                    name="Ad Spend", marker_color="#E05252",
                ))
                fig_nc.update_layout(
                    barmode="group", height=320,
                    margin=dict(l=0, r=0, t=10, b=0),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                    yaxis=dict(gridcolor="#E8EDF3"),
                )
                st.plotly_chart(fig_nc, use_container_width=True)

            st.divider()

            # ============================================================
            # SECTION 2: REPEAT / RETURNING CUSTOMERS
            # ============================================================
            st.header("Repeat Revenue")

            if _nav_channel in ('DTC', 'Rollup'):
                ret_aov = ret_rev / total_ret if total_ret > 0 else 0
                ret_pct = ret_rev / total_rev * 100 if total_rev > 0 else 0

                rc1, rc2, rc3, rc4 = st.columns(4)
                rc1.metric("Returning Orders", f"{total_ret:,}")
                rc2.metric("Repeat Revenue", f"${ret_rev:,.0f}")
                rc3.metric("Repeat AOV", f"${ret_aov:,.0f}")
                rc4.metric("% of Total Revenue", f"{ret_pct:.0f}%")

            # DB Ground Truth — Repeat Revenue per channel (with projections)
            if _db_nr_all['repeat_customers'] > 0:
                _rpt_gap_note = f" — *DOW-adjusted est. for missing days*" if any(d.get('gap_days', 0) > 0 for d in [_db_nr_all, _db_nr_dtc, _db_nr_amz]) else ""
                st.caption(f"**DB Ground Truth** (from Shopify + Amazon order history){_rpt_gap_note}")
                _rpt_gt_cols = []
                if _nav_channel == 'Rollup':
                    _rpt_gt_cols.append("rollup")
                if _nav_channel in ('Rollup', 'DTC'):
                    _rpt_gt_cols.append("dtc")
                if _nav_channel in ('Rollup', 'Amazon'):
                    _rpt_gt_cols.append("amz")
                _rpt_gt_st_cols = st.columns(len(_rpt_gt_cols))
                for _rgt_idx, _rgt_key in enumerate(_rpt_gt_cols):
                    with _rpt_gt_st_cols[_rgt_idx]:
                        if _rgt_key == "rollup":
                            st.markdown("**Roll Up**")
                            _dr1, _dr2, _dr3 = st.columns(3)
                            _dr1.metric("Repeat Orders", f"{_db_nr_all['repeat_orders']:,}")
                            _dr2.metric("Repeat Rev", _mkt_fmt(_db_nr_all['repeat_revenue'], _db_nr_all.get('projected_repeat_revenue', 0), prefix='$'))
                            _dr3.metric("Repeat AOV", f"${_db_nr_all['repeat_aov']:,.0f}")
                        elif _rgt_key == "dtc":
                            st.markdown("**DTC (Shopify)**")
                            _drd1, _drd2, _drd3 = st.columns(3)
                            _drd1.metric("Repeat Orders", f"{_db_nr_dtc['repeat_orders']:,}")
                            _drd2.metric("Repeat Rev", _mkt_fmt(_db_nr_dtc['repeat_revenue'], _db_nr_dtc.get('projected_repeat_revenue', 0), prefix='$'))
                            _drd3.metric("Repeat AOV", f"${_db_nr_dtc['repeat_aov']:,.0f}")
                        elif _rgt_key == "amz":
                            st.markdown("**Amazon**")
                            _dra1, _dra2, _dra3 = st.columns(3)
                            _dra1.metric("Repeat Orders", f"{_db_nr_amz['repeat_orders']:,}")
                            _dra2.metric("Repeat Rev", _mkt_fmt(_db_nr_amz['repeat_revenue'], _db_nr_amz.get('projected_repeat_revenue', 0), prefix='$'))
                            _dra3.metric("Repeat AOV", f"${_db_nr_amz['repeat_aov']:,.0f}")

            # Repeat revenue over time (DTC only)
            if _nav_channel in ('DTC', 'Rollup'):
                fig_ret = go.Figure()
                fig_ret.add_trace(go.Bar(
                    x=mkt_df["_date"], y=mkt_df["_ret_revenue"],
                    name="Repeat Revenue", marker_color="#5BB8D4",
                ))
                fig_ret.update_layout(
                    height=280, margin=dict(l=0, r=0, t=10, b=0),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                    yaxis=dict(gridcolor="#E8EDF3"),
                )
                st.caption('Daily repeat customer revenue from Shopify orders.')
                st.plotly_chart(fig_ret, use_container_width=True)

            # Subscription metrics (from Google Sheet via _load_gs_spend) — DTC only
            _has_subs = "_total_active_subscriptions" in mkt_df.columns and _nav_channel in ('DTC', 'Rollup')
            if _has_subs:
                st.subheader("Subscriptions")
                mkt_df["_active_subs"] = mkt_df["_total_active_subscriptions"].fillna(0)
                mkt_df["_new_subs"] = mkt_df.get("_total_new_subscriptions", pd.Series(0)).fillna(0)
                mkt_df["_cancelled_subs"] = mkt_df.get("_total_cancelled_subscriptions", pd.Series(0)).fillna(0)
                mkt_df["_sub_rev"] = mkt_df.get("_total_subscription_order_revenue", pd.Series(0)).fillna(0)

                sub1, sub2, sub3, sub4 = st.columns(4)
                sub1.metric("Active Subscriptions", f"{mkt_df['_active_subs'].iloc[-1]:,.0f}")
                sub2.metric("New Subs (Period)", f"{mkt_df['_new_subs'].sum():,.0f}")
                sub3.metric("Cancelled (Period)", f"{mkt_df['_cancelled_subs'].sum():,.0f}")
                sub4.metric("Sub Revenue", f"${mkt_df['_sub_rev'].sum():,.0f}")

                fig_subs = go.Figure()
                fig_subs.add_trace(go.Scatter(
                    x=mkt_df["_date"], y=mkt_df["_active_subs"],
                    name="Active Subscriptions", fill="tozeroy",
                    line=dict(color="#6B8FA3"),
                ))
                fig_subs.update_layout(height=200, margin=dict(l=0, r=0, t=10, b=0),
                                       plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                                       yaxis=dict(gridcolor="#E8EDF3"))
                st.caption('Active subscription count over time from Google Sheets.')
                st.plotly_chart(fig_subs, use_container_width=True)

                fig_net_sub = go.Figure()
                fig_net_sub.add_trace(go.Bar(x=mkt_df["_date"], y=mkt_df["_new_subs"], name="New", marker_color="#2DA87E"))
                fig_net_sub.add_trace(go.Bar(x=mkt_df["_date"], y=-mkt_df["_cancelled_subs"], name="Cancelled", marker_color="#E05252"))
                fig_net_sub.update_layout(
                    barmode="relative", height=200, margin=dict(l=0, r=0, t=10, b=0),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                    yaxis=dict(gridcolor="#E8EDF3"),
                )
                st.caption('New vs cancelled subscriptions from Google Sheets.')
                st.plotly_chart(fig_net_sub, use_container_width=True)

            # ============================================================
            # SECTION 3: COMBINED SUMMARY (DTC only)
            # ============================================================
            if _nav_channel in ('DTC', 'Rollup'):
                st.divider()
                st.header("Weekly Summary")
                mkt_df["_week"] = mkt_df["_date"].dt.to_period("W").apply(lambda x: x.start_time)
                weekly = mkt_df.groupby("_week").agg(
                    Revenue=("_revenue", "sum"),
                    NC_Revenue=("_nc_revenue", "sum"),
                    Ret_Revenue=("_ret_revenue", "sum"),
                    Ad_Spend=("_ad_spend", "sum"),
                    Orders=("_orders", "sum"),
                    NC_Orders=("_nc_orders", "sum"),
                ).reset_index()
                weekly["NC ROAS"] = (weekly["NC_Revenue"] / weekly["Ad_Spend"]).round(2).replace([float("inf")], 0)
                weekly["NC CPA"] = (weekly["Ad_Spend"] / weekly["NC_Orders"]).round(0).replace([float("inf")], 0)
                weekly["_week"] = weekly["_week"].dt.strftime("%Y-%m-%d")
                weekly = weekly.rename(columns={
                    "_week": "Week", "NC_Revenue": "NC Rev", "Ret_Revenue": "Repeat Rev",
                    "Ad_Spend": "Ad Spend", "NC_Orders": "New Custs",
                })
                for col in ["Revenue", "NC Rev", "Repeat Rev", "Ad Spend", "NC CPA"]:
                    weekly[col] = weekly[col].apply(lambda x: f"${x:,.0f}")
                weekly["Orders"] = weekly["Orders"].astype(int)
                weekly["New Custs"] = weekly["New Custs"].astype(int)
                weekly["NC ROAS"] = weekly["NC ROAS"].apply(lambda x: f"{x:.2f}x")
                st.caption('Weekly rollup of key marketing metrics from Shopify orders + Google Sheets spend data.')
                render_html_table(weekly.sort_values("Week", ascending=False))

        else:
            st.info("No marketing data available. Check that Shopify data has been synced.")
    else:
        st.info("No marketing data available. Sync Shopify data from the **Settings** page.")

    # --- Klaviyo Email Dashboard (DTC only) ---
    if ctx.get('channel', 'Rollup') in ('DTC', 'Rollup'):
        st.divider()
        st.subheader("Email Performance")
        if not _mkt_klaviyo_key:
            st.info("Add your Klaviyo API key on the **Settings** page to see email analytics.")
        else:
            # Refresh button — syncs metadata + metrics from API
            if st.button("Refresh from Klaviyo", key="kl_refresh"):
                with st.spinner("Syncing from Klaviyo API..."):
                    try:
                        from etl.klaviyo_client import (
                            fetch_campaigns, fetch_flows, fetch_lists,
                            fetch_placed_order_metric_id, fetch_any_metric_id,
                            fetch_campaign_metrics, fetch_flow_metrics,
                        )
                        campaigns = fetch_campaigns(_mkt_klaviyo_key)
                        flows = fetch_flows(_mkt_klaviyo_key)
                        lists = fetch_lists(_mkt_klaviyo_key)
                        with get_db() as conn:
                            for c in campaigns:
                                upsert_klaviyo_campaign(conn, c)
                            for f in flows:
                                upsert_klaviyo_flow(conn, f)
                            for l in lists:
                                upsert_klaviyo_list(conn, l)

                        # Metrics — need a conversion_metric_id (API requires it)
                        with get_db() as conn:
                            conv_id = get_setting(conn, "klaviyo_conversion_metric_id")
                        include_revenue = bool(conv_id)
                        if not conv_id:
                            conv_id = fetch_placed_order_metric_id(_mkt_klaviyo_key)
                            if conv_id:
                                include_revenue = True
                                with get_db() as conn:
                                    set_setting(conn, "klaviyo_conversion_metric_id", conv_id)
                            else:
                                conv_id = fetch_any_metric_id(_mkt_klaviyo_key)

                        cm = fetch_campaign_metrics(_mkt_klaviyo_key, conv_id, include_revenue)
                        fm = fetch_flow_metrics(_mkt_klaviyo_key, conv_id, include_revenue)
                        with get_db() as conn:
                            for cid, m in cm.items():
                                update_klaviyo_campaign_metrics(conn, cid, m)
                            for fid, m in fm.items():
                                update_klaviyo_flow_metrics(conn, fid, m)

                        parts = [f"Synced {len(campaigns)} campaigns, {len(flows)} flows"]
                        if cm or fm:
                            parts.append(f"metrics updated ({len(cm)} campaigns, {len(fm)} flows)")
                        if not include_revenue:
                            parts.append("revenue skipped (no Placed Order metric — set in Settings)")
                        st.success(" — ".join(parts))
                        st.rerun()
                    except Exception as e:
                        st.error(f"Sync failed: {e}")

            # Load from DB
            with get_db() as conn:
                kl_campaigns = get_klaviyo_campaigns(conn)
                kl_flows = get_klaviyo_flows(conn)
                kl_lists = get_klaviyo_lists(conn)
                has_revenue = bool(get_setting(conn, "klaviyo_conversion_metric_id"))

            # --- KPI summary cards ---
            if kl_campaigns:
                camp_df = pd.DataFrame(kl_campaigns)
                total_sends = int(camp_df["recipients"].sum())
                total_delivered = int(camp_df["delivered"].sum())
                # Weighted averages (by recipients)
                w = camp_df["recipients"].fillna(0)
                w_sum = w.sum()
                if w_sum > 0:
                    avg_open = (camp_df["open_rate"].fillna(0) * w).sum() / w_sum
                    avg_ctor = (camp_df["click_to_open_rate"].fillna(0) * w).sum() / w_sum
                    avg_click = (camp_df["click_rate"].fillna(0) * w).sum() / w_sum
                else:
                    avg_open = avg_ctor = avg_click = 0
                total_revenue = camp_df["revenue"].fillna(0).sum()

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Total Sends", f"{total_sends:,}")
                c2.metric("Avg Open Rate", f"{avg_open * 100:.1f}%")
                c3.metric("Avg CTOR", f"{avg_ctor * 100:.1f}%")
                if has_revenue:
                    c4.metric("Campaign Revenue", f"${total_revenue:,.0f}")
                else:
                    c4.metric("Campaign Revenue", "—")
                    st.caption("Set the Placed Order metric ID in **Settings** for revenue data.")

            # --- Tabs ---
            kl_tab1, kl_tab2, kl_tab3 = st.tabs(["Campaigns", "Flows", "Lists"])

            with kl_tab1:
                if kl_campaigns:
                    df = pd.DataFrame(kl_campaigns)
                    # Build display columns
                    display = pd.DataFrame()
                    display["Campaign"] = df["name"]
                    display["Send Date"] = df["send_time"].apply(
                        lambda x: str(x)[:10] if x else "—")
                    display["Status"] = df["status"]
                    display["Sends"] = df["recipients"]
                    display["Delivered"] = df["delivered"]
                    display["Open Rate"] = df["open_rate"].apply(
                        lambda x: f"{(x or 0) * 100:.1f}%")
                    display["Click Rate"] = df["click_rate"].apply(
                        lambda x: f"{(x or 0) * 100:.1f}%")
                    display["CTOR"] = df["click_to_open_rate"].apply(
                        lambda x: f"{(x or 0) * 100:.1f}%")
                    display["Unsubs"] = df["unsubscribes"]
                    if has_revenue:
                        display["Revenue"] = df["revenue"].apply(
                            lambda x: f"${(x or 0):,.0f}")
                        display["Rev/Recip"] = df["revenue_per_recipient"].apply(
                            lambda x: f"${(x or 0):,.2f}")

                    from ui.tables import format_number_cols
                    display = format_number_cols(display, ["Sends", "Delivered", "Unsubs"])
                    st.caption('Email campaign performance metrics from Klaviyo API.')
                    render_html_table(display, max_height=500)
                else:
                    st.info("No campaigns synced yet. Click **Refresh from Klaviyo** above.")

            with kl_tab2:
                if kl_flows:
                    df = pd.DataFrame(kl_flows)
                    display = pd.DataFrame()
                    display["Flow"] = df["name"]
                    display["Status"] = df["status"]
                    display["Sends"] = df["recipients"]
                    display["Delivered"] = df["delivered"]
                    display["Open Rate"] = df["open_rate"].apply(
                        lambda x: f"{(x or 0) * 100:.1f}%")
                    display["Click Rate"] = df["click_rate"].apply(
                        lambda x: f"{(x or 0) * 100:.1f}%")
                    display["CTOR"] = df["click_to_open_rate"].apply(
                        lambda x: f"{(x or 0) * 100:.1f}%")
                    display["Unsubs"] = df["unsubscribes"]
                    if has_revenue:
                        display["Revenue"] = df["revenue"].apply(
                            lambda x: f"${(x or 0):,.0f}")
                        display["Rev/Recip"] = df["revenue_per_recipient"].apply(
                            lambda x: f"${(x or 0):,.2f}")

                    from ui.tables import format_number_cols
                    display = format_number_cols(display, ["Sends", "Delivered", "Unsubs"])
                    st.caption('Automated email flow performance from Klaviyo API.')
                    render_html_table(display, max_height=500)
                else:
                    st.info("No flows synced yet.")

            with kl_tab3:
                if kl_lists:
                    list_df = pd.DataFrame(kl_lists)
                    display = pd.DataFrame()
                    display["List Name"] = list_df["name"]
                    display["Created"] = list_df["created"].apply(
                        lambda x: str(x)[:10] if x else "—")
                    display["Updated"] = list_df["updated"].apply(
                        lambda x: str(x)[:10] if x else "—")
                    st.caption('Email list sizes and dates from Klaviyo API.')
                    render_html_table(display, max_height=400)
                else:
                    st.info("No lists synced yet.")
