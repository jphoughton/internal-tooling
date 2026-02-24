"""
Database layer: PostgreSQL schema, connection pool, and upsert operations.

Uses psycopg2 with a connection pool. All SQL uses PostgreSQL syntax natively.
The _translate_sql() helper converts any remaining SQLite-style SQL found in
analytics/dashboard code (strftime, date('now'), julianday, ? placeholders).
"""
from __future__ import annotations

import os
import re
import psycopg2
import psycopg2.extras
import psycopg2.pool
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Optional, Union

if TYPE_CHECKING:
    import pandas as pd

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
            raise RuntimeError(
                'DATABASE_URL is not set. '
                'Set it in .env or Railway service variables.'
            )
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=20,
            dsn=url,
            connect_timeout=10,
            options='-c statement_timeout=30000',  # 30s per statement
        )
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
class _Row(dict):
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
        return _Row(row) if row is not None else None

    def fetchall(self) -> list[_Row]:
        return [_Row(r) for r in self._cur.fetchall()]

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
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        return _CursorWrapper(cur)

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
    raw = pool.getconn()
    wrapped = ConnectionWrapper(raw)
    try:
        yield wrapped
        wrapped.commit()
    except Exception:
        wrapped.rollback()
        raise
    finally:
        pool.putconn(raw)


# ---------------------------------------------------------------------------
# pd.read_sql_query helper
# ---------------------------------------------------------------------------
def read_sql(
    sql: str,
    conn_wrapper: ConnectionWrapper,
    params: Optional[Any] = None,
) -> Any:
    """Execute a SQL query through pandas with automatic SQL translation.

    Usage:
        with get_db() as conn:
            df = read_sql("SELECT ... WHERE sale_date >= date('now', '-30 days')", conn)
    """
    import pandas as pd
    translated = _translate_sql(sql)
    return pd.read_sql_query(translated, conn_wrapper.raw, params=params)


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
        currency TEXT DEFAULT 'USD',
        status TEXT DEFAULT 'completed',
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

    # Indexes
    "CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date)",
    "CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_order_items_sku ON order_items(sku)",
    "CREATE INDEX IF NOT EXISTS idx_daily_sales_sku ON daily_sku_sales(sku)",
    "CREATE INDEX IF NOT EXISTS idx_daily_sales_date ON daily_sku_sales(sale_date)",

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
]


def init_db() -> None:
    """Create all tables and indexes if they don't exist."""
    print("  init_db: connecting...", flush=True)
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
        if existing['cnt'] == 0:
            defaults = {
                1: 0.95, 2: 0.92, 3: 0.98, 4: 1.02, 5: 1.05, 6: 1.10,
                7: 1.12, 8: 1.08, 9: 1.02, 10: 0.98, 11: 0.92, 12: 0.88,
            }
            for m, v in defaults.items():
                conn.execute(
                    "INSERT INTO seasonal_indices (month_num, index_value) VALUES (%s, %s)",
                    (m, v),
                )

        # Migrate legacy media_spend source='all' to 'All Sources'
        print("  init_db: migrating media_spend source values...", flush=True)
        conn.execute("UPDATE media_spend SET source = 'All Sources' WHERE source = 'all'")
        print("  init_db: done.", flush=True)


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
    conn.execute("""
        INSERT INTO customers (customer_id, email, source, first_order_date)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT(customer_id) DO UPDATE SET
            email = COALESCE(excluded.email, customers.email),
            first_order_date = LEAST(customers.first_order_date, excluded.first_order_date)
    """, (customer_id, email, source, first_order_date))


def upsert_order(
    conn: ConnectionWrapper,
    order_id: str,
    source: str,
    source_order_id: str,
    customer_id: Optional[str],
    order_date: str,
    total_amount: float,
    currency: str = "USD",
) -> None:
    conn.execute("""
        INSERT INTO orders (order_id, source, source_order_id, customer_id, order_date, total_amount, currency)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(order_id) DO UPDATE SET
            total_amount = excluded.total_amount,
            status = 'completed'
    """, (order_id, source, source_order_id, customer_id, order_date, total_amount, currency))


