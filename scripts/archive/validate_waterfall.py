#!/usr/bin/env python3
"""
Validate waterfall output against the Google Sheets spreadsheet.

Checks:
1. Retention curve matches Google Sheet values (M0-M25)
2. Feb 2026 repeat revenue matches $121,591
3. Monthly first_order_revenue per cohort is reasonable
4. DB order counts match Shopify API counts
"""
import sys
import os
import time
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import config as cfg
from analytics.waterfall import clear_waterfall_cache, build_waterfall, get_average_retention_curve
from analytics.retention import get_revenue_retention_data
from db import get_db

# Google Sheet known retention curve values
GSHEET_CURVE = {
    0: 0.0434, 1: 0.2148, 2: 0.1644, 3: 0.1334, 4: 0.1096, 5: 0.1013,
    6: 0.0896, 7: 0.0781, 8: 0.0772, 9: 0.0807, 10: 0.0734, 11: 0.0708,
    12: 0.0736, 13: 0.0654, 14: 0.0610, 15: 0.0618, 16: 0.0658, 17: 0.0618,
    18: 0.0603, 19: 0.0469, 20: 0.0474, 21: 0.0429, 22: 0.0413, 23: 0.0335,
    24: 0.0392, 25: 0.0385,
}

SPREADSHEET_FEB_REPEAT = 121591


def check_db_vs_shopify():
    """Check DB order counts vs Shopify for key months."""
    print("\n=== DB vs Shopify Order Counts ===")
    base_url = f"https://{cfg.SHOPIFY_STORE_URL}/admin/api/{cfg.SHOPIFY_API_VERSION}"
    headers = {"X-Shopify-Access-Token": cfg.SHOPIFY_ACCESS_TOKEN}

    months_to_check = [
        ("2020-09", "2020-09-01", "2020-09-30"),
        ("2020-10", "2020-10-01", "2020-10-31"),
        ("2020-11", "2020-11-01", "2020-11-30"),
        ("2020-12", "2020-12-01", "2020-12-31"),
        ("2021-01", "2021-01-01", "2021-01-31"),
        ("2021-02", "2021-02-01", "2021-02-28"),
        ("2021-07", "2021-07-01", "2021-07-31"),
        ("2021-08", "2021-08-01", "2021-08-31"),
    ]

    with get_db() as conn:
        all_ok = True
        for label, start, end in months_to_check:
            r = requests.get(f"{base_url}/orders/count.json", headers=headers, params={
                "created_at_min": f"{start}T00:00:00-00:00",
                "created_at_max": f"{end}T23:59:59-00:00",
                "status": "any",
            })
            shopify = r.json().get("count", 0)

            db_row = conn.execute(
                "SELECT COUNT(*) as c FROM orders WHERE source = %s AND substr(order_date, 1, 7) = %s",
                ("shopify", label),
            ).fetchone()
            db = db_row["c"]

            pct = db / shopify * 100 if shopify else 100
            status = "OK" if pct > 95 else "GAP"
            print(f"  {label}: DB={db:,}  Shopify={shopify:,}  ({pct:.1f}%)  {status}")
            if pct < 95:
                all_ok = False
            time.sleep(0.3)

    return all_ok


def check_retention_curve():
    """Compare our retention curve to Google Sheet values."""
    print("\n=== Retention Curve Comparison ===")
    clear_waterfall_cache()
    curve = get_average_retention_curve()

    max_diff = 0
    all_ok = True
    for m in range(0, 26):
        ours = curve.get(m, 0) * 100
        gs = GSHEET_CURVE.get(m, 0) * 100
        diff = abs(ours - gs)
        max_diff = max(max_diff, diff)
        status = "OK" if diff < 1.5 else "DIFF"
        if diff >= 1.5:
            all_ok = False
        print(f"  M{m:2d}: ours={ours:6.2f}%  gs={gs:6.2f}%  diff={ours-gs:+.2f}pp  {status}")

    print(f"  Max diff: {max_diff:.2f}pp")
    return all_ok


def check_waterfall_output():
    """Check Feb 2026 repeat revenue against spreadsheet target."""
    print("\n=== Waterfall Output ===")
    clear_waterfall_cache()
    wf = build_waterfall(media_plan=[], horizon_months=12)

    if wf.empty:
        print("  ERROR: waterfall returned empty DataFrame")
        return False

    feb_repeat = wf.iloc[0]["repeat_revenue"]
    diff_pct = (feb_repeat / SPREADSHEET_FEB_REPEAT - 1) * 100

    print(f"  Feb 2026 repeat:   ${feb_repeat:,.0f}")
    print(f"  Spreadsheet target: ${SPREADSHEET_FEB_REPEAT:,}")
    print(f"  Difference:        {diff_pct:+.1f}%")

    for _, row in wf.head(6).iterrows():
        print(f"  {row['month']}: repeat=${row['repeat_revenue']:,.0f}  "
              f"new=${row['new_customer_revenue']:,.0f}  "
              f"total=${row['total_revenue']:,.0f}")

    return abs(diff_pct) < 5


def check_first_order_revenue():
    """Check first_order_revenue per cohort for reasonableness."""
    print("\n=== First Order Revenue Per Cohort (2020-2022) ===")
    clear_waterfall_cache()
    rev_data = get_revenue_retention_data(source_filter='shopify')
    hist_for = rev_data.get("first_order_revenue")
    hist_for = hist_for[hist_for.index >= "2020-01"]

    low_months = []
    for m in sorted(hist_for.index):
        if m >= "2022-07":
            break
        val = hist_for[m]
        flag = " << LOW" if val < 50000 else ""
        print(f"  {m}: ${val:,.0f}{flag}")
        if val < 50000:
            low_months.append(m)

    print(f"\n  Low months (<$50K): {len(low_months)}")
    return len(low_months) <= 3  # Allow a few legitimately low months


def main():
    print("=" * 60)
    print("WATERFALL VALIDATION")
    print("=" * 60)

    results = {}

    results["db_counts"] = check_db_vs_shopify()
    results["retention_curve"] = check_retention_curve()
    results["first_order_rev"] = check_first_order_revenue()
    results["waterfall_output"] = check_waterfall_output()

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    all_pass = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")
        if not passed:
            all_pass = False

    print(f"\n  Overall: {'ALL PASS' if all_pass else 'SOME FAILURES'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
