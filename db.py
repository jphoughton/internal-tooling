"""
Database layer: PostgreSQL schema, connection pool, and upsert operations.

Uses psycopg2 with a connection pool. All SQL uses PostgreSQL syntax natively.
The _translate_sql() helper converts any remaining SQLite-style SQL found in
analytics/dashboard code (strftime, date('now'), julianday, ? placeholders).
"""
from __future__ import annotations

import json
import logging
import os
import re
import psycopg2
import psycopg2.extras
import psycopg2.pool
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Optional, Union

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    """Raised when a database operation fails."""
    pass


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------
_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Lazy-init the connection pool (avoids import-time errors)."""
    global _pool
    if _pool is None:
        url = os.environ.get('DATABASE_URL', '')
        # Railway uses postgres:// but psycopg2 requires postgresql://
        if url.startswith('postgres://'):
            url = url.replace('postgres://', 'postgresql://', 1)
        if not url:
            raise DatabaseError(
                'DATABASE_URL is not set. '
                'Set it in .env or Railway service variables.'
            )
        try:
            _pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=20,
                dsn=url,
                connect_timeout=10,
                options='-c statement_timeout=30000',  # 30s per statement
            )
        except psycopg2.Error as exc:
            logger.error('Failed to create connection pool: %s', exc)
            raise DatabaseError(f'Failed to create connection pool: {exc}') from exc
    return _pool


# ---------------------------------------------------------------------------
# SQL translation (SQLite idioms -> PostgreSQL)
# ---------------------------------------------------------------------------
def _translate_sql(sql: str) -> str:
    """Convert SQLite-specific SQL patterns to PostgreSQL equivalents.

    Applied automatically to every query so that analytics/dashboard code
    written with SQLite functions keeps working after the migration.
    """
    # 1. julianday(a) - julianday(b) -> (a::date - b::date)
    sql = re.sub(
        r'julianday\(([^)]+)\)\s*-\s*julianday\(([^)]+)\)',
        r'(\1::date - \2::date)',
        sql,
    )

    # 2. strftime('%Y-%m', X) -> TO_CHAR(X::date, 'YYYY-MM')
    sql = re.sub(
        r"strftime\(\s*'%Y-%m'\s*,\s*([^)]+)\)",
        r"TO_CHAR(\1::date, 'YYYY-MM')",
        sql,
    )

    # 3. date('now', '-N days/months/years') -> CURRENT_DATE - INTERVAL 'N ...'
    def _date_offset(m: re.Match[str]) -> str:
        sign = '-' if m.group(1).startswith('-') else '+'
        num = m.group(1).lstrip('-+')
        unit = m.group(2)
        return f"(CURRENT_DATE {sign} INTERVAL '{num} {unit}')::text"
    sql = re.sub(
        r"date\('now',\s*'([^']+)\s+(days?|months?|years?)'\)",
        _date_offset,
        sql,
    )

    # 4. date('now') -> CURRENT_DATE::text
    sql = sql.replace("date('now')", "CURRENT_DATE::text")

    # 5. DATE(X) -> X::date (standalone DATE() function)
    sql = re.sub(r'\bDATE\(([^)]+)\)', r'\1::date', sql)

    # 6. AUTOINCREMENT -> SERIAL (DDL only)
    sql = re.sub(
        r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT',
        'SERIAL PRIMARY KEY',
        sql,
        flags=re.IGNORECASE,
    )

    # 7. Placeholders: ? -> %s
    sql = sql.replace('?', '%s')

    return sql


# ---------------------------------------------------------------------------
# Connection wrapper for dict-like row access
# ---------------------------------------------------------------------------
class _Row(dict[str, Any]):
    """Dict subclass that also supports positional indexing (row[0]).

    psycopg2's RealDictRow supports row["col"] but not row[0].
    Legacy code (especially ``SELECT COUNT(*) ...``.fetchone()[0])
    relies on positional access, so this wrapper adds it.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(data)
        self._keys = list(data.keys())

    def __getitem__(self, key: Union[int, str]) -> Any:
        if isinstance(key, int):
            return super().__getitem__(self._keys[key])
        return super().__getitem__(key)


class _CursorWrapper:
    """Wraps a psycopg2 RealDictCursor to match the interface used by callers."""

    def __init__(self, cursor: psycopg2.extensions.cursor) -> None:
        self._cur = cursor

    def fetchone(self) -> Optional[_Row]:
        row = self._cur.fetchone()
        return _Row(dict(row)) if row is not None else None

    def fetchall(self) -> list[_Row]:
        return [_Row(dict(r)) for r in self._cur.fetchall()]

    @property
    def description(self) -> Any:
        return self._cur.description

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount


class ConnectionWrapper:
    """Unified database connection that translates SQL and provides dict rows."""

    def __init__(self, raw_conn: psycopg2.extensions.connection) -> None:
        self._conn = raw_conn

    def execute(
        self,
        sql: str,
        params: Optional[Union[tuple[Any, ...], list[Any]]] = None,
    ) -> _CursorWrapper:
        sql = _translate_sql(sql)
        try:
            cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            return _CursorWrapper(cur)
        except psycopg2.Error as exc:
            logger.error('Database query failed: %s | SQL: %.200s', exc, sql)
            raise DatabaseError(f'Database query failed: {exc}') from exc

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    @property
    def raw(self) -> psycopg2.extensions.connection:
        """Raw psycopg2 connection for pd.read_sql_query()."""
        return self._conn


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------
@contextmanager
def get_db() -> Iterator[ConnectionWrapper]:
    """Yield a ConnectionWrapper; auto-commit on success, rollback on error."""
    pool = _get_pool()
    try:
        raw = pool.getconn()
    except psycopg2.Error as exc:
        logger.error('Failed to acquire connection from pool: %s', exc)
        raise DatabaseError(f'Failed to acquire connection from pool: {exc}') from exc
    wrapped = ConnectionWrapper(raw)
    try:
        yield wrapped
        wrapped.commit()
    except DatabaseError:
        wrapped.rollback()
        raise
    except Exception as exc:
        wrapped.rollback()
        logger.error('Transaction failed, rolling back: %s', exc)
        raise DatabaseError(f'Transaction failed: {exc}') from exc
    finally:
        pool.putconn(raw)


