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
from etl.retry import with_retry

logger = logging.getLogger(__name__)

# Default: Hydrant Daily Data sheet
DEFAULT_SHEET_ID = "14ehOxYbykZLBi-rC8g9gijqVcrsT-xRx-j98HJuFJ0s"
DEFAULT_GID = "786086379"  # "Daily Data" tab


def _build_csv_url(sheet_id=None, gid=None):
    """Build the public CSV export URL for a Google Sheets tab."""
    sid = sheet_id or DEFAULT_SHEET_ID
    g = gid or DEFAULT_GID
    return f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&gid={g}"


@with_retry
def _fetch_sheet_csv(url, timeout):
    """Fetch a single CSV export URL from Google Sheets."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp


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
        resp = _fetch_sheet_csv(url, timeout)
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

    # Backfill missing DTC spend for recent days using L7D average.
    # The Daily Data sheet often lags a few days — gap-fill so MTD pacing
    # is accurate (mirrors the Amazon spend backfill logic).
    if "blended_ad_spend" in df.columns and "date" in df.columns:
        try:
            from datetime import datetime, timedelta
            _yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

            # Parse spend to numeric for L7D calculation
            _spend_vals = (
                df["blended_ad_spend"].astype(str)
                .str.replace("$", "", regex=False)
                .str.replace(",", "", regex=False)
                .str.replace("%", "", regex=False)
                .str.strip()
            )
            _spend_num = pd.to_numeric(_spend_vals, errors="coerce")
            _has_spend = _spend_num.notna() & (_spend_num > 0)

            if _has_spend.any():
                _l7d_avg = _spend_num[_has_spend].tail(7).mean()

                # Parse dates for comparison
                _dates = pd.to_datetime(df["date"], format="mixed", dayfirst=False, errors="coerce")
                _date_strs = _dates.dt.strftime("%Y-%m-%d")
                _max_date = _date_strs.max()

                # Insert missing rows between last sheet date and yesterday
                if _max_date and _max_date < _yesterday_str:
                    _missing = pd.date_range(
                        start=pd.Timestamp(_max_date) + timedelta(days=1),
                        end=_yesterday_str, freq="D",
                    )
                    _n_gap = len(_missing)
                    if _n_gap > 0:
                        _spend_str = f"${_l7d_avg:,.2f}"
                        for _d in _missing:
                            _vals = {c: "" for c in df.columns}
                            _vals["date"] = _d.strftime("%m/%d/%Y")
                            _vals["blended_ad_spend"] = _spend_str
                            conn.execute(
                                f"INSERT INTO google_sheet_data ({col_names}) VALUES ({placeholders})",
                                tuple(_vals.get(c, "") for c in df.columns),
                            )
                        logger.info("DTC spend backfill: added %d gap days with L7D avg $%.2f",
                                    _n_gap, _l7d_avg)
        except Exception as e:
            logger.warning("DTC spend backfill failed: %s", e)

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
        resp = _fetch_sheet_csv(url, timeout)
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

    Columns used: date, spend
    (Customer counts, revenue, and other columns are NOT used — spend only.)
    Full refresh on each sync.

    Returns:
        int: number of rows synced, or -1 on error
    """
    df = fetch_amazon_rollup_tab()
    if df.empty:
        return -1

    # Clean column names
    df.columns = [c.strip().lower().replace(" ", "_").replace("-", "_") for c in df.columns]

    # Keep only relevant columns (date + spend only)
    col_map = {}
    for c in df.columns:
        if "date" in c:
            col_map[c] = "date"
        elif "spend" in c:
            col_map[c] = "spend"

    if "date" not in col_map.values():
        logger.error("Amazon Roll Up Date: no 'date' column found")
        return -1

    df = df.rename(columns=col_map)
    keep_cols = [c for c in ["date", "spend"] if c in df.columns]
    df = df[keep_cols].copy()

    # Drop rows with empty/zero dates
    df = df.dropna(subset=["date"])
    df = df[df["date"].astype(str).str.strip() != ""]

    # Parse dates to standard format
    df["date"] = pd.to_datetime(df["date"], format="mixed", dayfirst=False).dt.strftime("%Y-%m-%d")

    # Parse numeric columns (strip $ and commas)
    if "spend" in df.columns:
        df["spend"] = df["spend"].astype(str).str.replace("$", "", regex=False).str.replace(",", "", regex=False).str.strip()
        df["spend"] = pd.to_numeric(df["spend"], errors="coerce")

    # Backfill missing/zero spend for past days (before today) using L7D average.
    # The Google Sheet often lags a few days on spend data — NaN or 0 means
    # "not yet reported", not "zero spend". We estimate those gap days so MTD
    # pacing is accurate.
    if "spend" in df.columns:
        from datetime import datetime, timedelta
        today_str = datetime.now().strftime("%Y-%m-%d")
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        has_spend = df["spend"].notna() & (df["spend"] > 0)
        if has_spend.any():
            l7d_avg = df.loc[has_spend, "spend"].tail(7).mean()
            # Backfill rows with NaN or 0 spend on days before today
            gap_mask = ((df["spend"].isna()) | (df["spend"] == 0)) & (df["date"] < today_str)
            if gap_mask.any():
                df.loc[gap_mask, "spend"] = round(l7d_avg, 2)
                logger.info(f"Backfilled {gap_mask.sum()} days with L7D avg spend ${l7d_avg:.2f}")
            # Add missing date rows between last sheet row and yesterday
            max_date = df["date"].max()
            if max_date < yesterday_str:
                missing_dates = pd.date_range(
                    start=pd.Timestamp(max_date) + timedelta(days=1),
                    end=yesterday_str,
                    freq="D",
                )
                if len(missing_dates) > 0:
                    new_rows = pd.DataFrame({
                        "date": [d.strftime("%Y-%m-%d") for d in missing_dates],
                        "spend": round(l7d_avg, 2),
                    })
                    df = pd.concat([df, new_rows], ignore_index=True)
                    logger.info(f"Added {len(missing_dates)} missing date rows with L7D avg spend ${l7d_avg:.2f}")

    # Fill remaining NaN with 0 and drop rows with zero spend
    if "spend" in df.columns:
        df["spend"] = df["spend"].fillna(0)
        df = df[df["spend"] > 0]

    # Create table
    conn.execute("DROP TABLE IF EXISTS amazon_daily_rollup")
    conn.execute("""
        CREATE TABLE amazon_daily_rollup (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            spend REAL DEFAULT 0,
            synced_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    for _, row in df.iterrows():
        conn.execute(
            "INSERT INTO amazon_daily_rollup (date, spend) VALUES (?, ?)",
            (row["date"], row.get("spend", 0)),
        )

    logger.info(f"Synced {len(df)} rows to amazon_daily_rollup table")
    return len(df)
