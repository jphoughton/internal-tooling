"""
One-time preload of Highbeam bank transaction CSV into Railway PostgreSQL.

Steps:
  1. Parse CSV using etl/cashflow_csv.py parse_highbeam_csv()
  2. Import transactions using etl/cashflow_csv.py import_transactions()
  3. Auto-populate category_mappings from the regex suggestion engine
  4. Reclassify all transactions in a single efficient pass
  5. Print verification stats

Usage:
    python scripts/preload_bank_data.py
"""
import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
log = logging.getLogger(__name__)

from db import get_db, read_sql, upsert_category_mapping
from etl.cashflow_csv import parse_highbeam_csv, import_transactions
from analytics.cashflow import normalize_summary, suggest_category


CSV_PATH = os.path.expanduser('~/Downloads/hb_tx-20250702_20260302.csv')


def step1_import_csv():
    """Parse and import the Highbeam CSV into cashflow_transactions."""
    print('=' * 60)
    print('STEP 1: Import CSV transactions')
    print('=' * 60)

    if not os.path.exists(CSV_PATH):
        print('ERROR: CSV file not found at %s' % CSV_PATH)
        sys.exit(1)

    print('Reading CSV: %s' % CSV_PATH)
    with open(CSV_PATH, 'r') as f:
        df = parse_highbeam_csv(f)

    print('Parsed %d transactions from CSV' % len(df))
    print('  Date range: %s to %s' % (df['tx_date'].min(), df['tx_date'].max()))
    print('  Accounts: %s' % ', '.join(df['account'].unique()))

    with get_db() as conn:
        stats = import_transactions(conn, df)

    print('\nImport results:')
    print('  Total rows: %d' % stats['total'])
    print('  Inserted:   %d' % stats['inserted'])
    print('  Skipped:    %d (already existed)' % stats['skipped'])
    print('  Unmapped:   %d' % stats['unmapped'])
    return stats


def step2_auto_populate_mappings():
    """For each unique normalized pattern, if suggest_category() returns
    a non-'unmapped' result, insert it into category_mappings.
    """
    print('\n' + '=' * 60)
    print('STEP 2: Auto-populate category_mappings from regex suggestions')
    print('=' * 60)

    with get_db() as conn:
        # Get all unique summaries from cashflow_transactions
        df = read_sql(
            'SELECT DISTINCT summary FROM cashflow_transactions WHERE summary IS NOT NULL',
            conn,
        )

    if df.empty:
        print('No transactions found in DB.')
        return 0

    # Build a map of normalized_pattern -> (category, subcategory, raw_example)
    pattern_map = {}
    for _, row in df.iterrows():
        raw = row['summary'] or ''
        normalized = normalize_summary(raw)
        if not normalized:
            continue
        if normalized in pattern_map:
            continue  # already processed this pattern

        cat, sub = suggest_category(normalized)
        if cat != 'unmapped':
            is_transfer = 1 if cat == 'internal_transfer' else 0
            is_dup = 1 if cat == 'duplicate' else 0
            pattern_map[normalized] = {
                'category': cat,
                'subcategory': sub,
                'raw_example': raw,
                'is_transfer': is_transfer,
                'is_duplicate': is_dup,
            }

    print('Found %d unique patterns with regex suggestions' % len(pattern_map))

    # Insert mappings into DB
    inserted = 0
    with get_db() as conn:
        for pattern, info in pattern_map.items():
            upsert_category_mapping(
                conn,
                match_pattern=pattern,
                category=info['category'],
                subcategory=info['subcategory'],
                raw_example=info['raw_example'],
                is_transfer=info['is_transfer'],
                is_duplicate=info['is_duplicate'],
            )
            inserted += 1

    print('Inserted/updated %d category mappings' % inserted)

    # Print the mappings for review
    print('\nAuto-generated mappings:')
    for pattern, info in sorted(pattern_map.items(), key=lambda x: x[1]['category']):
        sub_str = ' / %s' % info['subcategory'] if info['subcategory'] else ''
        flags = ''
        if info['is_transfer']:
            flags += ' [TRANSFER]'
        if info['is_duplicate']:
            flags += ' [DUPLICATE]'
        print('  %-40s -> %s%s%s' % (pattern[:40], info['category'], sub_str, flags))

    return inserted