# ---------------------------------------------------------------------------
# pd.read_sql_query helper
# ---------------------------------------------------------------------------
def read_sql(
    sql: str,
    conn_wrapper: ConnectionWrapper,
    params: Optional[Any] = None,
) -> pd.DataFrame:
    """Execute a SQL query through pandas with automatic SQL translation.

    Usage:
        with get_db() as conn:
            df = read_sql("SELECT ... WHERE sale_date >= date('now', '-30 days')", conn)
    """
    import warnings
    import pandas as pd
    translated = _translate_sql(sql)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', message='.*pandas only supports SQLAlchemy.*')
            return pd.read_sql_query(translated, conn_wrapper.raw, params=params)
    except Exception as exc:
        logger.error('read_sql failed: %s | SQL: %.200s', exc, translated)
        raise DatabaseError(f'read_sql failed: {exc}') from exc


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------
_SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS customers (
        customer_id TEXT PRIMARY KEY,
        email TEXT,
        source TEXT NOT NULL,
        first_order_date TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP::text
    )""",

    """CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_order_id TEXT NOT NULL,
        customer_id TEXT,
        order_date TEXT NOT NULL,
        total_amount REAL DEFAULT 0,
        total_tax REAL DEFAULT 0,
        currency TEXT DEFAULT 'USD',
        status TEXT DEFAULT 'completed',
        financial_status TEXT DEFAULT 'paid',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP::text,
        FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
    )""",

    """CREATE TABLE IF NOT EXISTS order_items (
        id SERIAL PRIMARY KEY,
        order_id TEXT NOT NULL,
        sku TEXT NOT NULL,
        product_name TEXT,
        quantity INTEGER NOT NULL DEFAULT 1,
        unit_price REAL NOT NULL DEFAULT 0,
        total_price REAL NOT NULL DEFAULT 0,
        FOREIGN KEY (order_id) REFERENCES orders(order_id),
        UNIQUE(order_id, sku)
    )""",

    """CREATE TABLE IF NOT EXISTS sku_master (
        sku TEXT PRIMARY KEY,
        product_name TEXT,
        category TEXT,
        first_sale_date TEXT,
        sources TEXT,
        is_active INTEGER DEFAULT 1,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP::text
    )""",

    """CREATE TABLE IF NOT EXISTS daily_sku_sales (
        sale_date TEXT NOT NULL,
        sku TEXT NOT NULL,
        source TEXT NOT NULL,
        units_sold INTEGER DEFAULT 0,
        revenue REAL DEFAULT 0,
        order_count INTEGER DEFAULT 0,
        PRIMARY KEY (sale_date, sku, source)
    )""",

    """CREATE TABLE IF NOT EXISTS media_spend (
        id SERIAL PRIMARY KEY,
        month TEXT NOT NULL,
        spend REAL NOT NULL DEFAULT 0,
        new_customer_roas REAL NOT NULL DEFAULT 1.0,
        source TEXT DEFAULT 'All Sources',
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP::text,
        UNIQUE(month, source)
    )""",

    """CREATE TABLE IF NOT EXISTS amazon_revenue_forecast (
        month TEXT PRIMARY KEY,
        revenue REAL NOT NULL DEFAULT 0,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP::text
    )""",

    """CREATE TABLE IF NOT EXISTS planned_inbound (
        sku TEXT NOT NULL,
        month TEXT NOT NULL,
        units INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP::text,
        PRIMARY KEY (sku, month)
    )""",

    """CREATE TABLE IF NOT EXISTS sync_log (
        id SERIAL PRIMARY KEY,
        source TEXT NOT NULL,
        sync_date TEXT NOT NULL,
        records_fetched INTEGER DEFAULT 0,
        status TEXT DEFAULT 'success',
        error_message TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP::text
    )""",

    """CREATE TABLE IF NOT EXISTS seasonal_indices (
        month_num INTEGER PRIMARY KEY,
        index_value REAL NOT NULL DEFAULT 1.0,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP::text
    )""",

    """CREATE TABLE IF NOT EXISTS sku_seasonal_indices (
        sku TEXT NOT NULL,
        month_num INTEGER NOT NULL,
        index_value REAL NOT NULL DEFAULT 1.0,
        sample_months INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP::text,
        PRIMARY KEY (sku, month_num)
    )""",

    """CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP::text
    )""",

    """CREATE TABLE IF NOT EXISTS klaviyo_campaigns (
        id TEXT PRIMARY KEY,
        name TEXT,
        status TEXT,
        send_time TEXT,
        created_at TEXT,
        updated_at TEXT,
        synced_at TEXT DEFAULT CURRENT_TIMESTAMP::text
    )""",

    """CREATE TABLE IF NOT EXISTS klaviyo_flows (
        id TEXT PRIMARY KEY,
        name TEXT,
        status TEXT,
        trigger_type TEXT,
        created TEXT,
        updated TEXT,
        synced_at TEXT DEFAULT CURRENT_TIMESTAMP::text
    )""",

    """CREATE TABLE IF NOT EXISTS klaviyo_lists (
        id TEXT PRIMARY KEY,
        name TEXT,
        created TEXT,
        updated TEXT,
        synced_at TEXT DEFAULT CURRENT_TIMESTAMP::text
    )""",

    """CREATE TABLE IF NOT EXISTS inventory_snapshot (
        source TEXT PRIMARY KEY,
        snapshot_data TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP::text
    )""",

    """CREATE TABLE IF NOT EXISTS model_runs (
        model_name TEXT PRIMARY KEY,
        last_run_at TEXT NOT NULL,
        duration_seconds REAL,
        status TEXT DEFAULT 'success',
        error_message TEXT,
        triggered_by TEXT DEFAULT 'manual'
    )""",

    """CREATE TABLE IF NOT EXISTS cashflow_overrides (
        id SERIAL PRIMARY KEY,
        line_item TEXT NOT NULL,
        week_start TEXT NOT NULL,
        override_amount REAL NOT NULL,
        note TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP::text,
        UNIQUE(line_item, week_start)
    )""",

    """CREATE TABLE IF NOT EXISTS cashflow_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP::text
    )""",

    """CREATE TABLE IF NOT EXISTS precomputed_analytics (
        result_key TEXT PRIMARY KEY,
        result_json TEXT NOT NULL,
        computed_at TEXT DEFAULT CURRENT_TIMESTAMP::text,
        model_name TEXT,
        duration_seconds REAL
    )""",

    """CREATE TABLE IF NOT EXISTS threpl_invoices (
        invoice_number TEXT NOT NULL,
        week_start TEXT NOT NULL,
        week_end TEXT NOT NULL,
        invoice_date TEXT,
        total_amount REAL NOT NULL DEFAULT 0,
        line_items TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP::text,
        PRIMARY KEY (invoice_number)
    )""",

    """CREATE TABLE IF NOT EXISTS page_errors (
        id SERIAL PRIMARY KEY,
        page TEXT NOT NULL,
        error_type TEXT NOT NULL,
        error_message TEXT NOT NULL,
        traceback TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP::text
    )""",

    # Indexes
    "CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date)",
    "CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_order_items_sku ON order_items(sku)",
    "CREATE INDEX IF NOT EXISTS idx_daily_sales_sku ON daily_sku_sales(sku)",
    "CREATE INDEX IF NOT EXISTS idx_daily_sales_date ON daily_sku_sales(sale_date)",
    "CREATE INDEX IF NOT EXISTS idx_daily_sales_source_date ON daily_sku_sales(source, sale_date)",
    "CREATE INDEX IF NOT EXISTS idx_daily_sales_sku_date ON daily_sku_sales(sku, sale_date)",
    "CREATE INDEX IF NOT EXISTS idx_orders_customer_date ON orders(customer_id, order_date)",
    "CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_cashflow_overrides_week ON cashflow_overrides(week_start)",
    "CREATE INDEX IF NOT EXISTS idx_orders_source_customer ON orders(source, customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_source_date ON orders(source, order_date)",
    "CREATE INDEX IF NOT EXISTS idx_orders_source_total_customer ON orders(source, total_amount, customer_id, order_date)",
    "CREATE INDEX IF NOT EXISTS idx_customers_source ON customers(source)",

    # Klaviyo metric columns (ALTER is idempotent with IF NOT EXISTS)
    "ALTER TABLE klaviyo_campaigns ADD COLUMN IF NOT EXISTS recipients INTEGER DEFAULT 0",
    "ALTER TABLE klaviyo_campaigns ADD COLUMN IF NOT EXISTS delivered INTEGER DEFAULT 0",
    "ALTER TABLE klaviyo_campaigns ADD COLUMN IF NOT EXISTS opens_unique INTEGER DEFAULT 0",
    "ALTER TABLE klaviyo_campaigns ADD COLUMN IF NOT EXISTS clicks_unique INTEGER DEFAULT 0",
    "ALTER TABLE klaviyo_campaigns ADD COLUMN IF NOT EXISTS open_rate REAL DEFAULT 0",
    "ALTER TABLE klaviyo_campaigns ADD COLUMN IF NOT EXISTS click_rate REAL DEFAULT 0",
    "ALTER TABLE klaviyo_campaigns ADD COLUMN IF NOT EXISTS click_to_open_rate REAL DEFAULT 0",
    "ALTER TABLE klaviyo_campaigns ADD COLUMN IF NOT EXISTS unsubscribes INTEGER DEFAULT 0",
    "ALTER TABLE klaviyo_campaigns ADD COLUMN IF NOT EXISTS unsubscribe_rate REAL DEFAULT 0",
    "ALTER TABLE klaviyo_campaigns ADD COLUMN IF NOT EXISTS bounce_rate REAL DEFAULT 0",
    "ALTER TABLE klaviyo_campaigns ADD COLUMN IF NOT EXISTS revenue REAL DEFAULT 0",
    "ALTER TABLE klaviyo_campaigns ADD COLUMN IF NOT EXISTS revenue_per_recipient REAL DEFAULT 0",
    "ALTER TABLE klaviyo_campaigns ADD COLUMN IF NOT EXISTS average_order_value REAL DEFAULT 0",
    "ALTER TABLE klaviyo_campaigns ADD COLUMN IF NOT EXISTS conversion_rate REAL DEFAULT 0",

    "ALTER TABLE klaviyo_flows ADD COLUMN IF NOT EXISTS recipients INTEGER DEFAULT 0",
    "ALTER TABLE klaviyo_flows ADD COLUMN IF NOT EXISTS delivered INTEGER DEFAULT 0",
    "ALTER TABLE klaviyo_flows ADD COLUMN IF NOT EXISTS opens_unique INTEGER DEFAULT 0",
    "ALTER TABLE klaviyo_flows ADD COLUMN IF NOT EXISTS clicks_unique INTEGER DEFAULT 0",
    "ALTER TABLE klaviyo_flows ADD COLUMN IF NOT EXISTS open_rate REAL DEFAULT 0",
    "ALTER TABLE klaviyo_flows ADD COLUMN IF NOT EXISTS click_rate REAL DEFAULT 0",
    "ALTER TABLE klaviyo_flows ADD COLUMN IF NOT EXISTS click_to_open_rate REAL DEFAULT 0",
    "ALTER TABLE klaviyo_flows ADD COLUMN IF NOT EXISTS unsubscribes INTEGER DEFAULT 0",
    "ALTER TABLE klaviyo_flows ADD COLUMN IF NOT EXISTS unsubscribe_rate REAL DEFAULT 0",
    "ALTER TABLE klaviyo_flows ADD COLUMN IF NOT EXISTS bounce_rate REAL DEFAULT 0",
    "ALTER TABLE klaviyo_flows ADD COLUMN IF NOT EXISTS revenue REAL DEFAULT 0",
    "ALTER TABLE klaviyo_flows ADD COLUMN IF NOT EXISTS revenue_per_recipient REAL DEFAULT 0",
    "ALTER TABLE klaviyo_flows ADD COLUMN IF NOT EXISTS average_order_value REAL DEFAULT 0",
    "ALTER TABLE klaviyo_flows ADD COLUMN IF NOT EXISTS conversion_rate REAL DEFAULT 0",

    # Orders: financial_status for filtering refunds/cancellations
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS financial_status TEXT DEFAULT 'paid'",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS total_tax REAL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS refund_amount REAL DEFAULT 0",
]


def init_db() -> None:
    """Create all tables and indexes if they don't exist."""
    print("  init_db: connecting...", flush=True)
    try:
        with get_db() as conn:
            # Kill stale connections from crashed deploys that may hold locks
            print("  init_db: terminating stale connections...", flush=True)
            conn.execute("""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE pid <> pg_backend_pid()
                  AND state IN ('idle', 'idle in transaction', 'idle in transaction (aborted)')
                  AND query_start < NOW() - INTERVAL '60 seconds'
            """)

            for i, stmt in enumerate(_SCHEMA_SQL):
                label = stmt.strip()[:60].replace('\n', ' ')
                print(f"  init_db: [{i+1}/{len(_SCHEMA_SQL)}] {label}...", flush=True)
                conn.execute(stmt)

            print("  init_db: seeding seasonal indices...", flush=True)
            # Seed default seasonal indices if table is empty
            existing = conn.execute("SELECT COUNT(*) as cnt FROM seasonal_indices").fetchone()
            if existing is not None and existing['cnt'] == 0:
                defaults = {
                    1: 0.95, 2: 0.92, 3: 0.98, 4: 1.02, 5: 1.05, 6: 1.10,
                    7: 1.12, 8: 1.08, 9: 1.02, 10: 0.98, 11: 0.92, 12: 0.88,
                }
                for m, v in defaults.items():
                    conn.execute(
                        "INSERT INTO seasonal_indices (month_num, index_value) VALUES (%s, %s)",
                        (m, v),
                    )

            # Seed cashflow_settings if table is empty
            print("  init_db: seeding cashflow settings...", flush=True)
            existing_cf = conn.execute("SELECT COUNT(*) as cnt FROM cashflow_settings").fetchone()
            if existing_cf is not None and existing_cf['cnt'] == 0:
                cf_defaults = {
                    'dtc_payout_ratio': '0.94',
                    'amazon_payout_ratio': '0.62',
                    'cogs_pct': '0.25',
                    'min_cash_threshold': '100000',
                    'loc_balance': '510000',
                    'calibration_lookback_weeks': '12',
                    'calibration_method': 'ewma',
                }
                for k, v in cf_defaults.items():
                    conn.execute(
                        "INSERT INTO cashflow_settings (key, value) VALUES (%s, %s)",
                        (k, v),
                    )

            # Migrate legacy media_spend source='all' to 'All Sources'
            print("  init_db: migrating media_spend source values...", flush=True)
            conn.execute("UPDATE media_spend SET source = 'All Sources' WHERE source = 'all'")
            print("  init_db: done.", flush=True)
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('init_db failed: %s', exc)
        raise DatabaseError(f'init_db failed: {exc}') from exc


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------
def upsert_customer(
    conn: ConnectionWrapper,
    customer_id: str,
    email: Optional[str],
    source: str,
    first_order_date: Optional[str],
) -> None:
    try:
        conn.execute("""
            INSERT INTO customers (customer_id, email, source, first_order_date)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(customer_id) DO UPDATE SET
                email = COALESCE(excluded.email, customers.email),
                first_order_date = LEAST(customers.first_order_date, excluded.first_order_date)
        """, (customer_id, email, source, first_order_date))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('upsert_customer failed for customer_id=%s: %s', customer_id, exc)
        raise DatabaseError(f'upsert_customer failed: {exc}') from exc


