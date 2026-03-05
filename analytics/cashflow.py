"""
Cash Flow forecasting: projection engine powered by P&L revenue model and settings.

No bank data. All projections use the revenue model (Variables page P&L),
user-configured settings, and seed defaults from constants.py.
"""
import calendar
import json
import logging
from datetime import date, timedelta

import pandas as pd
import numpy as np

from db import read_sql, ConnectionWrapper
from utils.constants import (
    CASHFLOW_CATEGORIES, CASHFLOW_SEED_DEFAULTS,
    DTC_DOW_WEIGHTS, CASHFLOW_CONFIDENCE_WEEKLY_GROWTH,
    CASHFLOW_CONFIDENCE_MAX,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Week-in-month helper functions
# ---------------------------------------------------------------------------

def which_week_in_month(d: date) -> int:
    """Return 1-based week number within the month for date *d*.

    Uses Monday-start weeks. Week 1 is the week of the first Monday
    that falls on or after the 1st of the month. This matches the
    Excel model where weekly columns start on Mondays within the month.
    """
    first_of_month = date(d.year, d.month, 1)
    # First Monday on or after the 1st
    days_until_monday = (7 - first_of_month.weekday()) % 7
    first_monday = first_of_month + timedelta(days=days_until_monday)
    # Monday of the week containing d
    d_monday = d - timedelta(days=d.weekday())
    if d_monday < first_monday:
        return 1  # before the first full Monday — week 1
    week_num = ((d_monday - first_monday).days // 7) + 1
    return max(1, week_num)


def is_last_week_of_month(ws: date, we: date) -> bool:
    """True if the week [ws, we] contains the last day of ws's month."""
    _, last_day = calendar.monthrange(ws.year, ws.month)
    eom = date(ws.year, ws.month, last_day)
    return ws <= eom <= we


def is_first_week_of_month(ws: date, we: date) -> bool:
    """True if this is the first Monday-start week of ws's month.

    Uses a simple rule: the week is "first" if ws.day <= 7 (i.e. the Monday
    falls within the first 7 days of the month).  This avoids the
    month-boundary overlap bug where a week spanning two months would fire
    both last-week-of-month AND first-week-of-next-month expenses.
    """
    return ws.day <= 7


def count_weeks_in_month(year: int, month: int) -> int:
    """Count Monday-start weeks whose Monday falls within the month. Returns 4 or 5."""
    _, last_day = calendar.monthrange(year, month)
    first = date(year, month, 1)
    last = date(year, month, last_day)
    # First Monday on or after the 1st
    days_until_monday = (7 - first.weekday()) % 7
    first_monday = first + timedelta(days=days_until_monday)
    # Count Mondays within the month
    count = 0
    m = first_monday
    while m <= last:
        count += 1
        m += timedelta(days=7)
    return max(count, 4)


# ---------------------------------------------------------------------------
# 52-Week Cash Flow Projection Engine
# ---------------------------------------------------------------------------

def _get_week_boundaries(start_date: date, weeks: int) -> list:
    """Generate list of (week_start, week_end) tuples from start_date."""
    # Align to Monday
    days_since_monday = start_date.weekday()
    week_start = start_date - timedelta(days=days_since_monday)
    result = []
    for _ in range(weeks):
        week_end = week_start + timedelta(days=6)
        result.append((week_start, week_end))
        week_start = week_end + timedelta(days=1)
    return result


def _build_amazon_disbursement_schedule(week_list: list) -> dict:
    """Pre-compute Amazon disbursement events across ALL weeks at once.

    Amazon disburses biweekly, typically around day 8 (early) and day 24
    (late) of each month.  Each disbursement is assigned to exactly one
    week — the week that contains the midpoint date — eliminating the
    double-counting bug where a disbursement window spanning two weeks
    was detected by both.

    Returns dict keyed by week_start string: {ws_str: {month_key: count}}.
    """
    if not week_list:
        return {}

    first_ws = week_list[0][0]
    last_we = week_list[-1][1]

    # Generate all disbursement midpoint dates in the date range.
    # Early window midpoint = 8th, late window midpoint = 24th.
    disbursement_dates = []  # list of (date, month_key)
    y, m = first_ws.year, first_ws.month
    last_y, last_m = last_we.year, last_we.month
    while (y, m) <= (last_y, last_m):
        month_key = f'{y:04d}-{m:02d}'
        for mid_day in (8, 24):
            try:
                d = date(y, m, mid_day)
            except ValueError:
                continue
            if first_ws <= d <= last_we:
                disbursement_dates.append((d, month_key))
        m += 1
        if m > 12:
            m = 1
            y += 1

    # Assign each disbursement to the week that contains its midpoint date.
    schedule = {}  # {ws_str: {month_key: count}}
    for disb_date, month_key in disbursement_dates:
        for ws, we in week_list:
            if ws <= disb_date <= we:
                ws_str = str(ws)
                if ws_str not in schedule:
                    schedule[ws_str] = {}
                schedule[ws_str][month_key] = schedule[ws_str].get(month_key, 0) + 1
                break  # assigned — move to next disbursement

    return schedule


def _project_revenue_week(
    conn: ConnectionWrapper,
    category: str,
    week_start: date,
    week_end: date,
    ctx: dict,
) -> float:
    """Project revenue for a single future week."""
    if category == 'dtc_revenue':
        # DTC revenue net of processing fees (already deducted in context).
        # Distribute monthly total to weeks using DOW payout weights.
        monthly_rev = ctx.get('dtc_monthly_revenue', {})

        total = 0.0
        weight_sum = sum(DTC_DOW_WEIGHTS.values())
        if weight_sum <= 0:
            weight_sum = 1.0

        d = week_start
        while d <= week_end:
            dow_weight = DTC_DOW_WEIGHTS.get(d.weekday(), 0.0)
            month_key = d.strftime('%Y-%m')
            monthly = monthly_rev.get(month_key, 0)
            daily_share = (monthly / 30.44) * (dow_weight / (weight_sum / 7))
            total += daily_share
            d += timedelta(days=1)

        return total

    elif category == 'amazon_revenue':
        # Amazon revenue net of Amazon fees (already deducted in context).
        # Disburses biweekly (~8th and ~24th of each month).
        monthly_rev = ctx.get('amazon_monthly_revenue', {})

        schedule = ctx.get('_amazon_disbursements', {})
        week_disb = schedule.get(str(week_start), {})
        if not week_disb:
            return 0.0

        # Each disbursement event = half the month's net payout
        total = 0.0
        for month_key, count in week_disb.items():
            monthly = monthly_rev.get(month_key, 0)
            total += (monthly / 2) * count
        return total

    elif category == 'wholesale_revenue':
        # Wholesale: monthly from P&L, spread evenly across weeks
        month_key = week_start.strftime('%Y-%m')
        monthly = ctx.get('_rm_wholesale_monthly', {}).get(month_key, 0)
        if monthly > 0:
            weeks_in_month = count_weeks_in_month(week_start.year, week_start.month)
            return monthly / weeks_in_month
        return 0

    else:
        return 0


# ---------------------------------------------------------------------------
# Individual expense projector functions
# ---------------------------------------------------------------------------
# Each projector has signature: (conn, category, week_start, week_end, ctx) -> float

def _project_media_split(conn, category, week_start, week_end, ctx):
    """Split monthly P&L media into 2 payments: week 2 (40%) + last week (60%).

    Media is paid one month in arrears — March spend is billed in April.
    So the cash outflow in month N uses month N-1's P&L media spend.
    """
    # Look up PREVIOUS month's media spend (paid in arrears)
    if week_start.month == 1:
        prev_year, prev_month = week_start.year - 1, 12
    else:
        prev_year, prev_month = week_start.year, week_start.month - 1
    prior_month_key = f'{prev_year:04d}-{prev_month:02d}'
    monthly_media = ctx.get('monthly_media_spend', {})
    monthly = monthly_media.get(prior_month_key, CASHFLOW_SEED_DEFAULTS.get(category, 0) * 4.33)

    early_pct = ctx.get('media_split_early_pct', 0.40)
    early_week = ctx.get('media_early_week', 2)
    wk = which_week_in_month(week_start)

    if wk == early_week:
        return monthly * early_pct
    elif is_last_week_of_month(week_start, week_end):
        return monthly * (1.0 - early_pct)
    return 0.0


def _project_payroll_alternating(conn, category, week_start, week_end, ctx):
    """Biweekly payroll with alternating amounts A and B.

    Hits on weeks 2 and 4 of the month. Alternates between A (smaller)
    and B (larger) using a running toggle stored in ctx.
    """
    payroll_a = ctx.get('payroll_amount_a', 13000)
    payroll_b = ctx.get('payroll_amount_b', 15500)

    wk = which_week_in_month(week_start)
    if wk not in (2, 4):
        return 0.0

    # Alternate A/B using a running toggle in ctx
    next_is_a = ctx.get('_payroll_next_is_a', True)
    if next_is_a:
        amount = payroll_a
    else:
        amount = payroll_b
    ctx['_payroll_next_is_a'] = not next_is_a
    return amount


def _project_fulfillment_spread(conn, category, week_start, week_end, ctx):
    """P&L fulfillment spread over actual weeks in the month."""
    month_key = week_start.strftime('%Y-%m')
    rm_fulfill = ctx.get('_rm_monthly_fulfillment', {}).get(month_key, 0)
    if rm_fulfill > 0:
        weeks_in_month = count_weeks_in_month(week_start.year, week_start.month)
        return rm_fulfill / weeks_in_month

    # Fallback: use seed default (weekly amount)
    return CASHFLOW_SEED_DEFAULTS.get(category, 5000)


def _project_sales_tax_pct(conn, category, week_start, week_end, ctx):
    """Sales tax: % of net sales, paid monthly on week 1.

    Uses the P&L net_sales for the month × sales_tax_rate.
    Falls back to the flat sales_tax_default setting.
    """
    if not is_first_week_of_month(week_start, week_end):
        return 0.0

    month_key = week_start.strftime('%Y-%m')
    net_sales = ctx.get('_rm_net_sales', {}).get(month_key, 0)
    tax_rate = ctx.get('sales_tax_rate', 0)

    if net_sales > 0 and tax_rate > 0:
        return net_sales * tax_rate

    # Fallback to flat default
    return ctx.get('sales_tax_default', CASHFLOW_SEED_DEFAULTS.get('sales_tax', 13000))


def _project_shipping_pct(conn, category, week_start, week_end, ctx):
    """Shipping: % of DTC net sales, spread weekly.

    Uses the P&L DTC revenue for the month × shipping_pct, divided across weeks.
    """
    month_key = week_start.strftime('%Y-%m')
    dtc_rev = ctx.get('dtc_monthly_revenue', {}).get(month_key, 0)
    ship_pct = ctx.get('shipping_pct', 0)

    if dtc_rev > 0 and ship_pct > 0:
        weeks_in_month = count_weeks_in_month(week_start.year, week_start.month)
        return (dtc_rev * ship_pct) / weeks_in_month

    # Fallback to seed
    return CASHFLOW_SEED_DEFAULTS.get(category, 2000)


def _project_monthly_lump(conn, category, week_start, week_end, ctx):
    """Monthly lump on the 1st week of month (accounting, insurance, consulting, other_expense)."""
    if not is_first_week_of_month(week_start, week_end):
        return 0.0

    # For consulting, use the setting
    if category == 'consulting':
        return ctx.get('consulting_monthly', CASHFLOW_SEED_DEFAULTS.get('consulting', 0))

    return CASHFLOW_SEED_DEFAULTS.get(category, 0)


def _project_monthly_week2(conn, category, week_start, week_end, ctx):
    """Monthly lump on the 2nd week of month (software)."""
    wk = which_week_in_month(week_start)
    if wk != 2:
        return 0.0
    return CASHFLOW_SEED_DEFAULTS.get(category, 0)


def _project_mktg_opex(conn, category, week_start, week_end, ctx):
    """Marketing OpEx split: retainer on week 1, other on last week."""
    retainer = ctx.get('mktg_opex_retainer', 7000)
    other = ctx.get('mktg_opex_other', 6300)

    wk = which_week_in_month(week_start)
    if wk == 1:
        return retainer
    elif is_last_week_of_month(week_start, week_end):
        return other
    return 0.0


def _project_production_po(conn, category, week_start, week_end, ctx):
    """PO-based production: last week of month only. No COGS fallback."""
    if not is_last_week_of_month(week_start, week_end):
        return 0.0
    month_key = week_start.strftime('%Y-%m')
    return ctx.get('_po_monthly_production', {}).get(month_key, 0)


def _project_loan_principal(conn, category, week_start, week_end, ctx):
    """Loan principal from user-entered schedule, last week of month."""
    if not is_last_week_of_month(week_start, week_end):
        return 0.0
    month_key = week_start.strftime('%Y-%m')
    schedule = ctx.get('loan_principal_schedule', {})
    default = ctx.get('loan_default_principal', 30000)
    return schedule.get(month_key, default)


def _project_loan_interest_eom(conn, category, week_start, week_end, ctx):
    """LOC interest on last week of month. Interest = balance * APR / 12."""
    if not is_last_week_of_month(week_start, week_end):
        return 0.0
    loc_bal = ctx.get('_running_loc_balance', 0)
    if loc_bal <= 0:
        return 0.0
    apr = ctx.get('loc_apr', 0.1164)
    return loc_bal * apr / 12


# Dispatch table mapping method names to projector functions
_EXPENSE_PROJECTORS = {
    'media_split': _project_media_split,
    'biweekly_alternating': _project_payroll_alternating,
    'monthly_spread': _project_fulfillment_spread,
    'monthly_lump': _project_monthly_lump,
    'monthly_week2': _project_monthly_week2,
    'mktg_opex_split': _project_mktg_opex,
    'sales_tax_pct': _project_sales_tax_pct,
    'shipping_pct': _project_shipping_pct,
    'po_based': _project_production_po,
    'loan_schedule': _project_loan_principal,
    'loc_interest_eom': _project_loan_interest_eom,
}


def _project_expense_week(
    conn: ConnectionWrapper,
    category: str,
    week_start: date,
    week_end: date,
    ctx: dict,
) -> float:
    """Project expense for a single future week using dispatch table."""
    method = CASHFLOW_CATEGORIES.get(category, {}).get('method', 'monthly_lump')
    projector = _EXPENSE_PROJECTORS.get(method, _project_monthly_lump)
    return projector(conn, category, week_start, week_end, ctx)


def _apply_scenario(amount: float, category_group: str, scenario: str) -> float:
    """Adjust amount based on scenario."""
    if scenario == 'base':
        return amount
    elif scenario == 'conservative':
        if category_group == 'revenue':
            return amount * 0.85  # -15%
        elif category_group == 'expense':
            return amount * 1.10  # +10%
    elif scenario == 'aggressive':
        if category_group == 'revenue':
            return amount * 1.10  # +10%
        elif category_group == 'expense':
            return amount * 0.95  # -5%
    return amount


def _compute_confidence(week_index: int, net_flow: float) -> tuple:
    """Compute confidence interval for a projected week."""
    pct = min(week_index * CASHFLOW_CONFIDENCE_WEEKLY_GROWTH, CASHFLOW_CONFIDENCE_MAX)
    if week_index <= 4:
        pct = pct * 0.5  # tighter in near term
    magnitude = abs(net_flow) * pct if net_flow != 0 else 1000
    return (net_flow - magnitude, net_flow + magnitude)


def build_cashflow_forecast(
    conn: ConnectionWrapper,
    start_date: date = None,
    weeks: int = 52,
    scenario: str = 'base',
) -> pd.DataFrame:
    """Build a 52-week cash flow forecast.

    Returns weekly DataFrame with columns for each category plus totals,
    opening/closing balance, confidence intervals.

    All projections use the P&L revenue model and configured settings.
    No bank data dependency.
    """
    if start_date is None:
        start_date = date.today()

    week_list = _get_week_boundaries(start_date, weeks)

    # --- Build context dict with all the data the projectors need ---
    ctx = {}

    from db import get_cashflow_setting

    # COGS percentage
    ctx['cogs_pct'] = float(get_cashflow_setting(conn, 'cogs_pct', '0.25'))

    # Fulfillment % of DTC revenue
    ctx['fulfillment_pct'] = float(get_cashflow_setting(conn, 'fulfillment_pct', '0.18'))

    # LOC (line of credit) parameters
    ctx['loc_apr'] = float(get_cashflow_setting(conn, 'loc_apr', '0.1164'))
    loc_current = float(get_cashflow_setting(conn, 'loc_balance', '510000'))

    # Payroll alternating amounts
    ctx['payroll_amount_a'] = float(get_cashflow_setting(conn, 'payroll_amount_a', '13000'))
    ctx['payroll_amount_b'] = float(get_cashflow_setting(conn, 'payroll_amount_b', '15500'))

    # Loan principal schedule (JSON from app_settings)
    ctx['loan_default_principal'] = float(get_cashflow_setting(conn, 'loan_default_principal', '30000'))
    try:
        _lps_raw = get_cashflow_setting(conn, 'loan_principal_schedule', '{}')
        ctx['loan_principal_schedule'] = json.loads(_lps_raw) if _lps_raw else {}
    except Exception:
        ctx['loan_principal_schedule'] = {}

    # Media split timing
    ctx['media_split_early_pct'] = float(get_cashflow_setting(conn, 'media_split_early_pct', '0.40'))
    ctx['media_early_week'] = int(float(get_cashflow_setting(conn, 'media_early_week', '2')))

    # Sales tax: rate (% of net sales) or flat default fallback
    ctx['sales_tax_rate'] = float(get_cashflow_setting(conn, 'sales_tax_rate', '0.043'))
    ctx['sales_tax_default'] = float(get_cashflow_setting(conn, 'sales_tax_default', '13000'))

    # Shipping: % of DTC revenue
    ctx['shipping_pct'] = float(get_cashflow_setting(conn, 'shipping_pct', '0.02'))

    # Consulting monthly
    ctx['consulting_monthly'] = float(get_cashflow_setting(conn, 'consulting_monthly', '0'))

    # Marketing OpEx split
    ctx['mktg_opex_retainer'] = float(get_cashflow_setting(conn, 'mktg_opex_retainer', '7000'))
    ctx['mktg_opex_other'] = float(get_cashflow_setting(conn, 'mktg_opex_other', '6300'))

    # Revenue + mapped costs — sourced from the revenue model (variables page P&L).
    try:
        from db import get_revenue_model
        from analytics import revenue_model as rm

        # Generate months covering the forecast horizon (Feb 2026+)
        _rm_months = []
        _y, _m = week_list[0][0].year, week_list[0][0].month
        _end_y, _end_m = week_list[-1][1].year, week_list[-1][1].month
        while (_y, _m) <= (_end_y, _end_m):
            mk = f'{_y:04d}-{_m:02d}'
            if mk >= '2026-02':
                _rm_months.append(mk)
            _m += 1
            if _m > 12:
                _m = 1
                _y += 1

        db_data = get_revenue_model(conn)
        rm_inputs = rm.merge_with_defaults(db_data, _rm_months)
        rm_calc = rm.compute(rm_inputs, _rm_months)

        # Revenue net of platform fees (what actually hits the bank).
        ctx['dtc_monthly_revenue'] = {}
        ctx['amazon_monthly_revenue'] = {}
        for _mk in _rm_months:
            dtc_rev = rm_calc.get('dtc_rev', {}).get(_mk, 0)
            dtc_proc = rm_calc.get('dtc_processing_amt', {}).get(_mk, 0)
            ctx['dtc_monthly_revenue'][_mk] = dtc_rev - dtc_proc

            amz_rev = rm_calc.get('amazon_rev', {}).get(_mk, 0)
            amz_fees = rm_calc.get('amazon_fulfillment_amt', {}).get(_mk, 0)
            ctx['amazon_monthly_revenue'][_mk] = amz_rev - amz_fees

        ctx['monthly_media_spend'] = rm_calc.get('total_media_spend', {})
        ctx['_rm_net_sales'] = rm_calc.get('net_sales', {})

        # Wholesale revenue monthly
        ctx['_rm_wholesale_monthly'] = {}
        for _mk in _rm_months:
            ctx['_rm_wholesale_monthly'][_mk] = rm_inputs.get('wholesale_rev', {}).get(_mk, 0)

        # Monthly fulfillment (DTC 3PL only)
        ctx['_rm_monthly_fulfillment'] = {}
        for _mk in _rm_months:
            ctx['_rm_monthly_fulfillment'][_mk] = (
                rm_calc.get('dtc_fulfillment_amt', {}).get(_mk, 0)
            )
    except Exception as exc:
        log.warning('Could not load revenue model for cash flow: %s', exc)
        ctx['dtc_monthly_revenue'] = {}
        ctx['amazon_monthly_revenue'] = {}
        ctx['monthly_media_spend'] = {}
        ctx['_rm_net_sales'] = {}
        ctx['_rm_wholesale_monthly'] = {}
        ctx['_rm_monthly_fulfillment'] = {}

    # Planned inbound POs -> production cost outflows.
    try:
        from db import get_planned_inbound_dict
        po_cost_per_unit = float(get_cashflow_setting(conn, 'production_cost_per_unit', '40.00'))
        planned = get_planned_inbound_dict(conn)
        po_monthly_cost = {}
        for sku, months_dict in planned.items():
            for month_key, units in months_dict.items():
                if month_key >= '2026-02':
                    po_monthly_cost[month_key] = po_monthly_cost.get(month_key, 0) + units * po_cost_per_unit
        ctx['_po_monthly_production'] = po_monthly_cost
        if po_monthly_cost:
            log.info('Planned POs loaded: %d months, total $%,.0f',
                     len(po_monthly_cost), sum(po_monthly_cost.values()))
    except Exception as e:
        log.warning('Could not load planned inbound for cash flow: %s', e)
        ctx['_po_monthly_production'] = {}

    # --- Pre-compute Amazon disbursement schedule (once, not per-week) ---
    ctx['_amazon_disbursements'] = _build_amazon_disbursement_schedule(week_list)

    # --- Opening balance from setting ---
    opening_balance = float(get_cashflow_setting(conn, 'opening_cash_balance', '153000'))

    # --- LOC starting balance ---
    ctx['_running_loc_balance'] = loc_current

    # --- Revenue, expense, and COGS/debt categories ---
    revenue_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'revenue']
    expense_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'expense']
    cogs_debt_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'cogs_debt']

    # --- Fetch overrides ---
    overrides = {}
    try:
        override_rows = read_sql(
            "SELECT line_item, week_start, override_amount FROM cashflow_overrides",
            conn,
        )
        for _, row in override_rows.iterrows():
            key = (row['line_item'], row['week_start'])
            overrides[key] = float(row['override_amount'])
    except Exception as e:
        log.warning('Failed to load cashflow overrides: %s', e)

    # --- Build weekly rows ---
    rows = []
    balance = opening_balance

    for i, (ws, we) in enumerate(week_list):
        ws_str = str(ws)

        row = {
            'week_start': ws_str,
            'week_end': str(we),
            'week_num': i + 1,
            'is_actual': False,
            'opening_balance': balance,
        }

        # Revenue
        total_inflows = 0
        for cat in revenue_cats:
            override_key = (cat, ws_str)
            if override_key in overrides:
                val = overrides[override_key]
            else:
                val = _project_revenue_week(conn, cat, ws, we, ctx)
                val = _apply_scenario(val, 'revenue', scenario)

            row[cat] = round(val, 2)
            total_inflows += val

        row['total_inflows'] = round(total_inflows, 2)

        # Expenses (operating)
        total_expenses = 0
        for cat in expense_cats:
            override_key = (cat, ws_str)
            if override_key in overrides:
                val = overrides[override_key]
            else:
                val = _project_expense_week(conn, cat, ws, we, ctx)
                val = _apply_scenario(val, 'expense', scenario)

            row[cat] = round(val, 2)
            total_expenses += val

        row['total_expenses'] = round(total_expenses, 2)

        # COGS & Debt
        total_cogs_debt = 0
        loan_principal_this_week = 0
        for cat in cogs_debt_cats:
            override_key = (cat, ws_str)
            if override_key in overrides:
                val = overrides[override_key]
            else:
                val = _project_expense_week(conn, cat, ws, we, ctx)
                val = _apply_scenario(val, 'expense', scenario)

            row[cat] = round(val, 2)
            total_cogs_debt += val

            # Track loan principal for deferred LOC balance update
            if cat == 'loan' and val > 0:
                loan_principal_this_week = val

        # Decrement LOC balance AFTER both interest and principal are computed
        if loan_principal_this_week > 0:
            ctx['_running_loc_balance'] = max(
                ctx.get('_running_loc_balance', 0) - loan_principal_this_week, 0
            )

        row['total_cogs_debt'] = round(total_cogs_debt, 2)
        row['loc_balance'] = round(ctx.get('_running_loc_balance', 0), 2)

        total_outflows = total_expenses + total_cogs_debt
        row['total_outflows'] = round(total_outflows, 2)

        # Net and balance
        net = total_inflows - total_outflows
        row['net_cashflow'] = round(net, 2)
        row['closing_balance'] = round(balance + net, 2)

        # Confidence interval
        weeks_out = max(i, 1)
        lower, upper = _compute_confidence(weeks_out, net)
        row['confidence_lower'] = round(balance + lower, 2)
        row['confidence_upper'] = round(balance + upper, 2)

        balance = row['closing_balance']
        rows.append(row)

    df = pd.DataFrame(rows)
    log.info(
        'Cash flow forecast built: %d weeks, scenario=%s, opening=$%,.0f, closing=$%,.0f',
        weeks, scenario, opening_balance, balance,
    )
    return df


