#!/usr/bin/env python3
"""Analyst 15 — Actuals vs Projection Backtest.

Strategy: Monkey-patch date.today() to 4 weeks ago, so the model treats
weeks 1-4 as actuals and weeks 5-8 as projections. Then compare those
projections against what actually happened in bank data for weeks 5-8.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
import pandas as pd
from unittest.mock import patch
from db import get_db, read_sql

real_today = date.today()
# Pretend today is 4 weeks ago so weeks 5-8 become "projected"
fake_today = real_today - timedelta(weeks=4)

print(f"Real today: {real_today}")
print(f"Fake today (simulated): {fake_today}")

# Start the forecast 4 weeks before fake_today = 8 weeks before real_today
start_date = fake_today - timedelta(weeks=4)
print(f"Forecast start: {start_date}")
print(f"Weeks 1-4: {start_date} to ~{fake_today} (actuals)")
print(f"Weeks 5-8: ~{fake_today} to ~{real_today} (projected — to be backtested)")
print()

with get_db() as conn:
    # Monkey-patch date.today() inside analytics.cashflow module
    import analytics.cashflow as cf_module
    original_date = cf_module.date

    class FakeDate(date):
        @classmethod
        def today(cls):
            return fake_today

    with patch.object(cf_module, 'date', FakeDate):
        df = cf_module.build_cashflow_forecast(conn, start_date=start_date, weeks=12, scenario='base')

    print(f"Total rows: {len(df)}")
    print()

    # Show all rows with key columns
    print("=== Full Forecast Output (simulated from {}) ===".format(fake_today))
    cols = ['week_start', 'week_end', 'week_num', 'is_actual',
            'total_inflows', 'total_outflows', 'net_cashflow',
            'opening_balance', 'closing_balance']
    print(df[cols].to_string(index=False))
    print()

    actual_rows = df[df['is_actual'] == True]
    projected_rows = df[df['is_actual'] == False]
    print(f"Actual rows: {len(actual_rows)}")
    print(f"Projected rows: {len(projected_rows)}")
    print()

    # Backtest: compare projected weeks against real bank data
    print("=== Backtest: Projected Weeks vs Actual Bank Data ===")
    print()

    comparison_rows = []
    for _, row in projected_rows.iterrows():
        ws = row['week_start']
        we = row['week_end']
        week_num = row['week_num']

        # Only backtest weeks that are fully in the REAL past
        we_date = date.fromisoformat(we) if isinstance(we, str) else we
        if we_date >= real_today:
            print(f"Week {week_num} ({ws} to {we}): NOT fully in real past, skipping")
            continue

        # Query actual credits (inflows) for this week
        actual_credits = read_sql("""
            SELECT COALESCE(SUM(amount), 0) as total
            FROM cashflow_transactions
            WHERE tx_date >= %s AND tx_date <= %s
                AND direction = 'credit'
                AND is_transfer = 0 AND is_duplicate = 0
        """, conn, params=(ws, we))
        actual_inflows = float(actual_credits['total'].iloc[0]) if not actual_credits.empty else 0

        # Query actual debits (outflows) for this week
        actual_debits = read_sql("""
            SELECT COALESCE(SUM(amount), 0) as total
            FROM cashflow_transactions
            WHERE tx_date >= %s AND tx_date <= %s
                AND direction = 'debit'
                AND is_transfer = 0 AND is_duplicate = 0
        """, conn, params=(ws, we))
        actual_outflows = float(actual_debits['total'].iloc[0]) if not actual_debits.empty else 0

        actual_net = actual_inflows - actual_outflows

        proj_inflows = row['total_inflows']
        proj_outflows = row['total_outflows']
        proj_net = row['net_cashflow']

        # Calculate errors
        inflow_err = abs(proj_inflows - actual_inflows) / actual_inflows * 100 if actual_inflows > 0 else (float('inf') if proj_inflows > 0 else 0)
        outflow_err = abs(proj_outflows - actual_outflows) / actual_outflows * 100 if actual_outflows > 0 else (float('inf') if proj_outflows > 0 else 0)
        net_err_abs = abs(proj_net - actual_net)

        print(f"Week {week_num} ({ws} to {we}):")
        print(f"  Projected inflows:  ${proj_inflows:>12,.2f}  |  Actual inflows:  ${actual_inflows:>12,.2f}  |  Error: {inflow_err:.1f}%")
        print(f"  Projected outflows: ${proj_outflows:>12,.2f}  |  Actual outflows: ${actual_outflows:>12,.2f}  |  Error: {outflow_err:.1f}%")
        print(f"  Projected net:      ${proj_net:>12,.2f}  |  Actual net:      ${actual_net:>12,.2f}  |  Abs error: ${net_err_abs:>12,.2f}")
        print()

        comparison_rows.append({
            'week_num': week_num,
            'week_start': ws,
            'week_end': we,
            'proj_inflows': proj_inflows,
            'actual_inflows': actual_inflows,
            'inflow_error_pct': inflow_err,
            'proj_outflows': proj_outflows,
            'actual_outflows': actual_outflows,
            'outflow_error_pct': outflow_err,
            'proj_net': proj_net,
            'actual_net': actual_net,
            'net_error_abs': net_err_abs,
        })

    if comparison_rows:
        comp_df = pd.DataFrame(comparison_rows)
        print("=== Summary Statistics ===")
        avg_inflow_err = comp_df['inflow_error_pct'].mean()
        avg_outflow_err = comp_df['outflow_error_pct'].mean()
        avg_net_err = comp_df['net_error_abs'].mean()
        total_proj_inflows = comp_df['proj_inflows'].sum()
        total_actual_inflows = comp_df['actual_inflows'].sum()
        total_proj_outflows = comp_df['proj_outflows'].sum()
        total_actual_outflows = comp_df['actual_outflows'].sum()
        total_inflow_err = abs(total_proj_inflows - total_actual_inflows) / total_actual_inflows * 100 if total_actual_inflows > 0 else float('inf')
        total_outflow_err = abs(total_proj_outflows - total_actual_outflows) / total_actual_outflows * 100 if total_actual_outflows > 0 else float('inf')

        print(f"Weeks backtested: {len(comparison_rows)}")
        print(f"Average weekly inflow error: {avg_inflow_err:.1f}%")
        print(f"Average weekly outflow error: {avg_outflow_err:.1f}%")
        print(f"Average weekly net error (abs): ${avg_net_err:,.0f}")
        print()
        print(f"Total projected inflows:  ${total_proj_inflows:>12,.2f}")
        print(f"Total actual inflows:     ${total_actual_inflows:>12,.2f}")
        print(f"Total inflow error:       {total_inflow_err:.1f}%")
        print()
        print(f"Total projected outflows: ${total_proj_outflows:>12,.2f}")
        print(f"Total actual outflows:    ${total_actual_outflows:>12,.2f}")
        print(f"Total outflow error:      {total_outflow_err:.1f}%")
        print()

        # Projected closing balance at end of backtest period
        last_proj_close = comp_df.iloc[-1]['proj_net']  # not quite right, need cumulative
        # Get the projected closing balance at end of last backtested week
        last_backtest_week = comp_df.iloc[-1]['week_num']
        proj_closing = float(df[df['week_num'] == last_backtest_week]['closing_balance'].iloc[0])
        # Get actual cumulative: opening balance + actual net over all backtest weeks
        actual_cumulative_net = comp_df['actual_net'].sum()
        proj_cumulative_net = comp_df['proj_net'].sum()
        # Opening balance at start of first projected week
        first_proj_week = comp_df.iloc[0]['week_num']
        proj_opening = float(df[df['week_num'] == first_proj_week]['opening_balance'].iloc[0])
        actual_closing = proj_opening + actual_cumulative_net  # NOTE: opening is same for both (from actuals)
        proj_closing_check = proj_opening + proj_cumulative_net

        print(f"Opening balance (start of projections): ${proj_opening:>12,.2f}")
        print(f"Projected cumulative net (weeks 5-8):   ${proj_cumulative_net:>12,.2f}")
        print(f"Actual cumulative net (weeks 5-8):      ${actual_cumulative_net:>12,.2f}")
        print(f"Projected closing (end of week 8):      ${proj_closing_check:>12,.2f}")
        print(f"Actual closing (end of week 8):         ${actual_closing:>12,.2f}")
        print(f"Closing balance error:                  ${abs(proj_closing_check - actual_closing):>12,.2f}")
        print()

        overall_avg_err = (avg_inflow_err + avg_outflow_err) / 2
        print(f"Overall average error:    {overall_avg_err:.1f}%")
        if overall_avg_err < 20:
            print("VERDICT: PASS (<20% average error)")
        elif overall_avg_err < 30:
            print("VERDICT: CONDITIONAL PASS (20-30% average error)")
        else:
            print("VERDICT: FAIL (>30% average error)")
    else:
        print("No projected weeks are fully in the past — cannot backtest.")

    # Per-category error analysis
    print()
    print("=== Per-Category Error Analysis (Projected Weeks in Past) ===")
    print()

    from utils.constants import CASHFLOW_CATEGORIES
    revenue_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'revenue']
    expense_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'expense']

    cat_errors = []
    for cat in revenue_cats + expense_cats:
        if cat not in df.columns:
            continue

        proj_total = 0
        actual_total = 0
        weeks_counted = 0

        for _, row in projected_rows.iterrows():
            ws = row['week_start']
            we = row['week_end']
            we_date = date.fromisoformat(we) if isinstance(we, str) else we
            if we_date >= real_today:
                continue

            proj_total += row[cat]

            actual_q = read_sql("""
                SELECT COALESCE(SUM(amount), 0) as total
                FROM cashflow_transactions
                WHERE category = %s
                    AND tx_date >= %s AND tx_date <= %s
                    AND is_transfer = 0 AND is_duplicate = 0
            """, conn, params=(cat, ws, we))
            actual_total += float(actual_q['total'].iloc[0]) if not actual_q.empty else 0
            weeks_counted += 1

        if weeks_counted == 0:
            continue

        err = abs(proj_total - actual_total) / actual_total * 100 if actual_total > 0 else (float('inf') if proj_total > 0 else 0)
        direction = "OVER" if proj_total > actual_total else "UNDER"
        err_abs = abs(proj_total - actual_total)
        status = "PASS" if err < 25 else ("WARN" if err < 50 else "FAIL")
        cat_errors.append({
            'category': cat,
            'proj_total': proj_total,
            'actual_total': actual_total,
            'error_pct': err,
            'error_abs': err_abs,
            'direction': direction,
            'status': status,
        })
        print(f"  {cat:25s}  Proj: ${proj_total:>10,.0f}  Actual: ${actual_total:>10,.0f}  Error: {err:>6.1f}% {direction:5s}  [{status}]")

    if cat_errors:
        print()
        # Sort by absolute error to find biggest contributors
        cat_errors.sort(key=lambda x: x['error_abs'], reverse=True)
        print("=== Top Error Contributors (by absolute $ difference) ===")
        for i, ce in enumerate(cat_errors[:5]):
            print(f"  {i+1}. {ce['category']:25s}  ${ce['error_abs']:>10,.0f}  ({ce['error_pct']:.0f}% {ce['direction']})")

    print()
    print("Done.")
