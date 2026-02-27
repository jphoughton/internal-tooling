#!/usr/bin/env python3
"""
Targeted month-by-month Shopify backfill with Railway-proof DB handling.

Re-imports each month individually. Uses fresh DB connection per page
and resets connection pool on failures — same resilience as full_backfill.py.
"""
import sys
import os
import time
import calendar
import logging
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import config as cfg
import db as _db_mod
from db import get_db, rebuild_daily_sales
from etl.shopify_client import (
    get_base_url, get_headers, _get_orders_page, _get_next_page_url,
    generate_customer_id,
)
from db import upsert_customer, upsert_order, upsert_order_item, upsert_sku

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)


def _reset_pool():
    """Reset DB connection pool after a connection failure."""
    if _db_mod._pool is not None:
        try:
            _db_mod._pool.closeall()
        except Exception:
            pass
        _db_mod._pool = None


def shopify_count(start, end):
    r = requests.get(f"{get_base_url()}/orders/count.json", headers=get_headers(), params={
        "created_at_min": f"{start}T00:00:00-00:00",
        "created_at_max": f"{end}T23:59:59-00:00",
        "status": "any",
    })
    return r.json().get("count", 0)


def db_count(month_str):
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) as c FROM orders WHERE source = %s AND substr(order_date, 1, 7) = %s",
            ("shopify", month_str),
        ).fetchone()["c"]


def _insert_batch(batch):
    """Insert a small batch of orders with a fresh DB connection."""
    count = 0
    for attempt in range(3):
        try:
            with get_db() as conn:
                conn.execute("SET statement_timeout = 120000")
                for order in batch:
                    if order.get("financial_status") in ("refunded", "voided"):
                        continue
                    sid = str(order["id"])
                    od = order["created_at"][:10]
                    tot = float(order.get("total_price", 0))
                    cur = order.get("currency", "USD")
                    cd = order.get("customer")
                    cid = generate_customer_id(cd)
                    ce = cd.get("email") if cd else None
                    cfd = (cd.get("created_at", "")[:10] if cd else None) or od
                    if cid:
                        upsert_customer(conn, cid, ce, "shopify", cfd)
                    oid = f"shp-{sid}"
                    upsert_order(conn, oid, "shopify", sid, cid, od, tot, cur)
                    for item in order.get("line_items", []):
                        sku = str(item.get("sku") or item.get("variant_id") or "UNKNOWN")
                        pn = item.get("title", "")
                        vt = item.get("variant_title", "")
                        if vt:
                            pn = f"{pn} - {vt}"
                        qty = int(item.get("quantity", 1))
                        gp = float(item.get("price", 0))
                        ld = sum(float(d.get("amount", 0))
                                 for d in item.get("discount_allocations", []))
                        nt = (qty * gp) - ld
                        up = nt / qty if qty else gp
                        upsert_order_item(conn, oid, sku, pn, qty, up)
                        upsert_sku(conn, sku, pn, None, od, "shopify")
                    count += 1
            return count
        except Exception as e:
            _reset_pool()
            wait = 2 ** (attempt + 1)
            log.warning("  DB error (attempt %d): %s — retrying in %ds", attempt + 1, e, wait)
            time.sleep(wait)
            count = 0
    return count


def process_page(orders):
    """Insert one page of orders, split into small batches for Railway resilience."""
    BATCH_SIZE = 50
    total = 0
    for i in range(0, len(orders), BATCH_SIZE):
        batch = orders[i:i + BATCH_SIZE]
        total += _insert_batch(batch)
    return total


