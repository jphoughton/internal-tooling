"""
Google Sheets integration — fetches data from a public/link-shared Google Sheet
as CSV (no API key or OAuth needed).

Usage:
    from etl.google_sheets import fetch_daily_data_tab, sync_google_sheet
    df = fetch_daily_data_tab()
"""
import io
import logging
import re
import pandas as pd
import requests

logger = logging.getLogger(__name__)

# Default: Hydrant Daily Data sheet
DEFAULT_SHEET_ID = "1GE_upVue9Fh4okDs7s3qE1ae54DStY-H0K7MovgCcxk"
DEFAULT_GID = "1661449750"  # "Daily Data" tab


def _build_csv_url(sheet_id=None, gid=None):
    """Build the public CSV export URL for a Google Sheets tab."""
    sid = sheet_id or DEFAULT_SHEET_ID
    g = gid or DEFAULT_GID
    return f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&gid={g}"


def fetch_daily_data_tab(sheet_id=None, gid=None, timeout=30):
    """
    Fetch the Daily Data tab from Google Sheets as a pandas DataFrame.

    The sheet must be link-shareable (Anyone with the link can view).

    Returns:
        pd.DataFrame with the sheet contents, or empty DataFrame on failure.
    """
    url = _build_csv_url(sheet_id, gid)
    logger.info(f"Fetching Google Sheet: {url}")

    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch Google Sheet: {e}")
        return pd.DataFrame()

    try:
        df = pd.read_csv(io.StringIO(resp.text))
        logger.info(f"Fetched {len(df)} rows, {len(df.columns)} columns from Google Sheet")
        return df
    except Exception as e:
        logger.error(f"Failed to parse Google Sheet CSV: {e}")
        return pd.DataFrame()