def upsert_order_item(
    conn: ConnectionWrapper,
    order_id: str,
    sku: str,
    product_name: Optional[str],
    quantity: int,
    unit_price: float,
) -> None:
    conn.execute("""
        INSERT INTO order_items (order_id, sku, product_name, quantity, unit_price, total_price)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT(order_id, sku) DO UPDATE SET
            quantity = excluded.quantity,
            unit_price = excluded.unit_price,
            total_price = excluded.total_price
    """, (order_id, sku, product_name, quantity, unit_price, quantity * unit_price))


def upsert_sku(
    conn: ConnectionWrapper,
    sku: str,
    product_name: Optional[str],
    category: Optional[str],
    first_sale_date: Optional[str],
    source: str,
) -> None:
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


# ---------------------------------------------------------------------------
# Rebuild aggregate table
# ---------------------------------------------------------------------------
def rebuild_daily_sales(conn: ConnectionWrapper) -> None:
    """Rebuild the daily_sku_sales aggregate table from order_items.

    IMPORTANT: Only rebuilds Shopify data (from order_items/orders tables).
    Amazon data is inserted directly into daily_sku_sales by the Amazon ETL
    and must NOT be deleted here.
    """
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
        GROUP BY o.order_date::date, oi.sku, o.source
    """)


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------
def get_last_sync_date(conn: ConnectionWrapper, source: str) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(sync_date) as last_date FROM sync_log WHERE source = %s AND status = 'success'",
        (source,)
    ).fetchone()
    return row["last_date"] if row and row["last_date"] else None


def log_sync(
    conn: ConnectionWrapper,
    source: str,
    sync_date: str,
    records_fetched: int,
    status: str = "success",
    error_message: Optional[str] = None,
) -> None:
    conn.execute("""
        INSERT INTO sync_log (source, sync_date, records_fetched, status, error_message)
        VALUES (%s, %s, %s, %s, %s)
    """, (source, sync_date, records_fetched, status, error_message))


def get_last_sync_timestamp(conn: ConnectionWrapper, sources: list[str]) -> Optional[str]:
    """Return created_at of the most recent successful sync across given sources."""
    placeholders = ','.join('%s' for _ in sources)
    row = conn.execute(
        f"SELECT MAX(created_at) as last_ts FROM sync_log "
        f"WHERE source IN ({placeholders}) AND status = 'success'",
        sources
    ).fetchone()
    return row['last_ts'] if row and row['last_ts'] else None


def get_new_rows_since_yesterday(conn: ConnectionWrapper, sources: list[str]) -> int:
    """Return total records_fetched for today's syncs across given sources."""
    placeholders = ','.join('%s' for _ in sources)
    row = conn.execute(
        f"SELECT COALESCE(SUM(records_fetched), 0) as total "
        f"FROM sync_log WHERE source IN ({placeholders}) "
        f"AND sync_date = CURRENT_DATE::text AND status = 'success'",
        sources
    ).fetchone()
    return row['total']


def get_synced_sources(conn: ConnectionWrapper, sources: list[str]) -> list[str]:
    """Return list of source names that have at least one successful sync."""
    placeholders = ','.join('%s' for _ in sources)
    rows = conn.execute(
        f"SELECT DISTINCT source FROM sync_log "
        f"WHERE source IN ({placeholders}) AND status = 'success'",
        sources
    ).fetchall()
    return [r['source'] for r in rows]


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
    conn.execute("""
        INSERT INTO media_spend (month, spend, new_customer_roas, source)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT(month, source) DO UPDATE SET
            spend = excluded.spend,
            new_customer_roas = excluded.new_customer_roas,
            updated_at = CURRENT_TIMESTAMP::text
    """, (month, spend, roas, source))


