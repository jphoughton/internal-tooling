"""Overview page — compacted layout with hero pacing, trend tables, and key charts."""
import logging
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta
from db import get_db, read_sql, get_media_spend, get_cashflow_setting
from analytics.sku_flavors import get_flavor
from analytics.metrics import compute_cac_payback, compute_nc_roas, compute_nc_cpa, project_daily_spend_gaps
from analytics.amazon_nc_projector import project_amazon_nc
from ui.components import render_freshness_badge, render_html_table
from ui.perf_tables import render_perf_table_colored, render_perf_table_transposed, build_overview_trend_rows
from views.pacing import compute_pacing_data, render_hero_bars, render_pacing_detail_table
from utils.date_helpers import business_yesterday
from views.marketing import _load_shopify_daily_metrics, _load_gs_spend, _load_amazon_daily as _load_amazon_daily_mkt

log = logging.getLogger(__name__)


@st.cache_data(ttl=86400)
def _get_nc_projection():
    """Cached wrapper around project_amazon_nc() for yesterday."""
    try:
        return project_amazon_nc()
    except Exception as e:
        log.warning('NC projection failed: %s', e)
        return None


@st.cache_data(ttl=86400)
def _get_yesterday_rollup():
    """Get yesterday's combined DTC+Amazon metrics using the same data sources as Marketing rollup.

    Returns dict with keys: rev, spend, nc, nc_rev (all combined DTC+Amazon),
    plus dtc_* and amz_* breakdowns. Returns None if data unavailable.
    """
    yesterday = date.today() - timedelta(days=1)
    yd_str = yesterday.isoformat()
    result = {'dtc_rev': 0, 'dtc_spend': 0, 'dtc_nc': 0, 'dtc_nc_rev': 0,
              'amz_rev': 0, 'amz_spend': 0, 'amz_nc': 0, 'amz_nc_rev': 0}

    # DTC from Shopify daily metrics
    try:
        dtc = _load_shopify_daily_metrics()
        if not dtc.empty:
            dtc['_date'] = pd.to_datetime(dtc['_date']).dt.normalize()
            yd_dtc = dtc[dtc['_date'].dt.date == yesterday]
            if not yd_dtc.empty:
                result['dtc_rev'] = float(yd_dtc['_revenue'].sum())
                result['dtc_nc'] = int(yd_dtc['_nc_orders'].sum())
                result['dtc_nc_rev'] = float(yd_dtc['_nc_revenue'].sum())

        # DTC spend from Google Sheets
        gs = _load_gs_spend()
        if not gs.empty:
            gs['_date'] = pd.to_datetime(gs['_date']).dt.normalize()
            yd_gs = gs[gs['_date'].dt.date == yesterday]
            if not yd_gs.empty:
                result['dtc_spend'] = float(yd_gs['_ad_spend'].sum())
            elif not gs.empty:
                # Gap-fill: use L7D average if yesterday's spend not yet in sheet
                result['dtc_spend'] = float(gs.tail(7)['_ad_spend'].mean())
    except Exception as e:
        log.warning('_get_yesterday_rollup DTC failed: %s', e)

    # Amazon from marketing's _load_amazon_daily (has proper L7D gap-fill for spend)
    try:
        amz = _load_amazon_daily_mkt()
        if not amz.empty:
            amz_yd = amz[amz['sale_date'] == yd_str]
            if not amz_yd.empty:
                result['amz_rev'] = float(amz_yd['_amz_revenue'].sum())
                result['amz_spend'] = float(amz_yd['_amz_spend'].sum())
                result['amz_nc'] = int(amz_yd['_amz_new_cust'].sum())
                result['amz_nc_rev'] = float(amz_yd['_amz_new_rev'].sum())
            else:
                # Amazon data missing for yesterday — project from stable window
                if len(amz) >= 10 and '_amz_new_cust' in amz.columns:
                    amz_sorted = amz.sort_values('sale_date')
                    stable = amz_sorted.iloc[-10:-3]
                    if not stable.empty:
                        result['amz_rev'] = float(stable['_amz_revenue'].mean())
                        result['amz_spend'] = float(stable['_amz_spend'].mean())
                        result['amz_nc'] = int(round(stable['_amz_new_cust'].mean()))
                        result['amz_nc_rev'] = float(stable['_amz_new_rev'].mean())
                        result['amz_projected'] = True
    except Exception as e:
        log.warning('_get_yesterday_rollup Amazon failed: %s', e)

    result['rev'] = result['dtc_rev'] + result['amz_rev']
    result['spend'] = result['dtc_spend'] + result['amz_spend']
    result['nc'] = result['dtc_nc'] + result['amz_nc']
    result['nc_rev'] = result['dtc_nc_rev'] + result['amz_nc_rev']
    return result