def sync_google_sheet(conn, sheet_id=None, gid=None):
    """
    Fetch the Daily Data tab and store it in the google_sheet_data table.

    Creates the table dynamically based on the sheet's column structure.
    Replaces all existing data on each sync (full refresh).

    Returns:
        int: number of rows synced, or -1 on error
    """
    df = fetch_daily_data_tab(sheet_id, gid)
    if df.empty:
        return -1

    # Clean column names: strip whitespace, lowercase, replace spaces/hyphens with
    # underscores, then remove any remaining non-alphanumeric chars (e.g. ?, %, /)
    # to prevent them from being misinterpreted as SQL placeholders by psycopg2.
    df.columns = [
        re.sub(r'[^a-z0-9_]', '', c.strip().lower().replace(' ', '_').replace('-', '_'))
        for c in df.columns
    ]
    # Guard against columns that become empty after sanitization (e.g. "?" or "%")
    df.columns = [c if c else f'col_{i}' for i, c in enumerate(df.columns)]

    # Create table dynamically based on columns
    # All columns stored as TEXT for flexibility (the source data types vary)
    col_defs = ", ".join(f'"{c}" TEXT' for c in df.columns)
    conn.execute("DROP TABLE IF EXISTS google_sheet_data")
    conn.execute(f"""
        CREATE TABLE google_sheet_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {col_defs},
            synced_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Insert rows
    placeholders = ", ".join("?" for _ in df.columns)
    col_names = ", ".join(f'"{c}"' for c in df.columns)
    for _, row in df.iterrows():
        conn.execute(
            f"INSERT INTO google_sheet_data ({col_names}) VALUES ({placeholders})",
            tuple(str(v) if pd.notna(v) else None for v in row),
        )

    logger.info(f"Synced {len(df)} rows to google_sheet_data table")
    return len(df)


# ── Amazon Roll Up Date tab ──────────────────────────────────
AMAZON_ROLLUP_SHEET_ID = "1eiSrmsZg-cq4-1_Dx9Cjzkv8rJG7Z_vmRK0OEhvrQ3E"
AMAZON_ROLLUP_GID = "2017726907"


def fetch_amazon_rollup_tab(timeout=30):
    """Fetch the 'Amazon Roll Up Date' tab as a DataFrame."""
    url = _build_csv_url(AMAZON_ROLLUP_SHEET_ID, AMAZON_ROLLUP_GID)
    logger.info(f"Fetching Amazon Roll Up Date: {url}")
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch Amazon Roll Up Date: {e}")
        return pd.DataFrame()
    try:
        df = pd.read_csv(io.StringIO(resp.text))
        logger.info(f"Fetched {len(df)} rows from Amazon Roll Up Date")
        return df
    except Exception as e:
        logger.error(f"Failed to parse Amazon Roll Up Date CSV: {e}")
        return pd.DataFrame()


def sync_amazon_rollup(conn):
    """
    Fetch the Amazon Roll Up Date tab and store in amazon_daily_rollup table.

    Columns used: date, new_customers, new_customer_rev, spend
    (Repeat Revenue and Total Revenue are NOT used per business rules.)
    Full refresh on each sync.

    Returns:
        int: number of rows synced, or -1 on error
    """
    df = fetch_amazon_rollup_tab()
    if df.empty:
        return -1

    # Clean column names
    df.columns = [c.strip().lower().replace(" ", "_").replace("-", "_") for c in df.columns]

    # Keep only relevant columns
    col_map = {}
    for c in df.columns:
        if "date" in c:
            col_map[c] = "date"
        elif "new_customer" in c and "rev" in c:
            col_map[c] = "new_customer_rev"
        elif "new_customer" in c:
            col_map[c] = "new_customers"
        elif "spend" in c:
            col_map[c] = "spend"

    if "date" not in col_map.values():
        logger.error("Amazon Roll Up Date: no 'date' column found")
        return -1

    df = df.rename(columns=col_map)
    keep_cols = [c for c in ["date", "new_customers", "new_customer_rev", "spend"] if c in df.columns]
    df = df[keep_cols].copy()

    # Drop rows with empty/zero dates
    df = df.dropna(subset=["date"])
    df = df[df["date"].astype(str).str.strip() != ""]

    # Parse dates to standard format
    df["date"] = pd.to_datetime(df["date"], format="mixed", dayfirst=False).dt.strftime("%Y-%m-%d")

    # Parse numeric columns (strip $ and commas)
    for c in ["new_customers", "new_customer_rev", "spend"]:
        if c in df.columns:
            df[c] = df[c].astype(str).str.replace("$", "", regex=False).str.replace(",", "", regex=False).str.strip()
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # Backfill missing spend for recent days (up to today) using L7D average.
    # The Google Sheet often lags a few days on spend data — NaN means "not yet
    # reported", not "zero spend". We estimate those gap days so MTD pacing is
    # accurate.
    if "spend" in df.columns:
        from datetime import datetime
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        has_spend = df["spend"].notna() & (df["spend"] > 0)
        if has_spend.any():
            l7d_avg = df.loc[has_spend, "spend"].tail(7).mean()
            gap_mask = df["spend"].isna() & (df["date"] < today_str)
            if gap_mask.any():
                df.loc[gap_mask, "spend"] = round(l7d_avg, 2)
                logger.info(f"Backfilled {gap_mask.sum()} days with L7D avg spend ${l7d_avg:.2f}")

    # Fill remaining NaN with 0 and drop rows with all-zero data
    for c in ["new_customers", "new_customer_rev", "spend"]:
        if c in df.columns:
            df[c] = df[c].fillna(0)

    # Keep only rows that have any non-zero data
    data_cols = [c for c in ["new_customers", "new_customer_rev", "spend"] if c in df.columns]
    df = df[df[data_cols].sum(axis=1) > 0]

    # Create table
    conn.execute("DROP TABLE IF EXISTS amazon_daily_rollup")
    conn.execute("""
        CREATE TABLE amazon_daily_rollup (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            new_customers REAL DEFAULT 0,
            new_customer_rev REAL DEFAULT 0,
            spend REAL DEFAULT 0,
            synced_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    for _, row in df.iterrows():
        conn.execute(
            "INSERT INTO amazon_daily_rollup (date, new_customers, new_customer_rev, spend) VALUES (?, ?, ?, ?)",
            (row["date"], row.get("new_customers", 0), row.get("new_customer_rev", 0), row.get("spend", 0)),
        )

    logger.info(f"Synced {len(df)} rows to amazon_daily_rollup table")
    return len(df)