def upsert_order(
    conn: ConnectionWrapper,
    order_id: str,
    source: str,
    source_order_id: str,
    customer_id: Optional[str],
    order_date: str,
    total_amount: float,
    currency: str = "USD",
    financial_status: str = "paid",
    total_tax: float = 0.0,
    refund_amount: float = 0.0,
) -> None:
    try:
        conn.execute("""
            INSERT INTO orders (order_id, source, source_order_id, customer_id, order_date, total_amount, total_tax, refund_amount, currency, financial_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(order_id) DO UPDATE SET
                total_amount = excluded.total_amount,
                total_tax = excluded.total_tax,
                refund_amount = excluded.refund_amount,
                financial_status = excluded.financial_status,
                status = 'completed'
        """, (order_id, source, source_order_id, customer_id, order_date, total_amount, total_tax, refund_amount, currency, financial_status))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('upsert_order failed for order_id=%s: %s', order_id, exc)
        raise DatabaseError(f'upsert_order failed: {exc}') from exc


def upsert_order_item(
    conn: ConnectionWrapper,
    order_id: str,
    sku: str,
    product_name: Optional[str],
    quantity: int,
    unit_price: float,
) -> None:
    try:
        conn.execute("""
            INSERT INTO order_items (order_id, sku, product_name, quantity, unit_price, total_price)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(order_id, sku) DO UPDATE SET
                quantity = excluded.quantity,
                unit_price = excluded.unit_price,
                total_price = excluded.total_price
        """, (order_id, sku, product_name, quantity, unit_price, quantity * unit_price))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('upsert_order_item failed for order_id=%s sku=%s: %s', order_id, sku, exc)
        raise DatabaseError(f'upsert_order_item failed: {exc}') from exc


