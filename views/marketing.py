"""Marketing page — Google Sheet analytics, pacing, DoD/WoW/MoM performance."""
import logging
from datetime import date
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from db import (
    get_db, read_sql,
    get_media_spend, get_amazon_revenue_forecast,
    get_last_sync_timestamp, get_new_rows_since_yesterday, get_synced_sources,
)
from analytics.dtc_demand import build_master_dtc_forecast
from analytics.metrics import (
    compute_mer, compute_nc_roas, compute_nc_cpa, compute_aov,
)
from analytics.retention import get_projected_new_repeat_summary
from ui.components import render_html_table, render_freshness_badge, smart_date_filter
from ui.pacing_helpers import build_pace_row, style_pace_df, render_white_table
from utils.constants import FORECAST_SKUS
from utils.date_helpers import business_today, business_yesterday


def _merge_shopify_daily(rev_df, cust_df):
    """Merge revenue and customer DataFrames and compute derived columns."""
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


@st.cache_data(ttl=86400)
def _load_shopify_daily_metrics():
    """Load daily DTC metrics from Shopify DB (revenue, orders, new/repeat customers).

    Uses MIN(order_date) from orders table for accurate first-order classification
    (matches Shopify admin's new vs returning definition).
    Tries precomputed data first, falls back to live DB queries.
    """
    import json as _json_mkt

    # Try precomputed data first
    try:
        from db import get_precomputed
        with get_db() as conn:
            rev_cached = get_precomputed(conn, 'shopify_daily_revenue', max_age_hours=25)
            cust_cached = get_precomputed(conn, 'shopify_daily_customers', max_age_hours=25)
        if rev_cached and cust_cached:
            rev_df = pd.DataFrame(_json_mkt.loads(rev_cached))
            cust_df = pd.DataFrame(_json_mkt.loads(cust_cached))
            if not rev_df.empty and not cust_df.empty:
                return _merge_shopify_daily(rev_df, cust_df)
    except Exception:
        pass

    # Fall back to live DB queries
    # Use orders.total_amount for revenue (matches Shopify's total_price
    # which includes shipping + tax) and exclude refunded/voided orders
    with get_db() as conn:
        rev_df = read_sql(
            "SELECT o.order_date AS sale_date, "
            "SUM(o.total_amount - COALESCE(o.total_tax, 0)) AS revenue, "
            "SUM(oi_agg.units) AS units "
            "FROM orders o "
            "LEFT JOIN ("
            "  SELECT order_id, SUM(quantity) AS units "
            "  FROM order_items GROUP BY order_id"
            ") oi_agg ON o.order_id = oi_agg.order_id "
            "WHERE o.source = %s "
            "  AND COALESCE(o.financial_status, 'paid') NOT IN ('refunded', 'voided') "
            "GROUP BY o.order_date ORDER BY sale_date",
            conn, params=('shopify',),
        )
        cust_df = read_sql(
            "SELECT o.order_date AS sale_date, "
            "COUNT(DISTINCT o.order_id) AS total_orders, "
            "COUNT(DISTINCT o.customer_id) AS total_customers, "
            "COUNT(DISTINCT CASE WHEN cf.actual_first = o.order_date "
            "THEN o.customer_id END) AS new_customers, "
            "SUM(oi.total_price) AS oi_total_rev, "
            "SUM(CASE WHEN cf.actual_first = o.order_date "
            "THEN oi.total_price ELSE 0 END) AS oi_new_rev "
            "FROM orders o "
            "JOIN (SELECT customer_id, MIN(order_date) AS actual_first "
            "      FROM orders WHERE source = %s "
            "        AND COALESCE(financial_status, 'paid') NOT IN ('refunded', 'voided') "
            "      GROUP BY customer_id) cf "
            "  ON o.customer_id = cf.customer_id "
            "JOIN order_items oi ON o.order_id = oi.order_id "
            "WHERE o.source = %s "
            "  AND COALESCE(o.financial_status, 'paid') NOT IN ('refunded', 'voided') "
            "GROUP BY o.order_date",
            conn, params=('shopify', 'shopify'),
        )
    if rev_df.empty:
        return pd.DataFrame()

    return _merge_shopify_daily(rev_df, cust_df)


