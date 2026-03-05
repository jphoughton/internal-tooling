"""
Analytics model orchestrator.

Runs analytics models (retention cohorts, repeat forecast, seasonality
adjustment, SKU-level % of sales) on a daily schedule and triggers
event-driven waterfall reruns when new repeat data arrives.

All model results are persisted to the precomputed_analytics table so
the dashboard can read them instantly without recomputing.

Models:
    retention_cohorts   — rebuild cohort retention matrix
    repeat_forecast     — waterfall demand split (new + repeat)
    seasonality         — seasonal index adjustment
    sku_sales_mix       — SKU-level % of sales from recent orders
    waterfall           — full waterfall build (event-driven on new repeat data)
    cashflow_forecast   — cash flow projection refresh and KPI health logging
    global_alerts       — reorder + FBA transfer urgency alerts for notification bar
"""
import json
import logging
import time
from db import get_db, log_model_run, get_model_run, save_precomputed

logger = logging.getLogger(__name__)

# Model names used as keys in model_runs table
MODEL_RETENTION = 'retention_cohorts'
MODEL_REPEAT_FORECAST = 'repeat_forecast'
MODEL_SEASONALITY = 'seasonality'
MODEL_SKU_MIX = 'sku_sales_mix'
MODEL_WATERFALL = 'waterfall'
MODEL_CASHFLOW = 'cashflow_forecast'
MODEL_GLOBAL_ALERTS = 'global_alerts'
MODEL_RETENTION_PAGE = 'retention_page_data'
MODEL_PAGE_STATS = 'page_stats'
MODEL_OPS_PAGE = 'ops_page_data'

ALL_DAILY_MODELS = [
    MODEL_RETENTION,
    MODEL_REPEAT_FORECAST,
    MODEL_SEASONALITY,
    MODEL_SKU_MIX,
    MODEL_WATERFALL,
    MODEL_CASHFLOW,
    MODEL_GLOBAL_ALERTS,
    MODEL_RETENTION_PAGE,
    MODEL_PAGE_STATS,
    MODEL_OPS_PAGE,
]


def _run_model(model_name, fn, triggered_by='scheduler'):
    """Run a single model function, log timing and status to model_runs."""
    logger.info('[orchestrator] Running model: %s (triggered_by=%s)', model_name, triggered_by)
    t0 = time.time()
    error = None
    status = 'success'
    try:
        fn()
    except Exception as exc:
        error = str(exc)[:500]
        status = 'error'
        logger.error('[orchestrator] Model %s failed: %s', model_name, exc)

    duration = round(time.time() - t0, 2)
    logger.info('[orchestrator] Model %s completed in %.1fs (status=%s)', model_name, duration, status)

    try:
        with get_db() as conn:
            log_model_run(conn, model_name, duration, status, error, triggered_by)
    except Exception as exc:
        logger.error('[orchestrator] Failed to log model run for %s: %s', model_name, exc)

    return status


def _save_result(result_key, data, model_name, duration=None):
    """Serialize and persist a model result to precomputed_analytics."""
    try:
        if isinstance(data, dict):
            result_json = json.dumps(data, default=str)
        else:
            result_json = json.dumps(data, default=str)
        with get_db() as conn:
            save_precomputed(conn, result_key, result_json, model_name, duration)
        logger.info('[orchestrator] Saved precomputed result: %s (%.1f KB)',
                     result_key, len(result_json) / 1024)
    except Exception as exc:
        logger.error('[orchestrator] Failed to save precomputed %s: %s', result_key, exc)


def run_retention_cohorts(triggered_by='scheduler'):
    """Recompute retention cohort matrix and cache it (Shopify/DTC only)."""
    def _compute():
        from analytics.retention import (
            get_customer_cohort_data, get_revenue_retention_data,
            build_cohort_matrices, get_cohort_summary,
        )
        t0 = time.time()
        # Repeat model is DTC-only: compute Shopify cohorts
        get_customer_cohort_data(source_filter='shopify')
        get_revenue_retention_data(source_filter='shopify')

        # Persist cohort matrices for dashboard
        for src in ['shopify', 'amazon', None]:
            key_suffix = src or 'rollup'
            try:
                matrices = build_cohort_matrices(source_filter=src or 'shopify')
                serializable = {}
                for k, v in matrices.items():
                    if hasattr(v, 'to_json'):
                        serializable[k] = json.loads(v.to_json())
                    elif hasattr(v, 'to_dict'):
                        serializable[k] = v.to_dict()
                    else:
                        serializable[k] = v
                _save_result(f'cohort_matrices_{key_suffix}', serializable,
                             MODEL_RETENTION, time.time() - t0)
            except Exception as exc:
                logger.warning('[orchestrator] Failed to persist cohort_matrices_%s: %s', key_suffix, exc)

    return _run_model(MODEL_RETENTION, _compute, triggered_by)


