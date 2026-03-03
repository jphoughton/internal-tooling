"""
Targeted Shopify backfill for specific months with missing order data.

Identified gap months (zero orders in DB):
  2022-12, 2023-03, 2023-04, 2023-06, 2023-07, 2023-09, 2023-10

Usage:
    python scripts/backfill_gaps.py          # backfill all gap months
    python scripts/backfill_gaps.py --dry-run # show what would be fetched
"""
import sys
import os
import calendar
from datetime import datetime

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from db import get_db, init_db, rebuild_daily_sales, log_sync, read_sql
from etl.shopify_client import fetch_orders, test_connection

# Months with zero orders in our database
GAP_MONTHS = [
    '2022-12',
    '2023-03',
    '2023-04',
    '2023-06',
    '2023-07',
    '2023-09',
    '2023-10',
]


def month_date_range(year_month):
    """Return (first_day, last_day) as YYYY-MM-DD strings."""
    year, month = map(int, year_month.split('-'))
    last_day = calendar.monthrange(year, month)[1]
    return f'{year_month}-01', f'{year_month}-{last_day:02d}'


def check_existing_orders(conn, year_month):
    """Count orders already in DB for a given month."""
    start, end = month_date_range(year_month)
    result = read_sql(
        "SELECT COUNT(*) as cnt FROM orders WHERE order_date BETWEEN ? AND ?",
        conn, params=[start, end],
    )
    return int(result.iloc[0]['cnt']) if not result.empty else 0


def backfill_month(conn, year_month):
    """Fetch Shopify orders for a single month. Returns order count."""
    start, end = month_date_range(year_month)

    def _progress(orders_so_far, page_number):
        print(f'  [{year_month}] {orders_so_far:,} orders (page {page_number})')

    count = fetch_orders(
        conn,
        since_date=start,
        until_date=end,
        on_progress=_progress,
        commit_per_page=True,
    )
    return count


def main():
    dry_run = '--dry-run' in sys.argv

    init_db()

    # Test Shopify connection
    ok, msg = test_connection()
    if not ok:
        print(f'Shopify connection failed: {msg}')
        sys.exit(1)
    print(f'Shopify connection OK: {msg}\n')

    # Check current state
    print(f'Gap months to backfill: {len(GAP_MONTHS)}')
    print('-' * 50)

    with get_db() as conn:
        for ym in GAP_MONTHS:
            existing = check_existing_orders(conn, ym)
            start, end = month_date_range(ym)
            print(f'  {ym}: {existing:,} orders in DB  (will fetch {start} → {end})')

    if dry_run:
        print('\n--dry-run: no changes made.')
        return

    print(f'\n{"=" * 50}')
    print('Starting targeted backfill...')
    print(f'{"=" * 50}\n')

    start_time = datetime.now()
    results = {}

    for ym in GAP_MONTHS:
        print(f'[{ym}] Fetching orders...')
        try:
            with get_db() as conn:
                count = backfill_month(conn, ym)
                results[ym] = count
                print(f'[{ym}] Done: {count} orders fetched\n')
        except Exception as e:
            results[ym] = f'ERROR: {e}'
            print(f'[{ym}] ERROR: {e}\n')

    # Rebuild daily sales aggregates
    print('Rebuilding daily sales aggregates...')
    with get_db() as conn:
        rebuild_daily_sales(conn)
        total_fetched = sum(v for v in results.values() if isinstance(v, int))
        log_sync(conn, 'backfill-gaps', datetime.utcnow().strftime('%Y-%m-%d'), total_fetched)

    elapsed = datetime.now() - start_time

    # Summary
    print(f'\n{"=" * 50}')
    print(f'BACKFILL COMPLETE in {elapsed}')
    print(f'{"=" * 50}')

    errors = []
    for ym, result in results.items():
        if isinstance(result, int):
            print(f'  {ym}: {result:,} orders')
        else:
            errors.append(f'  {ym}: {result}')
            print(f'  {ym}: {result}')

    total = sum(v for v in results.values() if isinstance(v, int))
    print(f'\n  Total: {total:,} orders across {len(GAP_MONTHS)} months')

    if errors:
        print(f'\n  {len(errors)} month(s) had errors — check above.')

    # Verify
    print(f'\nVerifying...')
    with get_db() as conn:
        for ym in GAP_MONTHS:
            count = check_existing_orders(conn, ym)
            status = 'OK' if count > 0 else 'STILL EMPTY'
            print(f'  {ym}: {count:,} orders [{status}]')


if __name__ == '__main__':
    main()