def step3_reclassify_all():
    """Efficient single-pass reclassification of all transactions.

    Instead of the O(M*N) approach in analytics/cashflow.reclassify_all_transactions()
    which loads ALL transactions for EACH mapping, this does it in O(N):
      1. Load all category_mappings into a dict (pattern -> classification)
      2. Load all transactions once
      3. For each transaction, normalize its summary and look up the mapping
      4. Batch UPDATE via a single pass
    """
    print('\n' + '=' * 60)
    print('STEP 3: Reclassify all transactions (efficient single-pass)')
    print('=' * 60)

    with get_db() as conn:
        # 1. Load all mappings into memory
        mapping_rows = conn.execute(
            'SELECT match_pattern, category, subcategory, is_transfer, is_duplicate '
            'FROM category_mappings'
        ).fetchall()

        if not mapping_rows:
            print('No category mappings found. Skipping reclassification.')
            return 0

        mappings = {}
        for row in mapping_rows:
            mappings[row['match_pattern']] = {
                'category': row['category'],
                'subcategory': row['subcategory'],
                'is_transfer': int(row['is_transfer'] or 0),
                'is_duplicate': int(row['is_duplicate'] or 0),
            }

        print('Loaded %d category mappings' % len(mappings))

        # 2. Load all transactions once
        tx_rows = conn.execute(
            'SELECT tx_id, summary FROM cashflow_transactions'
        ).fetchall()

        print('Processing %d transactions...' % len(tx_rows))

        # 3. Single-pass: normalize each summary, look up mapping, update if needed
        updated = 0
        for tx in tx_rows:
            summary = tx['summary'] or ''
            normalized = normalize_summary(summary)
            if not normalized:
                continue

            mapping = mappings.get(normalized)
            if mapping:
                conn.execute(
                    'UPDATE cashflow_transactions '
                    'SET category = %s, subcategory = %s, '
                    'is_transfer = %s, is_duplicate = %s '
                    'WHERE tx_id = %s',
                    (mapping['category'], mapping['subcategory'],
                     mapping['is_transfer'], mapping['is_duplicate'],
                     tx['tx_id']),
                )
                updated += 1

    print('Updated %d transactions with categories' % updated)
    return updated


def step4_verify():
    """Print verification stats."""
    print('\n' + '=' * 60)
    print('STEP 4: Verification')
    print('=' * 60)

    with get_db() as conn:
        # Total transaction count
        total = conn.execute(
            'SELECT COUNT(*) as cnt FROM cashflow_transactions'
        ).fetchone()
        print('\nTotal transactions: %d' % total['cnt'])

        # Date range
        date_range = conn.execute(
            'SELECT MIN(tx_date) as min_date, MAX(tx_date) as max_date '
            'FROM cashflow_transactions'
        ).fetchone()
        print('Date range: %s to %s' % (date_range['min_date'], date_range['max_date']))

        # Count by category
        cat_counts = conn.execute(
            'SELECT COALESCE(category, \'unmapped\') as category, COUNT(*) as cnt '
            'FROM cashflow_transactions '
            'GROUP BY category '
            'ORDER BY cnt DESC'
        ).fetchall()

        print('\nTransactions by category:')
        for row in cat_counts:
            print('  %-25s %d' % (row['category'], row['cnt']))

        # Count of unmapped
        unmapped = conn.execute(
            'SELECT COUNT(*) as cnt FROM cashflow_transactions '
            'WHERE category = \'unmapped\' OR category IS NULL'
        ).fetchone()
        print('\nUnmapped transactions: %d' % unmapped['cnt'])

        # Count of category mappings
        mapping_count = conn.execute(
            'SELECT COUNT(*) as cnt FROM category_mappings'
        ).fetchone()
        print('Category mappings in DB: %d' % mapping_count['cnt'])

        # Coverage percentage
        if total['cnt'] > 0:
            mapped_pct = ((total['cnt'] - unmapped['cnt']) / total['cnt']) * 100
            print('Classification coverage: %.1f%%' % mapped_pct)


if __name__ == '__main__':
    print('Highbeam Bank Data Preload Script')
    print('Database: Railway PostgreSQL (from DATABASE_URL)')
    print()

    stats = step1_import_csv()
    step2_auto_populate_mappings()
    step3_reclassify_all()
    step4_verify()

    print('\n' + '=' * 60)
    print('DONE')
    print('=' * 60)