def run_repeat_forecast(triggered_by='scheduler'):
    """Recompute the average retention curve and AOV/units metrics (DTC only)."""
    def _compute():
        from analytics.waterfall import (
            get_average_retention_curve,
            get_aov_and_units,
            get_monthly_new_customers,
            clear_waterfall_cache,
        )
        # Clear stale cache before recomputing
        clear_waterfall_cache()
        # Repeat model is DTC-only: all functions use Shopify internally
        t0 = time.time()
        curve = get_average_retention_curve()
        _save_result('retention_curve', curve, MODEL_REPEAT_FORECAST, time.time() - t0)

        t0 = time.time()
        aov = get_aov_and_units()
        _save_result('aov_and_units', aov, MODEL_REPEAT_FORECAST, time.time() - t0)

        get_monthly_new_customers()
    return _run_model(MODEL_REPEAT_FORECAST, _compute, triggered_by)


def run_seasonality(triggered_by='scheduler'):
    """Recompute data-driven SKU-level seasonal indices from daily_sku_sales.

    Also updates the global seasonal_indices table with a sales-weighted
    average across all SKUs (unless user has set seasonality_mode='manual').
    """
    def _compute():
        from analytics.seasonal import refresh_sku_seasonal_indices
        from db import get_seasonal_indices
        result = refresh_sku_seasonal_indices()
        logger.info('[orchestrator] Seasonal index refresh: %s', result)
        # Validate global indices still exist
        with get_db() as conn:
            indices = get_seasonal_indices(conn)
        if not indices:
            logger.warning('[orchestrator] No seasonal indices found in DB')
        else:
            missing = [m for m in range(1, 13) if m not in indices]
            if missing:
                logger.warning('[orchestrator] Missing seasonal indices for months: %s', missing)
    return _run_model(MODEL_SEASONALITY, _compute, triggered_by)


def run_sku_sales_mix(triggered_by='scheduler'):
    """Recompute SKU % of sales mix from recent Shopify order data."""
    def _compute():
        from analytics.waterfall import _get_sku_mix
        # DTC-only: uses Shopify internally
        _get_sku_mix(lookback_months=3)
    return _run_model(MODEL_SKU_MIX, _compute, triggered_by)


def run_waterfall(triggered_by='scheduler'):
    """Build full waterfall forecast using current media plan and seasonality."""
    def _compute():
        from analytics.waterfall import build_waterfall, build_sku_forecast_table, clear_waterfall_cache
        from db import get_media_spend, get_seasonal_indices, get_sku_seasonal_indices, get_setting

        clear_waterfall_cache()

        with get_db() as conn:
            media_plan = get_media_spend(conn, source='All Sources')
            enabled = get_setting(conn, 'seasonality_enabled', 'true')
            seasonal = None
            if enabled == 'true':
                indices = get_seasonal_indices(conn)
                if indices:
                    seasonal = indices
            sku_seasonal = get_sku_seasonal_indices(conn)

        if not media_plan:
            logger.warning('[orchestrator] No media plan found, skipping waterfall build')
            return

        t0 = time.time()
        wf_df = build_waterfall(media_plan, horizon_months=12,
                                seasonal_indices=seasonal)
        wf_duration = time.time() - t0

        if wf_df is not None and not wf_df.empty:
            _save_result('waterfall_rollup', json.loads(wf_df.to_json()),
                         MODEL_WATERFALL, wf_duration)

            # Also build and persist SKU forecast
            t0 = time.time()
            sku_df = build_sku_forecast_table(
                wf_df, source_filter=None,
                sku_seasonal_indices=sku_seasonal if sku_seasonal else None,
                global_seasonal_indices=seasonal,
            )
            sku_duration = time.time() - t0
            if sku_df is not None and not sku_df.empty:
                _save_result('sku_forecast_rollup', json.loads(sku_df.to_json()),
                             MODEL_WATERFALL, sku_duration)

    return _run_model(MODEL_WATERFALL, _compute, triggered_by)


