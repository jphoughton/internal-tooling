"""
Database layer: SQLite schema, connection helpers, and upsert operations.
"""
import sqlite3
from contextlib import contextmanager
from config import DB_PATH


def get_connection():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables if they don't exist."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS customers (
                customer_id TEXT PRIMARY KEY,
                email TEXT,
                source TEXT NOT NULL,  -- 'amazon' or 'shopify'
                first_order_date TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_order_id TEXT NOT NULL,
                customer_id TEXT,
                order_date TEXT NOT NULL,
                total_amount REAL DEFAULT 0,
                currency TEXT DEFAULT 'USD',
                status TEXT DEFAULT 'completed',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
            );

            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                product_name TEXT,
                quantity INTEGER NOT NULL DEFAULT 1,
                unit_price REAL NOT NULL DEFAULT 0,
                total_price REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (order_id) REFERENCES orders(order_id),
                UNIQUE(order_id, sku)
            );

            CREATE TABLE IF NOT EXISTS sku_master (
                sku TEXT PRIMARY KEY,
                product_name TEXT,
                category TEXT,
                first_sale_date TEXT,
                sources TEXT,  -- comma-separated: 'amazon,shopify'
                is_active INTEGER DEFAULT 1,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS daily_sku_sales (
                sale_date TEXT NOT NULL,
                sku TEXT NOT NULL,
                source TEXT NOT NULL,
                units_sold INTEGER DEFAULT 0,
                revenue REAL DEFAULT 0,
                order_count INTEGER DEFAULT 0,
                PRIMARY KEY (sale_date, sku, source)
            );

            CREATE TABLE IF NOT EXISTS media_spend (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL,
                spend REAL NOT NULL DEFAULT 0,
                new_customer_roas REAL NOT NULL DEFAULT 1.0,
                source TEXT DEFAULT 'all',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(month, source)
            );

            CREATE TABLE IF NOT EXISTS amazon_revenue_forecast (
                month TEXT PRIMARY KEY,
                revenue REAL NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS planned_inbound (
                sku TEXT NOT NULL,
                month TEXT NOT NULL,
                units INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (sku, month)
            );

            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                sync_date TEXT NOT NULL,
                records_fetched INTEGER DEFAULT 0,
                status TEXT DEFAULT 'success',
                error_message TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS seasonal_indices (
                month_num INTEGER PRIMARY KEY,  -- 1-12
                index_value REAL NOT NULL DEFAULT 1.0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date);
            CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
            CREATE INDEX IF NOT EXISTS idx_order_items_sku ON order_items(sku);
            CREATE INDEX IF NOT EXISTS idx_daily_sales_sku ON daily_sku_sales(sku);
            CREATE INDEX IF NOT EXISTS idx_daily_sales_date ON daily_sku_sales(sale_date);
        """)

        # Seed default seasonal indices if table is empty (Hydrant's hydration seasonality)
        existing = conn.execute("SELECT COUNT(*) FROM seasonal_indices").fetchone()[0]
        if existing == 0:
            defaults = {
                1: 0.95, 2: 0.92, 3: 0.98, 4: 1.02, 5: 1.05, 6: 1.10,
                7: 1.12, 8: 1.08, 9: 1.02, 10: 0.98, 11: 0.92, 12: 0.88,
            }
            for m, v in defaults.items():
                conn.execute(
                    "INSERT INTO seasonal_indices (month_num, index_value) VALUES (?, ?)",
                    (m, v),
                )


def upsert_customer(conn, customer_id, email, source, first_order_date):
    conn.execute("""
        INSERT INTO customers (customer_id, email, source, first_order_date)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(customer_id) DO UPDATE SET
            email = COALESCE(excluded.email, customers.email),
            first_order_date = MIN(customers.first_order_date, excluded.first_order_date)
    """, (customer_id, email, source, first_order_date))


def upsert_order(conn, order_id, source, source_order_id, customer_id, order_date, total_amount, currency="USD"):
    conn.execute("""
        INSERT INTO orders (order_id, source, source_order_id, customer_id, order_date, total_amount, currency)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(order_id) DO UPDATE SET
            total_amount = excluded.total_amount,
            status = 'completed'
    """, (order_id, source, source_order_id, customer_id, order_date, total_amount, currency))


def upsert_order_item(conn, order_id, sku, product_name, quantity, unit_price):
    conn.execute("""
        INSERT INTO order_items (order_id, sku, product_name, quantity, unit_price, total_price)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(order_id, sku) DO UPDATE SET
            quantity = excluded.quantity,
            unit_price = excluded.unit_price,
            total_price = excluded.total_price
    """, (order_id, sku, product_name, quantity, unit_price, quantity * unit_price))


def upsert_sku(conn, sku, product_name, category, first_sale_date, source):
    # Get existing sources
    row = conn.execute("SELECT sources FROM sku_master WHERE sku = ?", (sku,)).fetchone()
    if row and row["sources"]:
        existing = set(row["sources"].split(","))
        existing.add(source)
        sources = ",".join(sorted(existing))
    else:
        sources = source

    conn.execute("""
        INSERT INTO sku_master (sku, product_name, category, first_sale_date, sources)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(sku) DO UPDATE SET
            product_name = COALESCE(excluded.product_name, sku_master.product_name),
            first_sale_date = MIN(sku_master.first_sale_date, excluded.first_sale_date),
            sources = ?,
            updated_at = CURRENT_TIMESTAMP
    """, (sku, product_name, category, first_sale_date, sources, sources))


def rebuild_daily_sales(conn):
    """Rebuild the daily_sku_sales aggregate table from order_items.

    IMPORTANT: Only rebuilds Shopify data (from order_items/orders tables).
    Amazon data is inserted directly into daily_sku_sales by the Amazon ETL
    and must NOT be deleted here.
    """
    # Only delete Shopify rows — Amazon rows are managed by etl/amazon.py
    conn.execute("DELETE FROM daily_sku_sales WHERE source = 'shopify'")
    conn.execute("""
        INSERT INTO daily_sku_sales (sale_date, sku, source, units_sold, revenue, order_count)
        SELECT
            DATE(o.order_date) as sale_date,
            oi.sku,
            o.source,
            SUM(oi.quantity) as units_sold,
            SUM(oi.total_price) as revenue,
            COUNT(DISTINCT o.order_id) as order_count
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.order_id
        WHERE o.status = 'completed' AND o.source = 'shopify'
        GROUP BY DATE(o.order_date), oi.sku, o.source
    """)


def get_last_sync_date(conn, source):
    row = conn.execute(
        "SELECT MAX(sync_date) as last_date FROM sync_log WHERE source = ? AND status = 'success'",
        (source,)
    ).fetchone()
    return row["last_date"] if row and row["last_date"] else None


def log_sync(conn, source, sync_date, records_fetched, status="success", error_message=None):
    conn.execute("""
        INSERT INTO sync_log (source, sync_date, records_fetched, status, error_message)
        VALUES (?, ?, ?, ?, ?)
    """, (source, sync_date, records_fetched, status, error_message))


def get_last_sync_timestamp(conn, sources):
    """Return created_at of the most recent successful sync across given sources."""
    placeholders = ','.join('?' for _ in sources)
    row = conn.execute(
        f"SELECT MAX(created_at) as last_ts FROM sync_log "
        f"WHERE source IN ({placeholders}) AND status = 'success'",
        sources
    ).fetchone()
    return row['last_ts'] if row and row['last_ts'] else None


def get_new_rows_since_yesterday(conn, sources):
    """Return total records_fetched for today's syncs across given sources."""
    placeholders = ','.join('?' for _ in sources)
    row = conn.execute(
        f"SELECT COALESCE(SUM(records_fetched), 0) as total "
        f"FROM sync_log WHERE source IN ({placeholders}) "
        f"AND sync_date = date('now') AND status = 'success'",
        sources
    ).fetchone()
    return row['total']


def get_synced_sources(conn, sources):
    """Return list of source names that have at least one successful sync."""
    placeholders = ','.join('?' for _ in sources)
    rows = conn.execute(
        f"SELECT DISTINCT source FROM sync_log "
        f"WHERE source IN ({placeholders}) AND status = 'success'",
        sources
    ).fetchall()
    return [r['source'] for r in rows]


def upsert_media_spend(conn, month, spend, roas, source="all"):
    conn.execute("""
        INSERT INTO media_spend (month, spend, new_customer_roas, source)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(month, source) DO UPDATE SET
            spend = excluded.spend,
            new_customer_roas = excluded.new_customer_roas,
            updated_at = CURRENT_TIMESTAMP
    """, (month, spend, roas, source))


def get_media_spend(conn, source="all"):
    rows = conn.execute(
        "SELECT month, spend, new_customer_roas FROM media_spend WHERE source = ? ORDER BY month",
        (source,)
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_amazon_revenue_forecast(conn, month, revenue):
    conn.execute("""
        INSERT INTO amazon_revenue_forecast (month, revenue)
        VALUES (?, ?)
        ON CONFLICT(month) DO UPDATE SET
            revenue = excluded.revenue,
            updated_at = CURRENT_TIMESTAMP
    """, (month, revenue))


def get_amazon_revenue_forecast(conn):
    rows = conn.execute(
        "SELECT month, revenue FROM amazon_revenue_forecast ORDER BY month"
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_planned_inbound(conn, sku, month, units):
    conn.execute("""
        INSERT INTO planned_inbound (sku, month, units)
        VALUES (?, ?, ?)
        ON CONFLICT(sku, month) DO UPDATE SET
            units = excluded.units,
            updated_at = CURRENT_TIMESTAMP
    """, (sku, month, units))


def get_planned_inbound(conn):
    """Return all planned inbound entries as list of dicts."""
    rows = conn.execute(
        "SELECT sku, month, units FROM planned_inbound ORDER BY sku, month"
    ).fetchall()
    return [dict(r) for r in rows]


def get_planned_inbound_dict(conn):
    """Return planned inbound as nested dict: {sku: {month: units}}."""
    rows = conn.execute(
        "SELECT sku, month, units FROM planned_inbound WHERE units > 0"
    ).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r["sku"], {})[r["month"]] = r["units"]
    return result


def get_seasonal_indices(conn):
    """Return seasonal indices as dict {month_num: value}, e.g. {1: 0.95, ...}."""
    rows = conn.execute(
        "SELECT month_num, index_value FROM seasonal_indices ORDER BY month_num"
    ).fetchall()
    return {r["month_num"]: r["index_value"] for r in rows}


def upsert_seasonal_index(conn, month_num, value):
    conn.execute("""
        INSERT INTO seasonal_indices (month_num, index_value)
        VALUES (?, ?)
        ON CONFLICT(month_num) DO UPDATE SET
            index_value = excluded.index_value,
            updated_at = CURRENT_TIMESTAMP
    """, (month_num, value))


def get_setting(conn, key, default=None):
    """Get an app setting by key, returning default if not found."""
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("""
        INSERT INTO app_settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = CURRENT_TIMESTAMP
    """, (key, str(value)))


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
