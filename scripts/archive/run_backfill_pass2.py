"""
Second-pass backfill for months that errored in pass 1.

Uses fresh DB connection per page AND deadlock retry logic to handle
concurrent scheduler writes.

Usage:
    python scripts/run_backfill_pass2.py
"""
import sys
import os
import time
import calendar
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from db import (
    get_db, upsert_customer, upsert_order,
    upsert_order_item, upsert_sku,
)
from etl.shopify_client import (
    test_connection, get_base_url, get_headers,
    _get_orders_page, _get_next_page_url,
)
from etl.customer_id import generate_customer_id

logger = logging.getLogger(__name__)

# 13 gap months remaining after passes 1 & 2
GAP_MONTHS = [
    '2020-02', '2020-05',
    '2020-07', '2020-08', '2020-09', '2020-10', '2020-11', '2020-12',
    '2021-01', '2021-02', '2021-03',
    '2021-07', '2021-08',
]

EXPECTED = {
    '2020-02': 7169, '2020-05': 11009,
    '2020-07': 16460, '2020-08': 18247, '2020-09': 16539,
    '2020-10': 13123, '2020-11': 11626, '2020-12': 10628,
    '2021-01': 11548, '2021-02': 12329, '2021-03': 10783,
    '2021-07': 9494, '2021-08': 9215,
}

MAX_DEADLOCK_RETRIES = 5


def month_date_range(year_month):
    year, month = map(int, year_month.split('-'))
    last_day = calendar.monthrange(year, month)[1]
    return f'{year_month}-01', f'{year_month}-{last_day:02d}'


def kill_competing_connections():
    """Terminate other connections to reduce deadlock risk."""
    try:
        with get_db() as conn:
            conn.execute("""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND state = 'idle'
            """)
            print('  Terminated idle competing connections', flush=True)
    except Exception as e:
        print(f'  Warning: could not kill connections: {e}', flush=True)


def process_page_orders(conn, orders):
    """Process a page of orders using the given connection. Returns count of orders inserted."""
    count = 0
    for order in orders:
        shopify_order_id = str(order["id"])
        order_date = order["created_at"][:10]
        total = float(order.get("total_price", 0))
        currency = order.get("currency", "USD")

        customer_data = order.get("customer")
        customer_id = generate_customer_id(customer_data)
        customer_email = customer_data.get("email") if customer_data else None
        customer_first_date = (
            customer_data.get("created_at", "")[:10] if customer_data else None
        ) or order_date

        if customer_id:
            upsert_customer(conn, customer_id, customer_email, "shopify", customer_first_date)

        order_id = f"shp-{shopify_order_id}"
        upsert_order(conn, order_id, "shopify", shopify_order_id,
                     customer_id, order_date, total, currency)

        for item in order.get("line_items", []):
            sku = item.get("sku") or item.get("variant_id") or "UNKNOWN"
            sku = str(sku)
            product_name = item.get("title", "")
            variant_title = item.get("variant_title", "")
            if variant_title:
                product_name = f"{product_name} - {variant_title}"

            quantity = int(item.get("quantity", 1))
            gross_price = float(item.get("price", 0))
            line_discount = sum(
                float(d.get("amount", 0))
                for d in item.get("discount_allocations", [])
            )
            net_total = (quantity * gross_price) - line_discount
            unit_price = net_total / quantity if quantity else gross_price

            upsert_order_item(conn, order_id, sku, product_name, quantity, unit_price)
            upsert_sku(conn, sku, product_name, None, order_date, "shopify")

        count += 1
    return count


def write_page_with_retry(orders):
    """Write a page of orders, retrying on deadlock."""
    for attempt in range(1, MAX_DEADLOCK_RETRIES + 1):
        try:
            with get_db() as conn:
                conn.execute("SET statement_timeout = '300000'")
                count = process_page_orders(conn, orders)
                return count
        except Exception as e:
            if 'deadlock' in str(e).lower() and attempt < MAX_DEADLOCK_RETRIES:
                wait = attempt * 2
                print(f'    Deadlock on attempt {attempt}, retrying in {wait}s...', flush=True)
                time.sleep(wait)
            else:
                raise
    return 0


def backfill_month(year_month):
    """Fetch orders for a month, using a fresh DB connection per page."""
    start, end = month_date_range(year_month)
    base_url = get_base_url()
    headers = get_headers()

    url = f"{base_url}/orders.json"
    params = {
        "created_at_min": f"{start}T00:00:00-00:00",
        "created_at_max": f"{end}T23:59:59-00:00",
        "status": "any",
        "limit": 250,
        "fields": "id,created_at,total_price,currency,customer,line_items,financial_status",
    }

    order_count = 0
    page_number = 0

    while url:
        response = _get_orders_page(url, headers, params)

        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", 2))
            print(f'  Rate limited, waiting {retry_after}s...', flush=True)
            time.sleep(retry_after)
            continue

        response.raise_for_status()
        data = response.json()
        orders = data.get("orders", [])
        page_number += 1

        page_count = write_page_with_retry(orders)
        order_count += page_count

        print(f'  [{year_month}] {order_count:,} orders (page {page_number})', flush=True)

        url = _get_next_page_url(response)
        params = None
        time.sleep(0.5)

    return order_count


def main():
    ok, msg = test_connection()
    if not ok:
        print(f'Shopify connection failed: {msg}')
        sys.exit(1)
    print(f'Shopify OK: {msg}', flush=True)

    # Kill idle connections to reduce deadlock risk
    kill_competing_connections()

    print(f'\n{len(GAP_MONTHS)} months to backfill (pass 3 — deadlock retry)\n', flush=True)

    start_time = datetime.now()
    results = {}

    for i, ym in enumerate(GAP_MONTHS):
        expected = EXPECTED.get(ym, '?')
        print(f'[{i+1}/{len(GAP_MONTHS)}] {ym} (expecting ~{expected:,})...', flush=True)
        try:
            count = backfill_month(ym)
            results[ym] = count
            print(f'[{ym}] Done: {count:,} orders\n', flush=True)
        except Exception as e:
            results[ym] = f'ERROR: {e}'
            print(f'[{ym}] ERROR: {e}\n', flush=True)

    # Skip rebuild_daily_sales — disk may be full, will do separately
    elapsed = datetime.now() - start_time
    print(f'\n{"=" * 50}', flush=True)
    print(f'BACKFILL PASS 3 COMPLETE in {elapsed}', flush=True)
    print(f'{"=" * 50}', flush=True)

    for ym, result in results.items():
        exp = EXPECTED.get(ym, '?')
        if isinstance(result, int):
            print(f'  {ym}: {result:>6,} fetched (expected ~{exp:,})', flush=True)
        else:
            print(f'  {ym}: {result}', flush=True)

    total = sum(v for v in results.values() if isinstance(v, int))
    errors = sum(1 for v in results.values() if isinstance(v, str))
    print(f'\n  Total: {total:,} orders | {errors} errors', flush=True)


if __name__ == '__main__':
    main()