def run_cashflow_forecast(triggered_by='scheduler'):
    """Refresh cash flow forecast to warm cache and log key KPIs."""
    def _compute():
        from analytics.cashflow import build_cashflow_forecast, get_cashflow_kpis

        with get_db() as conn:
            t0 = time.time()
            forecast_df = build_cashflow_forecast(conn, weeks=13)
            cf_duration = time.time() - t0
            if forecast_df is None or forecast_df.empty:
                logger.warning('[orchestrator] Cash flow forecast returned no data')
                return

            kpis = get_cashflow_kpis(conn, forecast_df)

        # Persist cashflow forecast
        _save_result('cashflow_forecast', json.loads(forecast_df.to_json()),
                     MODEL_CASHFLOW, cf_duration)
        _save_result('cashflow_kpis', kpis, MODEL_CASHFLOW)

        logger.info(
            '[orchestrator] Cash flow KPIs: cash=$%,.0f, 13w_projected=$%,.0f, '
            'monthly_burn=$%,.0f, runway=%d weeks',
            kpis.get('current_cash', 0),
            kpis.get('projected_13w', 0),
            kpis.get('monthly_burn', 0),
            kpis.get('runway_weeks', 0),
        )

        if kpis.get('alert_week'):
            logger.warning(
                '[orchestrator] Cash flow ALERT: balance drops below threshold at week %d',
                kpis['alert_week'],
            )

    return _run_model(MODEL_CASHFLOW, _compute, triggered_by)


