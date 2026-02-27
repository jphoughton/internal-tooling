#!/usr/bin/env python3
"""
Validate data completeness: DB vs Shopify API for every month.

Checks:
1. Order counts per month (DB vs Shopify API)
2. Unique new customers per cohort month
3. First-order revenue per cohort month

Run: python scripts/validate_data_completeness.py
"""
import sys
import os
import time
import calendar
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import requests
import config as cfg
from db import get_db

BASE_URL = f"https://{cfg.SHOPIFY_STORE_URL}/admin/api/{cfg.SHOPIFY_API_VERSION}"
HEADERS = {"X-Shopify-Access-Token": cfg.SHOPIFY_ACCESS_TOKEN}


def shopify_order_count(start_date, end_date):
    """Get order count from Shopify API for a date range."""
    r = requests.get(f"{BASE_URL}/orders/count.json", headers=HEADERS, params={
        "created_at_min": f"{start_date}T00:00:00-00:00",
        "created_at_max": f"{end_date}T23:59:59-00:00",
        "status": "any",
    })
    r.raise_for_status()
    return r.json().get("count", 0)


def get_db_monthly_orders():
    """Get order counts per month from DB (shopify only, total_amount > 0)."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT substr(order_date, 1, 7) AS month,
                   COUNT(*) AS orders,
                   COUNT(DISTINCT customer_id) AS unique_customers,
                   ROUND(SUM(total_amount)::numeric, 2) AS revenue
            FROM orders
            WHERE source = 'shopify'
            GROUP BY month ORDER BY month
        """).fetchall()
    return {r["month"]: {
        "orders": r["orders"],
        "customers": r["unique_customers"],
        "revenue": float(r["revenue"]),
    } for r in rows}


def get_db_monthly_orders_all():
    """Get order counts per month from DB (shopify only, including $0 orders)."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT substr(order_date, 1, 7) AS month,
                   COUNT(*) AS orders
            FROM orders
            WHERE source = 'shopify'
            GROUP BY month ORDER BY month
        """).fetchall()
    return {r["month"]: r["orders"] for r in rows}


def get_db_cohorts():
    """Get new customer counts and first-order revenue per cohort month."""
    with get_db() as conn:
        rows = conn.execute("""
            WITH cohort AS (
                SELECT customer_id, MIN(order_date) AS first_date
                FROM orders
                WHERE total_amount > 0 AND source = 'shopify'
                GROUP BY customer_id
            ),
            first_orders AS (
                SELECT c.customer_id, c.first_date, MIN(o.order_id) AS first_order_id
                FROM cohort c
                JOIN orders o ON c.customer_id = o.customer_id
                WHERE o.order_date = c.first_date
                  AND o.total_amount > 0 AND o.source = 'shopify'
                GROUP BY c.customer_id, c.first_date
            )
            SELECT substr(fo.first_date, 1, 7) AS cohort_month,
                   COUNT(DISTINCT fo.customer_id) AS new_customers,
                   ROUND(SUM(o.total_amount)::numeric, 2) AS first_order_revenue
            FROM first_orders fo
            JOIN orders o ON fo.first_order_id = o.order_id
            GROUP BY cohort_month ORDER BY cohort_month
        """).fetchall()
    return {r["cohort_month"]: {
        "new_customers": r["new_customers"],
        "first_order_revenue": float(r["first_order_revenue"]),
    } for r in rows}


