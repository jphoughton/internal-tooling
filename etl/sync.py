"""
ETL orchestration: coordinates daily sync from all sources.
"""
from datetime import datetime, timedelta
from db import get_db, init_db, rebuild_daily_sales, get_last_sync_date, log_sync
import config as cfg


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
            print(f"Amazon sales sync failed: {e}")
        step_idx += 1

        _report(step_idx, "Syncing Amazon retention data...")
        try:
            results["amazon_retention"] = _sync_amazon_retention(full_refresh)
        except Exception as e:
            results["amazon_retention"] = f"ERROR: {e}"
            print(f"Amazon retention sync failed: {e}")
        step_idx += 1
    else:
        print("Amazon SP-API not configured — skipping.")

    # --- Shopify (only if configured) ---
    if has_shopify:
        _report(step_idx, "Testing Shopify connection...")
        from etl.shopify_client import test_connection
        ok, msg = test_connection()
        if not ok:
            results["shopify"] = f"ERROR: {msg}"
            print(f"Shopify connection failed: {msg}")
            with get_db() as conn:
                log_sync(conn, "shopify", datetime.utcnow().strftime("%Y-%m-%d"),
                         0, status="error", error_message=msg)
        else:
            print(f"Shopify connection OK: {msg}")
            _report(step_idx, "Syncing Shopify orders...")
            try:
                results["shopify"] = _sync_shopify(full_refresh, on_status=on_status, step_idx=step_idx, total_steps=total_steps)
            except Exception as e:
                results["shopify"] = f"ERROR: {e}"
                print(f"Shopify sync failed: {e}")
                with get_db() as conn:
                    log_sync(conn, "shopify", datetime.utcnow().strftime("%Y-%m-%d"),
                             0, status="error", error_message=str(e))
        step_idx += 1
    else:
        print("Shopify not configured — skipping.")

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
                print(f"Google Sheet: synced {gs_count} rows")
        else:
            print("Google Sheet not configured — skipping.")
    except Exception as e:
        results["google_sheet"] = f"ERROR: {e}"
        print(f"Google Sheet sync failed: {e}")

    # --- Rebuild aggregates ---
    _report(step_idx, "Rebuilding sales aggregates...")
    print("Rebuilding daily sales aggregates...")
    with get_db() as conn:
        rebuild_daily_sales(conn)

    _report(total_steps, "Sync complete!")
    print(f"\nSync complete: {results}")
    return results


def _sync_amazon(full_refresh):
    """Pull Amazon order data for demand forecasting.

    Uses the flat-file all-orders report as the primary source because:
    - It includes both SKU and ASIN columns (needed for SKU mapping)
    - It handles longer date ranges than Sales & Traffic reports
    - It provides order-level detail
    """
    from etl.amazon import fetch_flat_file_orders

    with get_db() as conn:
        # Amazon flat-file reports only return ~30 days of data
        since = _get_since_date(conn, "amazon", full_refresh, max_days=30)
        today = datetime.utcnow().strftime("%Y-%m-%d")

        print(f"Amazon sales sync: {since} to {today}")
        count = fetch_flat_file_orders(conn, since_date=since, until_date=today)
        log_sync(conn, "amazon", today, count)

    return count


def _sync_amazon_retention(full_refresh):
    """Pull Amazon fulfillment data for customer retention tracking."""
    from etl.amazon import fetch_fulfillment_data

    with get_db() as conn:
        since = _get_since_date(conn, "amazon_retention", full_refresh, max_days=365)
        today = datetime.utcnow().strftime("%Y-%m-%d")

        print(f"Amazon retention sync: {since} to {today}")
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

        print(f"Shopify sync: {since} to {today}")
        count = fetch_orders(conn, since_date=since, until_date=today, on_progress=_order_progress)
        log_sync(conn, "shopify", today, count)

    return count


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


if __name__ == "__main__":
    results = run_daily_sync()
    print(f"Sync results: {results}")