def run_global_alerts(triggered_by='scheduler'):
    """Pre-compute global reorder & FBA transfer alerts for the notification bar.

    This chains waterfall + SKU forecast + inventory + reorder — the single
    biggest cold-start bottleneck (5-15s).
    """
    def _compute():
        import pandas as pd
        from datetime import timedelta
        from analytics.reorder import build_reorder_plan
        from analytics.waterfall import build_waterfall, build_sku_forecast_table
        from analytics.cashflow import build_cashflow_forecast, get_cashflow_kpis
        from analytics.sku_flavors import get_flavor
        from db import (
            get_media_spend, get_seasonal_indices, get_sku_seasonal_indices,
            get_setting, get_inventory_snapshot,
        )
        from ui.business_vars import get_business_vars
        from utils.constants import FORECAST_SKUS
        from utils.date_helpers import business_today, business_yesterday

        alerts = {'reorder': [], 'transfer': [], 'cashflow': []}

        # Cash flow alerts
        try:
            with get_db() as conn:
                cf_df = build_cashflow_forecast(conn, weeks=13, scenario='base')
                if cf_df is not None and not cf_df.empty:
                    cf_kpis = get_cashflow_kpis(conn, cf_df)
                    if cf_kpis.get('alert_week'):
                        alerts['cashflow'].append({
                            'week': cf_kpis['alert_week'],
                            'balance': float(cf_df[cf_df['week_num'] == cf_kpis['alert_week']].iloc[0]['closing_balance']),
                            'threshold': cf_kpis['min_cash_threshold'],
                        })
        except Exception:
            pass

        try:
            with get_db() as conn:
                media_plan = get_media_spend(conn, source='All Sources')
                enabled = get_setting(conn, 'seasonality_enabled', 'true')
                seasonal = None
                if enabled == 'true':
                    indices = get_seasonal_indices(conn)
                    if indices:
                        seasonal = indices
                sku_seasonal = get_sku_seasonal_indices(conn)

            if not media_plan:
                _save_result('global_alerts', alerts, MODEL_GLOBAL_ALERTS)
                return

            wf_df = build_waterfall(media_plan, horizon_months=12, seasonal_indices=seasonal)
            if wf_df is None or wf_df.empty:
                _save_result('global_alerts', alerts, MODEL_GLOBAL_ALERTS)
                return

            sku_df = build_sku_forecast_table(
                wf_df, source_filter=None,
                sku_seasonal_indices=sku_seasonal if sku_seasonal else None,
                global_seasonal_indices=seasonal,
            )
            if sku_df.empty:
                _save_result('global_alerts', alerts, MODEL_GLOBAL_ALERTS)
                return

            sku_df = sku_df[sku_df['SKU'].isin(FORECAST_SKUS)].copy()
            if 'Variant' in sku_df.columns:
                sku_df = sku_df.drop(columns=['Variant'])
            if 'Flavor' not in sku_df.columns:
                sku_df.insert(1, 'Flavor', sku_df['SKU'].map(lambda s: get_flavor(s)))

            # Load inventory from DB snapshots
            combined_inv = {}
            try:
                with get_db() as conn:
                    _3pl_items = get_inventory_snapshot(conn, 'packiyo', max_age_hours=24) or []
                for item in _3pl_items:
                    sku = item['sku']
                    if sku in FORECAST_SKUS:
                        combined_inv.setdefault(sku, {
                            'sku': sku, 'name': item.get('name', ''),
                            'quantity_available': 0, 'quantity_on_hand': 0,
                            'quantity_inbound': 0, '3pl_available': 0, 'fba_fulfillable': 0,
                        })
                        combined_inv[sku]['3pl_available'] = int(float(item.get('quantity_available', 0) or 0))
                        combined_inv[sku]['quantity_available'] += combined_inv[sku]['3pl_available']
                        combined_inv[sku]['quantity_inbound'] += int(float(item.get('quantity_inbound', 0) or 0))
            except Exception:
                pass

            amz_inv_items = []
            try:
                with get_db() as conn:
                    amz_inv_items = get_inventory_snapshot(conn, 'amazon', max_age_hours=24) or []
                for item in amz_inv_items:
                    sku = item['sku']
                    if sku in FORECAST_SKUS:
                        combined_inv.setdefault(sku, {
                            'sku': sku, 'name': item.get('name', ''),
                            'quantity_available': 0, 'quantity_on_hand': 0,
                            'quantity_inbound': 0, '3pl_available': 0, 'fba_fulfillable': 0,
                        })
                        fba_qty = int(float(item.get('total_quantity', 0) or 0))
                        combined_inv[sku]['fba_fulfillable'] = fba_qty
                        combined_inv[sku]['quantity_available'] += fba_qty
            except Exception:
                pass

            if not combined_inv:
                _save_result('global_alerts', alerts, MODEL_GLOBAL_ALERTS)
                return

            inv_data = list(combined_inv.values())

            # Build reorder plan
            with get_db() as conn:
                planned = {}
                try:
                    pi_rows = conn.execute('SELECT sku, month, units FROM planned_inbound').fetchall()
                    for r in pi_rows:
                        planned.setdefault(r[0], {})[r[1]] = int(r[2])
                except Exception:
                    pass

            bv = get_business_vars()
            reorder_df, _ = build_reorder_plan(
                sku_forecast_table=sku_df,
                inventory_data=inv_data,
                lead_time_weeks=bv['lead_time_weeks'],
                moq=bv['moq_units'],
                safety_weeks=bv['safety_buffer_weeks'],
                forecast_skus=FORECAST_SKUS,
                planned_inbound=planned,
            )

            if not reorder_df.empty:
                for _, row in reorder_df.iterrows():
                    urgency = row.get('Urgency', 'OK')
                    if urgency in ('OVERDUE', 'ORDER NOW', 'ORDER SOON', 'EN ROUTE \u26a0\ufe0f'):
                        days = row.get('Days Until Reorder')
                        alerts['reorder'].append({
                            'sku': row.get('SKU', ''),
                            'flavor': row.get('Flavor', ''),
                            'days': int(days) if pd.notna(days) else None,
                            'urgency': urgency,
                            'qty': int(row.get('Order Qty', 0)),
                        })

            # FBA transfer alerts
            if amz_inv_items:
                from db import read_sql
                with get_db() as conn:
                    amz_dem = read_sql("""
                        SELECT sku, SUM(units_sold) / 3.0 as monthly_demand
                        FROM daily_sku_sales
                        WHERE source = 'amazon' AND sale_date >= date('now', '-90 days')
                          AND sale_date <= %s
                        GROUP BY sku
                    """, conn, params=(str(business_yesterday()),))
                amz_dem_map = dict(zip(amz_dem['sku'], amz_dem['monthly_demand'])) if not amz_dem.empty else {}
                today = business_today()
                xfer_lt = bv['fba_transfer_lt_weeks'] * 7

                for item in amz_inv_items:
                    sku = item['sku']
                    if sku not in FORECAST_SKUS:
                        continue
                    fba_stock = int(float(item.get('total_quantity', 0) or 0))
                    monthly = amz_dem_map.get(sku, 0)
                    daily_rate = monthly / 30.44 if monthly > 0 else 0
                    if daily_rate <= 0:
                        continue
                    days_of_stock = fba_stock / daily_rate
                    if days_of_stock >= 365:
                        continue
                    fba_stockout = today + timedelta(days=int(days_of_stock))
                    transfer_by = fba_stockout - timedelta(days=xfer_lt)
                    days_until = (transfer_by - today).days
                    if days_until <= 21:
                        t_urg = 'OVERDUE' if days_until < 0 else ('TRANSFER NOW' if days_until <= 7 else 'TRANSFER SOON')
                        alerts['transfer'].append({
                            'sku': sku,
                            'flavor': get_flavor(sku),
                            'days': days_until,
                            'urgency': t_urg,
                            'fba_stock': fba_stock,
                            'days_of_stock': round(days_of_stock),
                        })

        except Exception as exc:
            logger.error('[orchestrator] Global alerts computation failed: %s', exc)

        _save_result('global_alerts', alerts, MODEL_GLOBAL_ALERTS)

    return _run_model(MODEL_GLOBAL_ALERTS, _compute, triggered_by)