def upsert_sku(
    conn: ConnectionWrapper,
    sku: str,
    product_name: Optional[str],
    category: Optional[str],
    first_sale_date: Optional[str],
    source: str,
) -> None:
    try:
        row = conn.execute("SELECT sources FROM sku_master WHERE sku = %s", (sku,)).fetchone()
        if row and row["sources"]:
            existing = set(row["sources"].split(","))
            existing.add(source)
            sources = ",".join(sorted(existing))
        else:
            sources = source

        conn.execute("""
            INSERT INTO sku_master (sku, product_name, category, first_sale_date, sources)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(sku) DO UPDATE SET
                product_name = COALESCE(excluded.product_name, sku_master.product_name),
                first_sale_date = LEAST(sku_master.first_sale_date, excluded.first_sale_date),
                sources = %s,
                updated_at = CURRENT_TIMESTAMP::text
        """, (sku, product_name, category, first_sale_date, sources, sources))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('upsert_sku failed for sku=%s: %s', sku, exc)
        raise DatabaseError(f'upsert_sku failed: {exc}') from exc


# ---------------------------------------------------------------------------
# Cash flow helpers
# ---------------------------------------------------------------------------
def get_cashflow_setting(conn: ConnectionWrapper, key: str, default: Optional[str] = None) -> Optional[str]:
    """Get a cashflow setting value."""
    try:
        row = conn.execute(
            "SELECT value FROM cashflow_settings WHERE key = %s", (key,)
        ).fetchone()
        return row['value'] if row else default
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_cashflow_setting failed for key=%s: %s', key, exc)
        raise DatabaseError(f'get_cashflow_setting failed: {exc}') from exc


def set_cashflow_setting(conn: ConnectionWrapper, key: str, value: str) -> None:
    """Set a cashflow setting value."""
    try:
        conn.execute("""
            INSERT INTO cashflow_settings (key, value, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP::text)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP::text
        """, (key, value))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('set_cashflow_setting failed for key=%s: %s', key, exc)
        raise DatabaseError(f'set_cashflow_setting failed: {exc}') from exc