def get_media_spend(
    conn: ConnectionWrapper,
    source: str = "All Sources",
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT month, spend, new_customer_roas FROM media_spend WHERE source = %s ORDER BY month",
        (source,)
    ).fetchall()
    return [{"month": r["month"], "spend": float(r["spend"] or 0), "new_customer_roas": float(r["new_customer_roas"] or 0)} for r in rows]


def upsert_amazon_revenue_forecast(
    conn: ConnectionWrapper,
    month: str,
    revenue: float,
) -> None:
    conn.execute("""
        INSERT INTO amazon_revenue_forecast (month, revenue)
        VALUES (%s, %s)
        ON CONFLICT(month) DO UPDATE SET
            revenue = excluded.revenue,
            updated_at = CURRENT_TIMESTAMP::text
    """, (month, revenue))


def get_amazon_revenue_forecast(conn: ConnectionWrapper) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT month, revenue FROM amazon_revenue_forecast ORDER BY month"
    ).fetchall()
    return [{"month": r["month"], "revenue": float(r["revenue"] or 0)} for r in rows]


def upsert_planned_inbound(
    conn: ConnectionWrapper,
    sku: str,
    month: str,
    units: int,
) -> None:
    conn.execute("""
        INSERT INTO planned_inbound (sku, month, units)
        VALUES (%s, %s, %s)
        ON CONFLICT(sku, month) DO UPDATE SET
            units = excluded.units,
            updated_at = CURRENT_TIMESTAMP::text
    """, (sku, month, units))


def get_planned_inbound(conn: ConnectionWrapper) -> list[dict[str, Any]]:
    """Return all planned inbound entries as list of dicts."""
    rows = conn.execute(
        "SELECT sku, month, units FROM planned_inbound ORDER BY sku, month"
    ).fetchall()
    return [{"sku": r["sku"], "month": r["month"], "units": int(r["units"] or 0)} for r in rows]


def get_planned_inbound_dict(conn: ConnectionWrapper) -> dict[str, dict[str, int]]:
    """Return planned inbound as nested dict: {sku: {month: units}}."""
    rows = conn.execute(
        "SELECT sku, month, units FROM planned_inbound WHERE units > 0"
    ).fetchall()
    result: dict[str, dict[str, int]] = {}
    for r in rows:
        result.setdefault(r["sku"], {})[r["month"]] = int(r["units"] or 0)
    return result


# ---------------------------------------------------------------------------
# Seasonal indices
# ---------------------------------------------------------------------------
def get_seasonal_indices(conn: ConnectionWrapper) -> dict[int, float]:
    """Return seasonal indices as dict {month_num: value}, e.g. {1: 0.95, ...}."""
    rows = conn.execute(
        "SELECT month_num, index_value FROM seasonal_indices ORDER BY month_num"
    ).fetchall()
    return {int(r["month_num"]): float(r["index_value"] or 1.0) for r in rows}


def upsert_seasonal_index(
    conn: ConnectionWrapper,
    month_num: int,
    value: float,
) -> None:
    conn.execute("""
        INSERT INTO seasonal_indices (month_num, index_value)
        VALUES (%s, %s)
        ON CONFLICT(month_num) DO UPDATE SET
            index_value = excluded.index_value,
            updated_at = CURRENT_TIMESTAMP::text
    """, (month_num, value))


# ---------------------------------------------------------------------------
# App settings
# ---------------------------------------------------------------------------
def get_setting(
    conn: ConnectionWrapper,
    key: str,
    default: Optional[str] = None,
) -> Optional[str]:
    """Get an app setting by key, returning default if not found."""
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = %s", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_setting(conn: ConnectionWrapper, key: str, value: Any) -> None:
    conn.execute("""
        INSERT INTO app_settings (key, value)
        VALUES (%s, %s)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = CURRENT_TIMESTAMP::text
    """, (key, str(value)))