@st.cache_data(ttl=86400)
def _load_amazon_daily():
    """Load Amazon daily data for trend tables (revenue, spend, new/repeat)."""
    # Try precomputed data first
    try:
        import json as _json_pre
        from db import get_precomputed
        with get_db() as conn:
            cached = get_precomputed(conn, 'amazon_daily_merged', max_age_hours=25)
        if cached:
            return pd.DataFrame(_json_pre.loads(cached))
    except Exception:
        pass

    try:
        with get_db() as conn:
            rev = read_sql(
                "SELECT sale_date, SUM(revenue) as _amz_revenue "
                "FROM daily_sku_sales WHERE source = 'amazon' "
                "GROUP BY sale_date ORDER BY sale_date", conn,
            )
        if rev.empty:
            log.warning('_load_amazon_daily: no Amazon revenue in daily_sku_sales')
            return pd.DataFrame()
        df = rev

        # Spend — query separately so a missing table doesn't kill everything
        try:
            with get_db() as conn:
                spend = read_sql(
                    "SELECT date as sale_date, spend as _amz_spend "
                    "FROM amazon_daily_rollup ORDER BY date", conn,
                )
            if not spend.empty:
                spend = project_daily_spend_gaps(spend, date_col='sale_date', spend_col='_amz_spend')
                df = df.merge(spend[['sale_date', '_amz_spend']], on='sale_date', how='left')
        except Exception as e:
            log.warning('_load_amazon_daily: spend query failed: %s', e)
        if '_amz_spend' not in df.columns:
            df['_amz_spend'] = 0
        df['_amz_spend'] = df['_amz_spend'].fillna(0)

        # Customer data — query separately
        try:
            with get_db() as conn:
                cust = read_sql(
                    "SELECT DATE(o.order_date) AS sale_date, "
                    "COUNT(DISTINCT o.customer_id) AS total_customers, "
                    "COUNT(DISTINCT CASE WHEN fc.first_date = DATE(o.order_date) "
                    "THEN o.customer_id END) AS new_customers, "
                    "SUM(oi.total_price) AS oi_total_rev, "
                    "SUM(CASE WHEN fc.first_date = DATE(o.order_date) "
                    "THEN oi.total_price ELSE 0 END) AS oi_new_rev "
                    "FROM orders o "
                    "JOIN (SELECT customer_id, DATE(MIN(order_date)) AS first_date "
                    "      FROM orders WHERE source = %s "
                    "      GROUP BY customer_id) fc "
                    "ON o.customer_id = fc.customer_id "
                    "JOIN order_items oi ON o.order_id = oi.order_id "
                    "WHERE o.source = %s "
                    "GROUP BY DATE(o.order_date)", conn, params=('amazon', 'amazon'),
                )
            if not cust.empty:
                cust['sale_date'] = cust['sale_date'].astype(str)
                df = df.merge(
                    cust[['sale_date', 'total_customers', 'new_customers', 'oi_total_rev', 'oi_new_rev']],
                    on='sale_date', how='left',
                )
        except Exception as e:
            log.warning('_load_amazon_daily: customer query failed: %s', e)

        for col in ['total_customers', 'new_customers', 'oi_total_rev', 'oi_new_rev']:
            if col not in df.columns:
                df[col] = 0
            df[col] = df[col].fillna(0)

        df['_amz_new_cust'] = df['new_customers'].astype(int)
        df['_amz_repeat_cust'] = (df['total_customers'] - df['new_customers']).clip(lower=0).astype(int)
        _oi_total = df['oi_total_rev']
        _new_frac = (df['oi_new_rev'] / _oi_total).where(_oi_total > 0, 0)
        df['_amz_new_rev'] = df['_amz_revenue'] * _new_frac
        df['_amz_repeat_rev'] = df['_amz_revenue'] * (1 - _new_frac)
        df['_projected'] = False
        df['_date'] = pd.to_datetime(df['sale_date'])

        # Inject NC projection for yesterday if actual NC data is missing
        yesterday_str = (date.today() - timedelta(days=1)).isoformat()
        yd_rows = df[df['sale_date'] == yesterday_str]
        yd_nc = int(yd_rows['new_customers'].sum()) if not yd_rows.empty else 0
        if yd_nc == 0:
            proj = _get_nc_projection()
            if proj and proj.get('actual_nc_customers') is None and proj.get('projected_nc_customers', 0) > 0:
                proj_nc = proj['projected_nc_customers']
                proj_rev = proj['projected_nc_revenue']
                if not yd_rows.empty:
                    idx = yd_rows.index[0]
                    yd_revenue = df.loc[idx, '_amz_revenue']
                    df.loc[idx, 'new_customers'] = proj_nc
                    df.loc[idx, '_amz_new_cust'] = int(round(proj_nc))
                    df.loc[idx, '_amz_new_rev'] = proj_rev
                    df.loc[idx, '_amz_repeat_rev'] = max(yd_revenue - proj_rev, 0)
                    df.loc[idx, '_projected'] = True

        return df
    except Exception as e:
        log.warning('_load_amazon_daily failed: %s', e)
        return pd.DataFrame()


@st.cache_data(ttl=86400)
def _load_overview_daily_trend(source_filter=None):
    """Load 90-day daily revenue trend by source for the stacked area chart."""
    # Try precomputed data first
    try:
        import json as _json_pre
        from db import get_precomputed
        _key = f'overview_daily_trend_{source_filter or "rollup"}'
        with get_db() as conn:
            cached = get_precomputed(conn, _key, max_age_hours=25)
        if cached:
            return pd.DataFrame(_json_pre.loads(cached))
    except Exception:
        pass

    with get_db() as conn:
        if source_filter:
            return read_sql(
                "SELECT sale_date, source, SUM(units_sold) as units, SUM(revenue) as revenue "
                "FROM daily_sku_sales "
                "WHERE sale_date >= date('now', '-90 days') AND source = %s "
                "GROUP BY sale_date, source ORDER BY sale_date", conn, params=(source_filter,),
            )
        return read_sql(
            "SELECT sale_date, source, SUM(units_sold) as units, SUM(revenue) as revenue "
            "FROM daily_sku_sales "
            "WHERE sale_date >= date('now', '-90 days') "
            "GROUP BY sale_date, source ORDER BY sale_date", conn,
        )


