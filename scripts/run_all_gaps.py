#!/usr/bin/env python3
"""Run targeted backfill for all gap months sequentially.

Each month gets its own process to avoid long-running connection issues.
Logs progress to /tmp/gap_backfill_progress.log.
"""
import sys
import os
import time
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

# All gap months to process
GAP_MONTHS = [
    (2020, 6), (2020, 7), (2020, 8), (2020, 9), (2020, 10),
    (2020, 11), (2020, 12),
    (2021, 1), (2021, 2), (2021, 3),
    (2021, 7), (2021, 8),
    (2023, 1), (2023, 8), (2023, 11),
]

LOG = "/tmp/gap_backfill_progress.log"
VENV = "/Users/joshhoughton/Desktop/Inventory/venv/bin/activate"
PROJECT = "/Users/joshhoughton/internal-tooling"


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def run_month(year, month):
    """Run backfill for one month as a subprocess."""
    ms = f"{year}-{month:02d}"
    log(f"Starting {ms}...")

    cmd = f"""
cd {PROJECT} && source {VENV} && python3 -u -c "
import sys, os
sys.path.insert(0, '.')
os.chdir('{PROJECT}')
from dotenv import load_dotenv; load_dotenv()
from scripts.targeted_backfill import backfill_month, db_count, shopify_count
import calendar
last_day = calendar.monthrange({year}, {month})[1]
sc = shopify_count('{year}-{month:02d}-01', '{year}-{month:02d}-' + str(last_day))
dc = db_count('{ms}')
if sc - dc < 50:
    print(f'{ms}: OK (DB={{dc}}, Shopify={{sc}}, gap={{sc-dc}})', flush=True)
else:
    dc2, sc2 = backfill_month({year}, {month})
    print(f'{ms}: DB={{dc2}}, Shopify={{sc2}} ({{dc2/sc2*100:.1f}}%)', flush=True)
"
"""
    t0 = time.time()
    result = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True, text=True, timeout=7200,  # 2hr max per month
    )

    elapsed = time.time() - t0
    if result.returncode == 0:
        # Get last line of stdout for summary
        lines = result.stdout.strip().split("\n")
        summary = lines[-1] if lines else "no output"
        log(f"  {ms} DONE in {elapsed:.0f}s: {summary}")
    else:
        log(f"  {ms} FAILED in {elapsed:.0f}s: {result.stderr[-500:]}")

    return result.returncode == 0


def main():
    log("=" * 55)
    log("GAP BACKFILL — ALL MONTHS")
    log("=" * 55)

    t0 = time.time()
    ok = fail = 0

    for year, month in GAP_MONTHS:
        try:
            success = run_month(year, month)
            if success:
                ok += 1
            else:
                fail += 1
        except Exception as e:
            log(f"  Exception: {e}")
            fail += 1
        time.sleep(2)

    elapsed = time.time() - t0
    log("=" * 55)
    log(f"DONE: {ok} OK, {fail} FAILED in {elapsed/60:.1f} min")

    # Run validation
    log("Running validation...")
    result = subprocess.run(
        ["bash", "-c",
         f"cd {PROJECT} && source {VENV} && python3 -u scripts/validate_waterfall.py"],
        capture_output=True, text=True, timeout=300,
    )
    log(result.stdout[-2000:] if result.stdout else "no output")
    if result.stderr:
        log(f"Validation errors: {result.stderr[-500:]}")


if __name__ == "__main__":
    main()
