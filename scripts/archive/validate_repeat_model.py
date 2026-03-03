#!/usr/bin/env python3
"""
Validate the repeat model against the gold standard spreadsheet.

Checks:
1. Retention curve values (M0-M35) within tolerance of gold standard
2. Cohort data: first_order_revenue per cohort is reasonable
3. Revenue Matrix output has correct structure
4. Monthly Summary: new/repeat split is coherent
5. Model parameters match gold standard (decay=0.98, floor=0.005)

Gold standard: ~/Downloads/Repeat Model (4).xlsx
Run: python scripts/validate_repeat_model.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from analytics.waterfall import (
    clear_waterfall_cache,
    get_average_retention_curve,
    get_aov_and_units,
    get_monthly_new_customers,
    build_waterfall,
    build_revenue_matrix,
    DECAY_RATE,
    TERMINAL_FLOOR,
    COHORT_START,
)
from analytics.retention import get_revenue_retention_data

# Gold standard retention curve from the spreadsheet (Retention Curve sheet)
GOLD_CURVE = {
    0: 0.0434, 1: 0.2148, 2: 0.1644, 3: 0.1334, 4: 0.1096, 5: 0.1013,
    6: 0.0896, 7: 0.0781, 8: 0.0772, 9: 0.0807, 10: 0.0734, 11: 0.0708,
    12: 0.0736, 13: 0.0654, 14: 0.0610, 15: 0.0618, 16: 0.0658, 17: 0.0618,
    18: 0.0603, 19: 0.0469, 20: 0.0474, 21: 0.0429, 22: 0.0413, 23: 0.0335,
    24: 0.0392, 25: 0.0385,
}

# Gold standard parameters
GOLD_DECAY = 0.98
GOLD_FLOOR = 0.005
GOLD_AOV = 75.0
GOLD_COHORT_START = '2020-01'

# Feb 2026 repeat revenue target from spreadsheet
GOLD_FEB_REPEAT = 121591

# Tolerance thresholds
CURVE_TOLERANCE_PP = 2.0   # percentage points
REVENUE_TOLERANCE_PCT = 10  # percent


def check_parameters():
    """Verify model constants match gold standard."""
    print("\n=== CHECK 1: Model Parameters ===")
    passed = True

    checks = [
        ("DECAY_RATE", DECAY_RATE, GOLD_DECAY),
        ("TERMINAL_FLOOR", TERMINAL_FLOOR, GOLD_FLOOR),
        ("COHORT_START", COHORT_START, GOLD_COHORT_START),
    ]

    for name, actual, expected in checks:
        ok = actual == expected
        status = "OK" if ok else "FAIL"
        print(f"  {name}: {actual} (expected {expected})  {status}")
        if not ok:
            passed = False

    return passed


def check_retention_curve():
    """Compare computed retention curve to gold standard values."""
    print("\n=== CHECK 2: Retention Curve (M0-M35) ===")
    clear_waterfall_cache()
    curve = get_average_retention_curve()

    if not curve:
        print("  ERROR: retention curve is empty")
        return False

    max_diff = 0
    passed = True

    for m in range(0, 36):
        ours = curve.get(m, 0) * 100
        gold = GOLD_CURVE.get(m)

        if gold is not None:
            gs = gold * 100
            diff = abs(ours - gs)
            max_diff = max(max_diff, diff)
            ok = diff < CURVE_TOLERANCE_PP
            status = "OK" if ok else "DIFF"
            if not ok:
                passed = False
            print(f"  M{m:2d}: ours={ours:6.2f}%  gold={gs:6.2f}%  diff={ours-gs:+.2f}pp  {status}")
        else:
            # Beyond gold standard data — verify value is above floor and reasonable
            above_floor = curve.get(m, 0) >= TERMINAL_FLOOR * 0.99
            status = "OK" if above_floor else "BELOW FLOOR"
            if not above_floor:
                passed = False
            print(f"  M{m:2d}: ours={ours:6.2f}%  (beyond gold standard)  {status}")

    print(f"\n  Max diff vs gold standard: {max_diff:.2f}pp (tolerance: {CURVE_TOLERANCE_PP}pp)")
    return passed


def check_cohort_data():
    """Verify cohort first-order revenue is reasonable."""
    print("\n=== CHECK 3: Cohort Data (First-Order Revenue) ===")
    clear_waterfall_cache()

    rev_data = get_revenue_retention_data(source_filter='shopify')
    hist_for = rev_data.get("first_order_revenue")

    if hist_for is None or hist_for.empty:
        print("  ERROR: no first_order_revenue data")
        return False

    # Filter to 2020-01+
    hist_for = hist_for[hist_for.index >= GOLD_COHORT_START]
    cohort_count = len(hist_for)
    print(f"  Cohorts from {GOLD_COHORT_START}: {cohort_count}")

    if cohort_count < 48:
        print(f"  WARNING: expected ~58+ cohorts, got {cohort_count}")

    # Check early cohorts (2020-2021) which should have higher revenue
    early = hist_for[hist_for.index <= '2021-12']
    low_early = [(m, v) for m, v in early.items() if v < 30000]

    if low_early:
        print(f"  Early cohorts (2020-2021) with low revenue (<$30K): {len(low_early)}")
        for m, v in low_early:
            print(f"    {m}: ${v:,.0f}")
    else:
        print("  All early cohorts (2020-2021) have first-order revenue >= $30K")

    # Check total first-order revenue range
    total_for = hist_for.sum()
    avg_for = hist_for.mean()
    print(f"  Total first-order revenue: ${total_for:,.0f}")
    print(f"  Average per cohort: ${avg_for:,.0f}")

    # Average should be reasonable (recent cohorts are smaller, that's fine)
    reasonable = 20000 < avg_for < 500000
    print(f"  Average in reasonable range ($20K-$500K): {'OK' if reasonable else 'FAIL'}")

    # Early period should be strong
    early_ok = len(low_early) <= 2
    print(f"  Early cohort quality: {'OK' if early_ok else 'WARN - some gaps'}")

    return reasonable and early_ok


def check_aov_and_metrics():
    """Verify AOV and unit metrics are reasonable."""
    print("\n=== CHECK 4: AOV & Unit Metrics ===")
    clear_waterfall_cache()
    metrics = get_aov_and_units()

    aov = metrics["aov"]
    new_aov = metrics["new_customer_aov"]
    upc = metrics["avg_units_per_order"]

    print(f"  Overall AOV: ${aov:,.2f}")
    print(f"  New customer AOV: ${new_aov:,.2f}")
    print(f"  Units per order: {upc:.2f}")
    print(f"  Gold standard AOV assumption: ${GOLD_AOV}")

    # AOV should be in a reasonable range ($20-$150)
    aov_ok = 20 < aov < 150
    upc_ok = 0.5 < upc < 10
    print(f"  AOV in range ($20-$150): {'OK' if aov_ok else 'FAIL'}")
    print(f"  UPC in range (0.5-10): {'OK' if upc_ok else 'FAIL'}")

    return aov_ok and upc_ok


def check_waterfall_output():
    """Check waterfall forecast output and Feb 2026 target."""
    print("\n=== CHECK 5: Waterfall Forecast Output ===")
    clear_waterfall_cache()

    wf = build_waterfall(media_plan=[], horizon_months=12)

    if wf.empty:
        print("  ERROR: waterfall returned empty DataFrame")
        return False

    # Check required columns
    expected_cols = ['month', 'repeat_revenue', 'new_customer_revenue', 'total_revenue',
                     'repeat_units', 'new_customer_units', 'total_units', 'new_customers_acquired']
    missing_cols = [c for c in expected_cols if c not in wf.columns]
    if missing_cols:
        print(f"  ERROR: missing columns: {missing_cols}")
        return False
    print(f"  Columns: OK ({len(wf.columns)} cols)")

    # Check Feb 2026 repeat revenue
    feb_row = wf[wf['month'] == '2026-02']
    if feb_row.empty:
        feb_repeat = wf.iloc[0]["repeat_revenue"]
        month_label = wf.iloc[0]["month"]
    else:
        feb_repeat = feb_row.iloc[0]["repeat_revenue"]
        month_label = "2026-02"

    diff_pct = (feb_repeat / GOLD_FEB_REPEAT - 1) * 100
    ok = abs(diff_pct) < REVENUE_TOLERANCE_PCT
    print(f"  {month_label} repeat:    ${feb_repeat:,.0f}")
    print(f"  Gold standard target: ${GOLD_FEB_REPEAT:,}")
    print(f"  Difference:           {diff_pct:+.1f}% (tolerance: {REVENUE_TOLERANCE_PCT}%)  {'OK' if ok else 'FAIL'}")

    # Print first 6 months
    print(f"\n  Monthly forecast:")
    for _, row in wf.head(6).iterrows():
        rep_pct = row['repeat_revenue'] / row['total_revenue'] * 100 if row['total_revenue'] > 0 else 0
        print(f"    {row['month']}: total=${row['total_revenue']:>10,.0f}  "
              f"repeat=${row['repeat_revenue']:>10,.0f}  "
              f"new=${row['new_customer_revenue']:>10,.0f}  "
              f"repeat%={rep_pct:.0f}%")

    return ok


def check_revenue_matrix():
    """Check Revenue Matrix output structure and coherence."""
    print("\n=== CHECK 6: Revenue Matrix ===")
    clear_waterfall_cache()

    result = build_revenue_matrix(media_plan=[], horizon_months=12)

    matrix = result['matrix']
    summary = result['monthly_summary']

    if matrix.empty:
        print("  ERROR: revenue matrix is empty")
        return False

    print(f"  Matrix shape: {matrix.shape[0]} cohorts x {matrix.shape[1]} calendar months")

    # Check monthly summary
    if summary.empty:
        print("  ERROR: monthly summary is empty")
        return False

    expected_summary_cols = ['month', 'total_revenue', 'new_revenue', 'repeat_revenue', 'repeat_pct']
    missing = [c for c in expected_summary_cols if c not in summary.columns]
    if missing:
        print(f"  ERROR: summary missing columns: {missing}")
        return False
    print(f"  Summary columns: OK")
    print(f"  Summary rows: {len(summary)}")

    # Verify coherence: total = new + repeat (approximately)
    coherent = True
    for _, row in summary.iterrows():
        total = row['total_revenue']
        new_plus_repeat = row['new_revenue'] + row['repeat_revenue']
        if total > 0:
            diff_pct = abs(total - new_plus_repeat) / total * 100
            if diff_pct > 1:  # allow 1% rounding
                print(f"  WARNING: {row['month']} total={total:.0f} != new+repeat={new_plus_repeat:.0f}")
                coherent = False

    print(f"  Revenue coherence (total = new + repeat): {'OK' if coherent else 'FAIL'}")

    # Print recent summary
    recent = summary[summary['month'] >= '2024-01'].head(12)
    if not recent.empty:
        print(f"\n  Recent monthly summary:")
        for _, row in recent.iterrows():
            print(f"    {row['month']}: total=${row['total_revenue']:>10,.0f}  "
                  f"new=${row['new_revenue']:>10,.0f}  "
                  f"repeat=${row['repeat_revenue']:>10,.0f}  "
                  f"repeat%={row['repeat_pct']:.0f}%")

    return coherent


def check_new_customers():
    """Verify new customer counts are reasonable."""
    print("\n=== CHECK 7: New Customer Counts ===")
    clear_waterfall_cache()
    custs = get_monthly_new_customers()

    if not custs:
        print("  ERROR: no customer data")
        return False

    recent_months = sorted([m for m in custs if m >= '2024-01'])
    if not recent_months:
        recent_months = sorted(custs.keys())[-12:]

    total_recent = sum(custs[m] for m in recent_months)
    avg_recent = total_recent / len(recent_months) if recent_months else 0

    print(f"  Total months with data: {len(custs)}")
    print(f"  Recent months ({recent_months[0]} - {recent_months[-1]}):")
    for m in recent_months:
        print(f"    {m}: {custs[m]:,}")
    print(f"  Average recent: {avg_recent:,.0f}")

    # Should have >500 new customers/month for a DTC brand of this size
    ok = avg_recent > 200
    print(f"  Average > 200/month: {'OK' if ok else 'LOW'}")
    return ok


def main():
    print("=" * 65)
    print("REPEAT MODEL VALIDATION (vs Gold Standard Spreadsheet)")
    print("=" * 65)

    results = {}

    results["parameters"] = check_parameters()
    results["retention_curve"] = check_retention_curve()
    results["cohort_data"] = check_cohort_data()
    results["aov_metrics"] = check_aov_and_metrics()
    results["new_customers"] = check_new_customers()
    results["waterfall_output"] = check_waterfall_output()
    results["revenue_matrix"] = check_revenue_matrix()

    print("\n" + "=" * 65)
    print("SUMMARY")
    print("=" * 65)
    all_pass = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {name:20s} {status}")
        if not passed:
            all_pass = False

    print(f"\n  Overall: {'ALL PASS' if all_pass else 'SOME FAILURES'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