# ---------------------------------------------------------------------------
# Rebuild aggregate table
# ---------------------------------------------------------------------------
def rebuild_daily_sales(conn: ConnectionWrapper) -> None:
    """Rebuild the daily_sku_sales aggregate table from order_items.

    IMPORTANT: Only rebuilds Shopify data (from order_items/orders tables).
    Amazon data is inserted directly into daily_sku_sales by the Amazon ETL
    and must NOT be deleted here.
    """
    try:
        conn.execute("DELETE FROM daily_sku_sales WHERE source = 'shopify'")
        conn.execute("""
            INSERT INTO daily_sku_sales (sale_date, sku, source, units_sold, revenue, order_count)
            SELECT
                o.order_date::date::text as sale_date,
                oi.sku,
                o.source,
                SUM(oi.quantity) as units_sold,
                SUM(oi.total_price) as revenue,
                COUNT(DISTINCT o.order_id) as order_count
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            WHERE o.status = 'completed' AND o.source = 'shopify'
              AND COALESCE(o.financial_status, 'paid') NOT IN ('refunded', 'voided')
            GROUP BY o.order_date::date, oi.sku, o.source
        """)
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('rebuild_daily_sales failed: %s', exc)
        raise DatabaseError(f'rebuild_daily_sales failed: {exc}') from exc


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------
def get_last_sync_date(conn: ConnectionWrapper, source: str) -> Optional[str]:
    try:
        row = conn.execute(
            "SELECT MAX(sync_date) as last_date FROM sync_log WHERE source = %s AND status = 'success'",
            (source,)
        ).fetchone()
        return row["last_date"] if row and row["last_date"] else None
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_last_sync_date failed for source=%s: %s', source, exc)
        raise DatabaseError(f'get_last_sync_date failed: {exc}') from exc


def log_sync(
    conn: ConnectionWrapper,
    source: str,
    sync_date: str,
    records_fetched: int,
    status: str = "success",
    error_message: Optional[str] = None,
) -> None:
    try:
        conn.execute("""
            INSERT INTO sync_log (source, sync_date, records_fetched, status, error_message)
            VALUES (%s, %s, %s, %s, %s)
        """, (source, sync_date, records_fetched, status, error_message))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('log_sync failed for source=%s: %s', source, exc)
        raise DatabaseError(f'log_sync failed: {exc}') from exc


def get_last_sync_timestamp(conn: ConnectionWrapper, sources: list[str]) -> Optional[str]:
    """Return created_at of the most recent successful sync across given sources."""
    try:
        placeholders = ','.join('%s' for _ in sources)
        row = conn.execute(
            f"SELECT MAX(created_at) as last_ts FROM sync_log "
            f"WHERE source IN ({placeholders}) AND status = 'success'",
            sources
        ).fetchone()
        return row['last_ts'] if row and row['last_ts'] else None
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_last_sync_timestamp failed: %s', exc)
        raise DatabaseError(f'get_last_sync_timestamp failed: {exc}') from exc


def get_new_rows_since_yesterday(conn: ConnectionWrapper, sources: list[str]) -> int:
    """Return total records_fetched for today's syncs across given sources."""
    try:
        placeholders = ','.join('%s' for _ in sources)
        row = conn.execute(
            f"SELECT COALESCE(SUM(records_fetched), 0) as total "
            f"FROM sync_log WHERE source IN ({placeholders}) "
            f"AND sync_date = CURRENT_DATE::text AND status = 'success'",
            sources
        ).fetchone()
        return int(row['total']) if row is not None else 0
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_new_rows_since_yesterday failed: %s', exc)
        raise DatabaseError(f'get_new_rows_since_yesterday failed: {exc}') from exc


def get_synced_sources(conn: ConnectionWrapper, sources: list[str]) -> list[str]:
    """Return list of source names that have at least one successful sync."""
    try:
        placeholders = ','.join('%s' for _ in sources)
        rows = conn.execute(
            f"SELECT DISTINCT source FROM sync_log "
            f"WHERE source IN ({placeholders}) AND status = 'success'",
            sources
        ).fetchall()
        return [r['source'] for r in rows]
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_synced_sources failed: %s', exc)
        raise DatabaseError(f'get_synced_sources failed: {exc}') from exc


# ---------------------------------------------------------------------------
# Media spend & forecasts
# ---------------------------------------------------------------------------
def upsert_media_spend(
    conn: ConnectionWrapper,
    month: str,
    spend: float,
    roas: float,
    source: str = "All Sources",
) -> None:
    try:
        conn.execute("""
            INSERT INTO media_spend (month, spend, new_customer_roas, source)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(month, source) DO UPDATE SET
                spend = excluded.spend,
                new_customer_roas = excluded.new_customer_roas,
                updated_at = CURRENT_TIMESTAMP::text
        """, (month, spend, roas, source))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('upsert_media_spend failed for month=%s source=%s: %s', month, source, exc)
        raise DatabaseError(f'upsert_media_spend failed: {exc}') from exc


def get_media_spend(
    conn: ConnectionWrapper,
    source: str = "All Sources",
) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT month, spend, new_customer_roas FROM media_spend WHERE source = %s ORDER BY month",
            (source,)
        ).fetchall()
        return [{"month": r["month"], "spend": float(r["spend"] or 0), "new_customer_roas": float(r["new_customer_roas"] or 0)} for r in rows]
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_media_spend failed for source=%s: %s', source, exc)
        raise DatabaseError(f'get_media_spend failed: {exc}') from exc


def upsert_amazon_revenue_forecast(
    conn: ConnectionWrapper,
    month: str,
    revenue: float,
) -> None:
    try:
        conn.execute("""
            INSERT INTO amazon_revenue_forecast (month, revenue)
            VALUES (%s, %s)
            ON CONFLICT(month) DO UPDATE SET
                revenue = excluded.revenue,
                updated_at = CURRENT_TIMESTAMP::text
        """, (month, revenue))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('upsert_amazon_revenue_forecast failed for month=%s: %s', month, exc)
        raise DatabaseError(f'upsert_amazon_revenue_forecast failed: {exc}') from exc


def get_amazon_revenue_forecast(conn: ConnectionWrapper) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT month, revenue FROM amazon_revenue_forecast ORDER BY month"
        ).fetchall()
        return [{"month": r["month"], "revenue": float(r["revenue"] or 0)} for r in rows]
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_amazon_revenue_forecast failed: %s', exc)
        raise DatabaseError(f'get_amazon_revenue_forecast failed: {exc}') from exc


def upsert_planned_inbound(
    conn: ConnectionWrapper,
    sku: str,
    month: str,
    units: int,
) -> None:
    try:
        conn.execute("""
            INSERT INTO planned_inbound (sku, month, units)
            VALUES (%s, %s, %s)
            ON CONFLICT(sku, month) DO UPDATE SET
                units = excluded.units,
                updated_at = CURRENT_TIMESTAMP::text
        """, (sku, month, units))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('upsert_planned_inbound failed for sku=%s month=%s: %s', sku, month, exc)
        raise DatabaseError(f'upsert_planned_inbound failed: {exc}') from exc