@st.cache_data(ttl=86400)
def _load_top_skus(source_filter=None):
    """Load top 10 flavors by units sold, optionally filtered by source."""
    # Try precomputed data first
    try:
        import json as _json_pre
        from db import get_precomputed
        _key = f'top_skus_{source_filter or "rollup"}'
        with get_db() as conn:
            cached = get_precomputed(conn, _key, max_age_hours=25)
        if cached:
            return pd.DataFrame(_json_pre.loads(cached))
    except Exception:
        pass

    with get_db() as conn:
        if source_filter:
            df = read_sql(
                "SELECT sku, SUM(units_sold) as total_units "
                "FROM daily_sku_sales WHERE source = %s "
                "GROUP BY sku ORDER BY total_units DESC",
                conn, params=(source_filter,),
            )
        else:
            df = read_sql(
                "SELECT sku, SUM(units_sold) as total_units "
                "FROM daily_sku_sales "
                "GROUP BY sku ORDER BY total_units DESC",
                conn,
            )
    if df.empty:
        return df
    # Map each SKU to its unified flavor name
    df['flavor'] = df['sku'].apply(lambda s: get_flavor(s) or s)
    # Group by flavor to combine same-flavor SKUs across channels/formats
    grouped = df.groupby('flavor', as_index=False)['total_units'].sum()
    grouped = grouped.sort_values('total_units', ascending=False).head(10)
    return grouped