def run_retention_page_data(triggered_by='scheduler'):
    """Pre-compute all retention page payloads (cohort summaries, sizes, repeat rates).

    Runs after run_retention_cohorts() which already saves cohort_matrices_{source}.
    This adds the remaining data the retention view needs.
    """
    def _compute():
        import pandas as pd
        from analytics.retention import (
            get_cohort_summary, get_customer_cohort_data,
            get_cohort_sizes, get_repeat_rate_summary,
        )

        for src in ['shopify', 'amazon']:
            key_suffix = src
            t0 = time.time()

            # Cohort summary (customers, NCPA, RPR per cohort)
            try:
                summary = get_cohort_summary(source_filter=src)
                if summary is not None and not summary.empty:
                    _save_result(
                        f'cohort_summary_{key_suffix}',
                        json.loads(summary.to_json()),
                        MODEL_RETENTION_PAGE, time.time() - t0,
                    )
            except Exception as exc:
                logger.warning('[orchestrator] cohort_summary_%s failed: %s', key_suffix, exc)

            # Customer cohort data (retention matrix for curve chart)
            t0 = time.time()
            try:
                ccd = get_customer_cohort_data(source_filter=src)
                if ccd is not None and not ccd.empty:
                    _save_result(
                        f'customer_cohort_data_{key_suffix}',
                        json.loads(ccd.to_json()),
                        MODEL_RETENTION_PAGE, time.time() - t0,
                    )
            except Exception as exc:
                logger.warning('[orchestrator] customer_cohort_data_%s failed: %s', key_suffix, exc)

            # Cohort sizes
            t0 = time.time()
            try:
                sizes = get_cohort_sizes(source_filter=src)
                if sizes is not None and not sizes.empty:
                    _save_result(
                        f'cohort_sizes_{key_suffix}',
                        json.loads(sizes.to_json()),
                        MODEL_RETENTION_PAGE, time.time() - t0,
                    )
            except Exception as exc:
                logger.warning('[orchestrator] cohort_sizes_%s failed: %s', key_suffix, exc)

        # Amazon repeat rate summary
        t0 = time.time()
        try:
            rr = get_repeat_rate_summary(source_filter='amazon')
            if rr is not None and not rr.empty:
                _save_result(
                    'repeat_rate_summary_amazon',
                    json.loads(rr.to_json()),
                    MODEL_RETENTION_PAGE, time.time() - t0,
                )
        except Exception as exc:
            logger.warning('[orchestrator] repeat_rate_summary_amazon failed: %s', exc)

    return _run_model(MODEL_RETENTION_PAGE, _compute, triggered_by)