def get_planned_inbound(conn: ConnectionWrapper) -> list[dict[str, Any]]:
    """Return all planned inbound entries as list of dicts."""
    try:
        rows = conn.execute(
            "SELECT sku, month, units FROM planned_inbound ORDER BY sku, month"
        ).fetchall()
        return [{"sku": r["sku"], "month": r["month"], "units": int(r["units"] or 0)} for r in rows]
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_planned_inbound failed: %s', exc)
        raise DatabaseError(f'get_planned_inbound failed: {exc}') from exc


def get_planned_inbound_dict(conn: ConnectionWrapper) -> dict[str, dict[str, int]]:
    """Return planned inbound as nested dict: {sku: {month: units}}."""
    try:
        rows = conn.execute(
            "SELECT sku, month, units FROM planned_inbound WHERE units > 0"
        ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for r in rows:
            result.setdefault(r["sku"], {})[r["month"]] = int(r["units"] or 0)
        return result
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_planned_inbound_dict failed: %s', exc)
        raise DatabaseError(f'get_planned_inbound_dict failed: {exc}') from exc


# ---------------------------------------------------------------------------
# Seasonal indices
# ---------------------------------------------------------------------------
def get_seasonal_indices(conn: ConnectionWrapper) -> dict[int, float]:
    """Return seasonal indices as dict {month_num: value}, e.g. {1: 0.95, ...}."""
    try:
        rows = conn.execute(
            "SELECT month_num, index_value FROM seasonal_indices ORDER BY month_num"
        ).fetchall()
        return {int(r["month_num"]): float(r["index_value"] or 1.0) for r in rows}
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_seasonal_indices failed: %s', exc)
        raise DatabaseError(f'get_seasonal_indices failed: {exc}') from exc


def upsert_seasonal_index(
    conn: ConnectionWrapper,
    month_num: int,
    value: float,
) -> None:
    try:
        conn.execute("""
            INSERT INTO seasonal_indices (month_num, index_value)
            VALUES (%s, %s)
            ON CONFLICT(month_num) DO UPDATE SET
                index_value = excluded.index_value,
                updated_at = CURRENT_TIMESTAMP::text
        """, (month_num, value))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('upsert_seasonal_index failed for month_num=%s: %s', month_num, exc)
        raise DatabaseError(f'upsert_seasonal_index failed: {exc}') from exc


# ---------------------------------------------------------------------------
# SKU-level seasonal indices
# ---------------------------------------------------------------------------
def get_sku_seasonal_indices(conn: ConnectionWrapper) -> dict[str, dict[int, float]]:
    """Return SKU-level seasonal indices as {sku: {month_num: value}}."""
    try:
        rows = conn.execute(
            "SELECT sku, month_num, index_value FROM sku_seasonal_indices ORDER BY sku, month_num"
        ).fetchall()
        result: dict[str, dict[int, float]] = {}
        for r in rows:
            sku = r['sku']
            if sku not in result:
                result[sku] = {}
            result[sku][int(r['month_num'])] = float(r['index_value'] or 1.0)
        return result
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_sku_seasonal_indices failed: %s', exc)
        raise DatabaseError(f'get_sku_seasonal_indices failed: {exc}') from exc


def upsert_sku_seasonal_indices(
    conn: ConnectionWrapper,
    sku: str,
    indices: dict[int, float],
    sample_months: int = 0,
) -> None:
    """Upsert all 12 monthly seasonal indices for a single SKU."""
    try:
        for month_num, value in indices.items():
            conn.execute("""
                INSERT INTO sku_seasonal_indices (sku, month_num, index_value, sample_months)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(sku, month_num) DO UPDATE SET
                    index_value = excluded.index_value,
                    sample_months = excluded.sample_months,
                    updated_at = CURRENT_TIMESTAMP::text
            """, (sku, month_num, value, sample_months))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('upsert_sku_seasonal_indices failed for sku=%s: %s', sku, exc)
        raise DatabaseError(f'upsert_sku_seasonal_indices failed: {exc}') from exc


# ---------------------------------------------------------------------------
# App settings
# ---------------------------------------------------------------------------
def get_setting(
    conn: ConnectionWrapper,
    key: str,
    default: Optional[str] = None,
) -> Optional[str]:
    """Get an app setting by key, returning default if not found."""
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = %s", (key,)
        ).fetchone()
        return row["value"] if row else default
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_setting failed for key=%s: %s', key, exc)
        raise DatabaseError(f'get_setting failed: {exc}') from exc


def set_setting(conn: ConnectionWrapper, key: str, value: Any) -> None:
    try:
        conn.execute("""
            INSERT INTO app_settings (key, value)
            VALUES (%s, %s)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP::text
        """, (key, str(value)))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('set_setting failed for key=%s: %s', key, exc)
        raise DatabaseError(f'set_setting failed: {exc}') from exc


# ---------------------------------------------------------------------------
# Revenue Model
# ---------------------------------------------------------------------------
def get_revenue_model(conn: ConnectionWrapper) -> dict:
    """Load revenue model inputs from app_settings.

    Returns {variable: {month: value}} dict.
    """
    raw = get_setting(conn, 'revenue_model')
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning('Failed to parse revenue_model setting, returning empty')
        return {}


def save_revenue_model_bulk(conn: ConnectionWrapper, live: dict) -> None:
    """Persist revenue model inputs to app_settings as JSON.

    live: {variable: {month: value}}
    """
    set_setting(conn, 'revenue_model', json.dumps(live))


# ---------------------------------------------------------------------------
# Klaviyo
# ---------------------------------------------------------------------------
def upsert_klaviyo_campaign(
    conn: ConnectionWrapper,
    campaign: dict[str, Any],
) -> None:
    try:
        conn.execute("""
            INSERT INTO klaviyo_campaigns (id, name, status, send_time, created_at, updated_at, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP::text)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name, status = excluded.status,
                send_time = excluded.send_time, created_at = excluded.created_at,
                updated_at = excluded.updated_at, synced_at = CURRENT_TIMESTAMP::text
        """, (campaign["id"], campaign["name"], campaign["status"],
              campaign["send_time"], campaign["created_at"], campaign["updated_at"]))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('upsert_klaviyo_campaign failed for id=%s: %s', campaign.get("id"), exc)
        raise DatabaseError(f'upsert_klaviyo_campaign failed: {exc}') from exc


