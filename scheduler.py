"""
Daily scheduler: runs ETL sync and forecast refresh on a schedule.
Can also be triggered manually.

Usage:
    python scheduler.py              # Run in daemon mode (stays alive, runs daily)
    python scheduler.py --now        # Run sync immediately and exit
    python scheduler.py --full       # Run full historical refresh and exit
    python scheduler.py --backfill   # Parallel backfill (Shopify 4y, Amazon 6mo)
    python scheduler.py --backfill --shopify-years 2 --amazon-months 3
"""
import argparse
import sys
import schedule
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from config import SYNC_HOUR, SYNC_MINUTE, SYNC_TIMEZONE
from etl.sync import run_daily_sync, run_parallel_backfill, run_amazon_catchup


def daily_job():
    """The main daily job: sync data, then run analytics models."""
    print(f"\n{'='*60}")
    print(f"Daily sync started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    try:
        results = run_daily_sync()
        print(f"\nDaily sync completed successfully: {results}")
    except Exception as e:
        print(f"\nDaily sync failed: {e}")
        import traceback
        traceback.print_exc()

    # Run daily analytics models after ETL sync
    print(f"\n{'='*60}")
    print(f"Daily model runs starting...")
    print(f"{'='*60}")
    try:
        from analytics.orchestrator import run_all_daily_models
        model_results = run_all_daily_models(triggered_by='scheduler')
        print(f"\nDaily model runs completed: {model_results}")
    except Exception as e:
        print(f"\nDaily model runs failed: {e}")
        import traceback
        traceback.print_exc()


def amazon_catchup_job():
    """Lightweight Amazon-only re-sync to catch delayed S&T report data."""
    print(f"\n--- Amazon catchup at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    try:
        count = run_amazon_catchup()
        print(f"Amazon catchup: {count} records synced")
    except Exception as e:
        print(f"Amazon catchup failed: {e}")


def run_daemon():
    """Run scheduler in daemon mode — keeps running and triggers daily at 5 AM PST.

    Also runs Amazon-only catchup syncs at 11 AM and 5 PM PST to pick up
    delayed S&T report data (Amazon has 24-48h reporting lag).
    """
    sync_time = f"{SYNC_HOUR:02d}:{SYNC_MINUTE:02d}"
    schedule.every().day.at(sync_time, SYNC_TIMEZONE).do(daily_job)

    # Amazon catchup syncs — S&T data arrives with a lag, retry twice more daily
    schedule.every().day.at("11:00", SYNC_TIMEZONE).do(amazon_catchup_job)
    schedule.every().day.at("17:00", SYNC_TIMEZONE).do(amazon_catchup_job)

    print(f"Scheduler started. Daily sync at {sync_time} {SYNC_TIMEZONE}.")
    print(f"Amazon catchup syncs at 11:00 and 17:00 {SYNC_TIMEZONE}.")
    print(f"Press Ctrl+C to stop.\n")

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    if "--backfill" in sys.argv:
        parser = argparse.ArgumentParser()
        parser.add_argument('--backfill', action='store_true')
        parser.add_argument('--shopify-years', type=int, default=4,
                            help='Years of Shopify data to backfill (default: 4)')
        parser.add_argument('--amazon-months', type=int, default=6,
                            help='Months of Amazon data to backfill (default: 6)')
        parser.add_argument('--amazon-workers', type=int, default=2,
                            help='Concurrent Amazon API threads (default: 2, max ~3)')
        parser.add_argument('--shopify-workers', type=int, default=3,
                            help='Concurrent Shopify API threads (default: 3)')
        parser.add_argument('--chunk-months', type=int, default=3,
                            help='Months per chunk (default: 3)')
        args = parser.parse_args()
        print(f"Starting parallel backfill: "
              f"Shopify {args.shopify_years}y, Amazon {args.amazon_months}mo, "
              f"{args.amazon_workers} Amazon workers, "
              f"{args.shopify_workers} Shopify workers...")
        run_parallel_backfill(
            shopify_years=args.shopify_years,
            amazon_months=args.amazon_months,
            amazon_workers=args.amazon_workers,
            shopify_workers=args.shopify_workers,
            chunk_months=args.chunk_months,
        )
    elif "--now" in sys.argv:
        print("Running immediate sync...")
        run_daily_sync(full_refresh=False)
    elif "--full" in sys.argv:
        print("Running full historical refresh...")
        run_daily_sync(full_refresh=True)
    else:
        run_daemon()
