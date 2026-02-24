#!/usr/bin/env python3
"""
Targeted order backfill with full date range gap detection.

Scans the database for date ranges that are missing Shopify orders or Amazon
sales data, then fetches only those specific gaps from the APIs — avoiding
redundant re-fetching of data that already exists.

Usage:
    python backfill_orders.py [options]

Options:
    --start YYYY-MM-DD   Start of date range to check (default: 2 years ago)
    --end   YYYY-MM-DD   End of date range to check   (default: yesterday)
    --source shopify|amazon|all
                         Which source to scan for gaps (default: shopify)
    --min-gap-days N     Minimum consecutive missing days to treat as a gap
                         (default: 1 — every missing day is reported)
    --dry-run            Detect and display gaps without fetching any data
    --yes                Skip confirmation prompts

Examples:
    # Show Shopify gaps over the last 2 years (dry run)
    python backfill_orders.py --dry-run

    # Fill all Shopify gaps since 2023-01-01
    python backfill_orders.py --source shopify --start 2023-01-01 --yes

    # Fill Amazon + Shopify gaps, only report runs of 3+ missing days
    python backfill_orders.py --source all --min-gap-days 3

Notes:
    - Shopify: gaps are detected from the `orders` table (source='shopify').
      If your Shopify app lacks the `read_all_orders` scope, the API only
      returns the most recent 60 days; this script will warn when a requested
      gap exceeds that window.
    - Amazon: gaps are detected from `daily_sku_sales` (source='amazon').
      The S&T report is requested one day at a time; large Amazon gaps take
      proportionally longer to fill.
    - After a Shopify backfill, `daily_sku_sales` is automatically rebuilt
      from the updated orders/order_items tables.
"""

import argparse
import logging
import sys
from datetime import date, timedelta
from typing import Optional

# config must be imported first — loads .env and overlays DB credentials
import config  # noqa: F401
import db

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

# Shopify's read_all_orders scope grants access to full history.
# Without it, the API silently truncates results to ~60 days.
_SHOPIFY_SCOPE_LIMIT_DAYS = 60


# ---------------------------------------------------------------------------
# Date utilities
# ---------------------------------------------------------------------------

def _date_range(start: date, end: date) -> list[date]:
    """Return every calendar date from start to end inclusive."""
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------

