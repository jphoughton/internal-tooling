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
    pacing_precompute   — all pacing data (MTD/L7D/yesterday actuals + goals)
    overview_data       — amazon daily merged, 90-day trends, top SKUs per channel
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
MODEL_PACING = 'pacing_precompute'
MODEL_OVERVIEW_DATA = 'overview_data_precompute'

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
    MODEL_PACING,
    MODEL_OVERVIEW_DATA,
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
    """Recompute retention curves and AOV/units for all source filters."""
    def _compute():
        from analytics.waterfall import (
            get_average_retention_curve,
            get_customer_retention_curve,
            get_aov_and_units,
            get_monthly_new_customers,
            clear_waterfall_cache,
        )
        # Clear stale cache before recomputing
        clear_waterfall_cache()

        # Precompute retention curves for all source filters used by dashboard
        for src in ['shopify', 'amazon']:
            key_suffix = src
            t0 = time.time()
            try:
                curve = get_average_retention_curve(src)
                _save_result(f'retention_curve_{key_suffix}', curve,
                             MODEL_REPEAT_FORECAST, time.time() - t0)
            except Exception as exc:
                logger.warning('[orchestrator] retention_curve_%s failed: %s', key_suffix, exc)

            t0 = time.time()
            try:
                cust_curve = get_customer_retention_curve(src)
                _save_result(f'customer_retention_curve_{key_suffix}', cust_curve,
                             MODEL_REPEAT_FORECAST, time.time() - t0)
            except Exception as exc:
                logger.warning('[orchestrator] customer_retention_curve_%s failed: %s', key_suffix, exc)

            t0 = time.time()
            try:
                aov = get_aov_and_units(src)
                _save_result(f'aov_and_units_{key_suffix}', aov,
                             MODEL_REPEAT_FORECAST, time.time() - t0)
            except Exception as exc:
                logger.warning('[orchestrator] aov_and_units_%s failed: %s', key_suffix, exc)

        # Also save the default (shopify) under the legacy key for compatibility
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
            get_setting, get_amazon_revenue_forecast,
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
            from analytics.daily_metrics import query_shopify_daily_raw
            with get_db() as conn:
                rev_df, cust_df = query_shopify_daily_raw(conn)
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