def upsert_klaviyo_flow(
    conn: ConnectionWrapper,
    flow: dict[str, Any],
) -> None:
    try:
        conn.execute("""
            INSERT INTO klaviyo_flows (id, name, status, trigger_type, created, updated, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP::text)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name, status = excluded.status,
                trigger_type = excluded.trigger_type, created = excluded.created,
                updated = excluded.updated, synced_at = CURRENT_TIMESTAMP::text
        """, (flow["id"], flow["name"], flow["status"],
              flow["trigger_type"], flow["created"], flow["updated"]))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('upsert_klaviyo_flow failed for id=%s: %s', flow.get("id"), exc)
        raise DatabaseError(f'upsert_klaviyo_flow failed: {exc}') from exc


def upsert_klaviyo_list(
    conn: ConnectionWrapper,
    lst: dict[str, Any],
) -> None:
    try:
        conn.execute("""
            INSERT INTO klaviyo_lists (id, name, created, updated, synced_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP::text)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name, created = excluded.created,
                updated = excluded.updated, synced_at = CURRENT_TIMESTAMP::text
        """, (lst["id"], lst["name"], lst["created"], lst["updated"]))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('upsert_klaviyo_list failed for id=%s: %s', lst.get("id"), exc)
        raise DatabaseError(f'upsert_klaviyo_list failed: {exc}') from exc


def upsert_threpl_invoice(
    conn: ConnectionWrapper,
    invoice_number: str,
    week_start: str,
    week_end: str,
    invoice_date: str,
    total_amount: float,
    line_items_json: str,
) -> None:
    try:
        conn.execute("""
            INSERT INTO threpl_invoices
                (invoice_number, week_start, week_end, invoice_date, total_amount, line_items, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP::text)
            ON CONFLICT(invoice_number) DO UPDATE SET
                week_start = excluded.week_start,
                week_end = excluded.week_end,
                invoice_date = excluded.invoice_date,
                total_amount = excluded.total_amount,
                line_items = excluded.line_items,
                created_at = CURRENT_TIMESTAMP::text
        """, (invoice_number, week_start, week_end, invoice_date,
              total_amount, line_items_json))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('upsert_threpl_invoice failed for #%s: %s', invoice_number, exc)
        raise DatabaseError(f'upsert_threpl_invoice failed: {exc}') from exc


def get_klaviyo_campaigns(conn: ConnectionWrapper) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT id, name, status, send_time, created_at, updated_at, "
            "recipients, delivered, opens_unique, clicks_unique, "
            "open_rate, click_rate, click_to_open_rate, "
            "unsubscribes, unsubscribe_rate, bounce_rate, "
            "revenue, revenue_per_recipient, average_order_value, conversion_rate "
            "FROM klaviyo_campaigns ORDER BY send_time DESC NULLS LAST"
        ).fetchall()
        return [dict(r) for r in rows]
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_klaviyo_campaigns failed: %s', exc)
        raise DatabaseError(f'get_klaviyo_campaigns failed: {exc}') from exc


def get_klaviyo_flows(conn: ConnectionWrapper) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT id, name, status, trigger_type, created, updated, "
            "recipients, delivered, opens_unique, clicks_unique, "
            "open_rate, click_rate, click_to_open_rate, "
            "unsubscribes, unsubscribe_rate, bounce_rate, "
            "revenue, revenue_per_recipient, average_order_value, conversion_rate "
            "FROM klaviyo_flows ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_klaviyo_flows failed: %s', exc)
        raise DatabaseError(f'get_klaviyo_flows failed: {exc}') from exc


def get_klaviyo_lists(conn: ConnectionWrapper) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT id, name, created, updated "
            "FROM klaviyo_lists ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_klaviyo_lists failed: %s', exc)
        raise DatabaseError(f'get_klaviyo_lists failed: {exc}') from exc


_METRIC_COLS = [
    "recipients", "delivered", "opens_unique", "clicks_unique",
    "open_rate", "click_rate", "click_to_open_rate",
    "unsubscribes", "unsubscribe_rate", "bounce_rate",
    "revenue", "revenue_per_recipient", "average_order_value", "conversion_rate",
]


def update_klaviyo_campaign_metrics(
    conn: ConnectionWrapper,
    campaign_id: str,
    metrics: dict[str, Any],
) -> None:
    try:
        set_clause = ", ".join(f"{c} = %s" for c in _METRIC_COLS)
        conn.execute(
            f"UPDATE klaviyo_campaigns SET {set_clause} WHERE id = %s",
            tuple(metrics.get(c, 0) or 0 for c in _METRIC_COLS) + (campaign_id,),
        )
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('update_klaviyo_campaign_metrics failed for id=%s: %s', campaign_id, exc)
        raise DatabaseError(f'update_klaviyo_campaign_metrics failed: {exc}') from exc


def update_klaviyo_flow_metrics(
    conn: ConnectionWrapper,
    flow_id: str,
    metrics: dict[str, Any],
) -> None:
    try:
        set_clause = ", ".join(f"{c} = %s" for c in _METRIC_COLS)
        conn.execute(
            f"UPDATE klaviyo_flows SET {set_clause} WHERE id = %s",
            tuple(metrics.get(c, 0) or 0 for c in _METRIC_COLS) + (flow_id,),
        )
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('update_klaviyo_flow_metrics failed for id=%s: %s', flow_id, exc)
        raise DatabaseError(f'update_klaviyo_flow_metrics failed: {exc}') from exc


# ---------------------------------------------------------------------------
# Model run tracking
# ---------------------------------------------------------------------------
def log_model_run(
    conn: ConnectionWrapper,
    model_name: str,
    duration_seconds: float,
    status: str = 'success',
    error_message: Optional[str] = None,
    triggered_by: str = 'manual',
) -> None:
    """Record the completion of a model run with timestamp and duration."""
    try:
        conn.execute("""
            INSERT INTO model_runs (model_name, last_run_at, duration_seconds, status, error_message, triggered_by)
            VALUES (%s, CURRENT_TIMESTAMP::text, %s, %s, %s, %s)
            ON CONFLICT(model_name) DO UPDATE SET
                last_run_at = CURRENT_TIMESTAMP::text,
                duration_seconds = excluded.duration_seconds,
                status = excluded.status,
                error_message = excluded.error_message,
                triggered_by = excluded.triggered_by
        """, (model_name, duration_seconds, status, error_message, triggered_by))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('log_model_run failed for model=%s: %s', model_name, exc)
        raise DatabaseError(f'log_model_run failed: {exc}') from exc


def get_model_runs(conn: ConnectionWrapper) -> dict[str, dict[str, Any]]:
    """Return all model run records as {model_name: {last_run_at, duration_seconds, status, ...}}."""
    try:
        rows = conn.execute(
            "SELECT model_name, last_run_at, duration_seconds, status, error_message, triggered_by "
            "FROM model_runs ORDER BY model_name"
        ).fetchall()
        return {r['model_name']: dict(r) for r in rows}
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_model_runs failed: %s', exc)
        raise DatabaseError(f'get_model_runs failed: {exc}') from exc