def backfill_month(year, month):
    last_day = calendar.monthrange(year, month)[1]
    since = f"{year}-{month:02d}-01"
    until = f"{year}-{month:02d}-{last_day:02d}"
    ms = f"{year}-{month:02d}"

    sc = shopify_count(since, until)
    dc = db_count(ms)
    missing = sc - dc

    log.info("=" * 55)
    log.info("%s  Shopify=%d  DB=%d  Missing=%d  (%.0f%%)",
             ms, sc, dc, missing, dc / sc * 100 if sc else 100)

    if missing < 50:
        log.info("  Small gap — skipping")
        return dc, sc

    url = f"{get_base_url()}/orders.json"
    params = {
        "created_at_min": f"{since}T00:00:00-00:00",
        "created_at_max": f"{until}T23:59:59-00:00",
        "status": "any",
        "limit": 250,
        "fields": "id,created_at,total_price,currency,customer,line_items,financial_status",
    }

    total = 0
    pg = 0
    t0 = time.time()

    while url:
        for attempt in range(5):
            try:
                resp = _get_orders_page(url, get_headers(), params)
                break
            except Exception as e:
                time.sleep(2 ** (attempt + 1))
        else:
            log.error("  Failed to fetch page after 5 attempts")
            break

        if resp.status_code == 429:
            ra = float(resp.headers.get("Retry-After", 2))
            log.warning("  Rate limited — waiting %ss", ra)
            time.sleep(ra)
            continue

        resp.raise_for_status()
        orders = resp.json().get("orders", [])
        pg += 1

        if not orders:
            break

        n = process_page(orders)
        total += n

        if pg % 5 == 0:
            elapsed = time.time() - t0
            rate = total / elapsed * 60 if elapsed > 0 else 0
            log.info("  %s pg=%d  orders=%d  (%.0f/min)", ms, pg, total, rate)

        url = _get_next_page_url(resp)
        params = None
        time.sleep(0.5)

    dc_after = db_count(ms)
    elapsed = time.time() - t0
    log.info("  %s DONE: pages=%d fetched=%d  DB: %d→%d  Shopify=%d  (%.0f%%)  [%.0fs]",
             ms, pg, total, dc, dc_after, sc,
             dc_after / sc * 100 if sc else 100, elapsed)
    return dc_after, sc


def main():
    from datetime import datetime
    from dateutil.relativedelta import relativedelta

    # All months from June 2020 through Aug 2021 (the big gap region)
    months = []
    d = datetime(2020, 6, 1)
    while d <= datetime(2021, 8, 31):
        months.append((d.year, d.month))
        d += relativedelta(months=1)
    # Plus a few later gaps
    months += [(2023, 1), (2023, 8), (2023, 11)]

    t0 = time.time()
    results = []

    for year, month in months:
        try:
            dc, sc = backfill_month(year, month)
            results.append((f"{year}-{month:02d}", dc, sc))
        except Exception as e:
            log.error("FAILED %d-%02d: %s", year, month, e, exc_info=True)
            results.append((f"{year}-{month:02d}", -1, -1))
            _reset_pool()
        time.sleep(1)

    # Rebuild daily sales
    log.info("Rebuilding daily sales aggregates...")
    try:
        with get_db() as conn:
            rebuild_daily_sales(conn)
    except Exception as e:
        log.error("Failed to rebuild daily sales: %s", e)

    total_elapsed = time.time() - t0
    log.info("")
    log.info("=" * 55)
    log.info("ALL DONE — %.1f minutes", total_elapsed / 60)
    log.info("=" * 55)
    log.info("%-10s %8s %10s %8s %6s", "Month", "DB", "Shopify", "Missing", "Pct")
    log.info("-" * 45)

    ok = gap = 0
    for ms, dc, sc in results:
        if dc < 0:
            log.info("%-10s FAILED", ms)
            gap += 1
        else:
            miss = sc - dc
            pct = dc / sc * 100 if sc else 100
            tag = "OK" if pct > 95 else "GAP"
            log.info("%-10s %8d %10d %8d %5.1f%% %s", ms, dc, sc, miss, pct, tag)
            if pct > 95:
                ok += 1
            else:
                gap += 1

    log.info("-" * 45)
    log.info("OK: %d  GAP: %d", ok, gap)


if __name__ == "__main__":
    main()