def run_page_stats(triggered_by='scheduler'):
    """Pre-compute overview, marketing, pacing, and demand forecast payloads.

    Saves new/repeat summaries, daily revenue, shopify daily metrics,
    master DTC forecast, and Amazon SKU velocity.
    """
    def _compute():
        import pandas as pd
        from datetime import datetime
        from dateutil.relativedelta import relativedelta
        from analytics.retention import (
            get_projected_new_repeat_summary,
            get_new_repeat_daily_revenue,
        )
        from analytics.dtc_demand import (
            build_master_dtc_forecast,
            get_amazon_sku_velocity,
        )
        from analytics.waterfall import build_waterfall, build_sku_forecast_table
        from db import (
            get_media_spend, get_seasonal_indices, get_sku_seasonal_indices,
            get_setting, get_amazon_revenue_forecast, read_sql,
        )
        from utils.constants import FORECAST_SKUS

        # --- New/repeat summaries (overview page) ---
        # Use wide date range to cover any filter the user might apply
        for src in ['shopify', 'amazon']:
            t0 = time.time()
            try:
                nr = get_projected_new_repeat_summary('2020-01-01', '2099-12-31', src)
                _save_result(
                    f'new_repeat_summary_{src}', nr,
                    MODEL_PAGE_STATS, time.time() - t0,
                )
            except Exception as exc:
                logger.warning('[orchestrator] new_repeat_summary_%s failed: %s', src, exc)

        # --- New/repeat daily revenue (overview chart) ---
        t0 = time.time()
        try:
            nr_daily = get_new_repeat_daily_revenue('2020-01-01', '2099-12-31')
            if nr_daily is not None and not nr_daily.empty:
                _save_result(
                    'new_repeat_daily_revenue',
                    json.loads(nr_daily.to_json()),
                    MODEL_PAGE_STATS, time.time() - t0,
                )
        except Exception as exc:
            logger.warning('[orchestrator] new_repeat_daily_revenue failed: %s', exc)

        # --- Shopify daily metrics (marketing/pacing) ---
        t0 = time.time()
        try:
            with get_db() as conn:
                # Use orders.total_amount for revenue (matches Shopify total_price
                # incl. shipping + tax), exclude refunded/voided orders
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
                    "COUNT(DISTINCT CASE WHEN fc.first_date = o.order_date "
                    "  THEN o.customer_id END) AS new_customers, "
                    "SUM(oi.total_price) AS oi_total_rev, "
                    "SUM(CASE WHEN fc.first_date = o.order_date "
                    "  THEN oi.total_price ELSE 0 END) AS oi_new_rev "
                    "FROM orders o "
                    "JOIN (SELECT customer_id, MIN(order_date) AS first_date "
                    "      FROM orders WHERE source = %s "
                    "        AND COALESCE(financial_status, 'paid') NOT IN ('refunded', 'voided') "
                    "      GROUP BY customer_id) fc "
                    "ON o.customer_id = fc.customer_id "
                    "JOIN order_items oi ON o.order_id = oi.order_id "
                    "WHERE o.source = %s "
                    "  AND COALESCE(o.financial_status, 'paid') NOT IN ('refunded', 'voided') "
                    "GROUP BY o.order_date ORDER BY sale_date",
                    conn, params=('shopify', 'shopify'),
                )
            if not rev_df.empty:
                _save_result(
                    'shopify_daily_revenue',
                    json.loads(rev_df.to_json()),
                    MODEL_PAGE_STATS, time.time() - t0,
                )
            if not cust_df.empty:
                _save_result(
                    'shopify_daily_customers',
                    json.loads(cust_df.to_json()),
                    MODEL_PAGE_STATS, time.time() - t0,
                )
        except Exception as exc:
            logger.warning('[orchestrator] shopify_daily_metrics failed: %s', exc)

        # --- Master DTC forecast (demand forecast + projected inventory pages) ---
        t0 = time.time()
        try:
            with get_db() as conn:
                media_plan = get_media_spend(conn, source='All Sources')
                enabled = get_setting(conn, 'seasonality_enabled', 'true')
                seasonal = None
                if enabled == 'true':
                    indices = get_seasonal_indices(conn)
                    if indices:
                        seasonal = indices
                sku_seasonal = get_sku_seasonal_indices(conn)
                amz_rev_rows = get_amazon_revenue_forecast(conn)

            if media_plan:
                wf_df = build_waterfall(media_plan, horizon_months=12,
                                        seasonal_indices=seasonal)
                if wf_df is not None and not wf_df.empty:
                    sku_fc = build_sku_forecast_table(
                        wf_df, source_filter='shopify',
                        sku_seasonal_indices=sku_seasonal if sku_seasonal else None,
                        global_seasonal_indices=seasonal,
                    )
                    amz_rev_dict = {r['month']: r['revenue'] for r in amz_rev_rows
                                    if r.get('revenue', 0) > 0} if amz_rev_rows else None

                    dtc = build_master_dtc_forecast(
                        shopify_waterfall_df=wf_df,
                        shopify_sku_forecast_df=sku_fc,
                        horizon_months=12,
                        forecast_skus=FORECAST_SKUS,
                        media_plan=media_plan,
                        amazon_revenue_forecast=amz_rev_dict,
                    )

                    # Serialize each DataFrame in the result
                    serializable = {}
                    for k, v in dtc.items():
                        if hasattr(v, 'to_json'):
                            serializable[k] = json.loads(v.to_json())
                        elif isinstance(v, dict):
                            # Handle nested dicts that may contain DataFrames
                            inner = {}
                            for ik, iv in v.items():
                                if hasattr(iv, 'to_json'):
                                    inner[ik] = json.loads(iv.to_json())
                                else:
                                    inner[ik] = iv
                            serializable[k] = inner
                        else:
                            serializable[k] = v

                    _save_result(
                        'master_dtc_forecast', serializable,
                        MODEL_PAGE_STATS, time.time() - t0,
                    )
        except Exception as exc:
            logger.warning('[orchestrator] master_dtc_forecast failed: %s', exc)

        # --- Amazon SKU velocity ---
        t0 = time.time()
        try:
            velocity = get_amazon_sku_velocity()
            _save_result('amazon_sku_velocity', velocity,
                         MODEL_PAGE_STATS, time.time() - t0)
        except Exception as exc:
            logger.warning('[orchestrator] amazon_sku_velocity failed: %s', exc)

    return _run_model(MODEL_PAGE_STATS, _compute, triggered_by)