@st.cache_data(ttl=600)
def _load_gs_spend():
    """Load ad spend and subscription data from Google Sheet."""
    log = logging.getLogger(__name__)

    def _clean_num(val):
        if pd.isna(val):
            return 0.0
        s = str(val).replace("$", "").replace(",", "").replace("%", "").strip()
        try:
            return float(s)
        except (ValueError, TypeError):
            return 0.0

    def _parse_spend_df(df):
        """Convert raw google_sheet_data DataFrame to cleaned spend DataFrame."""
        df['_date'] = pd.to_datetime(df['date'], format='mixed', dayfirst=False)
        df['_ad_spend'] = df['blended_ad_spend'].apply(_clean_num)
        sub_cols = []
        for sub_col in ['subscriptions', 'subscription_revenue', 'active_subscriptions',
                         'total_active_subscriptions', 'total_new_subscriptions',
                         'total_cancelled_subscriptions', 'total_subscription_order_revenue']:
            if sub_col in df.columns:
                df[f'_{sub_col}'] = df[sub_col].apply(_clean_num)
                sub_cols.append(f'_{sub_col}')
        return df[['_date', '_ad_spend'] + sub_cols].copy()

    # Path 1: precomputed data (fastest)
    try:
        import json as _json_pre
        from db import get_precomputed
        with get_db() as conn:
            cached = get_precomputed(conn, 'gs_spend_cleaned', max_age_hours=25)
        if cached:
            result = pd.DataFrame(_json_pre.loads(cached))
            if '_date' in result.columns and not result.empty:
                result['_date'] = pd.to_datetime(result['_date'], unit='ms', errors='coerce')
                result = result.dropna(subset=['_date'])
            if '_ad_spend' in result.columns and not result.empty:
                log.info('gs_spend loaded from precomputed cache (%d rows, spend sum=%.0f)',
                         len(result), result['_ad_spend'].sum())
                return result
            log.warning('gs_spend precomputed cache had no usable data')
    except Exception as exc:
        log.warning('gs_spend precomputed cache failed: %s', exc)

    # Path 2: live DB table
    try:
        with get_db() as conn:
            df = read_sql(
                "SELECT * FROM google_sheet_data ORDER BY id", conn
            )
            if not df.empty and 'date' in df.columns and 'blended_ad_spend' in df.columns:
                log.info('gs_spend loaded from DB table (%d rows)', len(df))
                return _parse_spend_df(df)
            elif not df.empty:
                log.warning('google_sheet_data missing columns: have %s', list(df.columns)[:5])
            else:
                log.warning('google_sheet_data table is empty')
    except Exception as exc:
        log.warning('gs_spend DB query failed: %s', exc)

    # Path 3: direct fetch from Google Sheets (fallback)
    try:
        from etl.google_sheets import fetch_daily_data_tab
        import re as _re_gs
        df = fetch_daily_data_tab()
        if not df.empty:
            df.columns = [
                _re_gs.sub(r'[^a-z0-9_]', '', c.strip().lower().replace(' ', '_').replace('-', '_'))
                for c in df.columns
            ]
            if 'date' in df.columns and 'blended_ad_spend' in df.columns:
                log.info('gs_spend loaded via direct sheet fetch (%d rows)', len(df))
                return _parse_spend_df(df)
    except Exception as exc:
        log.warning('gs_spend direct sheet fetch failed: %s', exc)

    return pd.DataFrame()


