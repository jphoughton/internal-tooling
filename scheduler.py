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
import logging
import sys
import schedule
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from config import SYNC_HOUR, SYNC_MINUTE, SYNC_TIMEZONE
from etl.sync import run_daily_sync, run_parallel_backfill

logger = logging.getLogger(__name__)


def daily_job():
    """The main daily job: sync data and log results."""
    logger.info("Daily sync started at %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    try:
        results = run_daily_sync()
        logger.info("Daily sync completed successfully: %s", results)
    except Exception as e:
        logger.error("Daily sync failed: %s", e, exc_info=True)


def run_daemon():
    """Run scheduler in daemon mode — keeps running and triggers daily at 5 AM PST."""
    sync_time = f"{SYNC_HOUR:02d}:{SYNC_MINUTE:02d}"
    schedule.every().day.at(sync_time, SYNC_TIMEZONE).do(daily_job)

    logger.info("Scheduler started. Daily sync scheduled at %s %s.", sync_time, SYNC_TIMEZONE)
    logger.info("Press Ctrl+C to stop.")

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if "--backfill" in sys.argv:
        parser = argparse.ArgumentParser()
        parser.add_argument("--backfill", action="store_true")
        parser.add_argument(
            "--shopify-years", type=int, default=4, help="Years of Shopify data to backfill (default: 4)"
        )
        parser.add_argument(
            "--amazon-months", type=int, default=6, help="Months of Amazon data to backfill (default: 6)"
        )
        parser.add_argument(
            "--amazon-workers", type=int, default=2, help="Concurrent Amazon API threads (default: 2, max ~3)"
        )
        parser.add_argument(
            "--shopify-workers", type=int, default=3, help="Concurrent Shopify API threads (default: 3)"
        )
        parser.add_argument("--chunk-months", type=int, default=3, help="Months per chunk (default: 3)")
        args = parser.parse_args()
        logger.info(
            "Starting parallel backfill: Shopify %dy, Amazon %dmo, %d Amazon workers, %d Shopify workers...",
            args.shopify_years,
            args.amazon_months,
            args.amazon_workers,
            args.shopify_workers,
        )
        run_parallel_backfill(
            shopify_years=args.shopify_years,
            amazon_months=args.amazon_months,
            amazon_workers=args.amazon_workers,
            shopify_workers=args.shopify_workers,
            chunk_months=args.chunk_months,
        )
    elif "--now" in sys.argv:
        logger.info("Running immediate sync...")
        run_daily_sync(full_refresh=False)
    elif "--full" in sys.argv:
        logger.info("Running full historical refresh...")
        run_daily_sync(full_refresh=True)
    else:
        run_daemon()
