"""
Fix customer first_order_date by fetching actual Shopify customer created_at.

Problem: Our sync sets first_order_date = earliest order we imported, but
our data only goes back to 2022-02-23. Customers who first purchased before
that date appear as "new" in Feb/Mar 2022 cohorts, inflating those cohorts
by 2-10x vs Triple Whale.

Fix: Query Shopify's /customers.json API to get each customer's real
created_at timestamp, then update first_order_date where the Shopify date
is earlier than what we have.

Usage:
    python scripts/fix_customer_dates.py          # fix all customers
    python scripts/fix_customer_dates.py --dry-run # show what would change
"""
import sys
import os
import time
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import config as cfg
from db import get_db, init_db, read_sql
from etl.customer_id import generate_customer_id


def get_shopify_customers(since_id=0, limit=250, max_retries=3):
    """Fetch a page of customers from Shopify Admin API with retry."""
    store = cfg.SHOPIFY_STORE_URL.rstrip('/')
    if not store.startswith('http'):
        store = f'https://{store}'
    url = f'{store}/admin/api/{cfg.SHOPIFY_API_VERSION}/customers.json'
    headers = {
        'X-Shopify-Access-Token': cfg.SHOPIFY_ACCESS_TOKEN,
        'Content-Type': 'application/json',
    }
    params = {
        'limit': limit,
        'since_id': since_id,
        'fields': 'id,email,first_name,last_name,created_at,orders_count',
    }
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=60)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get('Retry-After', 2))
                print(f'  Rate limited, waiting {retry_after}s...')
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json().get('customers', [])
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            wait = 2 ** (attempt + 1)
            print(f'  Request failed ({e.__class__.__name__}), retrying in {wait}s... (attempt {attempt + 1}/{max_retries})')
            time.sleep(wait)
    raise RuntimeError(f'Failed to fetch customers after {max_retries} retries (since_id={since_id})')


def main():
    dry_run = '--dry-run' in sys.argv

    # Skip init_db() — schema already exists, and index creation can timeout on Railway

    print('Fetching all customers from Shopify API...')
    all_customers = []
    since_id = 0
    page = 0

    while True:
        page += 1
        customers = get_shopify_customers(since_id=since_id)
        if not customers:
            break
        all_customers.extend(customers)
        since_id = customers[-1]['id']
        print(f'  Page {page}: {len(customers)} customers (total: {len(all_customers):,})')
        time.sleep(0.5)

    print(f'\nFetched {len(all_customers):,} customers from Shopify.')

    # Build mapping: customer_id -> shopify created_at date
    shopify_dates = {}
    for cust in all_customers:
        cid = generate_customer_id(cust)
        if cid and cust.get('created_at'):
            shopify_dates[cid] = cust['created_at'][:10]

    print(f'Mapped {len(shopify_dates):,} customer IDs to Shopify created_at dates.')

    # Reset DB pool — stale after long Shopify fetch
    import db as _db_mod
    if _db_mod._pool is not None:
        _db_mod._pool.closeall()
        _db_mod._pool = None

    # Compare with our database (fresh connection)
    print('Reading current customer dates from DB...')
    with get_db() as conn:
        our_customers = read_sql(
            'SELECT customer_id, first_order_date FROM customers',
            conn,
        )

    print(f'Our DB has {len(our_customers):,} customers.\n')

    updates = []
    for _, row in our_customers.iterrows():
        cid = row['customer_id']
        our_date = str(row['first_order_date'])[:10]
        shopify_date = shopify_dates.get(cid)

        if shopify_date and shopify_date < our_date:
            updates.append((cid, our_date, shopify_date))

    print(f'Found {len(updates):,} customers needing date correction.')
    if updates:
        # Show distribution of how far off dates are
        import collections
        year_dist = collections.Counter()
        for _, our_date, shopify_date in updates:
            year_dist[shopify_date[:4]] += 1
        print('\nTrue first-order year distribution for corrected customers:')
        for year in sorted(year_dist):
            print(f'  {year}: {year_dist[year]:,} customers')

    if dry_run:
        print('\n--dry-run: no changes made.')
        # Show what cohort sizes would look like
        print('\nProjected cohort sizes after fix (2022):')
        import collections
        current_cohorts = collections.Counter()
        fixed_cohorts = collections.Counter()
        update_map = {cid: sd for cid, _, sd in updates}

        for _, row in our_customers.iterrows():
            cid = row['customer_id']
            our_date = str(row['first_order_date'])[:10]
            our_cohort = our_date[:7]
            if our_cohort >= '2022-02' and our_cohort <= '2022-10':
                current_cohorts[our_cohort] += 1

            fixed_date = update_map.get(cid, our_date)
            fixed_cohort = fixed_date[:7]
            if fixed_cohort >= '2022-02' and fixed_cohort <= '2022-10':
                fixed_cohorts[fixed_cohort] += 1

        print(f'{"Cohort":<10} {"Current":>10} {"After Fix":>10} {"TW":>10}')
        tw_values = {
            '2022-02': 531, '2022-03': 531, '2022-04': 602,
            '2022-05': 849, '2022-06': 629, '2022-07': 459,
            '2022-08': 280, '2022-09': 205, '2022-10': 164,
        }
        for cohort in sorted(set(list(current_cohorts.keys()) + list(fixed_cohorts.keys()))):
            curr = current_cohorts.get(cohort, 0)
            fixed = fixed_cohorts.get(cohort, 0)
            tw = tw_values.get(cohort, '?')
            print(f'{cohort:<10} {curr:>10,} {fixed:>10,} {str(tw):>10}')
        return

    # Apply updates
    print(f'\nApplying {len(updates):,} date corrections...')
    batch_size = 500
    with get_db() as conn:
        for i in range(0, len(updates), batch_size):
            batch = updates[i:i + batch_size]
            for cid, _, shopify_date in batch:
                conn.execute(
                    'UPDATE customers SET first_order_date = %s WHERE customer_id = %s AND first_order_date > %s',
                    (shopify_date, cid, shopify_date),
                )
            conn.commit()
            print(f'  Updated {min(i + batch_size, len(updates)):,} / {len(updates):,}')

    print('\nDone! Verifying cohort sizes...')
    with get_db() as conn:
        df = read_sql('''
            SELECT TO_CHAR(first_order_date::date, 'YYYY-MM') as cohort,
                   COUNT(*) as customers
            FROM customers
            WHERE first_order_date >= '2022-02-01' AND first_order_date < '2022-11-01'
            GROUP BY cohort ORDER BY cohort
        ''', conn)

    tw_values = {
        '2022-02': 531, '2022-03': 531, '2022-04': 602,
        '2022-05': 849, '2022-06': 629, '2022-07': 459,
        '2022-08': 280, '2022-09': 205, '2022-10': 164,
    }
    print(f'\n{"Cohort":<10} {"Ours":>10} {"TW":>10} {"Ratio":>10}')
    for _, row in df.iterrows():
        cohort = row['cohort']
        ours = int(row['customers'])
        tw = tw_values.get(cohort, '?')
        ratio = f'{ours / tw:.2f}x' if isinstance(tw, int) else '?'
        print(f'{cohort:<10} {ours:>10,} {str(tw):>10} {ratio:>10}')


if __name__ == '__main__':
    main()