def get_model_run(conn: ConnectionWrapper, model_name: str) -> Optional[dict[str, Any]]:
    """Return a single model run record or None."""
    try:
        row = conn.execute(
            "SELECT model_name, last_run_at, duration_seconds, status, error_message, triggered_by "
            "FROM model_runs WHERE model_name = %s",
            (model_name,)
        ).fetchone()
        return dict(row) if row else None
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_model_run failed for model=%s: %s', model_name, exc)
        raise DatabaseError(f'get_model_run failed: {exc}') from exc


# ---------------------------------------------------------------------------
# Page error logging
# ---------------------------------------------------------------------------
def log_page_error(
    conn: ConnectionWrapper,
    page: str,
    error_type: str,
    error_message: str,
    traceback_str: Optional[str] = None,
) -> None:
    """Log a page rendering error for automated monitoring."""
    try:
        conn.execute("""
            INSERT INTO page_errors (page, error_type, error_message, traceback)
            VALUES (%s, %s, %s, %s)
        """, (page, error_type, error_message[:2000], (traceback_str or '')[:10000]))
    except Exception as exc:
        logger.error('log_page_error failed for page=%s: %s', page, exc)


def get_recent_page_errors(conn: ConnectionWrapper, hours: int = 24) -> list[dict]:
    """Return page errors from the last N hours."""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    try:
        rows = conn.execute("""
            SELECT id, page, error_type, error_message, traceback, created_at
            FROM page_errors
            WHERE created_at > %s
            ORDER BY created_at DESC
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error('get_recent_page_errors failed: %s', exc)
        return []


# ---------------------------------------------------------------------------
# Pre-computed analytics cache
# ---------------------------------------------------------------------------
def save_precomputed(
    conn: ConnectionWrapper,
    result_key: str,
    result_json: str,
    model_name: Optional[str] = None,
    duration_seconds: Optional[float] = None,
) -> None:
    """Persist a pre-computed analytics result to the database."""
    try:
        conn.execute("""
            INSERT INTO precomputed_analytics (result_key, result_json, computed_at, model_name, duration_seconds)
            VALUES (%s, %s, CURRENT_TIMESTAMP::text, %s, %s)
            ON CONFLICT(result_key) DO UPDATE SET
                result_json = excluded.result_json,
                computed_at = CURRENT_TIMESTAMP::text,
                model_name = excluded.model_name,
                duration_seconds = excluded.duration_seconds
        """, (result_key, result_json, model_name, duration_seconds))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('save_precomputed failed for key=%s: %s', result_key, exc)
        raise DatabaseError(f'save_precomputed failed: {exc}') from exc


def get_precomputed(
    conn: ConnectionWrapper,
    result_key: str,
    max_age_hours: float = 25.0,
) -> Optional[str]:
    """Retrieve a pre-computed result if it exists and is fresh enough.

    Returns the raw JSON string or None if missing/stale.
    """
    try:
        row = conn.execute(
            "SELECT result_json, computed_at FROM precomputed_analytics "
            "WHERE result_key = %s",
            (result_key,)
        ).fetchone()
        if not row:
            return None
        # Check age
        from datetime import datetime, timedelta, timezone
        computed_str = row['computed_at']
        if computed_str:
            try:
                computed_dt = datetime.strptime(computed_str[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
                if computed_dt < cutoff:
                    return None
            except (ValueError, TypeError):
                pass  # If we can't parse the timestamp, return the data anyway
        return row['result_json']
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_precomputed failed for key=%s: %s', result_key, exc)
        raise DatabaseError(f'get_precomputed failed: {exc}') from exc


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
def get_order_items_page(
    conn: ConnectionWrapper,
    page: int,
    per_page: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return a page of order_items rows and the total row count.

    Args:
        conn: Active database connection.
        page: 1-based page number.
        per_page: Number of rows per page.

    Returns:
        Tuple of (items, total_count) where items is a list of dicts
        and total_count is the total number of rows in order_items.
    """
    try:
        offset = (page - 1) * per_page
        total_row = conn.execute("SELECT COUNT(*) AS cnt FROM order_items").fetchone()
        total_count: int = total_row['cnt'] if total_row else 0
        rows = conn.execute(
            "SELECT id, order_id, sku, quantity, unit_price FROM order_items "
            "ORDER BY id LIMIT %s OFFSET %s",
            (per_page, offset),
        ).fetchall()
        items = [dict(row) for row in rows]
        return items, total_count
    except DatabaseError:
        raise
    except Exception as exc:
        raise DatabaseError(f'get_order_items_page failed: {exc}') from exc


# ---------------------------------------------------------------------------
# Inventory snapshots (pre-computed by 6AM scheduler)
# ---------------------------------------------------------------------------
def save_inventory_snapshot(
    conn: ConnectionWrapper,
    source: str,
    data: list[dict[str, Any]],
) -> None:
    """Upsert a JSON inventory snapshot for the given source ('packiyo' or 'amazon')."""
    import json as _json_snap
    try:
        conn.execute("""
            INSERT INTO inventory_snapshot (source, snapshot_data, created_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP::text)
            ON CONFLICT(source) DO UPDATE SET
                snapshot_data = excluded.snapshot_data,
                created_at = CURRENT_TIMESTAMP::text
        """, (source, _json_snap.dumps(data)))
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('save_inventory_snapshot failed for source=%s: %s', source, exc)
        raise DatabaseError(f'save_inventory_snapshot failed: {exc}') from exc


def get_inventory_snapshot(
    conn: ConnectionWrapper,
    source: str,
    max_age_hours: float = 24,
) -> Optional[list[dict[str, Any]]]:
    """Return parsed inventory list if a fresh snapshot exists, else None."""
    import json as _json_snap
    try:
        row = conn.execute(
            "SELECT snapshot_data, created_at FROM inventory_snapshot WHERE source = %s",
            (source,),
        ).fetchone()
        if row is None:
            return None
        from datetime import datetime as _dt_snap, timezone as _tz_snap
        created = _dt_snap.strptime(row['created_at'][:19], '%Y-%m-%d %H:%M:%S')
        age_hours = (_dt_snap.now() - created).total_seconds() / 3600
        if age_hours > max_age_hours:
            return None
        return _json_snap.loads(row['snapshot_data'])
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error('get_inventory_snapshot failed for source=%s: %s', source, exc)
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    print(f"Database initialized (PostgreSQL: {os.environ.get('DATABASE_URL', '')[:30]}...)")
