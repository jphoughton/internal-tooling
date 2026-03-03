#!/usr/bin/env python3
"""Fix Jameson loan misclassification.

Jameson Companies transactions are loan payments (principal + interest) that were
incorrectly mapped as 'interest_income' (a revenue category).  This creates a
~$110K/month swing: revenue inflated by ~$55K AND expenses understated by ~$55K.

This script:
1. Updates category_mappings to reclassify Jameson patterns from
   interest_income -> loan (principal subcategory).
2. Updates stale match_pattern values to match current normalize_summary()
   output (Task 4 fix changed the regex, making old patterns stale).
3. Directly updates cashflow_transactions for all Jameson rows.
4. Calls reclassify_all_transactions() to ensure consistency.

Safe to run multiple times (idempotent).
"""
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_db, read_sql
from analytics.cashflow import normalize_summary, reclassify_all_transactions

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
log = logging.getLogger(__name__)


def main():
    with get_db() as conn:
        # 1. Show current state
        before = read_sql(
            "SELECT category, COUNT(*) as cnt, SUM(amount) as total "
            "FROM cashflow_transactions "
            "WHERE LOWER(summary) LIKE %s "
            "AND is_transfer = 0 AND is_duplicate = 0 "
            "GROUP BY category",
            conn, params=('%jameson%',),
        )
        log.info('Jameson transactions BEFORE fix:\n%s', before.to_string(index=False))

        # 2. Update category_mappings: all Jameson -> loan/principal
        conn.execute(
            "UPDATE category_mappings SET category = 'loan', subcategory = 'principal' "
            "WHERE match_pattern LIKE %s",
            ('%jameson%',),
        )
        log.info('Updated category_mappings: all Jameson -> loan/principal')

        # 3. Update stale match_pattern values to match current normalize_summary()
        #    Task 4 fix made normalize_summary() strip words >= 8 chars, so old
        #    patterns with 'companies', 'principle', 'interest' no longer match.
        stale = read_sql(
            "SELECT id, match_pattern, raw_example FROM category_mappings "
            "WHERE match_pattern LIKE %s",
            conn, params=('%jameson%',),
        )
        for _, row in stale.iterrows():
            raw = row['raw_example'] or row['match_pattern']
            new_pattern = normalize_summary(raw)
            if new_pattern and new_pattern != row['match_pattern']:
                try:
                    conn.execute(
                        "UPDATE category_mappings SET match_pattern = %s WHERE id = %s",
                        (new_pattern, int(row['id'])),
                    )
                    log.info('Updated pattern: "%s" -> "%s"', row['match_pattern'], new_pattern)
                except Exception as e:
                    # Duplicate pattern — delete the stale one
                    log.info('Pattern "%s" conflicts, deleting stale row id=%s: %s',
                             new_pattern, row['id'], e)
                    conn.execute("DELETE FROM category_mappings WHERE id = %s", (int(row['id']),))

        # 4. Directly update all Jameson transactions to loan/principal
        #    This catches any that reclassify misses due to pattern mismatches.
        conn.execute(
            "UPDATE cashflow_transactions "
            "SET category = 'loan', subcategory = 'principal' "
            "WHERE LOWER(summary) LIKE %s AND category != 'loan'",
            ('%jameson%',),
        )
        log.info('Directly updated Jameson transactions to loan/principal')

        # 5. Reclassify all transactions to ensure full consistency
        updated = reclassify_all_transactions(conn)
        log.info('Reclassified %d transactions total', updated)

        # 6. Verify
        after = read_sql(
            "SELECT category, COUNT(*) as cnt, SUM(amount) as total "
            "FROM cashflow_transactions "
            "WHERE LOWER(summary) LIKE %s "
            "GROUP BY category",
            conn, params=('%jameson%',),
        )
        log.info('Jameson transactions AFTER fix:\n%s', after.to_string(index=False))

        # Also check interest_income has no Jameson left
        remaining = read_sql(
            "SELECT COUNT(*) as cnt FROM cashflow_transactions "
            "WHERE category = 'interest_income' AND LOWER(summary) LIKE %s",
            conn, params=('%jameson%',),
        )
        cnt = int(remaining.iloc[0]['cnt'])
        if cnt == 0:
            log.info('SUCCESS: No Jameson transactions in interest_income')
        else:
            log.error('FAIL: %d Jameson transactions still in interest_income', cnt)


if __name__ == '__main__':
    main()
