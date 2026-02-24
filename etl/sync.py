"""
ETL orchestration: coordinates daily sync from all sources.

Supports parallel backfill via run_parallel_backfill() which splits
date ranges into chunks and processes them concurrently.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from db import get_db, init_db, rebuild_daily_sales, get_last_sync_date, log_sync
import config as cfg

logger = logging.getLogger(__name__)


def run_daily_sync(full_refresh=False, on_status=None):
    """
    Run a daily sync from all configured sources.

    Args:
        full_refresh: If True, pull all historical data (up to 365 days).
                     If False, pull only since last successful sync.
        on_status: Optional callback(step, total_steps, message) for progress.
    """
    init_db()

    has_amazon = all(getattr(cfg, k, "") for k in [
        "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
    ])
    has_shopify = bool(getattr(cfg, "SHOPIFY_ACCESS_TOKEN", ""))

    # Count total steps for progress
    steps = []
    if has_amazon:
        steps.extend(["amazon_sales", "amazon_retention"])
    if has_shopify:
        steps.append("shopify")
    steps.append("rebuild")
    total_steps = len(steps)

    def _report(step_idx, msg):
        if on_status:
            on_status(step_idx, total_steps, msg)

    results = {}
    step_idx = 0

    # --- Amazon (only if configured) ---
    if has_amazon:
        _report(step_idx, "Syncing Amazon sales...")
        try:
            results["amazon_sales"] = _sync_amazon(full_refresh)
        except Exception as e:
            results["amazon_sales"] = f"ERROR: {e}"
            logger.error("Amazon sales sync failed: %s", e)
        step_idx += 1

        _report(step_idx, "Syncing Amazon retention data...")
        try:
            results["amazon_retention"] = _sync_amazon_retention(full_refresh)
        except Exception as e:
            results["amazon_retention"] = f"ERROR: {e}"
            logger.error("Amazon retention sync failed: %s", e)
        step_idx += 1
    else:
        logger.warning("Amazon SP-API not configured — skipping.")

    # --- Shopify (only if configured) ---
    if has_shopify:
        _report(step_idx, "Testing Shopify connection...")
        from etl.shopify_client import test_connection
        ok, msg = test_connection()
        if not ok:
            results["shopify"] = f"ERROR: {msg}"
            logger.error("Shopify connection failed: %s", msg)
            with get_db() as conn:
                log_sync(conn, "shopify", datetime.utcnow().strftime("%Y-%m-%d"),
                         0, status="error", error_message=msg)
        else:
            logger.info("Shopify connection OK: %s", msg)
            _report(step_idx, "Syncing Shopify orders...")
            try:
                results["shopify"] = _sync_shopify(full_refresh, on_status=on_status, step_idx=step_idx, total_steps=total_steps)
            except Exception as e:
                results["shopify"] = f"ERROR: {e}"
                logger.error("Shopify sync failed: %s", e)
                with get_db() as conn:
                    log_sync(conn, "shopify", datetime.utcnow().strftime("%Y-%m-%d"),
                             0, status="error", error_message=str(e))
        step_idx += 1
    else:
        logger.warning("Shopify not configured — skipping.")

    # --- Klaviyo (only if configured) ---
    with get_db() as conn:
        from db import get_setting
        _klaviyo_key = get_setting(conn, "klaviyo_api_key", "")
    if _klaviyo_key:
        _report(step_idx, "Syncing Klaviyo...")
        try:
            results["klaviyo"] = _sync_klaviyo(_klaviyo_key)
        except Exception as e:
            results["klaviyo"] = f"ERROR: {e}"
            logger.error("Klaviyo sync failed: %s", e)
            with get_db() as conn:
                log_sync(conn, "klaviyo", datetime.utcnow().strftime("%Y-%m-%d"),
                         0, status="error", error_message=str(e))
        step_idx += 1
    else:
        logger.warning("Klaviyo not configured — skipping.")

    # --- Google Sheet (if configured) ---
    _report(step_idx, "Syncing Google Sheet...")
    try:
        from db import get_setting
        with get_db() as conn:
            _gs_id = get_setting(conn, "google_sheet_id")
            _gs_gid = get_setting(conn, "google_sheet_gid")
        if _gs_id:
            from etl.google_sheets import sync_google_sheet
            with get_db() as conn:
                gs_count = sync_google_sheet(conn, _gs_id, _gs_gid)
                if gs_count > 0:
                    from db import set_setting
                    set_setting(conn, "google_sheet_last_sync",
                                datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
                results["google_sheet"] = gs_count
                logger.info("Google Sheet: synced %d rows", gs_count)
        else:
            logger.warning("Google Sheet not configured — skipping.")
    except Exception as e:
        results["google_sheet"] = f"ERROR: {e}"
        logger.error("Google Sheet sync failed: %s", e)

    # --- Rebuild aggregates ---
    _report(step_idx, "Rebuilding sales aggregates...")
    logger.info("Rebuilding daily sales aggregates...")
    with get_db() as conn:
        rebuild_daily_sales(conn)

    _report(total_steps, "Sync complete!")
    logger.info("Sync complete: %s", results)
    return results


def _sync_amazon(full_refresh):
    """Pull Amazon order data for demand forecasting via Sales & Traffic report."""
    from etl.amazon import fetch_sales_report

    with get_db() as conn:
        since = _get_since_date(conn, "amazon", full_refresh, max_days=30)
        today = datetime.utcnow().strftime("%Y-%m-%d")

        logger.info("Amazon sales sync: %s to %s", since, today)
        count = fetch_sales_report(conn, since_date=since, until_date=today)
        log_sync(conn, "amazon", today, count)

    return count


def _sync_amazon_retention(full_refresh):
    """Pull Amazon fulfillment data for customer retention tracking."""
    from etl.amazon import fetch_fulfillment_data

    with get_db() as conn:
        since = _get_since_date(conn, "amazon_retention", full_refresh, max_days=30)
        today = datetime.utcnow().strftime("%Y-%m-%d")

        logger.info("Amazon retention sync: %s to %s", since, today)
        count = fetch_fulfillment_data(conn, since_date=since, until_date=today)
        log_sync(conn, "amazon_retention", today, count)

    return count


def _sync_shopify(full_refresh, on_status=None, step_idx=0, total_steps=1):
    """Pull Shopify orders (with customer data for retention + line items for demand)."""
    from etl.shopify_client import fetch_orders

    def _order_progress(orders_so_far, page_number):
        if on_status:
            on_status(step_idx, total_steps, f"Shopify: {orders_so_far:,} orders fetched (page {page_number})...")

    with get_db() as conn:
        since = _get_since_date(conn, "shopify", full_refresh)
        today = datetime.utcnow().strftime("%Y-%m-%d")

        logger.info("Shopify sync: %s to %s", since, today)
        count = fetch_orders(conn, since_date=since, until_date=today, on_progress=_order_progress)
        log_sync(conn, "shopify", today, count)

    return count


def _sync_klaviyo(api_key):
    """Pull Klaviyo campaigns, flows, lists, and performance metrics into the DB."""
    from etl.klaviyo_client import (
        fetch_campaigns, fetch_flows, fetch_lists,
        fetch_placed_order_metric_id, fetch_any_metric_id,
        fetch_campaign_metrics, fetch_flow_metrics,
    )
    from db import (
        upsert_klaviyo_campaign, upsert_klaviyo_flow, upsert_klaviyo_list,
        update_klaviyo_campaign_metrics, update_klaviyo_flow_metrics,
        get_setting, set_setting,
    )

    # --- Metadata ---
    campaigns = fetch_campaigns(api_key)
    flows = fetch_flows(api_key)
    lists = fetch_lists(api_key)

    with get_db() as conn:
        for c in campaigns:
            upsert_klaviyo_campaign(conn, c)
        for f in flows:
            upsert_klaviyo_flow(conn, f)
        for l in lists:
            upsert_klaviyo_list(conn, l)

    logger.info("Klaviyo metadata: %d campaigns, %d flows, %d lists", len(campaigns), len(flows), len(lists))

    # --- Performance metrics ---
    with get_db() as conn:
        conversion_metric_id = get_setting(conn, "klaviyo_conversion_metric_id")

    include_revenue = bool(conversion_metric_id)
    if not conversion_metric_id:
        conversion_metric_id = fetch_placed_order_metric_id(api_key)
        if conversion_metric_id:
            include_revenue = True
            with get_db() as conn:
                set_setting(conn, "klaviyo_conversion_metric_id", conversion_metric_id)
            logger.info("Auto-detected Placed Order metric: %s", conversion_metric_id)
        else:
            # Fallback: any metric ID (API requires one even for engagement stats)
            conversion_metric_id = fetch_any_metric_id(api_key)
            if conversion_metric_id:
                logger.warning("Using fallback metric for engagement stats: %s", conversion_metric_id)

    try:
        campaign_metrics = fetch_campaign_metrics(api_key, conversion_metric_id, include_revenue)
        flow_metrics = fetch_flow_metrics(api_key, conversion_metric_id, include_revenue)

        with get_db() as conn:
            for cid, metrics in campaign_metrics.items():
                update_klaviyo_campaign_metrics(conn, cid, metrics)
            for fid, metrics in flow_metrics.items():
                update_klaviyo_flow_metrics(conn, fid, metrics)

        logger.info("Klaviyo metrics: %d campaigns, %d flows", len(campaign_metrics), len(flow_metrics))
    except Exception as e:
        logger.error("Klaviyo metrics fetch failed (metadata still saved): %s", e)

    total = len(campaigns) + len(flows) + len(lists)
    with get_db() as conn:
        log_sync(conn, "klaviyo", datetime.utcnow().strftime("%Y-%m-%d"), total)

    return total


def _get_since_date(conn, source, full_refresh, max_days=365 * 5):
    """Determine the start date for a sync.

    Args:
        max_days: Maximum number of days to look back (default 5 years).
                  Amazon reports typically limit to ~365 days.
    """
    if full_refresh:
        return (datetime.utcnow() - timedelta(days=max_days)).strftime("%Y-%m-%d")

    last = get_last_sync_date(conn, source)
    if last:
        # Overlap by 1 day to catch any late-arriving data
        return (datetime.strptime(last, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        # First sync: pull all available history
        return (datetime.utcnow() - timedelta(days=max_days)).strftime("%Y-%m-%d")


def _date_chunks(start_date, end_date, chunk_months=3):
    """Split a date range into chunks of N months. Returns list of (start, end) string tuples."""
    chunks = []
    current = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')
    while current < end:
        chunk_end = current + timedelta(days=chunk_months * 30)
        if chunk_end > end:
            chunk_end = end
        chunks.append((current.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')))
        current = chunk_end + timedelta(days=1)
    return chunks


def _backfill_amazon_sales_chunk(since, until, chunk_label):
    """Backfill Amazon sales for a single date chunk."""
    from etl.amazon import fetch_sales_report
    logger.info('[Amazon Sales %s] %s → %s', chunk_label, since, until)
    try:
        with get_db() as conn:
            count = fetch_sales_report(conn, since_date=since, until_date=until)
        logger.info('[Amazon Sales %s] Done: %d records', chunk_label, count)
        return ('amazon_sales', chunk_label, count)
    except Exception as e:
        logger.error('[Amazon Sales %s] ERROR: %s', chunk_label, e)
        return ('amazon_sales', chunk_label, f'ERROR: {e}')


def _backfill_amazon_retention_chunk(since, until, chunk_label):
    """Backfill Amazon retention for a single date chunk."""
    from etl.amazon import fetch_fulfillment_data
    logger.info('[Amazon Retention %s] %s → %s', chunk_label, since, until)
    try:
        with get_db() as conn:
            count = fetch_fulfillment_data(conn, since_date=since, until_date=until)
        logger.info('[Amazon Retention %s] Done: %d records', chunk_label, count)
        return ('amazon_retention', chunk_label, count)
    except Exception as e:
        logger.error('[Amazon Retention %s] ERROR: %s', chunk_label, e)
        return ('amazon_retention', chunk_label, f'ERROR: {e}')


def _backfill_shopify_chunk(since, until, chunk_label):
    """Backfill Shopify orders for a single date chunk, committing per page."""
    from etl.shopify_client import fetch_orders

    def _progress(orders_so_far, page_number):
        logger.info('[Shopify %s] %d orders (page %d)', chunk_label, orders_so_far, page_number)

    logger.info('[Shopify %s] %s → %s', chunk_label, since, until)
    try:
        with get_db() as conn:
            count = fetch_orders(conn, since_date=since, until_date=until,
                                 on_progress=_progress, commit_per_page=True)
        logger.info('[Shopify %s] Done: %d orders', chunk_label, count)
        return ('shopify', chunk_label, count)
    except Exception as e:
        logger.error('[Shopify %s] ERROR: %s', chunk_label, e)
        return ('shopify', chunk_label, f'ERROR: {e}')


def run_parallel_backfill(shopify_years=4, amazon_months=6,
                          amazon_workers=2, shopify_workers=3,
                          chunk_months=3):
    """
    Parallel backfill of historical data.

    Splits the date range into chunks and processes them concurrently:
    - Amazon Sales: N workers, last amazon_months only (S&T data limited)
    - Amazon Retention: N workers, last amazon_months
    - Shopify: M workers, last shopify_years

    Args:
        shopify_years: How many years of Shopify data to backfill (default 4).
        amazon_months: How many months of Amazon data to backfill (default 6).
        amazon_workers: Max concurrent Amazon API threads (default 2).
                       Keep <=3 to avoid SP-API QuotaExceeded errors.
        shopify_workers: Max concurrent Shopify API threads (default 3).
                        Keep <=4 to respect leaky bucket rate limit.
        chunk_months: Size of each date chunk in months (default 3).
    """
    init_db()

    end_date = datetime.utcnow().strftime('%Y-%m-%d')
    shopify_start = (datetime.utcnow() - timedelta(days=shopify_years * 365)).strftime('%Y-%m-%d')
    amazon_start = (datetime.utcnow() - timedelta(days=amazon_months * 30)).strftime('%Y-%m-%d')

    shopify_chunks = _date_chunks(shopify_start, end_date, chunk_months)
    amazon_chunks = _date_chunks(amazon_start, end_date, chunk_months)

    logger.info(
        'PARALLEL BACKFILL — Amazon: %s → %s (%d chunks), Shopify: %s → %s (%d chunks), '
        'Amazon workers: %d, Shopify workers: %d',
        amazon_start, end_date, len(amazon_chunks),
        shopify_start, end_date, len(shopify_chunks),
        amazon_workers, shopify_workers,
    )

    has_amazon = all(getattr(cfg, k, '') for k in [
        'AMAZON_REFRESH_TOKEN', 'AMAZON_LWA_CLIENT_ID', 'AMAZON_LWA_CLIENT_SECRET',
    ])
    has_shopify = bool(getattr(cfg, 'SHOPIFY_ACCESS_TOKEN', ''))

    all_results = []
    start_time = datetime.now()

    # --- Shopify first (parallel chunks) ---
    if has_shopify:
        from etl.shopify_client import test_connection
        ok, msg = test_connection()
        if not ok:
            logger.error('Shopify connection failed: %s', msg)
        else:
            logger.info('Shopify connection OK: %s', msg)
            logger.info('Starting Shopify backfill (%d chunks, %d workers)...', len(shopify_chunks), shopify_workers)
            futures = []
            with ThreadPoolExecutor(max_workers=shopify_workers, thread_name_prefix='shopify') as executor:
                for i, (chunk_start, chunk_end) in enumerate(shopify_chunks):
                    label = f'{i+1}/{len(shopify_chunks)}'
                    futures.append(executor.submit(_backfill_shopify_chunk, chunk_start, chunk_end, label))

                for future in as_completed(futures):
                    all_results.append(future.result())
    else:
        logger.warning('Shopify not configured — skipping.')

    # --- Amazon Sales (parallel chunks) ---
    if has_amazon:
        futures = []
        logger.info('Starting Amazon Sales backfill (%d chunks, %d workers)...', len(amazon_chunks), amazon_workers)
        with ThreadPoolExecutor(max_workers=amazon_workers, thread_name_prefix='amz-sales') as executor:
            for i, (chunk_start, chunk_end) in enumerate(amazon_chunks):
                label = f'{i+1}/{len(amazon_chunks)}'
                futures.append(executor.submit(_backfill_amazon_sales_chunk, chunk_start, chunk_end, label))

            for future in as_completed(futures):
                all_results.append(future.result())

        # --- Amazon Retention (parallel chunks) ---
        futures = []
        logger.info('Starting Amazon Retention backfill (%d chunks, %d workers)...', len(amazon_chunks), amazon_workers)
        with ThreadPoolExecutor(max_workers=amazon_workers, thread_name_prefix='amz-ret') as executor:
            for i, (chunk_start, chunk_end) in enumerate(amazon_chunks):
                label = f'{i+1}/{len(amazon_chunks)}'
                futures.append(executor.submit(_backfill_amazon_retention_chunk, chunk_start, chunk_end, label))

            for future in as_completed(futures):
                all_results.append(future.result())
    else:
        logger.warning('Amazon SP-API not configured — skipping.')

    # --- Rebuild aggregates ---
    logger.info('Rebuilding daily sales aggregates...')
    with get_db() as conn:
        rebuild_daily_sales(conn)
        log_sync(conn, 'backfill', end_date, sum(
            r[2] for r in all_results if isinstance(r[2], int)
        ))

    elapsed = datetime.now() - start_time

    totals = {}
    errors = []
    for source, label, result in all_results:
        if isinstance(result, int):
            totals[source] = totals.get(source, 0) + result
        else:
            errors.append(f'{source} {label}: {result}')

    logger.info('BACKFILL COMPLETE in %s', elapsed)
    for source, total in sorted(totals.items()):
        logger.info('  %s: %d records', source, total)
    if errors:
        logger.error('Backfill errors (%d): %s', len(errors), errors)

    return totals


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    results = run_daily_sync()
    logger.info('Sync results: %s', results)