def run_ops_page_data(triggered_by='scheduler'):
    """Pre-compute reorder plan and cashflow forecasts for ops pages.

    Saves reorder_plan, cashflow_forecast_13w, cashflow_forecast_52w.
    """
    def _compute():
        import pandas as pd
        from analytics.reorder import build_reorder_plan
        from analytics.cashflow import build_cashflow_forecast, get_cashflow_kpis
        from analytics.waterfall import build_waterfall, build_sku_forecast_table
        from analytics.sku_flavors import get_flavor
        from db import (
            get_media_spend, get_seasonal_indices, get_sku_seasonal_indices,
            get_setting, get_inventory_snapshot,
        )
        from ui.business_vars import get_business_vars
        from utils.constants import FORECAST_SKUS

        # --- Reorder plan ---
        t0 = time.time()
        try:
            with get_db() as conn:
                media_plan = get_media_spend(conn, source='All Sources')
                enabled = get_setting(conn, 'seasonality_enabled', 'true')
                seasonal = None
                if enabled == 'true':
                    indices = get_seasonal_indices(conn)
                    if indices:
                        seasonal = indices
                sku_seasonal = get_sku_seasonal_indices(conn)

            if media_plan:
                wf_df = build_waterfall(media_plan, horizon_months=12,
                                        seasonal_indices=seasonal)
                if wf_df is not None and not wf_df.empty:
                    sku_df = build_sku_forecast_table(
                        wf_df, source_filter=None,
                        sku_seasonal_indices=sku_seasonal if sku_seasonal else None,
                        global_seasonal_indices=seasonal,
                    )

                    if not sku_df.empty:
                        sku_df = sku_df[sku_df['SKU'].isin(FORECAST_SKUS)].copy()
                        if 'Variant' in sku_df.columns:
                            sku_df = sku_df.drop(columns=['Variant'])
                        if 'Flavor' not in sku_df.columns:
                            sku_df.insert(1, 'Flavor', sku_df['SKU'].map(lambda s: get_flavor(s)))

                        # Load inventory from DB snapshots
                        combined_inv = {}
                        try:
                            with get_db() as conn:
                                _3pl_items = get_inventory_snapshot(conn, 'packiyo', max_age_hours=24) or []
                            for item in _3pl_items:
                                sku = item['sku']
                                if sku in FORECAST_SKUS:
                                    combined_inv.setdefault(sku, {
                                        'sku': sku, 'name': item.get('name', ''),
                                        'quantity_available': 0, 'quantity_on_hand': 0,
                                        'quantity_inbound': 0, '3pl_available': 0, 'fba_fulfillable': 0,
                                    })
                                    combined_inv[sku]['3pl_available'] = int(float(item.get('quantity_available', 0) or 0))
                                    combined_inv[sku]['quantity_available'] += combined_inv[sku]['3pl_available']
                                    combined_inv[sku]['quantity_inbound'] += int(float(item.get('quantity_inbound', 0) or 0))
                        except Exception:
                            pass

                        try:
                            with get_db() as conn:
                                amz_inv_items = get_inventory_snapshot(conn, 'amazon', max_age_hours=24) or []
                            for item in amz_inv_items:
                                sku = item['sku']
                                if sku in FORECAST_SKUS:
                                    combined_inv.setdefault(sku, {
                                        'sku': sku, 'name': item.get('name', ''),
                                        'quantity_available': 0, 'quantity_on_hand': 0,
                                        'quantity_inbound': 0, '3pl_available': 0, 'fba_fulfillable': 0,
                                    })
                                    fba_qty = int(float(item.get('total_quantity', 0) or 0))
                                    combined_inv[sku]['fba_fulfillable'] = fba_qty
                                    combined_inv[sku]['quantity_available'] += fba_qty
                        except Exception:
                            pass

                        if combined_inv:
                            inv_data = list(combined_inv.values())
                            with get_db() as conn:
                                planned = {}
                                try:
                                    pi_rows = conn.execute(
                                        'SELECT sku, month, units FROM planned_inbound'
                                    ).fetchall()
                                    for r in pi_rows:
                                        planned.setdefault(r[0], {})[r[1]] = int(r[2])
                                except Exception:
                                    pass

                            bv = get_business_vars()
                            reorder_df, reorder_events = build_reorder_plan(
                                sku_forecast_table=sku_df,
                                inventory_data=inv_data,
                                lead_time_weeks=bv['lead_time_weeks'],
                                moq=bv['moq_units'],
                                safety_weeks=bv['safety_buffer_weeks'],
                                forecast_skus=FORECAST_SKUS,
                                planned_inbound=planned,
                            )

                            if not reorder_df.empty:
                                # Serialize reorder plan
                                reorder_serializable = json.loads(reorder_df.to_json())
                                events_serializable = []
                                for ev in reorder_events:
                                    ev_copy = {}
                                    for ek, ev_val in ev.items():
                                        ev_copy[ek] = str(ev_val) if hasattr(ev_val, 'isoformat') else ev_val
                                    events_serializable.append(ev_copy)

                                _save_result(
                                    'reorder_plan',
                                    {'df': reorder_serializable, 'events': events_serializable},
                                    MODEL_OPS_PAGE, time.time() - t0,
                                )
        except Exception as exc:
            logger.warning('[orchestrator] reorder_plan pre-compute failed: %s', exc)

        # --- Cashflow forecasts (13w and 52w, base scenario) ---
        for weeks, key in [(13, 'cashflow_forecast_13w'), (52, 'cashflow_forecast_52w')]:
            t0 = time.time()
            try:
                with get_db() as conn:
                    from datetime import date, timedelta
                    cf_df = build_cashflow_forecast(
                        conn,
                        start_date=date.today() - timedelta(weeks=4),
                        weeks=weeks + 4,
                        scenario='base',
                    )
                    if cf_df is not None and not cf_df.empty:
                        kpis = get_cashflow_kpis(conn, cf_df)
                        _save_result(
                            key,
                            {'df': json.loads(cf_df.to_json()), 'kpis': kpis},
                            MODEL_OPS_PAGE, time.time() - t0,
                        )
            except Exception as exc:
                logger.warning('[orchestrator] %s pre-compute failed: %s', key, exc)

    return _run_model(MODEL_OPS_PAGE, _compute, triggered_by)