def render(ctx):
    """Render the Overview page."""
    _channel = ctx.get('channel', 'Rollup')
    _source_filter = ctx.get('source_filter')  # 'shopify', 'amazon', or None (Rollup)

    # ================================================================
    # Header + freshness badge
    # ================================================================
    _badge_sources = ['shopify', 'amazon'] if not _source_filter else [_source_filter]
    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title('Overview')
    with _badge_col:
        _badge = ctx['cached_freshness_badge'](','.join(sorted(_badge_sources)))
        _src_label = ' + '.join(s.title() for s in sorted(_badge['srcs'])) if _badge['srcs'] else None
        render_freshness_badge(last_refreshed_str=_badge['ts'], new_rows=_badge['new'], source=_src_label)

    # Empty state check (cached — only changes after sync)
    @st.cache_data(ttl=86400, show_spinner=False)
    def _check_empty_state():
        with get_db() as conn:
            last_sync = conn.execute(
                'SELECT source, sync_date, records_fetched, status, error_message, created_at '
                'FROM sync_log ORDER BY created_at DESC LIMIT 1'
            ).fetchone()
            total_orders_check = conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
        return {'last_sync': dict(last_sync) if last_sync else None, 'total_orders': total_orders_check}

    _empty = _check_empty_state()
    last_sync = _empty['last_sync']
    total_orders_check = _empty['total_orders']

    if total_orders_check == 0:
        st.warning('No sales data yet. Click **Refresh Data** in the sidebar, or connect a store in **Settings**.')
        if last_sync and last_sync['status'] != 'success':
            st.error(f"Last sync failed ({last_sync['source'].title()}): {last_sync['error_message'] or 'Unknown error'}")
        return

    # ================================================================
    # Section 1: Hero Pacing Bars + Alert Banners
    # ================================================================
    pacing_data = compute_pacing_data(ctx)

    if pacing_data and pacing_data['has_goals']:
        if _source_filter:
            # Build channel-specific pacing data for hero bars
            _pd = pacing_data
            if _source_filter == 'shopify':
                _ch_pacing = dict(_pd, total_actual_rev=_pd['cm_dtc_rev'],
                                  goal_total_rev=_pd.get('goal_dtc_rev', 0),
                                  cm_contribution=_pd['cm_dtc_rev'] - _pd['cm_dtc_spend'],
                                  goal_contribution=_pd.get('goal_dtc_rev', 0) - _pd.get('goal_spend', 0))
            else:
                _ch_pacing = dict(_pd, total_actual_rev=_pd['cm_amz_rev'],
                                  goal_total_rev=_pd.get('goal_amz_rev', 0),
                                  cm_contribution=_pd['cm_amz_rev'] - _pd['cm_amz_spend'],
                                  goal_contribution=_pd.get('goal_amz_rev', 0) - _pd.get('goal_amz_spend', 0))
            _ch_pacing['has_goals'] = _ch_pacing['goal_total_rev'] > 0
            if _ch_pacing['has_goals']:
                render_hero_bars(_ch_pacing)
        else:
            render_hero_bars(pacing_data)

    # Show OVERDUE/ORDER NOW alerts if any
    global_alerts = ctx.get('global_alerts', {})
    _urgent_alerts = [a for a in global_alerts.get('reorder', [])
                      if a.get('urgency') in ('OVERDUE', 'ORDER NOW')]
    if _urgent_alerts:
        for a in _urgent_alerts[:3]:
            flavor = a.get('flavor') or a.get('sku', '')
            days = a.get('days')
            if a['urgency'] == 'OVERDUE':
                st.error(f"**{flavor}** reorder is {abs(days)}d overdue", icon='\u26A0\uFE0F')
            else:
                st.warning(f"**{flavor}** — {days}d until reorder deadline", icon='\U0001F514')

    # ================================================================
    # Section 2: Hero Numbers
    # ================================================================
    if pacing_data:
        d = pacing_data
        _pct = d['pct_month']

        # Channel-specific hero values
        if _source_filter == 'shopify':
            _hero_rev = d['cm_dtc_rev']
            _hero_goal_rev = d.get('goal_dtc_rev', 0)
            _hero_spend = d['cm_dtc_spend']
            _hero_goal_spend = d.get('goal_spend', 0)
            _hero_nc = d['cm_nc']
            _hero_nc_rev = d['cm_nc_rev']
            _hero_spend_gap = 0
        elif _source_filter == 'amazon':
            _hero_rev = d['cm_amz_rev']
            _hero_goal_rev = d.get('goal_amz_rev', 0)
            _hero_spend = d['cm_amz_spend']
            _hero_goal_spend = d.get('goal_amz_spend', 0)
            _hero_nc = d['cm_amz_nc']
            _hero_nc_rev = d['cm_amz_nc_rev']
            _hero_spend_gap = d.get('amz_spend_gap_days', 0)
        else:
            _hero_rev = d['total_actual_rev']
            _hero_goal_rev = d['goal_total_rev']
            _hero_spend = d['cm_total_spend']
            _hero_goal_spend = d.get('goal_total_spend', 0)
            _hero_nc = d['cm_total_nc']
            _hero_nc_rev = d['cm_total_nc_rev']
            _hero_spend_gap = d.get('amz_spend_gap_days', 0)

        _rev_pace_goal = _hero_goal_rev * _pct if d['has_goals'] and _hero_goal_rev > 0 else 0
        _nc_pace_goal = d['goal_nc_count'] * _pct if d.get('goal_nc_count', 0) > 0 and not _source_filter else 0
        _spend_pace_goal = _hero_goal_spend * _pct if _hero_goal_spend > 0 else 0
        _spend_label = 'Spend MTD (est)' if _hero_spend_gap > 0 else 'Spend MTD'
        _nc_is_est = d.get('amz_nc_projected', False) and _source_filter != 'shopify'
        _nc_label = 'New Customers MTD (est)' if _nc_is_est else 'New Customers MTD'

        h1, h2, h3, h4, h5 = st.columns(5)
        h1.metric('Rev MTD', f"${_hero_rev:,.0f}",
                   delta=f"Pace: ${_rev_pace_goal:,.0f}" if _rev_pace_goal > 0 else None,
                   delta_color='off')
        h2.metric(_spend_label, f"${_hero_spend:,.0f}",
                   delta=f"Pace: ${_spend_pace_goal:,.0f}" if _spend_pace_goal > 0 else None,
                   delta_color='off',
                   help=f"Includes {_hero_spend_gap}d projected Amazon spend (L7D avg)" if _hero_spend_gap > 0 else None)
        h3.metric(_nc_label, f"{_hero_nc:,}",
                   delta=f"Pace: {_nc_pace_goal:,.0f}" if _nc_pace_goal > 0 else None,
                   delta_color='off',
                   help='Includes projected Amazon NC for yesterday (est)' if _nc_is_est else None)
        _nc_roas_val = d.get('total_nc_roas', 0)
        _nc_roas_goal = d.get('goal_nc_roas_ratio', 0)
        h4.metric('NC-ROAS', f"{_nc_roas_val:.2f}x",
                   delta=f"Goal: {_nc_roas_goal:.2f}x" if _nc_roas_goal > 0 else None,
                   delta_color='off')

        # CAC Payback — pull cost assumptions from revenue model (Variables page)
        from analytics.revenue_model import DEFAULTS

        @st.cache_data(ttl=86400, show_spinner=False)
        def _cached_revenue_model():
            try:
                from db import get_revenue_model
                with get_db() as conn:
                    return get_revenue_model(conn)
            except Exception:
                return {}

        _rm = _cached_revenue_model() or {}

        def _rm_val(key, default):
            """Get first value from revenue model — handles both {month:val} dicts and lists."""
            vals = _rm.get(key)
            if not vals:
                d = DEFAULTS.get(key, [default])
                return float(d[0]) if isinstance(d, list) and d else float(default)
            if isinstance(vals, dict):
                # {month_str: value} — pick current month or first available
                _cm = datetime.now().strftime('%Y-%m')
                if _cm in vals:
                    return float(vals[_cm])
                # Fall back to first value
                return float(next(iter(vals.values())))
            if isinstance(vals, list) and vals:
                return float(vals[0])
            return float(default)

        _cogs_pct = _rm_val('cogs_pct', 0.165)
        _dtc_fulfill_pct = _rm_val('dtc_fulfillment_pct', 0.18)
        _amz_fulfill_pct = _rm_val('amazon_fulfillment_pct', 0.305)
        _dtc_ntg = _rm_val('dtc_net_to_gross', 1.3859589838)
        _dtc_proc_pct = _rm_val('dtc_processing_pct', 0.04)
        _amz_ntg = 1.0  # Amazon revenue is already gross

        _dtc_ret = None
        _amz_ret = None
        try:
            _dtc_ret = ctx['cached_retention_curve']('shopify')
        except Exception:
            pass
        try:
            _amz_ret = ctx['cached_retention_curve']('amazon')
        except Exception:
            pass

        # Choose retention curve and channel-specific cost params
        if _source_filter == 'shopify':
            _use_ret = _dtc_ret
            _dtc_w, _amz_w = 1.0, 0.0
        elif _source_filter == 'amazon':
            _use_ret = _amz_ret
            _dtc_w, _amz_w = 0.0, 1.0
        else:
            _dtc_nc = max(d.get('cm_nc', 0), 0)
            _amz_nc = max(d.get('cm_amz_nc', 0), 0)
            _total_nc_blend = _dtc_nc + _amz_nc
            _dtc_w = _dtc_nc / _total_nc_blend if _total_nc_blend > 0 else 1.0
            _amz_w = _amz_nc / _total_nc_blend if _total_nc_blend > 0 else 0.0
            _use_ret = {}
            if _dtc_ret or _amz_ret:
                _all_months = set()
                if _dtc_ret:
                    _all_months.update(_dtc_ret.keys())
                if _amz_ret:
                    _all_months.update(_amz_ret.keys())
                for _mk in _all_months:
                    _dr = (_dtc_ret or {}).get(_mk, 0)
                    _ar = (_amz_ret or {}).get(_mk, 0)
                    _use_ret[_mk] = _dr * _dtc_w + _ar * _amz_w
            _use_ret = _use_ret or _dtc_ret

        # DTC total fee = fulfillment + processing; Amazon fee is all-in
        _dtc_total_fee = _dtc_fulfill_pct + _dtc_proc_pct
        # Blend fees and net_to_gross by channel weight
        _fulfill_pct = _dtc_w * _dtc_total_fee + _amz_w * _amz_fulfill_pct
        _net_to_gross = _dtc_w * _dtc_ntg + _amz_w * _amz_ntg

        _cac_payback = compute_cac_payback(
            _hero_spend, _hero_nc, _hero_nc_rev,
            cogs_pct=_cogs_pct, fulfillment_pct=_fulfill_pct,
            net_to_gross=_net_to_gross,
            retention_curve=_use_ret,
        )
        _goal_cac_payback = None
        if d.get('goal_nc_count', 0) > 0 and _hero_goal_spend > 0 and not _source_filter:
            _goal_cac_payback = compute_cac_payback(
                _hero_goal_spend, d['goal_nc_count'], d['goal_nc_rev'],
                cogs_pct=_cogs_pct, fulfillment_pct=_fulfill_pct,
                net_to_gross=_net_to_gross,
                retention_curve=_use_ret,
            )
        h5.metric('CAC Payback',
                   f'{_cac_payback:.1f}mo' if _cac_payback > 0 else '\u2014',
                   delta=f"Target: {_goal_cac_payback:.1f}mo" if _goal_cac_payback and _goal_cac_payback > 0 else None,
                   delta_color='off',
                   help='Months until contribution margin covers CAC')

        # --- Yesterday tiles — use Marketing rollup data source for accuracy ---
        _yd_rollup = _get_yesterday_rollup()
        if _yd_rollup and _yd_rollup.get('rev', 0) > 0:
            if _source_filter == 'shopify':
                _yd_hero_rev = _yd_rollup['dtc_rev']
                _yd_hero_spend = _yd_rollup['dtc_spend']
                _yd_hero_nc = _yd_rollup['dtc_nc']
                _yd_hero_nc_rev = _yd_rollup['dtc_nc_rev']
            elif _source_filter == 'amazon':
                _yd_hero_rev = _yd_rollup['amz_rev']
                _yd_hero_spend = _yd_rollup['amz_spend']
                _yd_hero_nc = _yd_rollup['amz_nc']
                _yd_hero_nc_rev = _yd_rollup['amz_nc_rev']
            else:
                _yd_hero_rev = _yd_rollup['rev']
                _yd_hero_spend = _yd_rollup['spend']
                _yd_hero_nc = _yd_rollup['nc']
                _yd_hero_nc_rev = _yd_rollup['nc_rev']
            _yd_est = _yd_rollup.get('amz_projected', False) and _source_filter != 'shopify'
        else:
            # Fallback to pacing data if rollup unavailable
            if _source_filter == 'shopify':
                _yd_hero_rev = d['yd_dtc_rev']
                _yd_hero_spend = d['yd_dtc_spend']
                _yd_hero_nc = d['yd_nc']
                _yd_hero_nc_rev = d['yd_nc_rev']
            elif _source_filter == 'amazon':
                _yd_hero_rev = d['yd_amz_rev']
                _yd_hero_spend = d['yd_amz_spend']
                _yd_hero_nc = d['yd_amz_nc']
                _yd_hero_nc_rev = d['yd_amz_nc_rev']
            else:
                _yd_hero_rev = d['yd_total_rev']
                _yd_hero_spend = d['yd_total_spend']
                _yd_hero_nc = d['yd_total_nc']
                _yd_hero_nc_rev = d['yd_total_nc_rev']
            _yd_est = d.get('yd_amz_projected', False) and _source_filter != 'shopify'

        _yd_nc_roas_val = _yd_hero_nc_rev / _yd_hero_spend if _yd_hero_spend > 0 else 0
        _yd_cac_payback = compute_cac_payback(
            _yd_hero_spend, _yd_hero_nc, _yd_hero_nc_rev,
            cogs_pct=_cogs_pct, fulfillment_pct=_fulfill_pct,
            net_to_gross=_net_to_gross,
            retention_curve=_use_ret,
        ) if _yd_hero_nc > 0 and _yd_hero_spend > 0 else 0

        _yd_est_suffix = ' (est)' if _yd_est else ''
        y1, y2, y3, y4, y5 = st.columns(5)
        y1.metric(f'Rev Yesterday{_yd_est_suffix}', f"${_yd_hero_rev:,.0f}")
        y2.metric(f'Spend Yesterday{_yd_est_suffix}', f"${_yd_hero_spend:,.0f}")
        y3.metric(f'New Cust Yesterday{_yd_est_suffix}', f"{_yd_hero_nc:,}")
        y4.metric('NC-ROAS Yesterday', f"{_yd_nc_roas_val:.2f}x")
        y5.metric('CAC Payback Yesterday',
                   f'{_yd_cac_payback:.1f}mo' if _yd_cac_payback > 0 else '\u2014')

        # CAC Payback math breakdown
        _has_amz_ret = bool(_amz_ret and any(v > 0 for v in _amz_ret.values()))
        if _hero_nc > 0 and _hero_spend > 0:
            # Effective COGS on net = cogs_pct × net_to_gross (since COGS is % of gross)
            _cogs_on_net = _cogs_pct * _net_to_gross
            _pb_margin = 1 - _cogs_on_net - _fulfill_pct

            with st.expander('CAC Payback Breakdown', expanded=False):
                v1, v2, v3, v4, v5, v6 = st.columns(6)
                v1.metric('New Customers', f'{_hero_nc:,}')
                v2.metric('NC Revenue (M0)', f'${_hero_nc_rev:,.0f}')
                v3.metric('Total Media Spend', f'${_hero_spend:,.0f}')
                v4.metric('COGS % of Gross', f'{_cogs_pct:.0%}')
                if _source_filter == 'amazon':
                    v5.metric('Amazon Fee %', f'{_amz_fulfill_pct:.0%}')
                elif _source_filter == 'shopify':
                    v5.metric('DTC Fee %', f'{_dtc_total_fee:.0%}',
                              help=f'Fulfillment {_dtc_fulfill_pct:.0%} + Processing {_dtc_proc_pct:.0%}')
                else:
                    v5.metric('Blended Fee %', f'{_fulfill_pct:.0%}',
                              help=f'DTC {_dtc_total_fee:.0%} \u00d7 {_dtc_w:.0%} + Amazon {_amz_fulfill_pct:.0%} \u00d7 {_amz_w:.0%}')
                v6.metric('Net \u2192 Gross', f'{_net_to_gross:.2f}x')

                if _source_filter == 'shopify':
                    _ret_label = 'DTC only'
                elif _source_filter == 'amazon':
                    _ret_label = 'Amazon only'
                elif _has_amz_ret:
                    _ret_label = f'Blended (DTC {_dtc_w:.0%} + Amazon {_amz_w:.0%} by NC count)'
                else:
                    _ret_label = 'DTC only'
                _fee_label = f'{_fulfill_pct:.0%} Fee' if _source_filter else f'{_fulfill_pct:.0%} Fee (DTC {_dtc_total_fee:.0%} / Amz {_amz_fulfill_pct:.0%})'
                st.caption(f'Total media to recover: **${_hero_spend:,.0f}** | '
                           f'Margin rate: **{_pb_margin:.0%}** (1 \u2212 {_cogs_pct:.0%} COGS \u00d7 {_net_to_gross:.2f}x gross \u2212 {_fee_label}) | '
                           f'Retention: **{_ret_label}**')
                st.caption('*Revenue retention rate = incremental revenue in month N \u00f7 first-order revenue '
                           '(captures both repurchase probability and order value changes). '
                           '60/30/10 recency weighting across cohorts from 2020+. '
                           'Differs from Retention pages which show customer repurchase % only. '
                           'Payback = month when cumulative contribution margin covers total media spend.*')

                _max_show = 12
                _payback_found = False
                _month_data = {}
                _cumul = 0

                for _m in range(0, _max_show + 1):
                    _col = f'M{_m}'
                    if _m == 0:
                        _rev_total = _hero_nc_rev
                        _dtc_r = 1.0
                        _amz_r = 1.0
                        _blend_r = 1.0
                    else:
                        _dtc_r = (_dtc_ret or {}).get(_m, 0)
                        _amz_r = (_amz_ret or {}).get(_m, 0)
                        _blend_r = (_use_ret or {}).get(_m, 0)
                        _rev_total = _hero_nc_rev * _blend_r

                    _cogs_total = _rev_total * _net_to_gross * _cogs_pct
                    _ful_total = _rev_total * _fulfill_pct
                    _contrib_total = _rev_total - _cogs_total - _ful_total
                    _media_hit = _hero_spend if _m == 0 else 0
                    _net_total = _contrib_total - _media_hit
                    _cumul += _net_total

                    def _fmt(v):
                        return f'${v:,.0f}' if v >= 0 else f'(${abs(v):,.0f})'

                    _row_data = {}
                    if not _source_filter and _has_amz_ret:
                        _row_data['DTC Rev. Retention'] = '\u2014' if _m == 0 else f'{_dtc_r:.1%}'
                        _row_data['Amazon Rev. Retention'] = '\u2014' if _m == 0 else f'{_amz_r:.1%}'
                        _row_data['Blended Rev. Retention'] = '\u2014' if _m == 0 else f'{_blend_r:.1%}'
                    else:
                        _curve_label = 'Rev. Retention'
                        _row_data[_curve_label] = '\u2014' if _m == 0 else f'{_blend_r:.1%}'
                    _row_data['NC Revenue'] = f'${_rev_total:,.0f}'
                    _row_data[f'COGS ({_cogs_pct:.1%} of Gross)'] = f'(${_cogs_total:,.0f})'
                    _fee_col = 'Amazon Fee' if _source_filter == 'amazon' else 'Fulfillment' if _source_filter == 'shopify' else 'Channel Fees'
                    _row_data[f'{_fee_col} ({_fulfill_pct:.0%})'] = f'(${_ful_total:,.0f})'
                    _row_data['Media Spend'] = f'(${_media_hit:,.0f})' if _m == 0 else '\u2014'
                    _row_data['Net'] = _fmt(_net_total)
                    _row_data['Cumulative'] = _fmt(_cumul)
                    _month_data[_col] = _row_data

                    if _cumul >= 0 and _m > 0 and not _payback_found:
                        _payback_found = True
                        if _m < _max_show:
                            _max_show = _m + 1

                _cols_to_show = [f'M{i}' for i in range(_max_show + 1) if f'M{i}' in _month_data]
                _metric_names = list(next(iter(_month_data.values())).keys())
                _transposed = {' ': _metric_names}
                for _col in _cols_to_show:
                    _transposed[_col] = [_month_data[_col][k] for k in _metric_names]
                render_html_table(pd.DataFrame(_transposed))

    # ================================================================
    # Section 3: Pacing Detail (expander)
    # ================================================================
    _show_pacing_detail = pacing_data and pacing_data['has_goals']
    if _source_filter and _show_pacing_detail:
        # Channel pages: only show if that channel has goals
        if _source_filter == 'shopify':
            _show_pacing_detail = pacing_data.get('goal_dtc_rev', 0) > 0
        elif _source_filter == 'amazon':
            _show_pacing_detail = pacing_data.get('goal_amz_rev', 0) > 0
    if _show_pacing_detail:
        with st.expander('Pacing Detail', expanded=True):
            st.caption('*MTD actuals vs pro-rated monthly goals (goal \u00d7 days elapsed / days in month). '
                       'Revenue from daily_sku_sales, spend from Google Sheets + Amazon daily rollup, '
                       'goals from forecast model.*')
            render_pacing_detail_table(pacing_data, source_filter=_source_filter)

    # ================================================================
    # Section 4: Performance Trends (expander with DoD/WoW tabs)
    # ================================================================
    _shopify_daily = _load_shopify_daily_metrics()
    _gs_spend = _load_gs_spend()
    _amz_daily = _load_amazon_daily()

    # Determine which channels to show based on source filter
    if _source_filter == 'shopify':
        _trend_channels = ['DTC']
    elif _source_filter == 'amazon':
        _trend_channels = ['Amazon']
    else:
        _trend_channels = ['Rollup', 'DTC', 'Amazon']

    with st.expander('Performance Trends', expanded=False):
        st.caption('*DoD: 3-day avg vs same 3 days prior week. WoW: current week vs prior week. '
                   'Green = improving, red = declining. NC Rev % and Contribution % greyed for DoD (too volatile daily).*')
        _dod_tab, _wow_tab = st.tabs(['Day over Day', 'Week over Week'])

        with _dod_tab:
            if not _shopify_daily.empty:
                dod_df = build_overview_trend_rows(_shopify_daily, _gs_spend, _amz_daily, 'dod')
                if not dod_df.empty:
                    for channel in _trend_channels:
                        ch_df = dod_df[dod_df['Channel'] == channel].drop(columns=['Channel'])
                        if len(_trend_channels) > 1:
                            st.caption(f'**{channel}**' if channel == 'Rollup' else channel)
                        render_perf_table_transposed(
                            ch_df, 'Period', max_height=300,
                            grey_cols={'NC Rev %', 'Contribution %'},
                        )
                else:
                    st.caption('Not enough data for DoD comparison.')
            else:
                st.caption('No Shopify data available.')

        with _wow_tab:
            if not _shopify_daily.empty:
                wow_df = build_overview_trend_rows(_shopify_daily, _gs_spend, _amz_daily, 'wow')
                if not wow_df.empty:
                    for channel in _trend_channels:
                        ch_df = wow_df[wow_df['Channel'] == channel].drop(columns=['Channel'])
                        if len(_trend_channels) > 1:
                            st.caption(f'**{channel}**' if channel == 'Rollup' else channel)
                        render_perf_table_transposed(ch_df, 'Period', max_height=300)
                else:
                    st.caption('Not enough data for WoW comparison.')
            else:
                st.caption('No Shopify data available.')

    # ================================================================
    # Section 5: Marketing Performance Card
    # ================================================================
    if pacing_data:
        d = pacing_data
        if _source_filter == 'shopify':
            _l7d_sig_nc_rev = d['l7d_nc_rev'] * 7
            _l7d_sig_spend = d['l7d_spend'] * 7
            _l7d_sig_nc = d['l7d_nc'] * 7
            _l7d_sig_mer = compute_nc_roas(d['l7d_dtc_rev'] * 7, d['l7d_dtc_spend'] * 7)
            _l7d_sig_cpa = compute_nc_cpa(d['l7d_dtc_spend'] * 7, d['l7d_nc'] * 7)
        elif _source_filter == 'amazon':
            _l7d_sig_nc_rev = d['l7d_amz_nc_rev'] * 7
            _l7d_sig_spend = d['l7d_amz_spend'] * 7
            _l7d_sig_nc = d['l7d_amz_nc'] * 7
            _l7d_sig_mer = compute_nc_roas(d['l7d_amz_rev'] * 7, d['l7d_amz_spend'] * 7)
            _l7d_sig_cpa = compute_nc_cpa(d['l7d_amz_spend'] * 7, d['l7d_amz_nc'] * 7)
        else:
            _l7d_sig_nc_rev = d['l7d_total_nc_rev'] * 7
            _l7d_sig_spend = d['l7d_total_spend'] * 7
            _l7d_sig_nc = d['l7d_total_nc'] * 7
            _l7d_sig_mer = d['l7d_mer']
            _l7d_sig_cpa = d['l7d_total_nc_cpa']

        _l7d_nc_roas = compute_nc_roas(_l7d_sig_nc_rev, _l7d_sig_spend)

        st.markdown(
            '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;'
            'padding:16px 20px;margin:8px 0 16px;">'
            '<div style="font-weight:700;color:#0F3557;font-size:0.9rem;margin-bottom:6px;">'
            'Marketing Signal (L7D)</div>'
            f'<div style="display:flex;gap:32px;font-size:0.84rem;color:#334155;">'
            f'<span>NC ROAS: <b>{_l7d_nc_roas:.2f}x</b></span>'
            f'<span>New Customers: <b>{_l7d_sig_nc:.0f}</b></span>'
            f'<span>MER: <b>{_l7d_sig_mer:.2f}x</b></span>'
            f'<span>CPA: <b>${_l7d_sig_cpa:,.0f}</b></span>'
            '</div>'
            '<div style="font-size:0.72rem;color:#94a3b8;margin-top:4px;">'
            'See Marketing page for full breakdown &rarr;</div>'
            '</div>',
            unsafe_allow_html=True,
        )

    # ================================================================
    # Section 6: Revenue Trend Chart (90-day, full width)
    # ================================================================
    _src_label_chart = {'shopify': 'Shopify', 'amazon': 'Amazon'}.get(_source_filter, 'Shopify + Amazon')
    st.subheader('Revenue Trend')
    st.caption(f'90-day moving average of daily revenue ({_src_label_chart}).')
    daily = _load_overview_daily_trend(_source_filter)
    if not daily.empty:
        daily['sale_date'] = pd.to_datetime(daily['sale_date'])
        _source_colors = {'shopify': '#0F3557', 'amazon': '#F58B3D'}

        if _source_filter:
            # Single channel — simple line chart
            daily_agg = daily.groupby('sale_date')['revenue'].sum().reset_index()
            daily_agg['revenue_7d'] = daily_agg['revenue'].rolling(7, min_periods=1).mean()
            fig_rev = go.Figure()
            fig_rev.add_trace(go.Scatter(
                x=daily_agg['sale_date'], y=daily_agg['revenue_7d'],
                mode='lines', name=f'{_src_label_chart} (7d avg)',
                line=dict(color=_source_colors.get(_source_filter, '#0F3557'), width=2),
                fill='tozeroy', fillcolor='rgba(15,53,87,0.08)',
            ))
        else:
            # Rollup — stacked area + total dotted line
            daily_pivot = daily.pivot_table(
                index='sale_date', columns='source', values='revenue', aggfunc='sum'
            ).fillna(0).reset_index()

            fig_rev = go.Figure()
            for src in ['shopify', 'amazon']:
                if src in daily_pivot.columns:
                    ma_col = daily_pivot[src].rolling(7, min_periods=1).mean()
                    fig_rev.add_trace(go.Scatter(
                        x=daily_pivot['sale_date'], y=ma_col,
                        mode='lines', name=f'{src.title()} (7d avg)',
                        line=dict(color=_source_colors.get(src, '#888'), width=2),
                        stackgroup='one',
                    ))

            daily_total = daily.groupby('sale_date')['revenue'].sum().reset_index()
            daily_total['revenue_7d'] = daily_total['revenue'].rolling(7, min_periods=1).mean()
            fig_rev.add_trace(go.Scatter(
                x=daily_total['sale_date'], y=daily_total['revenue_7d'],
                mode='lines', name='Total (7d avg)',
                line=dict(color='#111827', width=2, dash='dot'),
            ))

        fig_rev.update_layout(
            yaxis_title='Revenue',
            height=380,
            margin=dict(l=0, r=0, t=10, b=0),
            legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='left', x=0),
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            yaxis=dict(gridcolor='#E8EDF3'),
            xaxis=dict(gridcolor='#E8EDF3'),
        )
        st.plotly_chart(fig_rev, use_container_width=True)

    # ================================================================
    # Section 7: Top SKUs bar chart
    # ================================================================
    st.subheader('Top SKUs')
    st.caption('Best-selling SKUs by units sold.')
    top = _load_top_skus(_source_filter)
    if not top.empty:
        fig = go.Figure(go.Bar(
            x=top['total_units'], y=top['flavor'], orientation='h',
            text=top['total_units'], textposition='outside',
            marker_color='#0F3557',
        ))
        fig.update_layout(
            height=min(360, len(top) * 36 + 40),
            margin=dict(l=0, r=0, t=10, b=0),
            yaxis=dict(categoryorder='total ascending', title=''),
            xaxis=dict(title='Units Sold', gridcolor='#E8EDF3'),
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        )
        fig.update_traces(textfont_size=11)
        st.plotly_chart(fig, use_container_width=True)

    # ================================================================
    # Section 8: Inventory footer (single HTML table)
    # ================================================================
    st.subheader('Inventory & Demand')

    _3pl_total = 0
    _fba_total = 0
    _3pl_ok = False
    _fba_ok = False

    try:
        _3pl = ctx['cached_3pl_inventory']()
        if _3pl:
            _3pl_total = sum(i.get('quantity_available', 0) or 0 for i in _3pl)
            _3pl_ok = True
    except Exception:
        pass

    try:
        _fba = ctx['cached_amazon_inventory']()
        if _fba:
            _fba_total = sum(i.get('total_quantity', 0) or 0 for i in _fba)
            _fba_ok = True
    except Exception:
        pass

    # Demand forecast rollup
    _demand_3mo = 0
    _demand_12mo = 0
    FORECAST_SKUS = ctx['forecast_skus']
    _cached_waterfall = ctx['cached_waterfall']
    _load_seasonal_json = ctx['load_seasonal_json']
    try:
        import json as _json_ov
        _ov_spend = ctx.get('media_spend')
        if not _ov_spend:
            _ov_spend = [{'month': (datetime.utcnow() + relativedelta(months=i)).strftime('%Y-%m'),
                          'spend': 0, 'new_customer_roas': 0.7} for i in range(12)]
        _ov_wf = _cached_waterfall(_json_ov.dumps(_ov_spend, sort_keys=True), None, 12, _load_seasonal_json())
        if not _ov_wf.empty:
            _demand_3mo = _ov_wf.head(3)['total_units'].sum()
            _demand_12mo = _ov_wf['total_units'].sum()
    except Exception:
        pass

    _total_inv = _3pl_total + _fba_total
    inv_rows = [{
        '3PL Stock': f'{_3pl_total:,.0f}' if _3pl_ok else '\u2014',
        'FBA Stock': f'{_fba_total:,.0f}' if _fba_ok else '\u2014',
        'Total Inventory': f'{_total_inv:,.0f}' if (_3pl_ok or _fba_ok) else '\u2014',
        'Demand (3mo)': f'{_demand_3mo:,.0f}',
        'Demand (12mo)': f'{_demand_12mo:,.0f}',
    }]
    render_html_table(pd.DataFrame(inv_rows))

    if (_3pl_ok or _fba_ok) and _demand_3mo > 0:
        _monthly_demand = _demand_3mo / 3
        _months_supply = _total_inv / _monthly_demand if _monthly_demand > 0 else 0
        _supply_icon = '\U0001f7e2' if _months_supply >= 3 else ('\U0001f7e1' if _months_supply >= 1.5 else '\U0001f534')
        st.caption(f'{_supply_icon} {_months_supply:.1f} months of supply across all channels')
