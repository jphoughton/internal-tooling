#!/usr/bin/env python3
"""
One-time migration of all historical data from local SQLite (data/inventory.db)
to the Railway PostgreSQL instance.

Reads from the local SQLite file and upserts all rows into PostgreSQL using
ON CONFLICT DO NOTHING so the script is safe to re-run (duplicate rows are
silently skipped).  The only exception is bank_transactions which has no
unique key — that table is skipped if PostgreSQL already has rows.

Tables migrated (in dependency order):
  customers, sku_master, orders, inventory_items (from SQLite order_items),
  daily_sku_sales (Amazon rows only), media_spend (normalises source 'all' →
  'All Sources'), amazon_revenue_forecast, planned_inbound, seasonal_indices,
  app_settings, bank_transactions

After orders + inventory_items are migrated, Shopify daily_sku_sales is
rebuilt automatically via db.rebuild_daily_sales().

Usage:
    python migrate_sqlite_to_postgres.py [options]

Options:
    --dry-run        Show SQLite row counts without writing to PostgreSQL.
    --yes            Skip the confirmation prompt.
    --batch-size N   Rows per INSERT batch (default: 500).
    --skip-rebuild   Skip the daily_sku_sales rebuild step.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

import psycopg2.extras

# config must be imported first — loads .env so DATABASE_URL is available
import config  # noqa: F401
import db

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

SQLITE_PATH = Path('data/inventory.db')
DEFAULT_BATCH = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_sqlite() -> sqlite3.Connection:
    if not SQLITE_PATH.exists():
        raise FileNotFoundError(
            f'SQLite database not found at {SQLITE_PATH}. '
            'Make sure you are running from the project root.'
        )
    conn = sqlite3.connect(str(SQLITE_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _pg_count(conn: db.ConnectionWrapper, table: str) -> int:
    row = conn.execute(f'SELECT COUNT(*) AS cnt FROM {table}').fetchone()
    return int(row['cnt']) if row else 0


def _sqlite_count(sc: sqlite3.Connection, table: str) -> int:
    row = sc.execute(f'SELECT COUNT(*) FROM {table}').fetchone()
    return int(row[0]) if row else 0


def _print_progress(label: str, done: int, total: int) -> None:
    pct = int(done / total * 100) if total else 100
    bar_len = 30
    filled = int(bar_len * done / total) if total else bar_len
    bar = '=' * filled + '-' * (bar_len - filled)
    print(f'  [{bar}] {pct:3d}%  {done:>7,}/{total:,}  {label}   ', end='\r', flush=True)


def _execute_values_batch(
    raw_conn: Any,
    sql: str,
    rows: list[tuple[Any, ...]],
) -> None:
    """Execute a batch insert using psycopg2.extras.execute_values."""
    with raw_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=len(rows))
    raw_conn.commit()


# ---------------------------------------------------------------------------
# Per-table migration functions
# ---------------------------------------------------------------------------

def migrate_customers(sc: sqlite3.Connection, batch: int, dry_run: bool) -> int:
    rows = sc.execute(
        'SELECT customer_id, email, source, first_order_date, created_at FROM customers'
    ).fetchall()
    total = len(rows)
    print(f'  customers: {total:,} rows in SQLite')
    if dry_run or total == 0:
        return total

    sql = """
        INSERT INTO customers (customer_id, email, source, first_order_date, created_at)
        VALUES %s
        ON CONFLICT (customer_id) DO NOTHING
    """
    with db.get_db() as conn:
        raw = conn.raw
        done = 0
        for i in range(0, total, batch):
            chunk = rows[i:i + batch]
            _execute_values_batch(raw, sql, [
                (r['customer_id'], r['email'], r['source'],
                 r['first_order_date'], r['created_at'])
                for r in chunk
            ])
            done += len(chunk)
            _print_progress('customers', done, total)
    print()
    return total


def migrate_sku_master(sc: sqlite3.Connection, batch: int, dry_run: bool) -> int:
    rows = sc.execute(
        'SELECT sku, product_name, category, first_sale_date, sources, '
        'is_active, updated_at FROM sku_master'
    ).fetchall()
    total = len(rows)
    print(f'  sku_master: {total:,} rows in SQLite')
    if dry_run or total == 0:
        return total

    sql = """
        INSERT INTO sku_master (sku, product_name, category, first_sale_date, sources, is_active, updated_at)
        VALUES %s
        ON CONFLICT (sku) DO NOTHING
    """
    with db.get_db() as conn:
        raw = conn.raw
        done = 0
        for i in range(0, total, batch):
            chunk = rows[i:i + batch]
            _execute_values_batch(raw, sql, [
                (r['sku'], r['product_name'], r['category'], r['first_sale_date'],
                 r['sources'], r['is_active'], r['updated_at'])
                for r in chunk
            ])
            done += len(chunk)
            _print_progress('sku_master', done, total)
    print()
    return total


def migrate_orders(sc: sqlite3.Connection, batch: int, dry_run: bool) -> int:
    rows = sc.execute(
        'SELECT order_id, source, source_order_id, customer_id, order_date, '
        'total_amount, currency, status, created_at FROM orders'
    ).fetchall()
    total = len(rows)
    print(f'  orders: {total:,} rows in SQLite')
    if dry_run or total == 0:
        return total

    sql = """
        INSERT INTO orders (order_id, source, source_order_id, customer_id,
                            order_date, total_amount, currency, status, created_at)
        VALUES %s
        ON CONFLICT (order_id) DO NOTHING
    """
    with db.get_db() as conn:
        raw = conn.raw
        done = 0
        for i in range(0, total, batch):
            chunk = rows[i:i + batch]
            _execute_values_batch(raw, sql, [
                (r['order_id'], r['source'], r['source_order_id'], r['customer_id'],
                 r['order_date'], r['total_amount'], r['currency'],
                 r['status'], r['created_at'])
                for r in chunk
            ])
            done += len(chunk)
            _print_progress('orders', done, total)
    print()
    return total


def migrate_inventory_items(sc: sqlite3.Connection, batch: int, dry_run: bool) -> int:
    """Migrate SQLite order_items → PostgreSQL inventory_items."""
    rows = sc.execute(
        'SELECT order_id, sku, product_name, quantity, unit_price, total_price '
        'FROM order_items'
    ).fetchall()
    total = len(rows)
    print(f'  inventory_items (from order_items): {total:,} rows in SQLite')
    if dry_run or total == 0:
        return total

    # Note: we do NOT migrate the SQLite auto-increment id — PostgreSQL assigns its own SERIAL id
    sql = """
        INSERT INTO inventory_items (order_id, sku, product_name, quantity, unit_price, total_price)
        VALUES %s
        ON CONFLICT (order_id, sku) DO NOTHING
    """
    with db.get_db() as conn:
        raw = conn.raw
        done = 0
        for i in range(0, total, batch):
            chunk = rows[i:i + batch]
            _execute_values_batch(raw, sql, [
                (r['order_id'], r['sku'], r['product_name'],
                 r['quantity'], r['unit_price'], r['total_price'])
                for r in chunk
            ])
            done += len(chunk)
            _print_progress('inventory_items', done, total)
    print()
    return total


def migrate_amazon_daily_sales(sc: sqlite3.Connection, batch: int, dry_run: bool) -> int:
    """Migrate Amazon-sourced rows from daily_sku_sales.

    Shopify rows are rebuilt from orders/inventory_items via rebuild_daily_sales(),
    so we only need to transfer Amazon rows here.
    """
    rows = sc.execute(
        "SELECT sale_date, sku, source, units_sold, revenue, order_count "
        "FROM daily_sku_sales WHERE source = 'amazon'"
    ).fetchall()
    total = len(rows)
    print(f'  daily_sku_sales (amazon): {total:,} rows in SQLite')
    if dry_run or total == 0:
        return total

    sql = """
        INSERT INTO daily_sku_sales (sale_date, sku, source, units_sold, revenue, order_count)
        VALUES %s
        ON CONFLICT (sale_date, sku, source) DO NOTHING
    """
    with db.get_db() as conn:
        raw = conn.raw
        done = 0
        for i in range(0, total, batch):
            chunk = rows[i:i + batch]
            _execute_values_batch(raw, sql, [
                (r['sale_date'], r['sku'], r['source'],
                 r['units_sold'], r['revenue'], r['order_count'])
                for r in chunk
            ])
            done += len(chunk)
            _print_progress('daily_sku_sales[amazon]', done, total)
    print()
    return total


def migrate_media_spend(sc: sqlite3.Connection, batch: int, dry_run: bool) -> int:
    """Migrate media_spend, normalising legacy source value 'all' → 'All Sources'."""
    rows = sc.execute(
        'SELECT month, spend, new_customer_roas, source, updated_at FROM media_spend'
    ).fetchall()
    total = len(rows)
    print(f'  media_spend: {total:,} rows in SQLite')
    if dry_run or total == 0:
        return total

    sql = """
        INSERT INTO media_spend (month, spend, new_customer_roas, source, updated_at)
        VALUES %s
        ON CONFLICT (month, source) DO NOTHING
    """
    with db.get_db() as conn:
        raw = conn.raw
        for i in range(0, total, batch):
            chunk = rows[i:i + batch]
            _execute_values_batch(raw, sql, [
                (r['month'], r['spend'], r['new_customer_roas'],
                 'All Sources' if r['source'] == 'all' else r['source'],
                 r['updated_at'])
                for r in chunk
            ])
    return total


def migrate_seasonal_indices(sc: sqlite3.Connection, batch: int, dry_run: bool) -> int:
    """Migrate seasonal_indices, preserving any user-customised values from SQLite."""
    try:
        rows = sc.execute(
            'SELECT month_num, index_value, updated_at FROM seasonal_indices'
        ).fetchall()
    except sqlite3.OperationalError:
        print('  seasonal_indices: table not in SQLite — skipping')
        return 0
    total = len(rows)
    print(f'  seasonal_indices: {total:,} rows in SQLite')
    if dry_run or total == 0:
        return total

    # Use DO UPDATE so user-customised values overwrite init_db()'s defaults.
    sql = """
        INSERT INTO seasonal_indices (month_num, index_value, updated_at)
        VALUES %s
        ON CONFLICT (month_num) DO UPDATE SET
            index_value = EXCLUDED.index_value,
            updated_at  = EXCLUDED.updated_at
    """
    with db.get_db() as conn:
        raw = conn.raw
        for i in range(0, total, batch):
            chunk = rows[i:i + batch]
            _execute_values_batch(raw, sql, [
                (r['month_num'], r['index_value'], r['updated_at'])
                for r in chunk
            ])
    return total


def migrate_amazon_revenue_forecast(sc: sqlite3.Connection, batch: int, dry_run: bool) -> int:
    rows = sc.execute(
        'SELECT month, revenue, updated_at FROM amazon_revenue_forecast'
    ).fetchall()
    total = len(rows)
    print(f'  amazon_revenue_forecast: {total:,} rows in SQLite')
    if dry_run or total == 0:
        return total

    sql = """
        INSERT INTO amazon_revenue_forecast (month, revenue, updated_at)
        VALUES %s
        ON CONFLICT (month) DO NOTHING
    """
    with db.get_db() as conn:
        raw = conn.raw
        for i in range(0, total, batch):
            chunk = rows[i:i + batch]
            _execute_values_batch(raw, sql, [
                (r['month'], r['revenue'], r['updated_at'])
                for r in chunk
            ])
    return total


def migrate_planned_inbound(sc: sqlite3.Connection, batch: int, dry_run: bool) -> int:
    rows = sc.execute(
        'SELECT sku, month, units, updated_at FROM planned_inbound'
    ).fetchall()
    total = len(rows)
    print(f'  planned_inbound: {total:,} rows in SQLite')
    if dry_run or total == 0:
        return total

    sql = """
        INSERT INTO planned_inbound (sku, month, units, updated_at)
        VALUES %s
        ON CONFLICT (sku, month) DO NOTHING
    """
    with db.get_db() as conn:
        raw = conn.raw
        for i in range(0, total, batch):
            chunk = rows[i:i + batch]
            _execute_values_batch(raw, sql, [
                (r['sku'], r['month'], r['units'], r['updated_at'])
                for r in chunk
            ])
    return total


def migrate_app_settings(sc: sqlite3.Connection, batch: int, dry_run: bool) -> int:
    rows = sc.execute('SELECT key, value, updated_at FROM app_settings').fetchall()
    total = len(rows)
    print(f'  app_settings: {total:,} rows in SQLite')
    if dry_run or total == 0:
        return total

    sql = """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES %s
        ON CONFLICT (key) DO NOTHING
    """
    with db.get_db() as conn:
        raw = conn.raw
        for i in range(0, total, batch):
            chunk = rows[i:i + batch]
            _execute_values_batch(raw, sql, [
                (r['key'], r['value'], r['updated_at'])
                for r in chunk
            ])
    return total


def migrate_bank_transactions(sc: sqlite3.Connection, batch: int, dry_run: bool) -> int:
    """Migrate bank_transactions.

    bank_transactions has no unique key so ON CONFLICT cannot be used.
    The table is skipped if PostgreSQL already has rows to avoid duplicates.
    """
    rows = sc.execute(
        'SELECT date, description, merchant, amount, category, '
        'department, user_name, is_transfer, source, created_at '
        'FROM bank_transactions'
    ).fetchall()
    total = len(rows)
    print(f'  bank_transactions: {total:,} rows in SQLite')
    if dry_run or total == 0:
        return total

    # Safety check: skip if PostgreSQL already has rows
    with db.get_db() as conn:
        pg_count = _pg_count(conn, 'bank_transactions')

    if pg_count > 0:
        print(f'  bank_transactions: PostgreSQL already has {pg_count:,} rows — skipping to avoid duplicates')
        return 0

    # SQLite schema predates direction/is_ramp_duplicate — supply defaults
    sql = """
        INSERT INTO bank_transactions
            (date, description, merchant, amount, direction, category,
             department, user_name, is_transfer, is_ramp_duplicate, source, created_at)
        VALUES %s
    """
    with db.get_db() as conn:
        raw = conn.raw
        done = 0
        for i in range(0, total, batch):
            chunk = rows[i:i + batch]
            _execute_values_batch(raw, sql, [
                (r['date'], r['description'], r['merchant'], r['amount'],
                 'debit',                   # direction default
                 r['category'] or 'Uncategorized',
                 r['department'], r['user_name'], r['is_transfer'],
                 0,                         # is_ramp_duplicate default
                 r['source'] or 'unknown', r['created_at'])
                for r in chunk
            ])
            done += len(chunk)
            _print_progress('bank_transactions', done, total)
    print()
    return total


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Show SQLite row counts without writing to PostgreSQL.')
    parser.add_argument('--yes', action='store_true',
                        help='Skip the confirmation prompt.')
    parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH, metavar='N',
                        help=f'Rows per INSERT batch (default: {DEFAULT_BATCH}).')
    parser.add_argument('--skip-rebuild', action='store_true',
                        help='Skip rebuilding Shopify daily_sku_sales after migration.')
    args = parser.parse_args(argv)

    print()
    print('SQLite → PostgreSQL Migration')
    print(f'  SQLite  : {SQLITE_PATH}')
    print(f'  Batch   : {args.batch_size:,} rows/batch')
    if args.dry_run:
        print('  Mode    : dry run (counts only, nothing written)')
    print()

    # --- Verify SQLite exists -----------------------------------------------
    try:
        sc = _open_sqlite()
    except FileNotFoundError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1

    # --- Show current PostgreSQL row counts ---------------------------------
    if not args.dry_run:
        print('Current PostgreSQL row counts:')
        try:
            with db.get_db() as conn:
                for tbl in ('customers', 'orders', 'inventory_items',
                            'daily_sku_sales', 'sku_master', 'media_spend',
                            'amazon_revenue_forecast', 'planned_inbound',
                            'seasonal_indices', 'app_settings', 'bank_transactions'):
                    cnt = _pg_count(conn, tbl)
                    print(f'  {tbl:<30} {cnt:>10,}')
        except db.DatabaseError as exc:
            print(f'ERROR connecting to PostgreSQL: {exc}', file=sys.stderr)
            return 1
        print()

    # --- Show SQLite counts -------------------------------------------------
    print('SQLite row counts (source):')
    sqlite_counts = {}
    for tbl in ('customers', 'orders', 'order_items', 'sku_master',
                'media_spend', 'amazon_revenue_forecast', 'planned_inbound',
                'seasonal_indices', 'app_settings', 'bank_transactions'):
        try:
            cnt = _sqlite_count(sc, tbl)
        except sqlite3.OperationalError:
            cnt = 0
        sqlite_counts[tbl] = cnt
        print(f'  {tbl:<30} {cnt:>10,}')

    # Amazon-only daily_sku_sales
    try:
        amazon_dss = sc.execute(
            "SELECT COUNT(*) FROM daily_sku_sales WHERE source='amazon'"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        amazon_dss = 0
    print(f'  {"daily_sku_sales (amazon)":<30} {amazon_dss:>10,}')
    print()

    if args.dry_run:
        print('Dry run complete — nothing was written.')
        return 0

    # --- Confirm ------------------------------------------------------------
    if not args.yes:
        try:
            resp = input('Proceed with migration? [y/N] ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print('Aborted.')
            return 0
        if resp not in ('y', 'yes'):
            print('Aborted.')
            return 0
        print()

    # --- Migrate ------------------------------------------------------------
    batch = args.batch_size
    results: dict[str, int] = {}

    print('Migrating customers...')
    results['customers'] = migrate_customers(sc, batch, dry_run=False)

    print('Migrating sku_master...')
    results['sku_master'] = migrate_sku_master(sc, batch, dry_run=False)

    print('Migrating orders...')
    results['orders'] = migrate_orders(sc, batch, dry_run=False)

    print('Migrating inventory_items (from order_items)...')
    results['inventory_items'] = migrate_inventory_items(sc, batch, dry_run=False)

    # Rebuild Shopify daily_sku_sales from the freshly migrated orders+items
    if not args.skip_rebuild:
        print('Rebuilding Shopify daily_sku_sales aggregates...')
        with db.get_db() as conn:
            db.rebuild_daily_sales(conn)
        print('  Shopify daily_sku_sales rebuilt.')
    else:
        print('  (Skipping Shopify daily_sku_sales rebuild — use --skip-rebuild was set)')

    print('Migrating Amazon daily_sku_sales...')
    results['daily_sku_sales[amazon]'] = migrate_amazon_daily_sales(sc, batch, dry_run=False)

    print('Migrating media_spend...')
    results['media_spend'] = migrate_media_spend(sc, batch, dry_run=False)

    print('Migrating amazon_revenue_forecast...')
    results['amazon_revenue_forecast'] = migrate_amazon_revenue_forecast(sc, batch, dry_run=False)

    print('Migrating planned_inbound...')
    results['planned_inbound'] = migrate_planned_inbound(sc, batch, dry_run=False)

    print('Migrating seasonal_indices...')
    results['seasonal_indices'] = migrate_seasonal_indices(sc, batch, dry_run=False)

    print('Migrating app_settings...')
    results['app_settings'] = migrate_app_settings(sc, batch, dry_run=False)

    print('Migrating bank_transactions...')
    results['bank_transactions'] = migrate_bank_transactions(sc, batch, dry_run=False)

    sc.close()

    # --- Summary ------------------------------------------------------------
    print()
    print('Migration complete:')
    for name, cnt in results.items():
        print(f'  {name:<35} {cnt:>10,} rows processed')

    # Final PostgreSQL counts
    print()
    print('Final PostgreSQL row counts:')
    with db.get_db() as conn:
        for tbl in ('customers', 'orders', 'inventory_items',
                    'daily_sku_sales', 'sku_master', 'media_spend',
                    'amazon_revenue_forecast', 'planned_inbound',
                    'seasonal_indices', 'app_settings', 'bank_transactions'):
            cnt = _pg_count(conn, tbl)
            print(f'  {tbl:<30} {cnt:>10,}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