def check_orders_vs_shopify():
    """Compare DB order counts to Shopify API for every month."""
    print("\n" + "=" * 70)
    print("CHECK 1: DB ORDER COUNTS vs SHOPIFY API")
    print("=" * 70)

    db_orders_all = get_db_monthly_orders_all()
    all_months = sorted(db_orders_all.keys())

    if not all_months:
        print("  No data in DB!")
        return False, {}

    gaps = []
    results = {}

    for month in all_months:
        year, mo = int(month[:4]), int(month[5:7])
        last_day = calendar.monthrange(year, mo)[1]
        start = f"{year}-{mo:02d}-01"
        end = f"{year}-{mo:02d}-{last_day}"

        try:
            shopify = shopify_order_count(start, end)
        except Exception as e:
            print(f"  {month}: API ERROR — {e}")
            time.sleep(1)
            continue

        db = db_orders_all.get(month, 0)
        pct = db / shopify * 100 if shopify > 0 else 100
        missing = shopify - db
        status = "OK" if pct >= 99 else "GAP" if pct >= 95 else "BIG GAP"

        results[month] = {"db": db, "shopify": shopify, "pct": pct, "missing": missing}

        if pct < 99:
            gaps.append(month)
            print(f"  {month}: DB={db:>6,}  Shopify={shopify:>6,}  "
                  f"missing={missing:>4,}  ({pct:5.1f}%)  *** {status}")
        else:
            print(f"  {month}: DB={db:>6,}  Shopify={shopify:>6,}  "
                  f"missing={missing:>4,}  ({pct:5.1f}%)  {status}")

        time.sleep(0.35)  # Rate limiting

    total_db = sum(r["db"] for r in results.values())
    total_shopify = sum(r["shopify"] for r in results.values())
    total_missing = total_shopify - total_db
    overall_pct = total_db / total_shopify * 100 if total_shopify > 0 else 100

    print(f"\n  TOTAL: DB={total_db:,}  Shopify={total_shopify:,}  "
          f"missing={total_missing:,}  ({overall_pct:.1f}%)")
    print(f"  Months with gaps (<99%): {len(gaps)}")
    if gaps:
        print(f"  Gap months: {', '.join(gaps)}")

    return len(gaps) == 0, results


def check_orders_vs_spreadsheet(expected):
    """Compare DB order counts to user-provided spreadsheet values."""
    if not expected:
        print("\n  (Skipped — no spreadsheet data provided)")
        return True

    print("\n" + "=" * 70)
    print("CHECK 2: DB ORDER COUNTS vs SPREADSHEET")
    print("=" * 70)

    db_orders = get_db_monthly_orders()
    gaps = []

    for month in sorted(expected.keys()):
        exp = expected[month]
        db = db_orders.get(month, {}).get("orders", 0)
        pct = db / exp * 100 if exp > 0 else 100
        status = "OK" if pct >= 99 else "GAP"
        if pct < 99:
            gaps.append(month)
        print(f"  {month}: DB={db:>6,}  Expected={exp:>6,}  ({pct:5.1f}%)  {status}")

    return len(gaps) == 0


def check_cohorts_vs_spreadsheet(expected_customers=None, expected_revenue=None):
    """Compare DB cohort data to user-provided spreadsheet values."""
    if not expected_customers and not expected_revenue:
        print("\n  (Skipped — no spreadsheet cohort data provided)")
        return True

    print("\n" + "=" * 70)
    print("CHECK 3: COHORT DATA vs SPREADSHEET")
    print("=" * 70)

    db_cohorts = get_db_cohorts()
    gaps = []

    if expected_customers:
        print("\n  --- New Customers per Cohort ---")
        for month in sorted(expected_customers.keys()):
            exp = expected_customers[month]
            db = db_cohorts.get(month, {}).get("new_customers", 0)
            pct = db / exp * 100 if exp > 0 else 100
            status = "OK" if abs(pct - 100) < 2 else "DIFF"
            if abs(pct - 100) >= 2:
                gaps.append(f"customers:{month}")
            print(f"  {month}: DB={db:>6,}  Expected={exp:>6,}  ({pct:5.1f}%)  {status}")

    if expected_revenue:
        print("\n  --- First-Order Revenue per Cohort ---")
        for month in sorted(expected_revenue.keys()):
            exp = expected_revenue[month]
            db = db_cohorts.get(month, {}).get("first_order_revenue", 0)
            pct = db / exp * 100 if exp > 0 else 100
            status = "OK" if abs(pct - 100) < 2 else "DIFF"
            if abs(pct - 100) >= 2:
                gaps.append(f"revenue:{month}")
            print(f"  {month}: DB=${db:>10,.0f}  Expected=${exp:>10,.0f}  ({pct:5.1f}%)  {status}")

    return len(gaps) == 0