@st.cache_data(ttl=600)
def _load_amazon_daily():
    """Load Amazon daily data with L7D projection for incomplete/missing recent days.

    Returns a DataFrame with columns: sale_date, _date, _amz_revenue, _amz_spend,
    _amz_new_cust, _amz_repeat_cust, _amz_new_rev, _amz_repeat_rev.
    Shared by both the pacing hero tiles and the performance tables.
    """
    log = logging.getLogger(__name__)
    try:
        with get_db() as conn:
            rev_daily = read_sql(
                "SELECT sale_date, SUM(revenue) as _amz_revenue "
                "FROM daily_sku_sales WHERE source = 'amazon' "
                "GROUP BY sale_date ORDER BY sale_date",
                conn,
            )
            spend_daily = read_sql(
                "SELECT date as sale_date, spend as _amz_spend "
                "FROM amazon_daily_rollup ORDER BY date",
                conn,
            )
            cust_daily = read_sql(
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
                conn,
                params=('amazon',),
            )
        if rev_daily.empty:
            return pd.DataFrame()

        df = rev_daily.copy()
        df["_date"] = pd.to_datetime(df["sale_date"])
        if not spend_daily.empty:
            try:
                from analytics.metrics import project_daily_spend_gaps
                spend_daily = project_daily_spend_gaps(
                    spend_daily, date_col='sale_date', spend_col='_amz_spend')
            except Exception:
                pass
            df = df.merge(
                spend_daily[['sale_date', '_amz_spend']], on="sale_date", how="left")
        if "_amz_spend" not in df.columns:
            df["_amz_spend"] = 0
        df["_amz_spend"] = df["_amz_spend"].fillna(0)

        if not cust_daily.empty:
            cust_daily["sale_date"] = cust_daily["sale_date"].astype(str)
            df = df.merge(
                cust_daily[["sale_date", "total_customers", "new_customers",
                            "oi_total_rev", "oi_new_rev"]],
                on="sale_date", how="left",
            )
        for col in ["total_customers", "new_customers", "oi_total_rev", "oi_new_rev"]:
            if col not in df.columns:
                df[col] = 0
            df[col] = df[col].fillna(0)

        df["_amz_new_cust"] = df["new_customers"].astype(int)
        df["_amz_repeat_cust"] = (df["total_customers"] - df["new_customers"]).clip(lower=0).astype(int)

        oi_total = df["oi_total_rev"]
        new_frac = (df["oi_new_rev"] / oi_total).where(oi_total > 0, 0)
        df["_amz_new_rev"] = df["_amz_revenue"] * new_frac
        df["_amz_repeat_rev"] = df["_amz_revenue"] * (1 - new_frac)
        # Exclude today
        df = df[df['_date'].dt.date < date.today()]

        # Project incomplete/missing recent Amazon days using L7D stable window
        if len(df) >= 10 and "_amz_new_cust" in df.columns:
            sorted_df = df.sort_values("_date")
            stable = sorted_df.iloc[-10:-3]
            if not stable.empty:
                avg = {
                    "_amz_new_cust": stable["_amz_new_cust"].mean(),
                    "_amz_repeat_cust": stable["_amz_repeat_cust"].mean(),
                    "_amz_new_rev": stable["_amz_new_rev"].mean(),
                    "_amz_repeat_rev": stable["_amz_repeat_rev"].mean(),
                    "_amz_revenue": stable["_amz_revenue"].mean(),
                    "_amz_spend": stable["_amz_spend"].mean(),
                }
                threshold = 0.4

                # 1. Backfill existing rows with incomplete data
                for idx in sorted_df.index[-3:]:
                    row_nc = sorted_df.at[idx, "_amz_new_cust"]
                    if avg["_amz_new_cust"] > 0 and row_nc < avg["_amz_new_cust"] * threshold:
                        for k, v in avg.items():
                            df.at[idx, k] = round(v) if 'cust' in k else round(v, 2)

                # 2. Create projected rows for missing recent days
                from datetime import timedelta
                yesterday = date.today() - timedelta(days=1)
                max_date = sorted_df["_date"].max().date()
                gap_rows = []
                cur = max_date + timedelta(days=1)
                while cur <= yesterday:
                    gap_rows.append({
                        "sale_date": str(cur),
                        "_date": pd.Timestamp(cur),
                        "_amz_revenue": round(avg["_amz_revenue"], 2),
                        "_amz_spend": round(avg["_amz_spend"], 2),
                        "_amz_new_cust": round(avg["_amz_new_cust"]),
                        "_amz_repeat_cust": round(avg["_amz_repeat_cust"]),
                        "_amz_new_rev": round(avg["_amz_new_rev"], 2),
                        "_amz_repeat_rev": round(avg["_amz_repeat_rev"], 2),
                        "_amz_projected": True,
                    })
                    cur += timedelta(days=1)
                if gap_rows:
                    log.info(
                        'Amazon NC projection: adding %d gap rows (max_date=%s, yesterday=%s, avg_nc=%.1f)',
                        len(gap_rows), max_date, yesterday, avg["_amz_new_cust"])
                    df = pd.concat(
                        [df, pd.DataFrame(gap_rows)],
                        ignore_index=True,
                    )
        if '_amz_projected' not in df.columns:
            df['_amz_projected'] = False
        df['_amz_projected'] = df['_amz_projected'].fillna(False)
        return df
    except Exception as exc:
        log.warning('Amazon daily build failed: %s', exc, exc_info=True)
        return pd.DataFrame()


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
        _badge = ctx['cached_freshness_badge']('amazon,shopify')
        _src_label = ' + '.join(s.title() for s in sorted(_badge['srcs'])) if _badge['srcs'] else None
        render_freshness_badge(last_refreshed_str=_badge['ts'], new_rows=_badge['new'], source=_src_label)

    # Load Shopify DB metrics (revenue, orders, new/repeat customers)
    _shopify_daily = _load_shopify_daily_metrics()
    _gs_spend = _load_gs_spend()
    _amz_daily = _load_amazon_daily()
    _has_shopify_data = not _shopify_daily.empty

    # Merge: Shopify DB for revenue/customers, Google Sheet for spend/sessions
    if _has_shopify_data:
        mkt_df = _shopify_daily.copy()
        mkt_df['_date'] = pd.to_datetime(mkt_df['_date']).dt.normalize()
        if not _gs_spend.empty:
            _gs = _gs_spend.copy()
            _gs['_date'] = pd.to_datetime(_gs['_date']).dt.normalize()
            mkt_df = mkt_df.merge(
                _gs, on='_date', how='left', suffixes=('', '_gs')
            )
        else:
            mkt_df['_ad_spend'] = 0
        mkt_df = mkt_df.sort_values('_date')
        mkt_df['_ad_spend'] = mkt_df.get('_ad_spend', pd.Series(0, index=mkt_df.index)).fillna(0)

        # Gap-fill DTC spend for hero metrics (same logic as perf tables)
        # Exclude yesterday — only fill intermediate gaps, not the most recent day
        _hero_has_spend = mkt_df[mkt_df['_ad_spend'] > 0]
        _hero_yesterday = pd.Timestamp(business_yesterday())
        if not _hero_has_spend.empty:
            _hero_l7d = _hero_has_spend.tail(7)['_ad_spend'].mean()
            _hero_last = _hero_has_spend['_date'].max()
            _hero_gap = (mkt_df['_ad_spend'] == 0) & (mkt_df['_date'] > _hero_last) & (mkt_df['_date'] < _hero_yesterday)
            if _hero_gap.any():
                mkt_df.loc[_hero_gap, '_ad_spend'] = round(_hero_l7d, 2)

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
            _mkt_media = []

            # Try precomputed master forecast first
            try:
                from db import get_precomputed
                import json as _json_mkt_pre
                with get_db() as _pc_mkt_conn:
                    _fc_mkt_cached = get_precomputed(_pc_mkt_conn, 'master_dtc_forecast', max_age_hours=25)
                if _fc_mkt_cached:
                    _precomputed_mkt = _json_mkt_pre.loads(_fc_mkt_cached)
                    if 'summary' in _precomputed_mkt:
                        _mkt_summary = pd.DataFrame(_precomputed_mkt['summary'])
                    _mkt_revenue = _precomputed_mkt.get('revenue', {})
            except Exception:
                pass

            # Always load media spend (needed for spend goals)
            try:
                with get_db() as _goals_conn:
                    _mkt_media = get_media_spend(_goals_conn, source="All Sources")
                    _mkt_amz_media = get_media_spend(_goals_conn, source="Amazon")
            except Exception:
                pass

            # Fall back to live computation if precomputed miss
            if _mkt_summary is None:
                try:
                    _seasonal_json_mkt = _load_seasonal_json()
                    with get_db() as _goals_conn2:
                        _amz_rev_f = get_amazon_revenue_forecast(_goals_conn2)
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

            # Amazon MTD: from shared _amz_daily (includes L7D projections)
            _cm_amz_rev = 0
            _cm_amz_spend = 0
            _cm_amz_nc = 0
            _cm_amz_nc_rev = 0
            if not _amz_daily.empty:
                _amz_cm = _amz_daily[
                    (_amz_daily['_date'].dt.strftime('%Y-%m') == _cur_month)
                    & (_amz_daily['_date'].dt.date <= _yesterday)
                ]
                _cm_amz_rev = _amz_cm['_amz_revenue'].sum()
                _cm_amz_spend = _amz_cm['_amz_spend'].sum()
                _cm_amz_nc = int(_amz_cm['_amz_new_cust'].sum())
                _cm_amz_nc_rev = _amz_cm['_amz_new_rev'].sum()

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

            # Amazon L7D from shared _amz_daily (divided by 7 for daily avg)
            _l7d_amz_rev = 0
            _l7d_amz_spend = 0
            _l7d_amz_nc = 0
            _l7d_amz_nc_rev = 0
            if not _amz_daily.empty:
                _amz_l7d = _amz_daily[
                    (_amz_daily['_date'].dt.date >= _l7d_start)
                    & (_amz_daily['_date'].dt.date <= _yesterday)
                ]
                if not _amz_l7d.empty:
                    _l7d_amz_rev = _amz_l7d['_amz_revenue'].sum() / 7
                    _l7d_amz_spend = _amz_l7d['_amz_spend'].sum() / 7
                    _l7d_amz_nc = _amz_l7d['_amz_new_cust'].sum() / 7
                    _l7d_amz_nc_rev = _amz_l7d['_amz_new_rev'].sum() / 7

            # Yesterday actuals from Google Sheet
            _yd_mask = mkt_df["_date"].dt.strftime("%Y-%m-%d") == _yesterday_str
            _yd_rev = mkt_df.loc[_yd_mask, "_revenue"].sum()
            _yd_nc_rev = mkt_df.loc[_yd_mask, "_nc_revenue"].sum()
            _yd_ret_rev = mkt_df.loc[_yd_mask, "_ret_revenue"].sum()
            _yd_spend = mkt_df.loc[_yd_mask, "_ad_spend"].sum()
            _yd_nc = int(mkt_df.loc[_yd_mask, "_nc_orders"].sum())

            # Amazon yesterday from shared _amz_daily (actuals only, no projections)
            _yd_amz_rev = 0
            _yd_amz_spend = 0
            _yd_amz_nc = 0
            _yd_amz_nc_rev = 0
            if not _amz_daily.empty:
                _amz_yd = _amz_daily[
                    (_amz_daily['_date'].dt.strftime('%Y-%m-%d') == _yesterday_str)
                    & (~_amz_daily.get('_amz_projected', pd.Series(False, index=_amz_daily.index)).astype(bool))
                ]
                if not _amz_yd.empty:
                    _yd_amz_rev = _amz_yd['_amz_revenue'].sum()
                    _yd_amz_spend = _amz_yd['_amz_spend'].sum()
                    _yd_amz_nc = int(_amz_yd['_amz_new_cust'].sum())
                    _yd_amz_nc_rev = _amz_yd['_amz_new_rev'].sum()

            _remaining_days = _days_in_month - _day_of_month
            _total_actual_rev = _cm_nc_rev + _cm_ret_rev + _cm_amz_rev

            # Channel from dashboard router context
            _nav_channel = ctx.get('channel', 'Rollup')

            if _has_goals:
                # -- DTC actuals (Google + Meta spend, Shopify revenue) --
                _goal_dtc_rev = _goal_nc_rev + _goal_repeat_rev
                _cm_dtc_rev = _cm_nc_rev + _cm_ret_rev
                _l7d_dtc_rev = _l7d_nc_rev + _l7d_ret_rev
                _yd_dtc_rev = _yd_nc_rev + _yd_ret_rev
                _cm_dtc_spend = _cm_spend
                _l7d_dtc_spend = _l7d_spend
                _yd_dtc_spend = _yd_spend

                # -- Amazon data: spend from amazon_daily_rollup, NC from orders/order_items --
                # _cm_amz_spend, _cm_amz_nc, _cm_amz_nc_rev set above from DB
                # _l7d_amz_spend, _l7d_amz_nc, _l7d_amz_nc_rev set above from DB
                # _yd_amz_spend, _yd_amz_nc, _yd_amz_nc_rev set above from DB

                # -- Combined Roll Up totals (DTC + Amazon) --
                _cm_total_spend = _cm_dtc_spend + _cm_amz_spend
                _l7d_total_spend = _l7d_dtc_spend + _l7d_amz_spend
                _yd_total_spend = _yd_dtc_spend + _yd_amz_spend
                _cm_total_repeat_rev = _cm_ret_rev  # Amazon repeat not tracked in this source

                # Efficiency metrics (centralized formulas)
                _business_mer = compute_mer(_total_actual_rev, _cm_total_spend)

                # DTC efficiency (NC metrics from Shopify orders DB only)
                _dtc_nc_roas = compute_nc_roas(_cm_nc_rev, _cm_dtc_spend)
                _dtc_nc_aov = compute_aov(_cm_nc_rev, _cm_nc)
                _dtc_cpa = compute_nc_cpa(_cm_dtc_spend, _cm_nc)

                # Combined NC metrics (DTC + Amazon)
                _cm_total_nc = _cm_nc + _cm_amz_nc
                _cm_total_nc_rev = _cm_nc_rev + _cm_amz_nc_rev
                _total_nc_roas = compute_nc_roas(_cm_total_nc_rev, _cm_total_spend)
                _blended_cpa = compute_nc_cpa(_cm_total_spend, _cm_total_nc)

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
                    eff2.metric("Total NC ROAS", f"{_total_nc_roas:.2f}x",
                                help="Total NC Revenue / Total Spend")
                    eff3.metric("New Customers", f"{_cm_total_nc:,}",
                                help="DTC + Amazon new customers MTD")
                    eff4.metric("Blended CPA", f"${_blended_cpa:,.0f}",
                                help="Total Spend / Total New Customers")

                    _rollup_rows = []
                    _goal_total_spend = _goal_spend + _goal_amz_spend
                    if _goal_total_spend > 0:
                        _rollup_rows.append(build_pace_row("Total Spend", _cm_total_spend, _goal_total_spend,
                                                           _l7d_total_spend, _yd_total_spend,
                                                           _pct_month, _days_in_month, _remaining_days, is_spend=True))
                    _rollup_rows.extend([
                        build_pace_row("Total Revenue", _total_actual_rev, _goal_total_rev,
                                       _l7d_rev + _l7d_amz_rev, _yd_rev + _yd_amz_rev,
                                       _pct_month, _days_in_month, _remaining_days),
                        build_pace_row("Repeat Revenue", _cm_total_repeat_rev, _goal_repeat_rev,
                                       _l7d_ret_rev, _yd_ret_rev,
                                       _pct_month, _days_in_month, _remaining_days),
                    ])
                    _rollup_df = pd.DataFrame(_rollup_rows)
                    render_white_table(style_pace_df(_rollup_df))

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
                        _dtc_rows.append(build_pace_row("Spend", _cm_dtc_spend, _goal_dtc_spend,
                                                        _l7d_dtc_spend, _yd_dtc_spend,
                                                        _pct_month, _days_in_month, _remaining_days, is_spend=True))
                    _dtc_rows.extend([
                        build_pace_row("Revenue", _cm_dtc_rev, _goal_dtc_rev, _l7d_dtc_rev, _yd_dtc_rev,
                                       _pct_month, _days_in_month, _remaining_days),
                        build_pace_row("New Customer Rev", _cm_nc_rev, _goal_nc_rev, _l7d_nc_rev, _yd_nc_rev,
                                       _pct_month, _days_in_month, _remaining_days),
                        build_pace_row("Repeat Customer Rev", _cm_ret_rev, _goal_repeat_rev, _l7d_ret_rev, _yd_ret_rev,
                                       _pct_month, _days_in_month, _remaining_days),
                    ])
                    _dtc_df = pd.DataFrame(_dtc_rows)
                    render_white_table(style_pace_df(_dtc_df))

                # ============================================================
                # AMAZON — revenue from daily_sku_sales, spend from amazon_daily_rollup, NC from orders
                # ============================================================
                if _nav_channel in ('Rollup', 'Amazon'):
                    if _nav_channel == 'Rollup':
                        st.markdown("---")
                    st.subheader("Amazon")
                    st.caption('Amazon-only pacing from daily_sku_sales + orders/order_items + amazon_daily_rollup.')

                    # Amazon KPIs (above table)
                    _amz_nc_roas = _cm_amz_nc_rev / _cm_amz_spend if _cm_amz_spend > 0 else 0
                    amz_k1, amz_k2, amz_k3, amz_k4 = st.columns(4)
                    amz_k1.metric("Revenue MTD", f"${_cm_amz_rev:,.0f}")
                    amz_k2.metric("Spend MTD", f"${_cm_amz_spend:,.0f}")
                    amz_k3.metric("New Customers", f"{_cm_amz_nc:,}")
                    amz_k4.metric("NC ROAS", f"{_amz_nc_roas:.2f}x",
                                  help="Amazon NC Revenue / Amazon Spend")

                    _amz_rows = []
                    if _goal_amz_spend > 0:
                        _amz_rows.append(
                            build_pace_row("Spend", _cm_amz_spend, _goal_amz_spend, _l7d_amz_spend, _yd_amz_spend,
                                           _pct_month, _days_in_month, _remaining_days, is_spend=True),
                        )
                    _amz_rows.append(
                        build_pace_row("Revenue", _cm_amz_rev, _goal_amz_rev, _l7d_amz_rev, _yd_amz_rev,
                                       _pct_month, _days_in_month, _remaining_days),
                    )
                    _amz_df = pd.DataFrame(_amz_rows)
                    render_white_table(style_pace_df(_amz_df))

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

                    dtc_total_cust = int(r.get("total_customers", nc_orders))
                    dtc_repeat_cust = max(0, dtc_total_cust - int(nc_orders))

                    combined_rev = total_rev + amz_rev_val
                    combined_spend = spend + amz_spend_val
                    combined_new_cust = int(nc_orders) + amz_new_cust
                    combined_repeat_cust = dtc_repeat_cust + amz_repeat_cust
                    combined_new_rev = nc_rev + amz_new_rev_val
                    combined_total_cust = combined_new_cust + combined_repeat_cust
                    nc_mer = combined_new_rev / combined_spend if combined_spend > 0 else 0
                    mer = combined_rev / combined_spend if combined_spend > 0 else 0
                    combined_nc_cpa = combined_spend / combined_new_cust if combined_new_cust > 0 else 0

                    rows_rollup.append({
                        period_col: period_label,
                        "Spend": f"${combined_spend:,.0f}",
                        "Revenue": f"${combined_rev:,.0f}",
                        "New Cust": combined_new_cust,
                        "Repeat Cust": combined_repeat_cust,
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

            from ui.perf_tables import render_perf_table_colored as _render_perf_table_colored

            # --- Aggregate helpers ---
            # We use the full (unfiltered by date range) dataset for these tabs
            # Shopify DB for revenue/customers, Google Sheet for spend/sessions
            _perf_df = _shopify_daily.copy()
            _perf_df['_date'] = pd.to_datetime(_perf_df['_date']).dt.normalize()
            if not _gs_spend.empty:
                _gs_perf = _gs_spend.copy()
                _gs_perf['_date'] = pd.to_datetime(_gs_perf['_date']).dt.normalize()
                _perf_df = _perf_df.merge(
                    _gs_perf, on='_date', how='left', suffixes=('', '_gs')
                )
            else:
                _perf_df['_ad_spend'] = 0
            _perf_df = _perf_df.sort_values('_date')
            _perf_df['_ad_spend'] = _perf_df['_ad_spend'].fillna(0)

            # Gap-fill DTC spend: Google Sheet often lags a few days — estimate
            # missing days using L7D average so the daily table isn't $0.
            # Exclude yesterday — don't show projected spend as actual.
            _has_dtc_spend = _perf_df[_perf_df['_ad_spend'] > 0]
            _perf_yesterday = pd.Timestamp(business_yesterday())
            if not _has_dtc_spend.empty:
                _dtc_l7d_spend = _has_dtc_spend.tail(7)['_ad_spend'].mean()
                _dtc_last_spend_date = _has_dtc_spend['_date'].max()
                _gap_mask = ((_perf_df['_ad_spend'] == 0)
                             & (_perf_df['_date'] > _dtc_last_spend_date)
                             & (_perf_df['_date'] < _perf_yesterday))
                if _gap_mask.any():
                    _perf_df.loc[_gap_mask, '_ad_spend'] = round(_dtc_l7d_spend, 2)

            # Ensure total_customers column exists for repeat-cust rollup
            if 'total_customers' not in _perf_df.columns:
                _perf_df['total_customers'] = _perf_df.get('_nc_orders', 0)

            # Exclude today — always start with yesterday
            _perf_df = _perf_df[_perf_df['_date'].dt.date < date.today()]

            # _amz_daily is pre-loaded by _load_amazon_daily() (shared with pacing hero tiles)

            # --- DAY OVER DAY ---
            with _perf_tab_dod:
                _dod_df = _perf_df.copy()
                _dod_df["Day"] = _dod_df["_date"].dt.strftime("%Y-%m-%d")
                _dod_agg_cols = {"_ad_spend": "sum", "_revenue": "sum", "_nc_orders": "sum",
                                 "_nc_revenue": "sum", "_ret_revenue": "sum",
                                 "_orders": "sum", "_units": "sum",
                                 "total_customers": "sum"}
                _dod_agg = _dod_df.groupby("Day", sort=True).agg(_dod_agg_cols).reset_index()

                # Amazon daily (exclude projected rows from performance tables)
                _amz_dod = pd.DataFrame(columns=["Day", "_amz_revenue", "_amz_spend",
                                                  "_amz_new_cust", "_amz_repeat_cust",
                                                  "_amz_new_rev", "_amz_repeat_rev"])
                if not _amz_daily.empty:
                    _amz_dod = _amz_daily[~_amz_daily['_amz_projected'].astype(bool)].copy()
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
                    total_customers=("total_customers", "sum"),
                    **{"Wk #": ("Wk #", "first")},
                ).reset_index()

                # Amazon weekly
                _amz_wow = pd.DataFrame(columns=["Week", "_amz_revenue", "_amz_spend",
                                                  "_amz_new_cust", "_amz_repeat_cust",
                                                  "_amz_new_rev", "_amz_repeat_rev"])
                if not _amz_daily.empty:
                    _amz_wow_tmp = _amz_daily[~_amz_daily['_amz_projected'].astype(bool)].copy()
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
                    total_customers=("total_customers", "sum"),
                ).reset_index()

                # Amazon monthly
                _amz_mom = pd.DataFrame(columns=["Month", "_amz_revenue", "_amz_spend",
                                                  "_amz_new_cust", "_amz_repeat_cust",
                                                  "_amz_new_rev", "_amz_repeat_rev"])
                if not _amz_daily.empty:
                    _amz_mom_tmp = _amz_daily[~_amz_daily['_amz_projected'].astype(bool)].copy()
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

