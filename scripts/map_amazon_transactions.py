#!/usr/bin/env python3
"""Map Amazon bank transactions to amazon_revenue category.

All Amazon bank deposit transactions (~51) are unmapped (category='unmapped'),
which blocks:
1. Amazon payout ratio auto-calibration (compute_payout_ratio returns None)
2. Accurate actual-vs-projected comparison for Amazon weeks
3. Proper backtest validation

This script:
1. Queries unmapped transactions with "amazon" or "amzn" in the summary.
2. Normalizes each summary to find distinct match patterns.
3. Inserts those patterns into category_mappings as amazon_revenue/disbursement.
4. Runs reclassify_all_transactions() to update all matching transactions.
5. Verifies compute_payout_ratio('amazon') returns a value.

Safe to run multiple times (idempotent).
"""
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_db, read_sql, upsert_category_mapping
from analytics.cashflow import (
    normalize_summary,
    reclassify_all_transactions,
    compute_payout_ratio,
)

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
log = logging.getLogger(__name__)


def main():
    with get_db() as conn:
        # 1. Show current state of Amazon transactions
        before = read_sql(
            "SELECT category, COUNT(*) as cnt, SUM(amount) as total "
            "FROM cashflow_transactions "
            "WHERE (LOWER(summary) LIKE %s OR LOWER(summary) LIKE %s) "
            "AND is_transfer = 0 AND is_duplicate = 0 "
            "GROUP BY category",
            conn, params=('%amazon%', '%amzn%'),
        )
        log.info('Amazon transactions BEFORE fix:\n%s', before.to_string(index=False))

        # 2. Find distinct normalized patterns from Amazon transactions
        amazon_txs = read_sql(
            "SELECT tx_id, summary, direction, amount, tx_date "
            "FROM cashflow_transactions "
            "WHERE (LOWER(summary) LIKE %s OR LOWER(summary) LIKE %s) "
            "AND is_transfer = 0 AND is_duplicate = 0",
            conn, params=('%amazon%', '%amzn%'),
        )
        log.info('Found %d Amazon transactions', len(amazon_txs))

        # Group by normalized pattern
        patterns = {}
        for _, tx in amazon_txs.iterrows():
            norm = normalize_summary(tx['summary'] or '')
            if norm:
                if norm not in patterns:
                    patterns[norm] = {
                        'example': tx['summary'],
                        'count': 0,
                        'total': 0.0,
                        'directions': set(),
                    }
                patterns[norm]['count'] += 1
                patterns[norm]['total'] += float(tx['amount'])
                patterns[norm]['directions'].add(tx['direction'])

        log.info('Found %d distinct normalized patterns:', len(patterns))
        for pat, info in sorted(patterns.items(), key=lambda x: -x[1]['count']):
            log.info('  "%s" — %d txs, $%.2f total, directions=%s, example="%s"',
                     pat, info['count'], info['total'],
                     info['directions'], info['example'])

        # 3. Insert category mappings for each Amazon pattern
        inserted = 0
        for pat, info in patterns.items():
            # Only map credit (deposit) transactions as amazon_revenue
            # Debit transactions from Amazon would be a different category
            if 'credit' in info['directions']:
                upsert_category_mapping(
                    conn,
                    match_pattern=pat,
                    category='amazon_revenue',
                    subcategory='disbursement',
                    raw_example=info['example'],
                    is_transfer=0,
                    is_duplicate=0,
                )
                log.info('Mapped pattern "%s" -> amazon_revenue/disbursement', pat)
                inserted += 1
            else:
                log.info('Skipping debit-only pattern "%s"', pat)

        log.info('Inserted/updated %d category mappings', inserted)

        # 4. Reclassify all transactions
        updated = reclassify_all_transactions(conn)
        log.info('Reclassified %d transactions total', updated)

        # 5. Verify: Amazon transactions should now be categorized
        after = read_sql(
            "SELECT category, COUNT(*) as cnt, SUM(amount) as total "
            "FROM cashflow_transactions "
            "WHERE (LOWER(summary) LIKE %s OR LOWER(summary) LIKE %s) "
            "AND is_transfer = 0 AND is_duplicate = 0 "
            "GROUP BY category",
            conn, params=('%amazon%', '%amzn%'),
        )
        log.info('Amazon transactions AFTER fix:\n%s', after.to_string(index=False))

        remaining = read_sql(
            "SELECT COUNT(*) as cnt FROM cashflow_transactions "
            "WHERE category = 'unmapped' "
            "AND (LOWER(summary) LIKE %s OR LOWER(summary) LIKE %s)",
            conn, params=('%amazon%', '%amzn%'),
        )
        unmapped_cnt = int(remaining.iloc[0]['cnt'])
        if unmapped_cnt == 0:
            log.info('SUCCESS: No Amazon transactions remain unmapped')
        else:
            log.warning('WARN: %d Amazon transactions still unmapped', unmapped_cnt)

        # 6. Verify compute_payout_ratio returns a value
        ratio = compute_payout_ratio(conn, 'amazon')
        if ratio is not None:
            log.info('SUCCESS: compute_payout_ratio("amazon") = %.4f (seed was 0.62)', ratio)
        else:
            log.warning('WARN: compute_payout_ratio("amazon") still returns None '
                        '(may need more data or longer lookback)')


if __name__ == '__main__':
    main()