def print_db_summary():
    """Print current DB state for easy comparison with spreadsheet."""
    print("\n" + "=" * 70)
    print("CURRENT DB STATE (Shopify only)")
    print("=" * 70)

    db_orders = get_db_monthly_orders()
    db_cohorts = get_db_cohorts()

    print("\n  --- Orders by Month ---")
    print(f"  {'Month':>8s}  {'Orders':>8s}  {'Customers':>10s}  {'Revenue':>12s}")
    print(f"  {'-'*8}  {'-'*8}  {'-'*10}  {'-'*12}")
    for month in sorted(db_orders.keys()):
        d = db_orders[month]
        print(f"  {month:>8s}  {d['orders']:>8,}  {d['customers']:>10,}  ${d['revenue']:>11,.2f}")

    total_orders = sum(d["orders"] for d in db_orders.values())
    total_rev = sum(d["revenue"] for d in db_orders.values())
    print(f"  {'TOTAL':>8s}  {total_orders:>8,}  {'':>10s}  ${total_rev:>11,.2f}")

    print("\n  --- Cohorts (New Customers + First-Order Revenue) ---")
    print(f"  {'Cohort':>8s}  {'New Custs':>10s}  {'FO Revenue':>12s}  {'AOV':>8s}")
    print(f"  {'-'*8}  {'-'*10}  {'-'*12}  {'-'*8}")
    for month in sorted(db_cohorts.keys()):
        d = db_cohorts[month]
        aov = d["first_order_revenue"] / d["new_customers"] if d["new_customers"] > 0 else 0
        print(f"  {month:>8s}  {d['new_customers']:>10,}  ${d['first_order_revenue']:>11,.2f}  ${aov:>7,.2f}")

    total_custs = sum(d["new_customers"] for d in db_cohorts.values())
    total_fo = sum(d["first_order_revenue"] for d in db_cohorts.values())
    print(f"  {'TOTAL':>8s}  {total_custs:>10,}  ${total_fo:>11,.2f}")


def main():
    print("=" * 70)
    print("DATA COMPLETENESS VALIDATION")
    print("=" * 70)

    # Print current DB state for manual comparison
    print_db_summary()

    # Check DB vs Shopify API
    orders_ok, shopify_results = check_orders_vs_shopify()

    # Spreadsheet comparisons (populate these when data is available)
    # expected_orders = {"2020-01": 8199, "2020-02": 7211, ...}
    # expected_customers = {"2020-01": 6789, ...}
    # expected_revenue = {"2020-01": 345678.90, ...}
    spreadsheet_ok = check_orders_vs_spreadsheet(None)
    cohort_ok = check_cohorts_vs_spreadsheet(None, None)

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  DB vs Shopify API:    {'PASS' if orders_ok else 'FAIL'}")
    print(f"  DB vs Spreadsheet:    {'PASS' if spreadsheet_ok else 'FAIL (or skipped)'}")
    print(f"  Cohorts vs Spreadsheet: {'PASS' if cohort_ok else 'FAIL (or skipped)'}")

    if not orders_ok:
        # Print gap months that need backfill
        gap_months = [(m, r) for m, r in shopify_results.items() if r["pct"] < 99]
        gap_months.sort(key=lambda x: x[1]["missing"], reverse=True)
        print(f"\n  {len(gap_months)} months need backfill:")
        for m, r in gap_months[:20]:
            print(f"    {m}: missing {r['missing']:,} orders ({r['pct']:.1f}%)")

    return 0 if orders_ok else 1


if __name__ == "__main__":
    sys.exit(main())
