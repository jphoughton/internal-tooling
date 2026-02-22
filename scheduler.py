"""
Daily scheduler: runs ETL sync and forecast refresh on a schedule.
Can also be triggered manually.

Usage:
    python scheduler.py          # Run in daemon mode (stays alive, runs daily)
    python scheduler.py --now    # Run sync immediately and exit
    python scheduler.py --full   # Run full historical refresh and exit
"""
import sys
import schedule
import time
from datetime import datetime
from config import SYNC_HOUR, SYNC_MINUTE
from etl.sync import run_daily_sync


def daily_job():
    """The main daily job: sync data and log results."""
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


def run_daemon():
    """Run scheduler in daemon mode — keeps running and triggers daily."""
    sync_time = f"{SYNC_HOUR:02d}:{SYNC_MINUTE:02d}"
    schedule.every().day.at(sync_time).do(daily_job)

    print(f"Scheduler started. Daily sync scheduled at {sync_time}.")
    print(f"Press Ctrl+C to stop.\n")

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    if "--now" in sys.argv:
        print("Running immediate sync...")
        run_daily_sync(full_refresh=False)
    elif "--full" in sys.argv:
        print("Running full historical refresh...")
        run_daily_sync(full_refresh=True)
    else:
        run_daemon()
