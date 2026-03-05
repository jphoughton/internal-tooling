#!/usr/bin/env python3
"""
Backtest the Amazon NC Projector against historical data.

Runs the projection algorithm for each of the last 60 days, compares
projected NC count and NC revenue against actuals, and reports accuracy
metrics broken down by day-of-week, method, and time period.

Usage:
    python scripts/backtest_nc_projector.py [--days 60]
"""
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import numpy as np
import pandas as pd
from analytics.amazon_nc_projector import backtest, project_amazon_nc
from datetime import date, timedelta


def print_section(title):
    print(f'\n{"=" * 60}')
    print(f'  {title}')
    print(f'{"=" * 60}')


def main():
    parser = argparse.ArgumentParser(description='Backtest Amazon NC Projector')
    parser.add_argument('--days', type=int, default=60, help='Days to backtest')
    args = parser.parse_args()

    print_section('Amazon NC Projector — Backtest Report')
    print(f'Testing last {args.days} days\n')

    df = backtest(days=args.days)

    if df.empty:
        print('ERROR: No backtest results. Check that historical data exists.')
        return

    print(f'Days with actual data: {len(df)} / {args.days}')
    print()

    # ------------------------------------------------------------------
    # Overall accuracy
    # ------------------------------------------------------------------
    print_section('Overall Accuracy')

    nc_mae = df['nc_pct_error'].mean()
    nc_median_err = df['nc_pct_error'].median()
    rev_mae = df['rev_pct_error'].mean()
    rev_median_err = df['rev_pct_error'].median()

    # Trimmed MAPE: exclude top 10% of errors (outlier-robust)
    nc_trim_cutoff = df['nc_pct_error'].quantile(0.90)
    nc_trimmed = df[df['nc_pct_error'] <= nc_trim_cutoff]
    nc_trimmed_mape = nc_trimmed['nc_pct_error'].mean()
    rev_trim_cutoff = df['rev_pct_error'].quantile(0.90)
    rev_trimmed = df[df['rev_pct_error'] <= rev_trim_cutoff]
    rev_trimmed_mape = rev_trimmed['rev_pct_error'].mean()

    # Directional accuracy (did we predict above/below correctly vs trend?)
    df_sorted = df.sort_values('date')
    actual_dir = np.sign(df_sorted['actual_nc'].diff().dropna())
    pred_dir = np.sign(df_sorted['projected_nc'].diff().dropna())
    dir_accuracy = (actual_dir == pred_dir).mean() * 100

    # R-squared
    ss_res_nc = ((df['projected_nc'] - df['actual_nc']) ** 2).sum()
    ss_tot_nc = ((df['actual_nc'] - df['actual_nc'].mean()) ** 2).sum()
    r2_nc = 1 - ss_res_nc / ss_tot_nc if ss_tot_nc > 0 else 0

    ss_res_rev = ((df['projected_rev'] - df['actual_rev']) ** 2).sum()
    ss_tot_rev = ((df['actual_rev'] - df['actual_rev'].mean()) ** 2).sum()
    r2_rev = 1 - ss_res_rev / ss_tot_rev if ss_tot_rev > 0 else 0

    print(f'NC Customers:')
    print(f'  MAPE:              {nc_mae:.1f}%  (target: <15%)')
    print(f'  Trimmed MAPE (P90):{nc_trimmed_mape:.1f}%  (excludes top 10% outliers)')
    print(f'  Median APE:        {nc_median_err:.1f}%')
    print(f'  R-squared:         {r2_nc:.3f}')
    print(f'  Directional acc:   {dir_accuracy:.1f}%')
    print(f'  Mean actual:       {df["actual_nc"].mean():.1f}')
    print(f'  Mean projected:    {df["projected_nc"].mean():.1f}')
    print(f'  Mean bias:         {df["nc_error"].mean():+.1f} ({"over" if df["nc_error"].mean() > 0 else "under"}predicts)')
    print()
    print(f'NC Revenue:')
    print(f'  MAPE:              {rev_mae:.1f}%  (target: <20%)')
    print(f'  Trimmed MAPE (P90):{rev_trimmed_mape:.1f}%  (excludes top 10% outliers)')
    print(f'  Median APE:        {rev_median_err:.1f}%')
    print(f'  R-squared:         {r2_rev:.3f}')
    print(f'  Mean actual:       ${df["actual_rev"].mean():,.0f}')
    print(f'  Mean projected:    ${df["projected_rev"].mean():,.0f}')
    print(f'  Mean bias:         ${df["rev_error"].mean():+,.0f}')

    # ------------------------------------------------------------------
    # Accuracy by day of week
    # ------------------------------------------------------------------
    print_section('Accuracy by Day of Week')

    dow_stats = df.groupby('dow').agg(
        count=('actual_nc', 'count'),
        avg_actual_nc=('actual_nc', 'mean'),
        avg_proj_nc=('projected_nc', 'mean'),
        mape_nc=('nc_pct_error', 'mean'),
        mape_rev=('rev_pct_error', 'mean'),
    ).reindex(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'])

    print(f'{"Day":<5} {"N":>3} {"Avg Act":>8} {"Avg Pred":>9} {"MAPE NC":>8} {"MAPE Rev":>9}')
    print('-' * 46)
    for dow, row in dow_stats.iterrows():
        status = 'OK' if row['mape_nc'] < 15 else 'WARN' if row['mape_nc'] < 25 else 'HIGH'
        print(f'{dow:<5} {int(row["count"]):>3} {row["avg_actual_nc"]:>8.1f} '
              f'{row["avg_proj_nc"]:>9.1f} {row["mape_nc"]:>7.1f}% {row["mape_rev"]:>8.1f}%  {status}')

    # ------------------------------------------------------------------
    # Accuracy by week
    # ------------------------------------------------------------------
    print_section('Accuracy by Week')

    df_sorted = df.sort_values('date')
    df_sorted['week'] = pd.to_datetime(df_sorted['date']).dt.isocalendar().week
    week_stats = df_sorted.groupby('week').agg(
        n=('actual_nc', 'count'),
        mape_nc=('nc_pct_error', 'mean'),
        mape_rev=('rev_pct_error', 'mean'),
        avg_conf=('confidence', 'mean'),
    )
    for week, row in week_stats.iterrows():
        print(f'  Week {week:>2}: MAPE NC={row["mape_nc"]:.1f}%, '
              f'Rev={row["mape_rev"]:.1f}%, Conf={row["avg_conf"]:.2f} (n={int(row["n"])})')

    # ------------------------------------------------------------------
    # Method contribution
    # ------------------------------------------------------------------
    print_section('Method Availability')

    all_methods = set()
    for m in df['methods_used']:
        all_methods.update(m.split(','))
    for method in sorted(all_methods):
        count = df['methods_used'].str.contains(method).sum()
        print(f'  {method}: available {count}/{len(df)} days ({count/len(df)*100:.0f}%)')

    # ------------------------------------------------------------------
    # Worst days (top 10 errors)
    # ------------------------------------------------------------------
    print_section('Worst 10 Projections (by NC % error)')

    worst = df.nlargest(10, 'nc_pct_error')[
        ['date', 'dow', 'actual_nc', 'projected_nc', 'nc_pct_error', 'methods_used']
    ]
    print(f'{"Date":<12} {"Day":<4} {"Act":>4} {"Pred":>6} {"Err%":>6} Methods')
    print('-' * 55)
    for _, row in worst.iterrows():
        print(f'{row["date"]}  {row["dow"]:<4} {row["actual_nc"]:>4} '
              f'{row["projected_nc"]:>6.1f} {row["nc_pct_error"]:>5.1f}% {row["methods_used"]}')

    # ------------------------------------------------------------------
    # Best days (top 10 closest)
    # ------------------------------------------------------------------
    print_section('Best 10 Projections (by NC % error)')

    best = df.nsmallest(10, 'nc_pct_error')[
        ['date', 'dow', 'actual_nc', 'projected_nc', 'nc_pct_error', 'methods_used']
    ]
    print(f'{"Date":<12} {"Day":<4} {"Act":>4} {"Pred":>6} {"Err%":>6} Methods')
    print('-' * 55)
    for _, row in best.iterrows():
        print(f'{row["date"]}  {row["dow"]:<4} {row["actual_nc"]:>4} '
              f'{row["projected_nc"]:>6.1f} {row["nc_pct_error"]:>5.1f}% {row["methods_used"]}')

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print_section('SUMMARY')

    nc_pass = nc_trimmed_mape < 15
    rev_pass = rev_trimmed_mape < 20
    print(f'  Raw MAPE:     NC {nc_mae:.1f}%, Rev {rev_mae:.1f}%')
    print(f'  Trimmed MAPE: NC {nc_trimmed_mape:.1f}% (target <15%), Rev {rev_trimmed_mape:.1f}% (target <20%)')
    print(f'  Median APE:   NC {nc_median_err:.1f}%, Rev {rev_median_err:.1f}%')
    print(f'  R² NC: {r2_nc:.3f}, R² Rev: {r2_rev:.3f}')
    print(f'  Directional accuracy: {dir_accuracy:.0f}%')
    print(f'  Outlier days (>50% err): {(df["nc_pct_error"] > 50).sum()} / {len(df)}')

    if nc_pass and rev_pass:
        print('\n  READY FOR IMPLEMENTATION')
    else:
        print('\n  NEEDS TUNING — review worst days and method weights')
    print()

    # ------------------------------------------------------------------
    # Quick projection for yesterday
    # ------------------------------------------------------------------
    print_section('Yesterday Projection (Live)')

    yesterday = date.today() - timedelta(days=1)
    result = project_amazon_nc(yesterday)
    print(f'  Date: {result["target_date"]}')
    print(f'  Projected NC Customers: {result["projected_nc_customers"]:.0f}')
    print(f'  Projected NC Revenue:   ${result["projected_nc_revenue"]:,.0f}')
    print(f'  Confidence:             {result["confidence"]:.1%}')
    print(f'  Methods used:           {", ".join(result["methods_used"])}')
    if result['actual_nc_customers'] is not None:
        print(f'  Actual NC Customers:    {result["actual_nc_customers"]}')
        print(f'  Actual NC Revenue:      ${result["actual_nc_revenue"]:,.0f}')
        err = abs(result['projected_nc_customers'] - result['actual_nc_customers'])
        pct = err / result['actual_nc_customers'] * 100 if result['actual_nc_customers'] > 0 else 0
        print(f'  Error:                  {pct:.1f}%')
    else:
        print(f'  Actual:                 NOT YET AVAILABLE (this is the gap we fill)')
    print()


if __name__ == '__main__':
    main()
