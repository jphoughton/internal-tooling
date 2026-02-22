"""
Shared fixtures and helpers for data quality tests.

Opens a READ-ONLY psycopg2 connection directly (no app imports)
so tests can never accidentally mutate production data.
"""
import os
import pytest
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Session-scoped read-only connection
# ---------------------------------------------------------------------------
@pytest.fixture(scope='session')
def db():
    """Yield a read-only psycopg2 connection for the entire test session."""
    dsn = os.environ.get('DATABASE_URL', '')
    if dsn.startswith('postgres://'):
        dsn = dsn.replace('postgres://', 'postgresql://', 1)
    if not dsn:
        pytest.skip('DATABASE_URL not set — cannot run data quality tests')

    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True, autocommit=True)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------
def fetchone(db, sql, params=None):
    """Execute SQL and return a single dict row (or None)."""
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()


def fetchall(db, sql, params=None):
    """Execute SQL and return a list of dict rows."""
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def scalar(db, sql, params=None):
    """Execute SQL and return the first column of the first row."""
    with db.cursor() as cur:
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# Skip helpers
# ---------------------------------------------------------------------------
def table_exists(db, table_name):
    """Check if a table exists in the database."""
    return scalar(db,
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table_name,)
    ) > 0


def skip_if_table_empty(db, table_name):
    """Skip the test if the table doesn't exist or has zero rows."""
    if not table_exists(db, table_name):
        pytest.skip(f'Table {table_name} does not exist')
    count = scalar(db, f'SELECT COUNT(*) FROM {table_name}')
    if count == 0:
        pytest.skip(f'Table {table_name} is empty')
    return count


def skip_if_source_never_synced(db, source):
    """Skip if sync_log has no entries for this source."""
    if not table_exists(db, 'sync_log'):
        pytest.skip('sync_log table does not exist')
    count = scalar(db,
        "SELECT COUNT(*) FROM sync_log WHERE source = %s AND status = 'success'",
        (source,)
    )
    if count == 0:
        pytest.skip(f'No successful syncs for source: {source}')
    return count


def is_mock_data(db):
    """Detect if the database contains mock data (WB-/TM-/LB- SKU prefixes)."""
    if not table_exists(db, 'sku_master'):
        return False
    count = scalar(db,
        "SELECT COUNT(*) FROM sku_master "
        "WHERE sku LIKE 'WB-%' OR sku LIKE 'TM-%' OR sku LIKE 'LB-%'"
    )
    return count is not None and count > 5