def run_pacing_precompute(triggered_by='scheduler'):
    """Pre-compute pacing data used by overview, marketing, and pacing pages.

    Replicates the data loading that compute_pacing_data() normally does via
    Streamlit-cached functions, but without any Streamlit dependency.
    Saves result as 'pacing_data' in precomputed_analytics.
    """
    def _compute():
        import pandas as pd
        import calendar
        from datetime import date, timedelta
        from analytics.waterfall import build_waterfall, build_sku_forecast_table
        from analytics.dtc_demand import build_master_dtc_forecast
        from analytics.metrics import (
            get_channel_revenue, get_amazon_spend, get_amazon_spend_projected,
            get_nc_stats, nc_revenue_fraction, get_total_customers,
            compute_mer, compute_nc_roas, compute_nc_cpa, compute_aov,
        )
        from analytics.amazon_nc_projector import project_amazon_nc
        from db import (
            get_media_spend, get_seasonal_indices, get_sku_seasonal_indices,
            get_setting, get_amazon_revenue_forecast, get_precomputed,
        )
        from utils.constants import FORECAST_SKUS
        from utils.date_helpers import business_today, business_yesterday

        t0 = time.time()

        # ---- Build ctx-equivalent functions for waterfall/forecast ----
        def _waterfall_for_pacing(media_json, source_filter, horizon, seasonal_json=None):
            media = json.loads(media_json)
            seasonal = json.loads(seasonal_json) if seasonal_json else None
            if seasonal:
                seasonal = {int(k): v for k, v in seasonal.items()}
            return build_waterfall(media, source_filter=source_filter,
                                   horizon_months=horizon, seasonal_indices=seasonal)

        def _sku_forecast_for_pacing(wf_json, source_filter, sku_seasonal_json=None,
                                     global_seasonal_json=None):
            df = pd.read_json(wf_json) if wf_json else pd.DataFrame()
            sku_seasonal = None
            global_seasonal = None
            if sku_seasonal_json:
                raw = json.loads(sku_seasonal_json)
                sku_seasonal = {sku: {int(k): v for k, v in months.items()}
                                for sku, months in raw.items()}
            if global_seasonal_json:
                raw = json.loads(global_seasonal_json)
                global_seasonal = {int(k): v for k, v in raw.items()}
            return build_sku_forecast_table(df, source_filter=source_filter,
                                            sku_seasonal_indices=sku_seasonal,
                                            global_seasonal_indices=global_seasonal)

        def _load_seasonal_for_pacing():
            with get_db() as conn:
                enabled = get_setting(conn, 'seasonality_enabled', 'true')
                if enabled != 'true':
                    return None
                indices = get_seasonal_indices(conn)
            if not indices:
                return None
            return json.dumps(indices, sort_keys=True)

        def _load_sku_seasonal_for_pacing():
            with get_db() as conn:
                enabled = get_setting(conn, 'seasonality_enabled', 'true')
                if enabled != 'true':
                    return None
                indices = get_sku_seasonal_indices(conn)
            if not indices:
                return None
            return json.dumps(indices, sort_keys=True)

        # ---- Load Shopify daily metrics and GS spend via centralized loaders ----
        from analytics.daily_metrics import load_shopify_daily, load_gs_spend

        # ---- Amazon pacing stats (without Streamlit cache) ----
        def _amazon_pacing_stats(cur_month, yesterday_str, l7d_start_str):
            """Replicate _cached_amazon_pacing_stats without st.cache_data."""
            result = {
                'cm_amz_rev': 0, 'cm_amz_spend': 0, 'cm_amz_nc': 0, 'cm_amz_nc_rev': 0,
                'amz_spend_projected': 0, 'amz_spend_gap_days': 0,
                'l7d_amz_rev': 0, 'l7d_amz_spend': 0, 'l7d_amz_nc': 0, 'l7d_amz_nc_rev': 0,
                'yd_amz_rev': 0, 'yd_amz_spend': 0, 'yd_amz_nc': 0, 'yd_amz_nc_rev': 0,
                'cm_dtc_total_cust': 0, 'cm_amz_total_cust': 0,
            }
            try:
                with get_db() as conn:
                    result['cm_amz_rev'] = get_channel_revenue(conn, 'amazon', f"{cur_month}-01", yesterday_str)
                    result['cm_amz_spend'], result['amz_spend_projected'], result['amz_spend_gap_days'] = \
                        get_amazon_spend_projected(conn, f"{cur_month}-01", yesterday_str)
                    nc = get_nc_stats(conn, 'amazon', f"{cur_month}-01", yesterday_str)
                    result['cm_amz_nc'] = nc['new_customers']
                    result['cm_amz_nc_rev'] = nc_revenue_fraction(nc['oi_total_rev'], nc['oi_new_rev'], result['cm_amz_rev'])
                    result['l7d_amz_rev'] = get_channel_revenue(conn, 'amazon', l7d_start_str, yesterday_str) / 7
                    l7d_spend_total, _, _ = get_amazon_spend_projected(conn, l7d_start_str, yesterday_str)
                    result['l7d_amz_spend'] = l7d_spend_total / 7
                    nc = get_nc_stats(conn, 'amazon', l7d_start_str, yesterday_str)
                    result['l7d_amz_nc'] = nc['new_customers'] / 7
                    result['l7d_amz_nc_rev'] = nc_revenue_fraction(nc['oi_total_rev'], nc['oi_new_rev'],
                                                                    result['l7d_amz_rev'] * 7) / 7
                    result['yd_amz_rev'] = get_channel_revenue(conn, 'amazon', yesterday_str, yesterday_str)
                    yd_spend_total, _, _ = get_amazon_spend_projected(conn, yesterday_str, yesterday_str)
                    result['yd_amz_spend'] = yd_spend_total
                    nc = get_nc_stats(conn, 'amazon', yesterday_str, yesterday_str)
                    result['yd_amz_nc'] = nc['new_customers']
                    result['yd_amz_nc_rev'] = nc_revenue_fraction(nc['oi_total_rev'], nc['oi_new_rev'], result['yd_amz_rev'])
                    result['cm_dtc_total_cust'] = get_total_customers(conn, 'shopify', f"{cur_month}-01", yesterday_str)
                    result['cm_amz_total_cust'] = get_total_customers(conn, 'amazon', f"{cur_month}-01", yesterday_str)
            except Exception as e:
                logger.warning('[orchestrator] Amazon pacing stats failed: %s', e)
            return result

        # ---- Revenue model goals (without Streamlit cache) ----
        def _revenue_model_goals(cur_month):
            try:
                import analytics.revenue_model as rm
                from db import get_revenue_model, get_seasonal_indices
                from analytics.waterfall import build_waterfall
                with get_db() as conn:
                    rm_data = get_revenue_model(conn)
                if not rm_data:
                    return None
                months = [cur_month]
                inputs = rm.merge_with_defaults(rm_data, months)

                # Inject waterfall-computed repeat revenue
                with get_db() as conn:
                    seasonal = get_seasonal_indices(conn)

                # DTC repeat from Shopify waterfall
                if _mkt_media:
                    try:
                        wf_dtc = build_waterfall(
                            media_plan=_mkt_media,
                            source_filter='shopify',
                            horizon_months=12,
                            seasonal_indices=seasonal,
                        )
                        if wf_dtc is not None and not wf_dtc.empty:
                            for _, row in wf_dtc.iterrows():
                                m = str(row.get('month', ''))
                                if m in months:
                                    inputs['dtc_repeat'][m] = float(row.get('repeat_revenue', 0))
                    except Exception:
                        logger.exception('DTC waterfall failed in orchestrator pacing goals')

                # Amazon repeat from Amazon waterfall
                _v = lambda d, k: d.get(k, {}).get(cur_month, 0)
                dtc_spend = _v(inputs, 'dtc_spend')
                dtc_roas = _v(inputs, 'dtc_nc_roas')
                nc_mult = _v(inputs, 'nc_multiplier')
                amz_new_rev = nc_mult * dtc_roas * dtc_spend
                if amz_new_rev > 0:
                    try:
                        amz_media = [{'month': cur_month, 'spend': amz_new_rev, 'roas': 1.0}]
                        wf_amz = build_waterfall(
                            media_plan=amz_media,
                            source_filter='amazon',
                            horizon_months=12,
                            seasonal_indices=seasonal,
                        )
                        if wf_amz is not None and not wf_amz.empty:
                            for _, row in wf_amz.iterrows():
                                m = str(row.get('month', ''))
                                if m in months:
                                    inputs['amazon_repeat'][m] = float(row.get('repeat_revenue', 0))
                    except Exception:
                        logger.exception('Amazon waterfall failed in orchestrator pacing goals')

                calc = rm.compute(inputs, months)
                return {
                    'nc_rev': round(_v(calc, 'total_new')),
                    'nc_roas': _v(calc, 'blended_nc_roas'),
                    'dtc_new': round(_v(calc, 'dtc_new')),
                    'amazon_new': round(_v(calc, 'amazon_new')),
                    'dtc_repeat': round(_v(inputs, 'dtc_repeat')),
                    'amazon_repeat': round(_v(calc, 'amazon_repeat')),
                }
            except Exception:
                logger.exception('_revenue_model_goals failed')
            return None

        # ---- NC projection (without Streamlit cache) ----
        def _get_nc_proj():
            try:
                return project_amazon_nc()
            except Exception:
                return None

        # ============================================================
        # Now replicate compute_pacing_data logic
        # ============================================================
        _shopify_daily = load_shopify_daily()
        if _shopify_daily.empty:
            logger.warning('[orchestrator] Pacing precompute: no Shopify daily data')
            return

        mkt_df = _shopify_daily.copy()
        mkt_df['_date'] = pd.to_datetime(mkt_df['_date'])
        _gs_spend = load_gs_spend()
        if not _gs_spend.empty:
            _gs = _gs_spend[['_date', '_ad_spend']].copy()
            _gs['_date'] = pd.to_datetime(_gs['_date'])
            mkt_df = mkt_df.merge(
                _gs, on='_date', how='left', suffixes=('', '_gs')
            )
        else:
            mkt_df['_ad_spend'] = 0
        mkt_df = mkt_df.sort_values('_date')

        # Load revenue goals from precomputed master forecast
        _mkt_summary = None
        _mkt_amz_media = []
        _wf = None
        _mkt_media = []

        try:
            with get_db() as _pc_conn:
                _fc_cached = get_precomputed(_pc_conn, 'master_dtc_forecast', max_age_hours=25)
            if _fc_cached:
                _precomputed_fc = json.loads(_fc_cached)
                if 'summary' in _precomputed_fc:
                    _mkt_summary = pd.DataFrame(_precomputed_fc['summary'])
        except Exception:
            pass

        try:
            with get_db() as _goals_conn:
                _mkt_media = get_media_spend(_goals_conn, source='All Sources')
                _mkt_amz_media = get_media_spend(_goals_conn, source='Amazon')
        except Exception:
            pass

        if _mkt_summary is None:
            try:
                _seasonal_json = _load_seasonal_for_pacing()
                with get_db() as _goals_conn2:
                    _amz_rev_f = get_amazon_revenue_forecast(_goals_conn2)
                _wf = _waterfall_for_pacing(
                    json.dumps(_mkt_media, sort_keys=True), 'shopify', 12, _seasonal_json
                )
                if _wf is not None and not _wf.empty:
                    _sku_table = _sku_forecast_for_pacing(
                        _wf.to_json(), None,
                        _load_sku_seasonal_for_pacing(), _seasonal_json,
                    )
                    _amz_rev_dict = {r['month']: r['revenue'] for r in _amz_rev_f
                                     if r.get('revenue', 0) > 0}
                    _dtc_fc = build_master_dtc_forecast(
                        shopify_waterfall_df=_wf,
                        shopify_sku_forecast_df=_sku_table,
                        horizon_months=12,
                        forecast_skus=FORECAST_SKUS,
                        media_plan=_mkt_media,
                        amazon_revenue_forecast=_amz_rev_dict if _amz_rev_dict else None,
                    )
                    _mkt_summary = _dtc_fc['summary']
            except Exception:
                pass

        _today = business_today()
        _yesterday = business_yesterday()
        _yesterday_str = str(_yesterday)
        _cur_month = _today.strftime('%Y-%m')
        _cur_month_name = _today.strftime('%B %Y')
        _days_in_month = calendar.monthrange(_today.year, _today.month)[1]
        _day_of_month = _yesterday.day if _yesterday.month == _today.month else 0
        _pct_month = _day_of_month / _days_in_month if _days_in_month > 0 else 0

        _cm_mask = (mkt_df['_date'].dt.strftime('%Y-%m') == _cur_month) & (mkt_df['_date'].dt.date <= _yesterday)
        _cm_nc_rev = mkt_df.loc[_cm_mask, '_nc_revenue'].sum()
        _cm_ret_rev = mkt_df.loc[_cm_mask, '_ret_revenue'].sum()
        _cm_spend = mkt_df.loc[_cm_mask, '_ad_spend'].sum()
        _cm_rev = mkt_df.loc[_cm_mask, '_revenue'].sum()
        _cm_orders = int(mkt_df.loc[_cm_mask, '_orders'].sum())
        _cm_nc = int(mkt_df.loc[_cm_mask, '_nc_orders'].sum())

        _l7d_start = _yesterday - pd.Timedelta(days=6)
        _amz = _amazon_pacing_stats(_cur_month, _yesterday_str, str(_l7d_start))
        _cm_amz_rev = _amz['cm_amz_rev']
        _cm_amz_spend = _amz['cm_amz_spend']
        _cm_amz_nc = _amz['cm_amz_nc']
        _cm_amz_nc_rev = _amz['cm_amz_nc_rev']
        _amz_spend_projected = _amz['amz_spend_projected']
        _amz_spend_gap_days = _amz['amz_spend_gap_days']
        _amz_nc_projected = False

        _goal_nc_rev = 0
        _goal_repeat_rev = 0
        _goal_amz_rev = 0
        _goal_amz_nc_rev = 0
        _goal_amz_repeat_rev = 0
        _goal_blended_nc_rev = 0
        _goal_blended_nc_roas = 0
        _has_goals = False

        # Try revenue model first (matches pacing.py logic)
        try:
            _rm_goals = _revenue_model_goals(_cur_month)
        except Exception:
            _rm_goals = None

        if _rm_goals and _rm_goals.get('nc_rev', 0) > 0:
            _goal_nc_rev = _rm_goals['dtc_new']
            _goal_repeat_rev = _rm_goals['dtc_repeat']
            _goal_amz_nc_rev = _rm_goals['amazon_new']
            _goal_amz_repeat_rev = _rm_goals['amazon_repeat']
            _goal_amz_rev = _goal_amz_nc_rev + _goal_amz_repeat_rev
            _goal_blended_nc_rev = _rm_goals['nc_rev']
            _goal_blended_nc_roas = _rm_goals['nc_roas']
            _has_goals = True
        elif _mkt_summary is not None and not _mkt_summary.empty:
            _plan_row = _mkt_summary[_mkt_summary['month'] == _cur_month]
            if not _plan_row.empty:
                _goal_nc_rev = float(_plan_row.iloc[0].get('shopify_new_rev', 0))
                _goal_repeat_rev = float(_plan_row.iloc[0].get('shopify_repeat_rev', 0))
                _goal_amz_rev = float(_plan_row.iloc[0].get('amazon_rev', 0))
                _goal_amz_nc_rev = float(_plan_row.iloc[0].get('amazon_new_rev', 0))
                _goal_amz_repeat_rev = float(_plan_row.iloc[0].get('amazon_repeat_rev', 0))
                _goal_blended_nc_rev = _goal_nc_rev + _goal_amz_nc_rev
                _has_goals = (_goal_nc_rev + _goal_repeat_rev + _goal_amz_rev) > 0

        _goal_total_rev = _goal_nc_rev + _goal_repeat_rev + _goal_amz_rev
        _goal_total_repeat_rev = _goal_repeat_rev + _goal_amz_repeat_rev
        _total_actual_rev = _cm_nc_rev + _cm_ret_rev + _cm_amz_rev
        _remaining_days = max(_days_in_month - _day_of_month, 1)

        _l7d_mask = (mkt_df['_date'].dt.date >= _l7d_start) & (mkt_df['_date'].dt.date <= _yesterday)
        _l7d_rev = mkt_df.loc[_l7d_mask, '_revenue'].sum() / 7 if _l7d_mask.any() else 0
        _l7d_nc_rev = mkt_df.loc[_l7d_mask, '_nc_revenue'].sum() / 7 if _l7d_mask.any() else 0
        _l7d_ret_rev = mkt_df.loc[_l7d_mask, '_ret_revenue'].sum() / 7 if _l7d_mask.any() else 0
        _l7d_spend = mkt_df.loc[_l7d_mask, '_ad_spend'].sum() / 7 if _l7d_mask.any() else 0
        _l7d_nc = mkt_df.loc[_l7d_mask, '_nc_orders'].sum() / 7 if _l7d_mask.any() else 0

        _l7d_amz_rev = _amz['l7d_amz_rev']
        _l7d_amz_spend = _amz['l7d_amz_spend']
        _l7d_amz_nc = _amz['l7d_amz_nc']
        _l7d_amz_nc_rev = _amz['l7d_amz_nc_rev']

        _yd_mask = mkt_df['_date'].dt.strftime('%Y-%m-%d') == _yesterday_str
        _yd_rev = mkt_df.loc[_yd_mask, '_revenue'].sum()
        _yd_nc_rev = mkt_df.loc[_yd_mask, '_nc_revenue'].sum()
        _yd_ret_rev = mkt_df.loc[_yd_mask, '_ret_revenue'].sum()
        _yd_spend = mkt_df.loc[_yd_mask, '_ad_spend'].sum()
        _yd_nc = int(mkt_df.loc[_yd_mask, '_nc_orders'].sum())

        _yd_amz_rev = _amz['yd_amz_rev']
        _yd_amz_spend = _amz['yd_amz_spend']
        _yd_amz_nc = _amz['yd_amz_nc']
        _yd_amz_nc_rev = _amz['yd_amz_nc_rev']

        # Project Amazon yesterday from L7D averages when actual data is missing (API lag)
        _yd_amz_projected = False
        if _yd_amz_rev == 0 and _l7d_amz_rev > 0:
            _yd_amz_rev = _l7d_amz_rev  # already daily avg
            _yd_amz_projected = True
        if _yd_amz_spend == 0 and _l7d_amz_spend > 0:
            _yd_amz_spend = _l7d_amz_spend  # already daily avg
            _yd_amz_projected = True

        # Inject NC projection for yesterday
        if _yd_amz_nc == 0:
            _proj = _get_nc_proj()
            if _proj and _proj.get('actual_nc_customers') is None and _proj.get('projected_nc_customers', 0) > 0:
                _yd_amz_nc = int(round(_proj['projected_nc_customers']))
                _yd_amz_nc_rev = _proj['projected_nc_revenue']
                _cm_amz_nc += _yd_amz_nc
                _cm_amz_nc_rev += _yd_amz_nc_rev
                _amz_nc_projected = True
            elif _l7d_amz_nc > 0:
                _yd_amz_nc = int(round(_l7d_amz_nc))  # already daily avg
                _yd_amz_nc_rev = _l7d_amz_nc_rev  # already daily avg
                _yd_amz_projected = True

        _cm_dtc_total_cust = _amz['cm_dtc_total_cust']
        _cm_amz_total_cust = _amz['cm_amz_total_cust']
        _cm_dtc_repeat_cust = max(_cm_dtc_total_cust - _cm_nc, 0)
        _cm_amz_repeat_cust = max(_cm_amz_total_cust - _cm_amz_nc, 0)

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

        _goal_spend = 0
        _spend_plan = [m for m in _mkt_media if m.get('month') == _cur_month]
        if _spend_plan:
            _goal_spend = float(_spend_plan[0].get('spend', 0))

        _goal_amz_spend = 0
        _amz_spend_plan = [m for m in _mkt_amz_media if m.get('month') == _cur_month]
        if _amz_spend_plan:
            _goal_amz_spend = float(_amz_spend_plan[0].get('spend', 0))

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
        _goal_nc_roas_ratio = _goal_blended_nc_roas if _goal_blended_nc_roas > 0 else (
            _goal_nc_rev / _goal_spend if _goal_spend > 0 else 0)
        _goal_nc_cpa_target = _goal_spend / _goal_nc_count if _goal_nc_count > 0 else 0

        _cm_contribution = _total_actual_rev - _cm_total_spend
        _goal_contribution = _goal_total_rev - _goal_total_spend

        pacing_result = {
            'cur_month_name': _cur_month_name,
            'day_of_month': _day_of_month,
            'days_in_month': _days_in_month,
            'pct_month': _pct_month,
            'remaining_days': _remaining_days,
            'has_goals': _has_goals,
            'total_actual_rev': _total_actual_rev,
            'cm_nc_rev': _cm_nc_rev, 'cm_ret_rev': _cm_ret_rev,
            'cm_spend': _cm_spend, 'cm_rev': _cm_rev,
            'cm_orders': _cm_orders, 'cm_nc': _cm_nc,
            'cm_amz_rev': _cm_amz_rev, 'cm_amz_spend': _cm_amz_spend,
            'cm_amz_nc': _cm_amz_nc, 'cm_amz_nc_rev': _cm_amz_nc_rev,
            'amz_spend_projected': _amz_spend_projected,
            'amz_spend_gap_days': _amz_spend_gap_days,
            'amz_nc_projected': _amz_nc_projected,
            'goal_nc_rev': _goal_blended_nc_rev,
            'goal_repeat_rev': _goal_repeat_rev,
            'goal_total_repeat_rev': _goal_total_repeat_rev,
            'goal_amz_rev': _goal_amz_rev,
            'goal_amz_nc_rev': _goal_amz_nc_rev,
            'goal_amz_repeat_rev': _goal_amz_repeat_rev,
            'goal_total_rev': _goal_total_rev,
            'goal_dtc_rev': _goal_dtc_rev,
            'goal_spend': _goal_spend,
            'goal_amz_spend': _goal_amz_spend,
            'goal_total_spend': _goal_total_spend,
            'goal_nc_count': _goal_nc_count,
            'goal_mer': _goal_mer,
            'goal_nc_roas_ratio': _goal_nc_roas_ratio,
            'goal_nc_cpa_target': _goal_nc_cpa_target,
            'cm_contribution': _cm_contribution,
            'goal_contribution': _goal_contribution,
            'cm_dtc_rev': _cm_dtc_rev, 'cm_dtc_spend': _cm_dtc_spend,
            'l7d_dtc_rev': _l7d_dtc_rev, 'l7d_dtc_spend': _l7d_dtc_spend,
            'yd_dtc_rev': _yd_dtc_rev, 'yd_dtc_spend': _yd_dtc_spend,
            'cm_total_spend': _cm_total_spend,
            'cm_total_nc': _cm_total_nc, 'cm_total_nc_rev': _cm_total_nc_rev,
            'cm_total_repeat_rev': _cm_total_repeat_rev,
            'cm_total_repeat_cust': _cm_total_repeat_cust,
            'l7d_total_rev': _l7d_total_rev, 'l7d_total_spend': _l7d_total_spend,
            'l7d_total_nc': _l7d_total_nc, 'l7d_total_nc_rev': _l7d_total_nc_rev,
            'l7d_total_repeat_rev': _l7d_total_repeat_rev,
            'l7d_nc_rev': _l7d_nc_rev, 'l7d_ret_rev': _l7d_ret_rev,
            'l7d_nc': _l7d_nc, 'l7d_spend': _l7d_spend,
            'l7d_amz_rev': _l7d_amz_rev, 'l7d_amz_spend': _l7d_amz_spend,
            'l7d_amz_nc': _l7d_amz_nc, 'l7d_amz_nc_rev': _l7d_amz_nc_rev,
            'yd_total_rev': _yd_total_rev, 'yd_total_spend': _yd_total_spend,
            'yd_total_nc': _yd_total_nc, 'yd_total_nc_rev': _yd_total_nc_rev,
            'yd_total_repeat_rev': _yd_total_repeat_rev,
            'yd_nc_rev': _yd_nc_rev, 'yd_ret_rev': _yd_ret_rev,
            'yd_nc': _yd_nc, 'yd_spend': _yd_spend,
            'yd_amz_rev': _yd_amz_rev, 'yd_amz_spend': _yd_amz_spend,
            'yd_amz_nc': _yd_amz_nc, 'yd_amz_nc_rev': _yd_amz_nc_rev,
            'yd_amz_projected': _yd_amz_projected,
            'business_mer': _business_mer,
            'total_nc_roas': _total_nc_roas, 'total_nc_cpa': _total_nc_cpa,
            'dtc_nc_roas': _dtc_nc_roas, 'dtc_nc_aov': _dtc_nc_aov,
            'dtc_cpa': _dtc_cpa,
            'l7d_mer': _l7d_mer,
            'l7d_total_nc_roas': _l7d_total_nc_roas,
            'l7d_total_nc_cpa': _l7d_total_nc_cpa,
            'yd_mer': _yd_mer,
            'yd_total_nc_roas': _yd_total_nc_roas,
            'yd_total_nc_cpa': _yd_total_nc_cpa,
        }
        _save_result('pacing_data', pacing_result, MODEL_PACING, time.time() - t0)

        # Also save the cleaned GS spend data
        try:
            _gs = load_gs_spend()
            if not _gs.empty:
                _save_result('gs_spend_cleaned', json.loads(_gs.to_json()),
                             MODEL_PACING)
        except Exception as exc:
            logger.warning('[orchestrator] gs_spend_cleaned failed: %s', exc)

    return _run_model(MODEL_PACING, _compute, triggered_by)


