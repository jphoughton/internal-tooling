"""
One-time migration: SQLite (data/inventory.db) → PostgreSQL.

Migrates all historical data including Shopify orders/customers/line-items,
Amazon daily sales, and all configuration/reference tables.

Usage:
    python -m etl.migrate_sqlite_to_postgres          # dry-run (default)
    python -m etl.migrate_sqlite_to_postgres --run    # actually migrate

The migration is idempotent — re-running is safe:
  - Core tables use ON CONFLICT DO UPDATE (upserts).
  - Append-only tables (bank_transactions, google_sheet_data,
    amazon_daily_rollup) skip if PostgreSQL already contains rows.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_SQLITE_DB = Path(__file__).parent.parent / 'data' / 'inventory.db'
_BATCH = 500  # rows per INSERT batch


def _pg_conn() -> psycopg2.extensions.connection:
    url = os.environ.get('DATABASE_URL', '')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    if not url:
        log.error('DATABASE_URL is not set — cannot connect to PostgreSQL')
        sys.exit(1)
    return psycopg2.connect(url, connect_timeout=15)


def _sqlite_conn() -> sqlite3.Connection:
    if not _SQLITE_DB.exists():
        log.error('SQLite database not found: %s', _SQLITE_DB)
        sys.exit(1)
    conn = sqlite3.connect(str(_SQLITE_DB))
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_table_exists(sq: sqlite3.Connection, table: str) -> bool:
    row = sq.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _pg_count(pg: psycopg2.extensions.connection, table: str) -> int:
    with pg.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        return cur.fetchone()[0]


def _pg_table_exists(pg: psycopg2.extensions.connection, table: str) -> bool:
    with pg.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=%s",
            (table,),
        )
        return cur.fetchone() is not None


def _batch_upsert(
    pg: psycopg2.extensions.connection,
    sql: str,
    rows: list[tuple[Any, ...]],
    dry_run: bool,
    label: str,
) -> int:
    if dry_run:
        log.info('[DRY RUN] %s — would upsert %d rows', label, len(rows))
        return len(rows)
    total = 0
    with pg.cursor() as cur:
        for i in range(0, len(rows), _BATCH):
            batch = rows[i : i + _BATCH]
            psycopg2.extras.execute_values(cur, sql, batch)
            total += len(batch)
            log.info('  %s: %d / %d rows', label, min(i + _BATCH, len(rows)), len(rows))
    pg.commit()
    return total


# ---------------------------------------------------------------------------
# Table migrations
# ---------------------------------------------------------------------------

def migrate_customers(sq: sqlite3.Connection, pg: psycopg2.extensions.connection, dry_run: bool) -> int:
    rows = [
        (r['customer_id'], r['email'], r['source'], r['first_order_date'], r['created_at'])
        for r in sq.execute('SELECT customer_id, email, source, first_order_date, created_at FROM customers')
    ]
    sql = """
        INSERT INTO customers (customer_id, email, source, first_order_date, created_at)
        VALUES %s
        ON CONFLICT (customer_id) DO UPDATE SET
            email = COALESCE(excluded.email, customers.email),
            first_order_date = LEAST(customers.first_order_date, excluded.first_order_date)
    """
    log.info('customers: %d rows to migrate', len(rows))
    return _batch_upsert(pg, sql, rows, dry_run, 'customers')


def migrate_sku_master(sq: sqlite3.Connection, pg: psycopg2.extensions.connection, dry_run: bool) -> int:
    rows = [
        (r['sku'], r['product_name'], r['category'], r['first_sale_date'], r['sources'],
         int(r['is_active']) if r['is_active'] is not None else 1, r['updated_at'])
        for r in sq.execute(
            'SELECT sku, product_name, category, first_sale_date, sources, is_active, updated_at FROM sku_master'
        )
    ]
    sql = """
        INSERT INTO sku_master (sku, product_name, category, first_sale_date, sources, is_active, updated_at)
        VALUES %s
        ON CONFLICT (sku) DO UPDATE SET
            product_name = COALESCE(excluded.product_name, sku_master.product_name),
            first_sale_date = LEAST(sku_master.first_sale_date, excluded.first_sale_date),
            sources = excluded.sources,
            is_active = excluded.is_active,
            updated_at = excluded.updated_at
    """
    log.info('sku_master: %d rows to migrate', len(rows))
    return _batch_upsert(pg, sql, rows, dry_run, 'sku_master')


def migrate_orders(sq: sqlite3.Connection, pg: psycopg2.extensions.connection, dry_run: bool) -> int:
    rows = [
        (r['order_id'], r['source'], r['source_order_id'], r['customer_id'],
         r['order_date'], float(r['total_amount'] or 0),
         r['currency'] or 'USD', r['status'] or 'completed', r['created_at'])
        for r in sq.execute(
            'SELECT order_id, source, source_order_id, customer_id, '
            'order_date, total_amount, currency, status, created_at FROM orders'
        )
    ]
    sql = """
        INSERT INTO orders (order_id, source, source_order_id, customer_id,
                            order_date, total_amount, currency, status, created_at)
        VALUES %s
        ON CONFLICT (order_id) DO UPDATE SET
            total_amount = excluded.total_amount,
            status = excluded.status
    """
    log.info('orders: %d rows to migrate', len(rows))
    return _batch_upsert(pg, sql, rows, dry_run, 'orders')


def migrate_inventory_items(sq: sqlite3.Connection, pg: psycopg2.extensions.connection, dry_run: bool) -> int:
    """Migrate SQLite order_items → PostgreSQL inventory_items."""
    rows = [
        (r['order_id'], r['sku'], r['product_name'],
         int(r['quantity'] or 1), float(r['unit_price'] or 0), float(r['total_price'] or 0))
        for r in sq.execute(
            'SELECT order_id, sku, product_name, quantity, unit_price, total_price FROM order_items'
        )
    ]
    sql = """
        INSERT INTO inventory_items (order_id, sku, product_name, quantity, unit_price, total_price)
        VALUES %s
        ON CONFLICT (order_id, sku) DO UPDATE SET
            quantity = excluded.quantity,
            unit_price = excluded.unit_price,
            total_price = excluded.total_price
    """
    log.info('inventory_items (from order_items): %d rows to migrate', len(rows))
    return _batch_upsert(pg, sql, rows, dry_run, 'inventory_items')


def migrate_daily_sku_sales(sq: sqlite3.Connection, pg: psycopg2.extensions.connection, dry_run: bool) -> int:
    rows = [
        (r['sale_date'], r['sku'], r['source'],
         int(r['units_sold'] or 0), float(r['revenue'] or 0), int(r['order_count'] or 0))
        for r in sq.execute(
            'SELECT sale_date, sku, source, units_sold, revenue, order_count FROM daily_sku_sales'
        )
    ]
    sql = """
        INSERT INTO daily_sku_sales (sale_date, sku, source, units_sold, revenue, order_count)
        VALUES %s
        ON CONFLICT (sale_date, sku, source) DO UPDATE SET
            units_sold = excluded.units_sold,
            revenue = excluded.revenue,
            order_count = excluded.order_count
    """
    log.info('daily_sku_sales: %d rows to migrate', len(rows))
    return _batch_upsert(pg, sql, rows, dry_run, 'daily_sku_sales')


def migrate_media_spend(sq: sqlite3.Connection, pg: psycopg2.extensions.connection, dry_run: bool) -> int:
    rows = [
        (r['month'],
         float(r['spend'] or 0),
         float(r['new_customer_roas'] or 1.0),
         # SQLite used source='all'; PG normalises to 'All Sources'
         'All Sources' if (r['source'] or 'all').lower() in ('all', 'all sources') else (r['source'] or 'All Sources'),
         r['updated_at'])
        for r in sq.execute('SELECT month, spend, new_customer_roas, source, updated_at FROM media_spend')
    ]
    sql = """
        INSERT INTO media_spend (month, spend, new_customer_roas, source, updated_at)
        VALUES %s
        ON CONFLICT (month, source) DO UPDATE SET
            spend = excluded.spend,
            new_customer_roas = excluded.new_customer_roas,
            updated_at = excluded.updated_at
    """
    log.info('media_spend: %d rows to migrate', len(rows))
    return _batch_upsert(pg, sql, rows, dry_run, 'media_spend')


def migrate_amazon_revenue_forecast(sq: sqlite3.Connection, pg: psycopg2.extensions.connection, dry_run: bool) -> int:
    rows = [
        (r['month'], float(r['revenue'] or 0), r['updated_at'])
        for r in sq.execute('SELECT month, revenue, updated_at FROM amazon_revenue_forecast')
    ]
    sql = """
        INSERT INTO amazon_revenue_forecast (month, revenue, updated_at)
        VALUES %s
        ON CONFLICT (month) DO UPDATE SET
            revenue = excluded.revenue,
            updated_at = excluded.updated_at
    """
    log.info('amazon_revenue_forecast: %d rows to migrate', len(rows))
    return _batch_upsert(pg, sql, rows, dry_run, 'amazon_revenue_forecast')


def migrate_planned_inbound(sq: sqlite3.Connection, pg: psycopg2.extensions.connection, dry_run: bool) -> int:
    rows = [
        (r['sku'], r['month'], int(r['units'] or 0), r['updated_at'])
        for r in sq.execute('SELECT sku, month, units, updated_at FROM planned_inbound')
    ]
    sql = """
        INSERT INTO planned_inbound (sku, month, units, updated_at)
        VALUES %s
        ON CONFLICT (sku, month) DO UPDATE SET
            units = excluded.units,
            updated_at = excluded.updated_at
    """
    log.info('planned_inbound: %d rows to migrate', len(rows))
    return _batch_upsert(pg, sql, rows, dry_run, 'planned_inbound')


def migrate_seasonal_indices(sq: sqlite3.Connection, pg: psycopg2.extensions.connection, dry_run: bool) -> int:
    rows = [
        (int(r['month_num']), float(r['index_value'] or 1.0), r['updated_at'])
        for r in sq.execute('SELECT month_num, index_value, updated_at FROM seasonal_indices')
    ]
    sql = """
        INSERT INTO seasonal_indices (month_num, index_value, updated_at)
        VALUES %s
        ON CONFLICT (month_num) DO UPDATE SET
            index_value = excluded.index_value,
            updated_at = excluded.updated_at
    """
    log.info('seasonal_indices: %d rows to migrate', len(rows))
    return _batch_upsert(pg, sql, rows, dry_run, 'seasonal_indices')


def migrate_app_settings(sq: sqlite3.Connection, pg: psycopg2.extensions.connection, dry_run: bool) -> int:
    rows = [
        (r['key'], r['value'], r['updated_at'])
        for r in sq.execute('SELECT key, value, updated_at FROM app_settings')
    ]
    sql = """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES %s
        ON CONFLICT (key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
    """
    log.info('app_settings: %d rows to migrate', len(rows))
    return _batch_upsert(pg, sql, rows, dry_run, 'app_settings')


def migrate_bank_transactions(
    sq: sqlite3.Connection, pg: psycopg2.extensions.connection, dry_run: bool
) -> int:
    """Migrate bank_transactions; skips if PostgreSQL already has rows."""
    if not dry_run:
        existing = _pg_count(pg, 'bank_transactions')
        if existing > 0:
            log.info('bank_transactions: already has %d rows in PostgreSQL — skipping', existing)
            return 0

    rows = []
    for r in sq.execute(
        'SELECT date, description, merchant, amount, category, department, '
        'user_name, is_transfer, source, created_at FROM bank_transactions'
    ):
        rows.append((
            r['date'], r['description'], r['merchant'], float(r['amount'] or 0),
            'debit',           # direction: SQLite didn't track this
            r['category'] or 'Uncategorized',
            r['department'], r['user_name'],
            int(r['is_transfer'] or 0),
            0,                 # is_ramp_duplicate: not in old schema
            r['source'] or 'unknown',
            r['created_at'],
        ))

    sql = """
        INSERT INTO bank_transactions
            (date, description, merchant, amount, direction, category,
             department, user_name, is_transfer, is_ramp_duplicate, source, created_at)
        VALUES %s
    """
    log.info('bank_transactions: %d rows to migrate', len(rows))
    return _batch_upsert(pg, sql, rows, dry_run, 'bank_transactions')


def migrate_amazon_daily_rollup(
    sq: sqlite3.Connection, pg: psycopg2.extensions.connection, dry_run: bool
) -> int:
    """Migrate amazon_daily_rollup; skips if PostgreSQL already has rows."""
    if not _sqlite_table_exists(sq, 'amazon_daily_rollup'):
        log.info('amazon_daily_rollup: table not found in SQLite — skipping')
        return 0

    if not dry_run:
        if not _pg_table_exists(pg, 'amazon_daily_rollup'):
            log.info('amazon_daily_rollup: table not yet in PostgreSQL — skipping (run sync first)')
            return 0
        existing = _pg_count(pg, 'amazon_daily_rollup')
        if existing > 0:
            log.info('amazon_daily_rollup: already has %d rows in PostgreSQL — skipping', existing)
            return 0

    rows = [
        (r['date'], float(r['new_customers'] or 0),
         float(r['new_customer_rev'] or 0), float(r['spend'] or 0))
        for r in sq.execute('SELECT date, new_customers, new_customer_rev, spend FROM amazon_daily_rollup')
    ]
    log.info('amazon_daily_rollup: %d rows to migrate', len(rows))
    if dry_run:
        log.info('[DRY RUN] amazon_daily_rollup — would insert %d rows', len(rows))
        return len(rows)

    sql = """
        INSERT INTO amazon_daily_rollup (date, new_customers, new_customer_rev, spend)
        VALUES %s
        ON CONFLICT (date) DO UPDATE SET
            new_customers = excluded.new_customers,
            new_customer_rev = excluded.new_customer_rev,
            spend = excluded.spend
    """
    return _batch_upsert(pg, sql, rows, dry_run, 'amazon_daily_rollup')


def migrate_google_sheet_data(
    sq: sqlite3.Connection, pg: psycopg2.extensions.connection, dry_run: bool
) -> int:
    """Migrate google_sheet_data by dynamically replicating the SQLite schema."""
    if not _sqlite_table_exists(sq, 'google_sheet_data'):
        log.info('google_sheet_data: table not found in SQLite — skipping')
        return 0

    if not dry_run:
        if _pg_table_exists(pg, 'google_sheet_data'):
            existing = _pg_count(pg, 'google_sheet_data')
            if existing > 0:
                log.info('google_sheet_data: already has %d rows in PostgreSQL — skipping', existing)
                return 0

    # Discover SQLite column names (skip 'id' — PostgreSQL SERIAL will generate new ones)
    pragma = sq.execute('PRAGMA table_info(google_sheet_data)').fetchall()
    cols = [row[1] for row in pragma if row[1] != 'id']

    rows_raw = sq.execute(
        f'SELECT {", ".join(cols)} FROM google_sheet_data'
    ).fetchall()
    rows = [tuple(r) for r in rows_raw]

    log.info('google_sheet_data: %d rows, %d columns to migrate', len(rows), len(cols))
    if not rows:
        return 0

    if dry_run:
        log.info('[DRY RUN] google_sheet_data — would create table and insert %d rows', len(rows))
        return len(rows)

    # Sanitise column names the same way google_sheets.py does
    safe_cols = [
        re.sub(r'[^a-z0-9_]', '', c.strip().lower().replace(' ', '_').replace('-', '_'))
        or f'col_{i}'
        for i, c in enumerate(cols)
    ]

    col_defs = ', '.join(f'"{c}" TEXT' for c in safe_cols)
    with pg.cursor() as cur:
        cur.execute('DROP TABLE IF EXISTS google_sheet_data')
        cur.execute(f"""
            CREATE TABLE google_sheet_data (
                id SERIAL PRIMARY KEY,
                {col_defs},
                synced_at TEXT DEFAULT CURRENT_TIMESTAMP::text
            )
        """)
    pg.commit()

    col_names = ', '.join(f'"{c}"' for c in safe_cols)
    insert_sql = f'INSERT INTO google_sheet_data ({col_names}) VALUES %s'
    with pg.cursor() as cur:
        for i in range(0, len(rows), _BATCH):
            batch = rows[i : i + _BATCH]
            psycopg2.extras.execute_values(cur, insert_sql, batch)
            log.info('  google_sheet_data: %d / %d rows', min(i + _BATCH, len(rows)), len(rows))
    pg.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(dry_run: bool) -> None:
    mode = 'DRY RUN' if dry_run else 'LIVE'
    log.info('=== SQLite → PostgreSQL migration (%s) ===', mode)
    log.info('SQLite source: %s', _SQLITE_DB)

    sq = _sqlite_conn()
    pg = _pg_conn()

    totals: dict[str, int] = {}

    steps = [
        ('customers',               lambda: migrate_customers(sq, pg, dry_run)),
        ('sku_master',              lambda: migrate_sku_master(sq, pg, dry_run)),
        ('orders',                  lambda: migrate_orders(sq, pg, dry_run)),
        ('inventory_items',         lambda: migrate_inventory_items(sq, pg, dry_run)),
        ('daily_sku_sales',         lambda: migrate_daily_sku_sales(sq, pg, dry_run)),
        ('media_spend',             lambda: migrate_media_spend(sq, pg, dry_run)),
        ('amazon_revenue_forecast', lambda: migrate_amazon_revenue_forecast(sq, pg, dry_run)),
        ('planned_inbound',         lambda: migrate_planned_inbound(sq, pg, dry_run)),
        ('seasonal_indices',        lambda: migrate_seasonal_indices(sq, pg, dry_run)),
        ('app_settings',            lambda: migrate_app_settings(sq, pg, dry_run)),
        ('bank_transactions',       lambda: migrate_bank_transactions(sq, pg, dry_run)),
        ('amazon_daily_rollup',     lambda: migrate_amazon_daily_rollup(sq, pg, dry_run)),
        ('google_sheet_data',       lambda: migrate_google_sheet_data(sq, pg, dry_run)),
    ]

    for name, fn in steps:
        try:
            totals[name] = fn()
        except Exception as exc:
            log.error('FAILED %s: %s', name, exc)
            if not dry_run:
                pg.rollback()
            totals[name] = -1

    sq.close()
    pg.close()

    log.info('')
    log.info('=== Migration summary (%s) ===', mode)
    for name, count in totals.items():
        status = 'FAILED' if count == -1 else f'{count:>7,} rows'
        log.info('  %-30s %s', name, status)

    if dry_run:
        log.info('')
        log.info('Re-run with --run to apply changes.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--run', action='store_true', help='Actually perform the migration (default is dry-run)')
    args = parser.parse_args()

    # Load .env if present
    env_file = Path(__file__).parent.parent / '.env'
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                os.environ.setdefault(key.strip(), val.strip())

    run(dry_run=not args.run)