# ---------------------------------------------------------------------------
# Klaviyo
# ---------------------------------------------------------------------------
def upsert_klaviyo_campaign(
    conn: ConnectionWrapper,
    campaign: dict[str, Any],
) -> None:
    conn.execute("""
        INSERT INTO klaviyo_campaigns (id, name, status, send_time, created_at, updated_at, synced_at)
        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP::text)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name, status = excluded.status,
            send_time = excluded.send_time, created_at = excluded.created_at,
            updated_at = excluded.updated_at, synced_at = CURRENT_TIMESTAMP::text
    """, (campaign["id"], campaign["name"], campaign["status"],
          campaign["send_time"], campaign["created_at"], campaign["updated_at"]))


def upsert_klaviyo_flow(
    conn: ConnectionWrapper,
    flow: dict[str, Any],
) -> None:
    conn.execute("""
        INSERT INTO klaviyo_flows (id, name, status, trigger_type, created, updated, synced_at)
        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP::text)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name, status = excluded.status,
            trigger_type = excluded.trigger_type, created = excluded.created,
            updated = excluded.updated, synced_at = CURRENT_TIMESTAMP::text
    """, (flow["id"], flow["name"], flow["status"],
          flow["trigger_type"], flow["created"], flow["updated"]))


def upsert_klaviyo_list(
    conn: ConnectionWrapper,
    lst: dict[str, Any],
) -> None:
    conn.execute("""
        INSERT INTO klaviyo_lists (id, name, created, updated, synced_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP::text)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name, created = excluded.created,
            updated = excluded.updated, synced_at = CURRENT_TIMESTAMP::text
    """, (lst["id"], lst["name"], lst["created"], lst["updated"]))


def get_klaviyo_campaigns(conn: ConnectionWrapper) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, status, send_time, created_at, updated_at, "
        "recipients, delivered, opens_unique, clicks_unique, "
        "open_rate, click_rate, click_to_open_rate, "
        "unsubscribes, unsubscribe_rate, bounce_rate, "
        "revenue, revenue_per_recipient, average_order_value, conversion_rate "
        "FROM klaviyo_campaigns ORDER BY send_time DESC NULLS LAST"
    ).fetchall()
    return [dict(r) for r in rows]


def get_klaviyo_flows(conn: ConnectionWrapper) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, status, trigger_type, created, updated, "
        "recipients, delivered, opens_unique, clicks_unique, "
        "open_rate, click_rate, click_to_open_rate, "
        "unsubscribes, unsubscribe_rate, bounce_rate, "
        "revenue, revenue_per_recipient, average_order_value, conversion_rate "
        "FROM klaviyo_flows ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def get_klaviyo_lists(conn: ConnectionWrapper) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, created, updated "
        "FROM klaviyo_lists ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


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
    set_clause = ", ".join(f"{c} = %s" for c in _METRIC_COLS)
    conn.execute(
        f"UPDATE klaviyo_campaigns SET {set_clause} WHERE id = %s",
        tuple(metrics.get(c, 0) or 0 for c in _METRIC_COLS) + (campaign_id,),
    )


def update_klaviyo_flow_metrics(
    conn: ConnectionWrapper,
    flow_id: str,
    metrics: dict[str, Any],
) -> None:
    set_clause = ", ".join(f"{c} = %s" for c in _METRIC_COLS)
    conn.execute(
        f"UPDATE klaviyo_flows SET {set_clause} WHERE id = %s",
        tuple(metrics.get(c, 0) or 0 for c in _METRIC_COLS) + (flow_id,),
    )




# ---------------------------------------------------------------------------
# Export helper
# ---------------------------------------------------------------------------
def fetch_table(conn: ConnectionWrapper, table: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Return (column_names, rows) for a full table scan.

    The table name must be validated against an allowlist by the caller
    before passing here -- it is interpolated directly into SQL.
    """
    cur = conn.execute(f'SELECT * FROM {table} ORDER BY 1')
    columns = [d.name for d in cur.description]
    rows = [tuple(row[col] for col in columns) for row in cur.fetchall()]
    return columns, rows

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    print(f"Database initialized (PostgreSQL: {DATABASE_URL[:30]}...)")