def run_overview_data_precompute(triggered_by='scheduler'):
    """Pre-compute overview page data: Amazon daily merged.

    Saves:
      - amazon_daily_merged
    """
    def _compute():
        import pandas as pd
        from datetime import date, timedelta
        from analytics.daily_metrics import load_amazon_daily
        from analytics.amazon_nc_projector import project_amazon_nc

        # ---- 1. Amazon daily merged ----
        t0 = time.time()
        try:
            df = load_amazon_daily()
            if not df.empty:
                # Rename _amz_projected to _projected for overview compatibility
                if '_amz_projected' in df.columns:
                    df['_projected'] = df['_amz_projected']

                # Inject NC projection for yesterday
                yesterday_str = (date.today() - timedelta(days=1)).isoformat()
                yd_rows = df[df['sale_date'] == yesterday_str]
                yd_nc = int(yd_rows['_amz_new_cust'].sum()) if not yd_rows.empty else 0
                if yd_nc == 0:
                    try:
                        proj = project_amazon_nc()
                        if proj and proj.get('actual_nc_customers') is None and proj.get('projected_nc_customers', 0) > 0:
                            proj_nc = proj['projected_nc_customers']
                            proj_rev = proj['projected_nc_revenue']
                            if not yd_rows.empty:
                                idx = yd_rows.index[0]
                                yd_revenue = df.loc[idx, '_amz_revenue']
                                df.loc[idx, '_amz_new_cust'] = int(round(proj_nc))
                                df.loc[idx, '_amz_new_rev'] = proj_rev
                                df.loc[idx, '_amz_repeat_rev'] = max(yd_revenue - proj_rev, 0)
                                df.loc[idx, '_projected'] = True
                    except Exception:
                        pass

                _save_result('amazon_daily_merged', json.loads(df.to_json()),
                             MODEL_OVERVIEW_DATA, time.time() - t0)
        except Exception as exc:
            logger.warning('[orchestrator] amazon_daily_merged failed: %s', exc)

    return _run_model(MODEL_OVERVIEW_DATA, _compute, triggered_by)


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
    # Phase 3: cross-page pre-compute (depends on page_stats for master_dtc_forecast)
    results[MODEL_PACING] = run_pacing_precompute(triggered_by)
    results[MODEL_OVERVIEW_DATA] = run_overview_data_precompute(triggered_by)

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
