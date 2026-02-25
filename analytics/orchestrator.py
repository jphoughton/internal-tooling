"""
Analytics model orchestrator.

Runs analytics models (retention cohorts, repeat forecast, seasonality
adjustment, SKU-level % of sales) on a daily schedule and triggers
event-driven waterfall reruns when new repeat data arrives.

Models:
    retention_cohorts   — rebuild cohort retention matrix
    repeat_forecast     — waterfall demand split (new + repeat)
    seasonality         — seasonal index adjustment
    sku_sales_mix       — SKU-level % of sales from recent orders
    waterfall           — full waterfall build (event-driven on new repeat data)
"""
import logging
import time
from datetime import datetime
from db import get_db, log_model_run, get_model_run

logger = logging.getLogger(__name__)

# Model names used as keys in model_runs table
MODEL_RETENTION = 'retention_cohorts'
MODEL_REPEAT_FORECAST = 'repeat_forecast'
MODEL_SEASONALITY = 'seasonality'
MODEL_SKU_MIX = 'sku_sales_mix'
MODEL_WATERFALL = 'waterfall'

ALL_DAILY_MODELS = [
    MODEL_RETENTION,
    MODEL_REPEAT_FORECAST,
    MODEL_SEASONALITY,
    MODEL_SKU_MIX,
    MODEL_WATERFALL,
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


def run_retention_cohorts(triggered_by='scheduler'):
    """Recompute retention cohort matrix and cache it."""
    def _compute():
        from analytics.retention import get_customer_cohort_data
        # Run for all sources (no filter) to prime the cache
        get_customer_cohort_data(source_filter=None)
        # Also run for shopify specifically (used by waterfall)
        get_customer_cohort_data(source_filter='shopify')
    return _run_model(MODEL_RETENTION, _compute, triggered_by)


def run_repeat_forecast(triggered_by='scheduler'):
    """Recompute the average retention curve and AOV/units metrics."""
    def _compute():
        from analytics.waterfall import (
            get_average_retention_curve,
            get_aov_and_units,
            get_monthly_new_customers,
            clear_waterfall_cache,
        )
        # Clear stale cache before recomputing
        clear_waterfall_cache()
        # Recompute core metrics (no filter = all sources)
        get_average_retention_curve(source_filter=None)
        get_aov_and_units(source_filter=None)
        get_monthly_new_customers(source_filter=None)
        # Also for shopify
        get_average_retention_curve(source_filter='shopify')
        get_aov_and_units(source_filter='shopify')
        get_monthly_new_customers(source_filter='shopify')
    return _run_model(MODEL_REPEAT_FORECAST, _compute, triggered_by)


def run_seasonality(triggered_by='scheduler'):
    """Reload seasonal indices from DB (validates they exist and are sane)."""
    def _compute():
        from db import get_seasonal_indices
        with get_db() as conn:
            indices = get_seasonal_indices(conn)
        if not indices:
            logger.warning('[orchestrator] No seasonal indices found in DB')
        else:
            # Sanity check: all 12 months should be present
            missing = [m for m in range(1, 13) if m not in indices]
            if missing:
                logger.warning('[orchestrator] Missing seasonal indices for months: %s', missing)
    return _run_model(MODEL_SEASONALITY, _compute, triggered_by)


def run_sku_sales_mix(triggered_by='scheduler'):
    """Recompute SKU % of sales mix from recent order data."""
    def _compute():
        from analytics.waterfall import _get_sku_mix
        # Recompute for all sources and shopify
        _get_sku_mix(source_filter=None, lookback_months=3)
        _get_sku_mix(source_filter='shopify', lookback_months=3)
    return _run_model(MODEL_SKU_MIX, _compute, triggered_by)


def run_waterfall(triggered_by='scheduler'):
    """Build full waterfall forecast using current media plan and seasonality."""
    def _compute():
        import json
        from analytics.waterfall import build_waterfall, clear_waterfall_cache
        from db import get_media_spend, get_seasonal_indices, get_setting

        clear_waterfall_cache()

        with get_db() as conn:
            media_plan = get_media_spend(conn, source='All Sources')
            enabled = get_setting(conn, 'seasonality_enabled', 'true')
            seasonal = None
            if enabled == 'true':
                indices = get_seasonal_indices(conn)
                if indices:
                    seasonal = indices

        if not media_plan:
            logger.warning('[orchestrator] No media plan found, skipping waterfall build')
            return

        build_waterfall(media_plan, source_filter=None, horizon_months=12,
                        seasonal_indices=seasonal)

    return _run_model(MODEL_WATERFALL, _compute, triggered_by)


def run_all_daily_models(triggered_by='scheduler'):
    """Run all daily analytics models in dependency order.

    Order matters: retention → repeat_forecast → seasonality → sku_mix → waterfall.
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
    results[MODEL_WATERFALL] = run_waterfall(triggered_by)
    return results