def run_all_daily_models(triggered_by='scheduler'):
    """Run all daily analytics models in dependency order.

    Order matters: retention → repeat_forecast → seasonality → sku_mix → waterfall → cashflow → alerts.
    Returns dict of {model_name: status}.
    """
    logger.info('[orchestrator] Starting daily model run (triggered_by=%s)', triggered_by)
    t0 = time.time()

    results = {}
    # Run in dependency order
    results[MODEL_RETENTION] = run_retention_cohorts(triggered_by)
    results[MODEL_REPEAT_FORECAST] = run_repeat_forecast(triggered_by)
    results[MODEL_SEASONALITY] = run_seasonality(triggered_by)
    results[MODEL_SKU_MIX] = run_sku_sales_mix(triggered_by)
    results[MODEL_WATERFALL] = run_waterfall(triggered_by)
    results[MODEL_CASHFLOW] = run_cashflow_forecast(triggered_by)
    results[MODEL_GLOBAL_ALERTS] = run_global_alerts(triggered_by)
    # Phase 2: page-level pre-compute (depends on above models)
    results[MODEL_RETENTION_PAGE] = run_retention_page_data(triggered_by)
    results[MODEL_PAGE_STATS] = run_page_stats(triggered_by)
    results[MODEL_OPS_PAGE] = run_ops_page_data(triggered_by)

    duration = round(time.time() - t0, 1)
    successes = sum(1 for s in results.values() if s == 'success')
    logger.info(
        '[orchestrator] Daily model run complete in %.1fs: %d/%d succeeded',
        duration, successes, len(results),
    )
    return results


def run_waterfall_if_new_repeat_data(triggered_by='etl_sync'):
    """Event-driven: rerun waterfall only if new repeat-relevant data arrived.

    Checks whether the ETL sync brought in new order data since the last
    waterfall run. If so, reruns the full pipeline (retention → repeat → waterfall).
    """
    with get_db() as conn:
        last_waterfall = get_model_run(conn, MODEL_WATERFALL)
        last_sync_row = conn.execute(
            "SELECT MAX(created_at) as ts FROM sync_log "
            "WHERE status = 'success' AND source IN ('shopify', 'amazon', 'amazon_retention')"
        ).fetchone()

    last_sync_ts = last_sync_row['ts'] if last_sync_row and last_sync_row['ts'] else None
    if not last_sync_ts:
        logger.info('[orchestrator] No sync data found, skipping event-driven waterfall')
        return {}

    # If waterfall has never run, or last sync is newer than last waterfall run
    needs_rerun = True
    if last_waterfall and last_waterfall['last_run_at'] and last_waterfall['status'] == 'success':
        needs_rerun = last_sync_ts > last_waterfall['last_run_at']

    if not needs_rerun:
        logger.info('[orchestrator] Waterfall is up-to-date (last run: %s, last sync: %s)',
                     last_waterfall['last_run_at'] if last_waterfall else 'never', last_sync_ts)
        return {}

    logger.info('[orchestrator] New repeat data detected, rerunning waterfall pipeline')
    results = {}
    results[MODEL_RETENTION] = run_retention_cohorts(triggered_by)
    results[MODEL_REPEAT_FORECAST] = run_repeat_forecast(triggered_by)
    results[MODEL_SEASONALITY] = run_seasonality(triggered_by)
    results[MODEL_WATERFALL] = run_waterfall(triggered_by)
    results[MODEL_GLOBAL_ALERTS] = run_global_alerts(triggered_by)
    return results