def find_shopify_gaps(conn: db.ConnectionWrapper, start: date, end: date) -> list[date]:
    """Return dates in [start, end] that have no Shopify orders in the DB."""
    rows = conn.execute(
        """
        SELECT DISTINCT order_date
        FROM orders
        WHERE source = 'shopify'
          AND order_date >= ?
          AND order_date <= ?
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    # Normalise to plain YYYY-MM-DD strings (Postgres may return date objects)
    covered = {str(row['order_date'])[:10] for row in rows}
    return [d for d in _date_range(start, end) if d.isoformat() not in covered]


def find_amazon_gaps(conn: db.ConnectionWrapper, start: date, end: date) -> list[date]:
    """Return dates in [start, end] that have no Amazon rows in daily_sku_sales."""
    rows = conn.execute(
        """
        SELECT DISTINCT sale_date
        FROM daily_sku_sales
        WHERE source = 'amazon'
          AND sale_date >= ?
          AND sale_date <= ?
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    covered = {str(row['sale_date'])[:10] for row in rows}
    return [d for d in _date_range(start, end) if d.isoformat() not in covered]


def group_into_ranges(
    missing: list[date],
    min_gap_days: int = 1,
) -> list[tuple[date, date]]:
    """Merge consecutive missing dates into (start, end) gap ranges.

    Only returns ranges that span at least *min_gap_days* days so that
    isolated low-volume days (e.g. Sunday with zero orders) can be filtered
    out when the caller sets a higher threshold.
    """
    if not missing:
        return []

    ranges: list[tuple[date, date]] = []
    gap_start = missing[0]
    prev = missing[0]

    for d in missing[1:]:
        if (d - prev).days == 1:
            prev = d
        else:
            span = (prev - gap_start).days + 1
            if span >= min_gap_days:
                ranges.append((gap_start, prev))
            gap_start = d
            prev = d

    span = (prev - gap_start).days + 1
    if span >= min_gap_days:
        ranges.append((gap_start, prev))

    return ranges


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print_gap_table(ranges: list[tuple[date, date]], source: str) -> None:
    """Print a human-readable summary of gap ranges."""
    if not ranges:
        print(f'  No gaps found for {source}.')
        return

    total_days = sum((e - s).days + 1 for s, e in ranges)
    print(f'  {"Start":<12} {"End":<12} {"Days":>6}')
    print(f'  {"-" * 32}')
    for start, end in ranges:
        days = (end - start).days + 1
        print(f'  {start.isoformat():<12} {end.isoformat():<12} {days:>6}')
    print(f'  {"-" * 32}')
    print(f'  {len(ranges)} gap(s)  |  {total_days:,} missing day(s)')


# ---------------------------------------------------------------------------
# Backfill helpers
# ---------------------------------------------------------------------------

def _backfill_shopify(
    gap_ranges: list[tuple[date, date]],
    dry_run: bool = False,
) -> int:
    """Fetch Shopify orders for each gap range. Returns total orders inserted."""
    from etl.shopify_client import fetch_orders

    total = 0
    for i, (start, end) in enumerate(gap_ranges, 1):
        days = (end - start).days + 1
        label = f'[{i}/{len(gap_ranges)}] Shopify {start} → {end} ({days}d)'
        print(f'  {label}...', end='', flush=True)

        if dry_run:
            print(' (dry run)')
            continue

        if days > _SHOPIFY_SCOPE_LIMIT_DAYS:
            logger.warning(
                'Gap spans %d days. If the Shopify app lacks read_all_orders scope '
                'the API will only return the most recent %d days.',
                days,
                _SHOPIFY_SCOPE_LIMIT_DAYS,
            )

        with db.get_db() as conn:
            fetched = fetch_orders(
                conn,
                since_date=start.isoformat(),
                until_date=end.isoformat(),
                commit_per_page=True,  # persist each page immediately for resilience
            )

        print(f' {fetched:,} orders')
        total += fetched

    return total


def _backfill_amazon(
    gap_ranges: list[tuple[date, date]],
    dry_run: bool = False,
) -> int:
    """Fetch Amazon S&T sales data for each gap range. Returns total records inserted."""
    from etl.amazon import fetch_sales_report

    total = 0
    for i, (start, end) in enumerate(gap_ranges, 1):
        days = (end - start).days + 1
        label = f'[{i}/{len(gap_ranges)}] Amazon {start} → {end} ({days}d)'
        print(f'  {label}...', end='', flush=True)

        if dry_run:
            print(' (dry run)')
            continue

        with db.get_db() as conn:
            fetched = fetch_sales_report(
                conn,
                since_date=start.isoformat(),
                until_date=end.isoformat(),
            )

        print(f' {fetched:,} records')
        total += fetched

    return total


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Detect and backfill date range gaps in order history.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '--start',
        metavar='YYYY-MM-DD',
        help='Start of date range to check (default: 2 years ago).',
    )
    parser.add_argument(
        '--end',
        metavar='YYYY-MM-DD',
        help='End of date range to check (default: yesterday).',
    )
    parser.add_argument(
        '--source',
        choices=['shopify', 'amazon', 'all'],
        default='shopify',
        help='Source(s) to scan for gaps (default: shopify).',
    )
    parser.add_argument(
        '--min-gap-days',
        type=int,
        default=1,
        metavar='N',
        help='Minimum consecutive missing days to report as a gap (default: 1).',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Detect and display gaps without fetching any data.',
    )
    parser.add_argument(
        '--yes',
        action='store_true',
        help='Skip confirmation prompts.',
    )
    args = parser.parse_args(argv)

    # --- Date range ---------------------------------------------------------
    yesterday = date.today() - timedelta(days=1)
    end_date = date.fromisoformat(args.end) if args.end else yesterday
    start_date = (
        date.fromisoformat(args.start)
        if args.start
        else end_date - timedelta(days=365 * 2)
    )

    if start_date > end_date:
        print(
            f'Error: --start ({start_date}) must be before --end ({end_date}).',
            file=sys.stderr,
        )
        return 1

    sources = ['shopify', 'amazon'] if args.source == 'all' else [args.source]
    total_days = (end_date - start_date).days + 1

    # --- Header -------------------------------------------------------------
    print()
    print('Order Backfill — Gap Detection')
    print(f'  Range  : {start_date} → {end_date} ({total_days:,} days)')
    print(f'  Sources: {", ".join(sources)}')
    print(f'  Min gap: {args.min_gap_days} consecutive day(s)')
    if args.dry_run:
        print('  Mode   : dry run (gap detection only, no fetching)')
    print()

    # --- Detect gaps --------------------------------------------------------
    shopify_gaps: list[tuple[date, date]] = []
    amazon_gaps: list[tuple[date, date]] = []

    with db.get_db() as conn:
        if 'shopify' in sources:
            print('Scanning Shopify order gaps...')
            missing = find_shopify_gaps(conn, start_date, end_date)
            shopify_gaps = group_into_ranges(missing, args.min_gap_days)
            _print_gap_table(shopify_gaps, 'Shopify')
            print()

        if 'amazon' in sources:
            print('Scanning Amazon sales gaps...')
            missing = find_amazon_gaps(conn, start_date, end_date)
            amazon_gaps = group_into_ranges(missing, args.min_gap_days)
            _print_gap_table(amazon_gaps, 'Amazon')
            print()

    total_gap_ranges = len(shopify_gaps) + len(amazon_gaps)

    if total_gap_ranges == 0:
        print('No gaps detected. Database looks complete for the requested range.')
        return 0

    if args.dry_run:
        print('Dry run complete — no data was fetched.')
        return 0

    # --- Confirm ------------------------------------------------------------
    if not args.yes:
        try:
            resp = input(
                f'Proceed with backfilling {total_gap_ranges} gap range(s)? [y/N] '
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print('Aborted.')
            return 0

        if resp not in ('y', 'yes'):
            print('Aborted.')
            return 0

        print()

    # --- Backfill -----------------------------------------------------------
    total_fetched = 0

    if shopify_gaps:
        print(f'Backfilling {len(shopify_gaps)} Shopify gap(s)...')
        fetched = _backfill_shopify(shopify_gaps)
        total_fetched += fetched
        print(f'  → {fetched:,} Shopify orders fetched')

        print()
        print('Rebuilding Shopify daily_sku_sales aggregates...')
        with db.get_db() as conn:
            db.rebuild_daily_sales(conn)
        print('  ✓ Rebuild complete')

    if amazon_gaps:
        if shopify_gaps:
            print()
        print(f'Backfilling {len(amazon_gaps)} Amazon gap(s)...')
        fetched = _backfill_amazon(amazon_gaps)
        total_fetched += fetched
        print(f'  → {fetched:,} Amazon records fetched')

    print()
    print(f'Backfill complete — {total_fetched:,} total records fetched.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