def get_cashflow_kpis(conn: ConnectionWrapper, forecast_df: pd.DataFrame) -> dict:
    """Extract key KPIs from a cash flow forecast DataFrame.

    Returns dict with: current_cash, projected_13w, projected_52w,
    monthly_burn, runway_weeks, min_cash_threshold, alert_week.
    """
    from db import get_cashflow_setting

    min_threshold = float(get_cashflow_setting(conn, 'min_cash_threshold', '100000'))

    # Current Cash = opening balance (first week's opening)
    current_cash = forecast_df.iloc[0]['opening_balance'] if not forecast_df.empty else 0

    # 13-week projected
    if len(forecast_df) > 13:
        projected_13w = forecast_df.iloc[13]['closing_balance']
    else:
        projected_13w = forecast_df.iloc[-1]['closing_balance'] if not forecast_df.empty else 0

    # End-of-horizon projected
    projected_52w = forecast_df.iloc[-1]['closing_balance'] if not forecast_df.empty else 0

    # Monthly burn
    if len(forecast_df) >= 2:
        monthly_burn = -forecast_df['net_cashflow'].mean() * 4.33
    else:
        monthly_burn = 0

    # Runway
    runway_weeks = 52
    for i in range(len(forecast_df)):
        if forecast_df.iloc[i]['closing_balance'] <= 0:
            runway_weeks = i
            break
    else:
        runway_weeks = min(len(forecast_df), 52)

    # Alert: first week below threshold
    alert_week = None
    for _, r in forecast_df.iterrows():
        if r['closing_balance'] < min_threshold:
            alert_week = r['week_num']
            break

    return {
        'current_cash': current_cash,
        'projected_13w': projected_13w,
        'projected_52w': projected_52w,
        'monthly_burn': monthly_burn,
        'runway_weeks': runway_weeks,
        'min_cash_threshold': min_threshold,
        'alert_week': alert_week,
        'balance_freshness_date': str(date.today()),
    }
