"""
CSV import for bank transactions (Highbeam format).

Handles:
- Parsing Highbeam CSV format (Date, Account, Balance, Id, Direction, Amount, Summary)
- Normalizing account names, amounts, dates
- Deduplicating via tx_id (ON CONFLICT DO NOTHING)
- Auto-classifying using analytics/cashflow.py normalizer + DB mappings

CFO note: This replaces the destructive DROP TABLE approach in settings.py.
Every import is additive -- re-uploading the same CSV is safe (dedup by tx_id).
"""
import hashlib
import io
import logging

import pandas as pd

from db import ConnectionWrapper, upsert_cashflow_tx

log = logging.getLogger(__name__)


def parse_highbeam_csv(file) -> pd.DataFrame:
    """Parse a Highbeam bank CSV export into a normalized DataFrame.

    Args:
        file: file-like object (from st.file_uploader or open())

    Returns:
        DataFrame with columns: tx_id, tx_date, account, direction, amount,
        balance_after, summary, raw_source
    """
    # Read CSV, handling quoted amounts with commas
    if hasattr(file, 'read'):
        content = file.read()
        if isinstance(content, bytes):
            content = content.decode('utf-8')
        df = pd.read_csv(io.StringIO(content))
    else:
        df = pd.read_csv(file)

    if df.empty:
        log.warning('Empty CSV file')
        return pd.DataFrame()

    # Normalize column names (lowercase, strip whitespace)
    df.columns = [c.strip().lower() for c in df.columns]

    # Validate required columns
    required = {'date', 'account', 'direction', 'amount', 'summary'}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            'Missing required columns: %s. Found: %s' % (missing, list(df.columns))
        )

    # Build output DataFrame
    result = pd.DataFrame()

    # tx_id -- use 'id' column if present, otherwise generate deterministic ID
    if 'id' in df.columns:
        result['tx_id'] = df['id'].astype(str).str.strip()
    else:
        # Generate deterministic ID from row content using MD5 (stable across runs)
        result['tx_id'] = df.apply(
            lambda r: 'gen_' + hashlib.md5(
                ('%s_%s_%s_%s' % (
                    r.get('date', ''),
                    r.get('account', ''),
                    r.get('amount', ''),
                    r.get('summary', ''),
                )).encode('utf-8')
            ).hexdigest()[:12],
            axis=1,
        )

    # Date
    result['tx_date'] = pd.to_datetime(
        df['date'], format='mixed'
    ).dt.strftime('%Y-%m-%d')

    # Account -- normalize
    result['account'] = (
        df['account'].astype(str).str.strip().str.lower().str.replace(' ', '_')
    )

    # Direction -- normalize to 'credit' or 'debit'
    result['direction'] = df['direction'].astype(str).str.strip().str.lower()

    # Amount -- always positive, strip commas and dollar signs
    amount_str = (
        df['amount']
        .astype(str)
        .str.replace(',', '', regex=False)
        .str.replace('$', '', regex=False)
        .str.strip()
    )
    result['amount'] = pd.to_numeric(amount_str, errors='coerce').abs().fillna(0)

    # Balance
    if 'balance' in df.columns:
        bal_str = (
            df['balance']
            .astype(str)
            .str.replace(',', '', regex=False)
            .str.replace('$', '', regex=False)
            .str.strip()
        )
        result['balance_after'] = pd.to_numeric(bal_str, errors='coerce')
    else:
        result['balance_after'] = None

    # Summary
    result['summary'] = df['summary'].astype(str).str.strip()

    result['raw_source'] = 'highbeam'

    # Drop rows with no tx_id or no date
    result = result.dropna(subset=['tx_id', 'tx_date'])
    result = result[result['tx_id'] != '']

    log.info('Parsed %d transactions from Highbeam CSV', len(result))
    return result


def import_transactions(conn: ConnectionWrapper, df: pd.DataFrame) -> dict:
    """Import parsed transactions into cashflow_transactions table.

    Deduplicates via tx_id. Auto-classifies using category mappings.

    Args:
        conn: database connection (ConnectionWrapper from db.py)
        df: DataFrame from parse_highbeam_csv()

    Returns:
        dict with keys: total, inserted, skipped, unmapped
    """
    if df.empty:
        return {'total': 0, 'inserted': 0, 'skipped': 0, 'unmapped': 0}

    # Import the classifier (created by another agent, may not exist yet)
    try:
        from analytics.cashflow import classify_transaction
        has_classifier = True
    except ImportError:
        has_classifier = False
        log.info('analytics.cashflow not available -- all transactions will be unmapped')

    inserted = 0
    skipped = 0
    unmapped = 0

    for _, row in df.iterrows():
        tx_id = str(row['tx_id'])
        summary = str(row.get('summary', ''))
        direction = str(row.get('direction', ''))
        amount = float(row.get('amount', 0))
        account = str(row.get('account', ''))

        # Classify
        category = 'unmapped'
        subcategory = None
        is_transfer = 0
        is_duplicate = 0

        if has_classifier:
            try:
                category, subcategory, is_transfer, is_duplicate = classify_transaction(
                    conn, summary, direction, amount, account,
                )
            except Exception as exc:
                log.warning('classify_transaction failed for tx %s: %s', tx_id, exc)
                category = 'unmapped'

        # Check if already exists (upsert_cashflow_tx uses ON CONFLICT DO NOTHING
        # but always returns True, so we need an explicit check for accurate counts)
        existing = conn.execute(
            'SELECT tx_id FROM cashflow_transactions WHERE tx_id = %s', (tx_id,)
        ).fetchone()

        if existing:
            skipped += 1
            continue

        try:
            # balance_after may be NaN from pandas -- convert to None
            balance_after = row.get('balance_after')
            if pd.isna(balance_after):
                balance_after = None
            else:
                balance_after = float(balance_after)

            upsert_cashflow_tx(
                conn,
                tx_id=tx_id,
                tx_date=str(row['tx_date']),
                account=account,
                direction=direction,
                amount=amount,
                balance_after=balance_after,
                summary=summary,
                category=category,
                subcategory=subcategory,
                is_transfer=is_transfer,
                is_duplicate=is_duplicate,
                raw_source=str(row.get('raw_source', 'highbeam')),
            )
            inserted += 1
            if category == 'unmapped':
                unmapped += 1
        except Exception as exc:
            log.warning('Failed to insert tx %s: %s', tx_id, exc)
            skipped += 1

    log.info(
        'Import complete: %d total, %d inserted, %d skipped, %d unmapped',
        len(df), inserted, skipped, unmapped,
    )
    return {
        'total': len(df),
        'inserted': inserted,
        'skipped': skipped,
        'unmapped': unmapped,
    }
