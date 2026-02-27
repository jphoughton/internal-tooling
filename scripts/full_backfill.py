#!/usr/bin/env python3
"""Full Shopify backfill with per-page DB connections.

Gets a fresh DB connection for every 250-order page to avoid Railway's
proxy killing long-lived connections. Each page is fully committed
before moving to the next, so a crash at any point loses at most 1 page.

Usage:
    python scripts/full_backfill.py                  # full backfill from 2018-01-01
    python scripts/full_backfill.py --since 2021-01-01  # from specific date
"""
import sys
import os
import time
import logging
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import config as cfg
from db import get_db, rebuild_daily_sales
from etl.shopify_client import (
    get_base_url, get_headers, _get_orders_page, _get_next_page_url,
    generate_customer_id,
)
from db import upsert_customer, upsert_order, upsert_order_item, upsert_sku

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def backfill(since_date='2018-01-01', until_date=None):
    if until_date is None:
        from datetime import datetime
        until_date = datetime.utcnow().strftime('%Y-%m-%d')

    logger.info('Starting backfill: %s → %s', since_date, until_date)

    base_url = get_base_url()
    headers = get_headers()

    url = f"{base_url}/orders.json"
    params = {
        "created_at_min": f"{since_date}T00:00:00-00:00",
        "created_at_max": f"{until_date}T23:59:59-00:00",
        "status": "any",
        "limit": 250,
        "fields": "id,created_at,total_price,currency,customer,line_items,financial_status",
    }

    total_orders = 0
    page_number = 0
    start_time = time.time()

    while url:
        # Fetch page from Shopify
        for attempt in range(5):
            try:
                response = _get_orders_page(url, headers, params)
                break
            except Exception as e:
                wait = 2 ** (attempt + 1)
                logger.warning('Page fetch failed (attempt %d): %s, retrying in %ds', attempt + 1, e, wait)
                time.sleep(wait)
        else:
            logger.error('Failed to fetch page after 5 attempts, stopping.')
            break

        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", 2))
            logger.warning('Rate limited, waiting %ss', retry_after)
            time.sleep(retry_after)
            continue

        response.raise_for_status()
        data = response.json()
        orders = data.get("orders", [])
        page_number += 1

        if not orders:
            break

        # Process page with FRESH connection (avoids Railway proxy drops)
        page_count = 0
        for attempt in range(3):
            try:
                with get_db() as conn:
                    conn.execute('SET statement_timeout = 300000')
                    for order in orders:
                        if order.get("financial_status") in ("refunded", "voided"):
                            continue

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
                            sku = str(item.get("sku") or item.get("variant_id") or "UNKNOWN")
                            product_name = item.get("title", "")
                            variant_title = item.get("variant_title", "")
                            if variant_title:
                                product_name = f"{product_name} - {variant_title}"
                            quantity = int(item.get("quantity", 1))
                            gross_price = float(item.get("price", 0))
                            line_discount = sum(
                                float(d.get("amount", 0)) for d in item.get("discount_allocations", [])
                            )
                            net_total = (quantity * gross_price) - line_discount
                            unit_price = net_total / quantity if quantity else gross_price
                            upsert_order_item(conn, order_id, sku, product_name, quantity, unit_price)
                            upsert_sku(conn, sku, product_name, None, order_date, "shopify")

                        page_count += 1
                    # conn auto-commits on exit from `with get_db()`
                break  # success
            except Exception as e:
                if 'closed' in str(e).lower() or 'connection' in str(e).lower():
                    wait = 2 ** (attempt + 1)
                    logger.warning('DB connection error on page %d (attempt %d): %s, retrying in %ds',
                                   page_number, attempt + 1, e, wait)
                    time.sleep(wait)
                    # Reset connection pool so next get_db() gets a fresh connection
                    import db as _db_mod
                    if _db_mod._pool is not None:
                        try:
                            _db_mod._pool.closeall()
                        except Exception:
                            pass
                        _db_mod._pool = None
                else:
                    raise

        total_orders += page_count
        elapsed = time.time() - start_time
        rate = total_orders / elapsed * 60 if elapsed > 0 else 0
        logger.info('Page %d: %d orders this page, %d total (%.0f orders/min)',
                     page_number, page_count, total_orders, rate)

        # Pagination
        url = _get_next_page_url(response)
        params = None
        time.sleep(0.5)

    elapsed = time.time() - start_time
    logger.info('DONE: %d orders in %.1f minutes (%.0f orders/min)',
                total_orders, elapsed / 60, total_orders / elapsed * 60 if elapsed > 0 else 0)

    # Rebuild aggregates
    logger.info('Rebuilding daily sales aggregates...')
    with get_db() as conn:
        rebuild_daily_sales(conn)
    logger.info('All done!')

    return total_orders


if __name__ == '__main__':
    since = '2018-01-01'
    for i, arg in enumerate(sys.argv):
        if arg == '--since' and i + 1 < len(sys.argv):
            since = sys.argv[i + 1]
    backfill(since_date=since)
