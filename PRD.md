# Cash Flow Forecasting Engine — PRD

## Vision

Replace Hydrant's manual Google Sheets 13-week cash flow model with an automated 52-week forecasting engine inside the Streamlit Command Center. The CFO should be able to open one page, see exactly where cash stands today, where it's headed, and what levers to pull — backed by actual bank data, not manual spreadsheet entries.

## User

Josh Houghton, CEO/CFO of Hydrant. Makes weekly cash decisions: when to run production, how much to spend on media, whether to draw on the line of credit. Needs a model he can trust without manually updating a spreadsheet every Monday.

## Success Criteria

The model is "done" when:
1. **Current Cash KPI** matches the sum of actual bank account balances (within $500 of manual check)
2. **13-week forecast** is within 15% of the Google Sheet model's 13-week projection when fed the same inputs
3. **Amazon inflows** show exactly 2 disbursement events per month, landing in the correct weeks
4. **Expense timing** matches reality — payroll hits biweekly, media monthly, taxes quarterly — NOT spread evenly
5. **Every cell** in the weekly table is editable, persists to DB, and feeds back into projections
6. **10 sequential analyst agents** each validate and pass their specific mandate
7. **No error banners** on the deployed Railway page

## Scope Boundaries

**In scope:** 52-week weekly cash projection, bank CSV import, auto-classification, editable overrides, scenario toggling, alert banners, payout ratio auto-calibration.

**Out of scope (not building):** Monte Carlo simulation, daily granularity, multi-entity consolidation, automatic bank API connection (Plaid), accounts receivable/payable tracking, P&L statement generation.

## Reference Data (Ground Truth from Google Sheet)

These numbers come from the original Cash Flow Model and Daily Revenue Plan Google Sheets:

### Revenue
- **DTC (Shopify):** ~$5-10K/day gross, ~$42-52K/month. Payout ratio ~94% of Total Sales (6% processing fees). Settles daily, Tuesday-heavy (Mon 15.7%, Tue 30.9%, Wed 25.2%, Thu 14.4%, Fri 13.7%, Sat/Sun 0%).
- **Amazon:** ~$4.4-12.4K/day gross, ~$54-102K/month actual (DB shows $54K-$102K/month). Payout ratio ~60-65% (35-40% Amazon fees/FBA/returns). Disburses biweekly around days 7-10 and 23-25 of each month, ~$45K average per disbursement.
- **Other channels:** TikTok, Faire, wholesale — small, use trailing averages from bank actuals.
- **Total gross revenue:** $300K+/month combined.

### Expenses (from Cash Flow Model tab)
| Category | Monthly Amount | Timing |
|----------|---------------|--------|
| Media (Meta/Google/TikTok/AppLovin) | $40-55K | Bills monthly, near end of month |
| Payroll (Justworks) | $32K ($16K biweekly) | Hits ~10th and ~25th of month |
| Loan — Jameson principal | $25-75K (declining) | Monthly |
| Loan — Jameson interest | $400-6,500 (declining) | Monthly |
| Fulfillment (Avenue Shops/3PL) | $20K (~$5K/week) | Weekly |
| Production (StikPak/manufacturers) | $0-108K (spiky) | PO-driven, irregular |
| Software/SaaS | $42K (~$10.5K/week) | Mix of monthly and annual |
| Agency fees | $10.5K | Monthly |
| Accounting/CPA | $5K | Monthly |
| Insurance | $4K | Monthly |
| Sales tax | $12-23K/quarter | Quarterly (Jan, Apr, Jul, Oct) |
| Shipping | Varies | Weekly |

### Balance Sheet
- **Starting cash (Google Sheet model):** $153K
- **Actual bank balances (from imported CSV, as of latest data):**
  - Highbeam Checking: $38,667
  - Highbeam Savings: $15,473
  - BofA Checking: $62,867
  - Amex: -$30,258 (credit card liability, excluded from cash)
  - **Total available cash: ~$117K**
- **Line of credit:** $510K remaining (declining)
- **Min cash threshold:** $100K

### Key Ratios
- DTC payout ratio: 0.94 (seed, auto-calibrates)
- Amazon payout ratio: 0.62 (seed, auto-calibrates)
- COGS as % of gross revenue: 25% (seed, adjustable)

## Current State

The model works end-to-end. Bank data (2,069 transactions, 8 months) is loaded. The page renders on Railway with KPIs, chart, editable table, and upload. Six analyst agents audited it and found critical accuracy bugs. Those bugs are Phase 1.

## Architecture

```
analytics/cashflow.py  — projection engine (993 lines)
  normalize_summary()           — strip IDs/amounts from bank tx summaries
  classify_transaction()        — DB lookup → regex fallback → 'unmapped'
  compute_payout_ratio()        — EWMA auto-calibration from actuals
  compute_trailing_avg()        — trailing weekly average for any category
  _detect_expense_schedule()    — inter-payment gap analysis for timing
  _project_revenue_week()       — DTC (waterfall + DOW weights), Amazon (biweekly), other (trailing)
  _project_expense_week()       — schedule detection → method fallback → seed defaults
  build_cashflow_forecast()     — main orchestrator, returns 52-week DataFrame
  get_cashflow_kpis()           — extract KPIs from forecast DataFrame

views/cashflow.py              — dashboard page (648 lines)
  _build_balance_chart()        — Plotly area chart with confidence bands
  _render_editable_table()      — st.data_editor with per-cell persistence
  _save_edits()                 — upsert to cashflow_overrides table
  render()                      — main page: KPIs, chart, table, settings, upload

etl/cashflow_csv.py            — Highbeam CSV import with dedup
views/tx_mapping.py            — transaction → category mapping screen
utils/constants.py             — CASHFLOW_CATEGORIES, DTC_DOW_WEIGHTS, seed defaults
db.py                          — schema: cashflow_transactions, cashflow_overrides, cashflow_settings, category_mappings
```

## Rollback Plan

If the model produces worse results after changes, `git revert` back to commit `360543d` (last known working state). The bank transaction data in the DB is unaffected by code changes.

---

## Phase 1: Fix Known Bugs (from Analyst Round 1)

Acceptance criteria to exit Phase 1: All 9 tasks checked, code committed and pushed, `pytest tests/ -x` passes.

### Tasks

- [x] **1. Fix Amazon disbursement overcounting (~29% overstatement)**
  - File: `analytics/cashflow.py`, function `_is_amazon_disbursement_week()` (lines 392-417)
  - Bug: The `seen` set is local to each per-week call. When the forecast loop calls it for Week A (Mar 2-8) and Week B (Mar 9-15), and the early disbursement window (days 7-10) spans both weeks, both calls independently detect a disbursement event. This counts the same disbursement twice. Result: ~2.6 events/month instead of 2, overstating Amazon inflows by ~$22K/month.
  - Fix: Add a new function `_build_amazon_disbursement_schedule(week_list)` that pre-computes ALL disbursement events across all weeks ONCE. Assign each disbursement to exactly one week — the week containing the midpoint of the window (day 8 for early, day 24 for late). Store in `ctx['_amazon_disbursements']` keyed by week_start string. In `_project_revenue_week()` for amazon_revenue, look up from ctx instead of calling per-week. Remove `_is_amazon_disbursement_week()`.
  - Verify: Print the disbursement schedule for 3 months. Each month must have exactly 2 events. No week has >1 event unless a month boundary puts late+early in the same 7 days.

- [x] **2. Fix "Current Cash" KPI showing stale 4-week-old balance**
  - File: `analytics/cashflow.py`, function `get_cashflow_kpis()` (lines 942-993)
  - Bug: `current_cash = forecast_df.iloc[0]['opening_balance']` reads the opening balance of row 0, which is 4 weeks in the past (because `start_date=date.today() - timedelta(weeks=4)`). The "Current Cash" label shows a month-old number.
  - Fix: Find the row where `week_start <= str(today) <= week_end` and use its `opening_balance`. Fallback: last actual week's `closing_balance`. Also fix `projected_13w` to use `current_week_index + 13` and `projected_52w` to use the last row — both relative to current week, not row 0.
  - Verify: "Current Cash" should show ~$117K (or whatever the current-week opening balance is after actuals flow through), not ~$38K.

- [x] **3. Fix missing DTC seasonality in waterfall call**
  - File: `analytics/cashflow.py`, function `build_cashflow_forecast()` (line 747)
  - Bug: `build_waterfall(media_plan, source_filter='shopify', horizon_months=12)` is called without `seasonal_indices`. The function accepts it (confirmed: `def build_waterfall(media_plan, source_filter=None, horizon_months=12, seasonal_indices=None)`), but we're not passing it.
  - Fix: Load seasonal indices from DB and pass them:
    ```python
    seasonal_df = read_sql('SELECT month_num, index_value FROM seasonal_indices', conn)
    seasonal_dict = dict(zip(seasonal_df['month_num'], seasonal_df['index_value'])) if not seasonal_df.empty else None
    wf = build_waterfall(media_plan, source_filter='shopify', horizon_months=12, seasonal_indices=seasonal_dict)
    ```
  - Verify: March revenue (seasonal index 0.98) should be ~12% lower than June revenue (index 1.10). If seasonal_indices table is empty, the model should still work (graceful None handling).

- [x] **4. Fix normalize_summary dead regex pattern**
  - File: `analytics/cashflow.py`, line 34
  - Bug: Pattern `r'\b[A-Z0-9]{8,}\b'` uses uppercase character class but input is lowercased on line 60 before patterns run. Dead code — never matches.
  - Fix: Change line 34 to `r'\b[a-z0-9]{8,}\b'`.
  - Verify: `normalize_summary("SHOPIFY DES:FUNDING ID:ABC12345678 INDN:Josh")` should strip `abc12345678` after lowercasing. Before this fix it wouldn't.

- [x] **5. Fix reclassify_all_transactions O(M*N) performance**
  - File: `analytics/cashflow.py`, function `reclassify_all_transactions()` (lines 200-232)
  - Bug: For each of M mappings (~300), loads ALL N transactions (~2000) and iterates them. That's 600K normalize calls.
  - Fix: Load all transactions once, build `{normalized_pattern: [tx_ids]}` lookup, then for each mapping do one batch UPDATE:
    ```python
    txs = read_sql("SELECT tx_id, summary FROM cashflow_transactions", conn)
    pattern_to_txids = {}
    for _, tx in txs.iterrows():
        norm = normalize_summary(tx['summary'] or '')
        pattern_to_txids.setdefault(norm, []).append(tx['tx_id'])
    updated = 0
    for mapping in mappings:
        tx_ids = pattern_to_txids.get(mapping['match_pattern'], [])
        if tx_ids:
            placeholders = ','.join(['%s'] * len(tx_ids))
            conn.execute(
                f"UPDATE cashflow_transactions SET category=%s, subcategory=%s, "
                f"is_transfer=%s, is_duplicate=%s WHERE tx_id IN ({placeholders})",
                (mapping['category'], mapping['subcategory'],
                 mapping['is_transfer'], mapping['is_duplicate'], *tx_ids))
            updated += len(tx_ids)
    ```
  - Verify: Function should complete in <2 seconds. Updated count should match the old implementation.

- [x] **6. Fix current-week DTC proration to use DOW weights**
  - File: `analytics/cashflow.py`, build_cashflow_forecast() revenue blending (lines 868-879)
  - Bug: Proration uses `(days_total - days_elapsed) / days_total` — linear. But DTC is Tuesday-heavy. On Wednesday (3 days elapsed), remaining = 4/7 = 57%, but actual remaining DOW weight = Thu 14.4% + Fri 13.7% + Sat 0% + Sun 0% = 28.1%.
  - Fix: For `dtc_revenue` only, replace linear fraction with DOW-weighted:
    ```python
    if cat == 'dtc_revenue':
        remaining_weight = 0.0
        for d_off in range(7):
            d = ws + timedelta(days=d_off)
            if d > today and d <= we:
                remaining_weight += DTC_DOW_WEIGHTS.get(d.weekday(), 0)
        total_weight = sum(DTC_DOW_WEIGHTS.values()) or 1.0
        val = actual + projected_full * (remaining_weight / total_weight)
    else:
        val = actual + projected_full * (days_total - days_elapsed) / days_total
    ```
  - Verify: On a Wednesday, DTC remainder fraction should be ~0.28, not 0.57.

- [x] **7. Fix monthly burn KPI to only use actual data**
  - File: `analytics/cashflow.py`, function `get_cashflow_kpis()` (lines 963-968)
  - Bug: Fallback `forecast_df.head(4)` can include projected weeks if fewer than 4 actual weeks exist.
  - Fix: Use however many actuals exist (minimum 2), extrapolate to monthly. Only fall back to projected if zero actuals:
    ```python
    recent = forecast_df[forecast_df['is_actual'] == True]
    if len(recent) >= 2:
        monthly_burn = -recent.tail(min(4, len(recent)))['net_cashflow'].mean() * 4.33
    else:
        monthly_burn = -forecast_df.head(4)['net_cashflow'].mean() * 4.33
    ```
  - Verify: With 4 weeks of actuals loaded, burn KPI should only use actual transaction data.

- [x] **8. Add data freshness check to opening balance**
  - File: `analytics/cashflow.py`, in `build_cashflow_forecast()` (lines 798-818)
  - Fix: After computing opening balance, check if the latest `tx_date` in the DB is >14 days old. If so, log a warning. Add `'balance_freshness_date'` to the KPIs dict. In `views/cashflow.py`, show a stale-data warning badge next to "Current Cash" if freshness > 14 days.
  - Verify: KPIs dict should contain `balance_freshness_date` key. View should render badge when data is stale.

- [x] **9. Commit and push all Phase 1 fixes**
  - Run `pytest tests/ -x` to verify no regressions.
  - Commit: `fix(cashflow): phase 1 — Amazon overcounting, KPI accuracy, seasonality, perf`
  - Push to main.

---

## Phase 2: Validate Against Google Sheet (10 Sequential Analysts)

Acceptance criteria to exit Phase 2: All 10 analysts complete. Findings written to `ANALYST_ROUND2.md`. Phase 3 tasks generated from any FAILs.

Each analyst runs SEQUENTIALLY so it can build on the previous analyst's findings. Each analyst must read `analytics/cashflow.py`, `views/cashflow.py`, and any prior analyst output before starting.

### Tasks

- [x] **10. Research — build VALIDATION_BASELINE.md**
  - Query the Railway database (use `from db import get_db, read_sql`) to extract:
    - Revenue by channel by month (last 6 months) from `daily_sku_sales`
    - Expense totals by category by month from `cashflow_transactions`
    - Amazon revenue forecast table values
    - Media spend table values
    - Current bank account balances
    - Count of mapped vs unmapped transactions
  - Cross-reference against the Google Sheet reference data in this PRD (see "Reference Data" section above).
  - Write `VALIDATION_BASELINE.md` with all findings. This file is the source of truth for all analysts.

- [x] **11. Analyst 1 — Revenue Projection Accuracy**
  - Read `VALIDATION_BASELINE.md` and `analytics/cashflow.py`.
  - Validate: Does the DTC monthly revenue from `build_waterfall()` roughly match actual Shopify revenue in `daily_sku_sales`? Is it in the $42-52K/month range? Are seasonal indices being applied?
  - Validate: Does the Amazon monthly revenue from `amazon_revenue_forecast` table match reality? The DB shows $54-102K/month actual Amazon revenue — if the forecast table says $150-200K, that's a critical discrepancy.
  - Calculate the projected weekly DTC and Amazon cash inflows for the next 4 weeks. Do they pass the smell test against the reference data?
  - Write findings (PASS/FAIL with specifics) and append to `ANALYST_ROUND2.md`.

- [x] **12. Analyst 2 — Amazon Disbursement Timing**
  - Read prior analyst findings in `ANALYST_ROUND2.md`.
  - Run `build_cashflow_forecast()` and extract all `amazon_revenue` column values for 13 weeks.
  - Count disbursement events per month. Must be exactly 2 per month.
  - Cross-reference against actual Amazon deposits in `cashflow_transactions` (category='amazon_revenue') — when did real disbursements land?
  - Verify the fix from Task 1 eliminated the overcounting bug.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **13. Analyst 3 — Expense Completeness & Timing**
  - Read prior analyst findings.
  - For each of the 12 expense categories: compare projected 13-week total against actual 13-week total from `cashflow_transactions`.
  - Flag any category where projected vs actual differs by >25%.
  - Verify expense timing: payroll should show in 2 weeks per month (not all 4), media in ~1 week per month, taxes in 1 week per quarter. Check that gap weeks show $0 for scheduled expenses.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **14. Analyst 4 — Balance Arithmetic & KPI Accuracy**
  - Read prior analyst findings.
  - Verify for every row: `closing_balance == opening_balance + net_cashflow` (within $1 tolerance).
  - Verify: `opening_balance[row N+1] == closing_balance[row N]`.
  - Verify "Current Cash" KPI shows the current week's opening balance, not row 0's.
  - Verify "13-Week Projected" is indexed from current week, not from 4 weeks ago.
  - Verify "Monthly Burn" only uses actual (is_actual=True) rows.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **15. Analyst 5 — Payout Ratio Calibration**
  - Read prior analyst findings.
  - Compute actual DTC payout ratio: sum of `cashflow_transactions` (category='dtc_revenue', direction='credit') / sum of `daily_sku_sales` (source='shopify') for the same period (with 3-day lag offset).
  - Compute actual Amazon payout ratio: same approach with 21-day lag.
  - Compare against the model's auto-calibrated ratios from `compute_payout_ratio()`.
  - If ratios differ by >5% from the reference values (DTC ~0.94, Amazon ~0.62), flag it.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **16. Analyst 6 — Seasonality Impact**
  - Read prior analyst findings.
  - Check if `seasonal_indices` table has data. If empty, flag as FAIL (Task 3 won't work).
  - If populated: verify March revenue forecast uses index ~0.98 and June uses ~1.10. The ratio between June and March projected DTC revenue should be approximately 1.12x.
  - If seasonality appears flat across all months, the fix from Task 3 didn't work.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **17. Analyst 7 — Google Sheet Comparison**
  - Read all prior analyst findings and `VALIDATION_BASELINE.md`.
  - Compare our model's 13-week output against the Google Sheet reference data:
    - Total monthly revenue: should be $250-350K/month (DTC $42-52K + Amazon $54-102K + other)
    - Total monthly expenses: should be roughly $150-250K/month (media + payroll + loan + fulfillment + software + rest)
    - Net monthly cash flow: should be positive $50-150K/month based on the Google Sheet
    - 13-week closing balance: should be higher than opening if net positive
  - Flag any metric that's >30% off from the Google Sheet model's range.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **18. Analyst 8 — Override & Edit Persistence**
  - Read prior findings.
  - Test the override flow: insert a manual override into `cashflow_overrides` table for a future week, re-run `build_cashflow_forecast()`, verify the override value appears in the output DataFrame instead of the projected value.
  - Test that past-week overrides are NOT honored (actuals should always win for past weeks). Actually check the code logic for this.
  - Test the "Reset to Smart Projection" flow: deleting from `cashflow_overrides` should restore the projected value.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **19. Analyst 9 — Edge Cases & Robustness**
  - Read prior findings.
  - Test: week spanning two months (does revenue get attributed to the right month for each day?).
  - Test: what happens if `amazon_revenue_forecast` table is empty? Should fall back to trailing average, not crash.
  - Test: COGS calculation when Amazon revenue is $0 for a week (division by zero guard on payout ratio gross-up).
  - Test: `normalize_summary()` with edge inputs: empty string, None, string of only numbers, string with only special characters.
  - Test: confidence intervals — do they widen over time? Is week 1 tighter than week 13?
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **20. Analyst 10 — Code Quality & Security**
  - Read prior findings.
  - Check all SQL queries in `analytics/cashflow.py` use parameterized queries (%s placeholders), not f-strings.
  - Check for division-by-zero guards on all ratio calculations.
  - Check that no exceptions are silently swallowed (bare `except: pass`).
  - Check logging coverage: are errors logged before being caught?
  - Check for any remaining dead code, TODO comments, or commented-out blocks.
  - Verify `_STRIP_PATTERNS` regex (Task 4 fix) now uses lowercase character class.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **20b. Analyst 11 — Business Sanity Check (Does This Look Like a Real Business?)**
  - Read ALL prior analyst findings in `ANALYST_ROUND2.md`.
  - Run `build_cashflow_forecast()` for 52 weeks and examine the output holistically. This is NOT a code review — this is a CFO looking at the numbers and asking "does this make sense?"
  - Check these specific sanity gates:
    - **52-week ending cash**: Hydrant does ~$300K/month gross, ~$150-250K/month expenses. Net positive ~$50-100K/month. After 52 weeks starting from ~$117K, ending cash should be roughly $700K-$1.3M. If it shows >$2M or <$0, something is fundamentally wrong. FAIL if outside $400K-$2M range.
    - **Weekly revenue range**: DTC should be ~$10-15K/week in cash (not gross). Amazon should be $0 most weeks and $40-60K in disbursement weeks. If any week shows >$100K DTC or >$150K Amazon, something is wrong.
    - **Weekly expense range**: Total outflows should be ~$35-60K/week average. If any single week shows >$150K expenses (excluding spiky production), flag it.
    - **Expense-revenue correlations**: Fulfillment costs should rise as revenue rises (more orders = more 3PL fees). COGS should scale with revenue (it's a % of gross). Shipping should correlate with order volume. If revenue doubles month-over-month but fulfillment stays flat, the model is wrong. If COGS stays constant while revenue swings, the revenue_pct method isn't working.
    - **No negative revenue weeks**: Revenue should never be negative. If it is, refunds or sign errors are leaking in.
    - **Cash never goes massively negative**: If closing balance drops below -$100K at any point, the model is broken (they have a $510K LOC as backstop, but the model shouldn't need it for normal operations).
    - **Month-over-month consistency**: Compare total inflows for month 1 vs month 6 vs month 12. They should be roughly similar (within 30%) unless seasonality is very strong. A 3x jump between months means something is compounding incorrectly.
  - For any FAIL: identify which line item is causing the unrealistic number and trace it back to the projection function.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **20c. Analyst 12 — Cross-Dashboard Consistency Check**
  - Read ALL prior findings.
  - The cash flow model should be consistent with other parts of the dashboard. Check:
    - **Media spend**: Read the `media_spend` table. The cash flow model's media expense projection should roughly match the media spend values entered in the Settings/Business Variables page. If the Settings page says $50K/month Meta and the cash flow shows $20K/month media, they're disconnected.
    - **Amazon revenue forecast**: Read `amazon_revenue_forecast` table. Compare those values against what the cash flow model actually uses for Amazon projections. They MUST match — if the forecast table says $80K/month but the model projects $150K, the forecast table needs updating or the model is reading wrong data.
    - **Waterfall DTC revenue**: The DTC revenue in the cash flow should roughly match what the waterfall model on the Forecast page produces. Read `analytics/waterfall.py` and check if `build_waterfall()` returns similar monthly totals when called from the cash flow engine vs the forecast page.
    - **Planned inbound / production**: Read the `planned_inbound` table. If there are planned POs, the cash flow should ideally reflect those as production expense spikes in the relevant weeks (this may not be implemented yet — if not, flag it as a gap, not a bug).
    - **Seasonal indices**: Read `seasonal_indices` table. Verify the values match `DEFAULT_SEASONAL_INDICES` in `utils/constants.py`. If the table is empty, the waterfall is using defaults — note this.
  - For any mismatch >20%: trace the data flow and identify where the disconnection happens.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **20d. Analyst 13 — Stress Test Extreme Scenarios**
  - Read ALL prior findings.
  - Run the forecast in Conservative scenario. Verify:
    - Revenue drops 15%, expenses rise 10% vs Base
    - Closing balance at week 52 is meaningfully lower than Base
    - If Conservative shows cash going negative, the alert banner should trigger
  - Run the forecast in Aggressive scenario. Verify:
    - Revenue rises 10%, expenses drop 5% vs Base
    - Closing balance at week 52 is higher than Base
  - Test what happens if ALL revenue is zero (empty `dtc_monthly_revenue` and `amazon_monthly_revenue` in ctx). The model should still run and show only expenses draining cash — not crash.
  - Test what happens if ALL expense categories have zero trailing average and zero seed defaults. The model should show only revenue building cash.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **20e. Analyst 14 — Week-by-Week Narrative Walkthrough**
  - Read ALL prior findings.
  - Run `build_cashflow_forecast()` and print weeks 1-8 in detail (every category value).
  - Walk through the first 8 weeks as if you're the CFO presenting to a board. For each week, narrate: "We start with $X. DTC brings in $Y (is that reasonable for ~$7K/day * 7 * 0.94?). Amazon brings in $Z this week (is it a disbursement week? does the amount match ~$45K?). Expenses: payroll hits this week ($16K — is it the right biweekly cadence?). Media $0 this week (correct, it bills monthly). Net is $W, closing at $V."
  - This is a human-readability check. If the narrative doesn't make sense for any week, the model is wrong regardless of what the code says.
  - Flag any week where the narrative breaks down (e.g., "payroll hits 3 weeks in a row" or "Amazon disbursement is $120K which is the full month not half").
  - Append the full narrative to `ANALYST_ROUND2.md`.

- [x] **20f. Analyst 15 — Actuals vs Projection Backtest**
  - Read ALL prior findings.
  - This is the most important analyst. Run `build_cashflow_forecast()` starting 8 weeks ago. The first 4 weeks should be marked `is_actual=True` and use real bank data. Weeks 5-8 should be projections.
  - Now compare weeks 5-8 projections against what ACTUALLY happened in the bank (query `cashflow_transactions` for those weeks).
  - For each of weeks 5-8, calculate:
    - Projected total inflows vs actual total credits
    - Projected total outflows vs actual total debits
    - Projected closing balance vs actual (sum of latest account balances adjusted for that week)
  - Calculate the overall forecast error: `abs(projected - actual) / actual * 100` for each metric.
  - **PASS if average error <20%.** This is the gold standard test — if the model can't predict 4-8 weeks out within 20%, it's not useful for cash management.
  - **FAIL if average error >30%.** Identify which categories contribute the most error.
  - Append detailed comparison table to `ANALYST_ROUND2.md`.

- [x] **20g. Analyst 16 — LOC Trigger & Payroll Coverage**
  - Read ALL prior findings.
  - Run `build_cashflow_forecast()` for 52 weeks in Base scenario.
  - **LOC draw timing**: Find the first week where `closing_balance < 50000`. This is when the CFO would need to draw on the $510K line of credit. Report the week number and projected balance. If no week dips below $50K, report "No LOC draw needed — PASS."
  - **Payroll stress test**: For every week that has a payroll payment (should be biweekly), check if `opening_balance - payroll_amount > 0`. If cash goes negative AFTER payroll in any week (before other inflows), that's a CRITICAL flag — payroll cannot bounce.
  - **Minimum cash week**: Identify the single week with the lowest closing balance across all 52 weeks. Report it. If it's negative, FAIL. If it's below $50K, WARN.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **20h. Analyst 17 — Amazon Concentration Risk**
  - Read ALL prior findings.
  - Run `build_cashflow_forecast()` for 52 weeks.
  - **Amazon dependency %**: Calculate what % of total weekly inflows come from Amazon vs DTC vs other. If Amazon is >60% of total cash inflows, flag as HIGH concentration risk.
  - **Amazon freeze simulation**: Manually zero out all `amazon_revenue` values for 4 consecutive future weeks in the forecast DataFrame (don't change the model, just the output). Recalculate closing balances. How many weeks until cash hits zero? If <6 weeks, CRITICAL. If <10 weeks, HIGH.
  - **Disbursement gap stress**: What's the longest gap between Amazon disbursements in the 52-week forecast? If any gap is >21 days, flag it — that's longer than normal and could indicate a model timing bug.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **20i. Analyst 18 — Ratio Drift Detection**
  - Read ALL prior findings.
  - The model auto-calibrates payout ratios from actuals using EWMA. Check:
  - **DTC payout ratio**: Call `compute_payout_ratio(conn, 'dtc')`. Compare result against the seed value (0.94). If the auto-calibrated value is >5% different from seed (i.e., below 0.89 or above 0.99), flag it with the direction of drift and what it means: dropping ratio = rising processing fees or more refunds; rising ratio = improving margins.
  - **Amazon payout ratio**: Call `compute_payout_ratio(conn, 'amazon')`. Compare against seed (0.62). Same 5% threshold. Amazon ratio dropping could mean higher FBA fees, more returns, or advertising cost changes.
  - **COGS ratio check**: The model uses `cogs_pct` (default 0.25). Query actual production spend from `cashflow_transactions` (category='production') and compare against 25% of gross revenue. If actual COGS is >30% or <15%, the default is wrong and should be flagged.
  - **If any ratio has drifted >5%**: recommend updating the seed value in `cashflow_settings` and explain why, so the CFO can investigate the underlying cause.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **20j. Analyst 19 — CEO Monday Morning Decision Test**
  - Read ALL prior findings.
  - You are the CEO. It's Monday morning. You open the Cash Flow page to answer these 5 questions. For each one, check if the model gives you a clear, trustworthy answer:
  - **"Can I approve this $80K production run?"** — Look at the closing balance 4 weeks out (when the PO would hit). Is there enough cash to cover it without going below $100K? Does the model make this obvious at a glance, or do you have to do mental math?
  - **"Should I increase media spend by $10K/month?"** — Can you toggle to Aggressive scenario and see the impact? Does increasing spend show up as higher revenue in later weeks (via waterfall), or does the model treat media and revenue as disconnected?
  - **"Are we going to make payroll on the 10th?"** — Look at the week containing the 10th. Is there a payroll line item showing ~$16K? Is the opening balance that week clearly above $16K?
  - **"How much cash do we actually have right now?"** — Is the "Current Cash" KPI obviously the real number? Is it stale? Does it match what you'd see if you logged into Highbeam + BofA right now?
  - **"When is our next tight cash week?"** — Scan the closing balance column. Is there an obvious low point? Does the alert banner flag it? Can you see it on the chart?
  - For each question: PASS if the answer is clear within 5 seconds of looking. FAIL if you'd need to open a spreadsheet to double-check.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **20k. Analyst 20 — "Does Last Month's Forecast Match What Actually Happened?"**
  - Read ALL prior findings. This is the ultimate trust test.
  - Query `cashflow_transactions` for the most recent COMPLETE month (all weeks fully in the past).
  - Sum actual credits (inflows) and actual debits (outflows) for that month from bank data.
  - Now run `build_cashflow_forecast()` starting 8 weeks before that month. Look at the projected values for that same month's weeks.
  - Compare:
    - **Projected monthly inflows vs actual monthly inflows**: Error % = ?
    - **Projected monthly outflows vs actual monthly outflows**: Error % = ?
    - **Projected month-end cash vs actual month-end cash**: Error $ = ?
  - If monthly inflow error >25% → FAIL (revenue model is off)
  - If monthly outflow error >25% → FAIL (expense model is off)
  - If month-end cash error >$30K → FAIL (cumulative drift too high)
  - This is the number the CEO will use to calibrate trust. If the model said February would end at $130K and it actually ended at $95K, that's a $35K miss — not trustworthy for $80K PO decisions.
  - Append findings to `ANALYST_ROUND2.md`.

- [x] **21. Generate Phase 3 fix tasks**
  - Read `ANALYST_ROUND2.md` from top to bottom.
  - For each FAIL or issue with severity HIGH or CRITICAL, append a new task to this PRD at the bottom of Phase 3 with: file, bug description, fix, verify.
  - If ALL analysts PASSed, write "ALL PASS — skipping Phase 3" and skip to task 24.
  - Update `progress.txt`.

---

## Phase 3: Fix Round 2 Issues

Acceptance criteria: All dynamically-generated tasks complete. Code committed and pushed.

### Generated Fix Tasks (from Task 21 — Analyst Round 2 Findings)

- [ ] **22a. CRITICAL: Fix opening balance double-counting (+$110K / 94% overstatement)**
  - File: `analytics/cashflow.py`, `build_cashflow_forecast()` (lines 832-852)
  - Bug: The model uses the latest bank balance ($117K, as of Mar 1-3) as the opening balance for row 0 (start_date = 4 weeks ago, ~Feb 2). It then replays 4 weeks of actual transactions that are *already reflected* in that $117K balance. This inflates current cash to $227K — the CFO sees nearly double the actual bank balance. Every downstream balance, KPI, and alert is wrong.
  - Fix: Reconstruct the historical opening balance by subtracting actual transactions between start_date and the latest transaction date. Specifically:
    ```python
    # After computing opening_balance from latest bank balances:
    # Subtract actual net transactions from start_date to latest_tx_date
    # so the opening balance represents what it was at start_date, not today.
    net_since_start = read_sql("""
        SELECT COALESCE(SUM(CASE WHEN direction='credit' THEN amount ELSE -amount END), 0) as net
        FROM cashflow_transactions
        WHERE tx_date >= %s AND is_transfer = 0 AND is_duplicate = 0
    """, conn, params=(str(start_date),))
    if not net_since_start.empty:
        opening_balance -= float(net_since_start.iloc[0]['net'])
    ```
  - Verify: "Current Cash" KPI should show ~$117K (matching actual bank balances), not $227K. Row 0 opening_balance + 4 weeks of actual net cashflow should converge to ~$117K at the current week.
  - Flagged by: Analysts 4, 7, 11, 14, 17, 19 (most-cited bug in entire audit)

- [ ] **22b. CRITICAL: Fix Jameson loan misclassification ($110K/month swing)**
  - File: DB `category_mappings` table + `analytics/cashflow.py` function `_get_actual_weekly_totals()` (lines 368-394)
  - Bug: Jameson Companies loan payments ($5K-$76K/month, principal + interest) are mapped as `interest_income` in `category_mappings`, which is a REVENUE category. This creates a $110K/month double swing: revenue inflated by $55K + expenses understated by $55K. The `_get_actual_weekly_totals()` function has no direction filter, so debit transactions in revenue categories are summed as positive revenue.
  - Fix (two parts):
    1. **Data fix**: Update `category_mappings` to reclassify Jameson from `interest_income` to `loan`:
       ```python
       conn.execute("""
           UPDATE category_mappings SET category='loan', subcategory='principal'
           WHERE match_pattern LIKE '%jameson%' AND category='interest_income'
       """)
       ```
       Then call `reclassify_all_transactions(conn)` to propagate.
    2. **Code fix**: Add direction filter to `_get_actual_weekly_totals()` to prevent this class of bug:
       ```python
       # Determine expected direction from category group
       group = CASHFLOW_CATEGORIES.get(category, {}).get('group', 'expense')
       direction_filter = 'credit' if group == 'revenue' else 'debit'
       # Add to WHERE clause: AND direction = %s
       ```
  - Verify: After fix, `interest_income` actuals should show only actual interest credits (near $0). Loan expenses should show $55K+/month. Net monthly cashflow should drop by ~$110K.
  - Flagged by: Analysts 3, 4, 7, 11, 14, 19

- [ ] **22c. HIGH: Fix schedule detection overriding method-based expense timing**
  - File: `analytics/cashflow.py`, function `_project_expense_week()` (lines 614-699)
  - Bug: The schedule detection from bank actuals (`_detect_expense_schedule`) ALWAYS takes priority over method-based projections (media_plan, biweekly_schedule, quarterly_detect). When bank data exists for a category, the schedule detection classifies it as "daily" or "weekly" frequency and spreads the monthly total evenly. This causes: payroll shows $5-7K every week instead of biweekly $16K spikes; media spreads across all weeks instead of one monthly lump; sales tax spreads daily instead of quarterly.
  - Fix: Invert the priority — method-based projection should take priority over schedule detection for categories that have an explicit method. Move the schedule detection block (lines 641-658) to AFTER the method-based fallback block (lines 660-699). Only use schedule detection as a last resort for categories with method='trailing_avg' or 'schedule':
    ```python
    # 1. COGS (revenue_pct) — already handled, no change
    # 2. Method-based projection FIRST for: media_plan, biweekly_schedule, quarterly_detect
    if method == 'media_plan':
        ...
    elif method == 'biweekly_schedule':
        ...
    elif method == 'quarterly_detect':
        ...
    # 3. Schedule detection ONLY for trailing_avg and schedule methods
    elif schedule and schedule.get('has_data'):
        ...
    # 4. Final fallback
    else:
        ...
    ```
  - Verify: Payroll should show $0 in non-payroll weeks and ~$16K in biweekly payroll weeks. Media should show ~$0 most weeks and the full monthly amount near end of month. Sales tax should show in one week per quarter only.
  - Flagged by: Analysts 3, 14, 16, 19

- [ ] **22d. HIGH: Map Amazon bank transactions to amazon_revenue category**
  - File: DB `category_mappings` table (via code in `analytics/cashflow.py` or `views/tx_mapping.py`)
  - Bug: All 51 Amazon bank deposit transactions are unmapped (category='unmapped'). This blocks: (1) Amazon payout ratio auto-calibration (`compute_payout_ratio` returns None), (2) accurate actual-vs-projected comparison for Amazon weeks, (3) proper backtest validation. The transactions contain patterns like "amazon" or "amzn" in their summaries.
  - Fix: Add category mapping entries for Amazon disbursements:
    ```python
    # Insert mapping for Amazon deposit patterns
    conn.execute("""
        INSERT INTO category_mappings (match_pattern, category, subcategory, is_transfer, is_duplicate)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (match_pattern) DO UPDATE SET category=EXCLUDED.category, subcategory=EXCLUDED.subcategory
    """, ('amazon', 'amazon_revenue', 'disbursement', False, False))
    ```
    Then identify the actual normalized patterns from the Amazon transactions and add appropriate mappings. Run `reclassify_all_transactions(conn)` after.
  - Verify: `compute_payout_ratio(conn, 'amazon')` should return a value (~0.57-0.62) instead of None. Amazon actual weeks should show non-zero values in the forecast.
  - Flagged by: Analysts 2, 5, 12, 15, 18

- [ ] **22e. HIGH: Add logging to all silent exception handlers**
  - File: `analytics/cashflow.py` (10 locations), `views/cashflow.py` (2 locations)
  - Bug: 10 of 16 exception handlers silently swallow errors without logging. 3 are bare `except: pass`. Critical data loads (opening balance, Amazon forecast, media plan) have zero logging on failure. If any of these fail, the model silently falls back to defaults with no indication that data is missing.
  - Fix: Add `log.error()` or `log.warning()` to every exception handler. For critical paths (opening balance, revenue sources), log at ERROR level. For non-critical paths (overrides, settings), log at WARNING level. Replace bare `except: pass` with `except Exception as e: log.warning(...)`:
    - Line 144-145: classify_transaction DB lookup → `log.warning('classify_transaction DB lookup failed: %s', e)`
    - Line 799-800: Amazon forecast load → `log.error('Failed to load Amazon revenue forecast: %s', e)`
    - Line 805-806: Media plan load → `log.error('Failed to load media spend plan: %s', e)`
    - Line 816-817: Payroll detection → `log.warning('Payroll schedule detection failed: %s', e)`
    - Line 829-830: Expense schedule detection → `log.warning('Expense schedule detection failed for %s: %s', cat, e)`
    - Line 851-852: Opening balance → `log.error('Failed to compute opening balance, using seed: %s', e)`
    - Line 873-874: Override loading → `log.warning('Failed to load cashflow overrides: %s', e)`
    - Line 1053-1054: Balance freshness → `log.warning('Balance freshness query failed: %s', e)`
    - views/cashflow.py:380-381: Override loading in view → `log.warning(...)`
    - views/cashflow.py:587-589: Settings load → `log.warning(...)`
  - Verify: After fix, no bare `except: pass` should remain. `grep -n "except.*pass" analytics/cashflow.py` should return 0 results.
  - Flagged by: Analyst 10

- [ ] **22f. HIGH: Link fulfillment costs to revenue volume**
  - File: `analytics/cashflow.py`, function `_project_expense_week()` (lines 696-699, trailing_avg fallback)
  - Bug: Fulfillment costs stay flat at ~$5K/week regardless of projected revenue growth. In reality, fulfillment scales with order volume — more orders = more 3PL fees. The model uses a trailing average which never increases even as DTC revenue is projected to grow.
  - Fix: For fulfillment category, use a revenue-scaling method similar to COGS (revenue_pct). Calculate the historical fulfillment-to-DTC-revenue ratio from actuals, then project future fulfillment as that ratio × projected DTC revenue:
    ```python
    if category == 'fulfillment':
        # Calculate fulfillment as % of DTC revenue from trailing actuals
        trailing_fulfillment = compute_trailing_avg(conn, 'fulfillment', lookback_weeks=8)
        trailing_dtc = compute_trailing_avg(conn, 'dtc_revenue', lookback_weeks=8)
        if trailing_dtc > 0:
            fulfill_ratio = trailing_fulfillment / trailing_dtc
            projected_dtc = _project_revenue_week(conn, 'dtc_revenue', week_start, week_end, ctx)
            return projected_dtc * fulfill_ratio
        return trailing_fulfillment if trailing_fulfillment > 0 else CASHFLOW_SEED_DEFAULTS.get(category, 0)
    ```
  - Verify: As DTC revenue grows month-over-month, fulfillment should grow proportionally. If DTC doubles, fulfillment should approximately double.
  - Flagged by: Analyst 11

- [ ] **22g. HIGH: Fix past-week overrides being honored over actuals**
  - File: `analytics/cashflow.py`, `build_cashflow_forecast()` (override lookup in weekly row loop, ~lines 876-920)
  - Bug: The engine checks for overrides BEFORE checking if a week is in the past (is_actual=True). A manually inserted override for a past week overrides actual bank data. The UI correctly prevents editing past weeks (disabled columns), but the engine has no guard — any direct DB insert can override actuals.
  - Fix: In the weekly row loop, skip override lookup for past weeks. Only apply overrides when `is_past` is False:
    ```python
    # Only check overrides for future weeks
    if not is_past:
        override_key = (category, str(ws))
        if override_key in overrides:
            val = overrides[override_key]
            ...
    ```
  - Verify: Insert a test override for a past week. Re-run forecast. The past week should show actual bank data, not the override value.
  - Flagged by: Analyst 8

- [ ] **22h. HIGH: Fix COGS scenario interaction (expense multiplier on revenue_pct)**
  - File: `analytics/cashflow.py`, `_apply_scenario()` (line 702) and COGS calculation in `_project_expense_week()` (lines 630-639)
  - Bug: COGS uses `revenue_pct` method (25% of gross revenue), but the main loop also applies the expense scenario multiplier (conservative: +10%) on top. In conservative scenario, revenue drops 15% but COGS gets the expense +10% multiplier applied to already-reduced revenue, creating a perverse margin squeeze. In aggressive, COGS gets -5% on already-increased revenue, creating unrealistic margin expansion.
  - Fix: Skip the scenario expense multiplier for COGS since it already inherits the revenue scenario adjustment through its revenue inputs:
    ```python
    # In _apply_scenario or in the main loop where scenario is applied to expenses:
    if category == 'production' or method == 'revenue_pct':
        return amount  # COGS already tracks revenue scenario via its revenue inputs
    ```
  - Verify: In conservative scenario, COGS should be ~15% lower (matching revenue drop), not higher. COGS/revenue ratio should stay constant across scenarios.
  - Flagged by: Analyst 13

- [ ] **22. Implement all Phase 3 fixes**
  - Work through each task (22a-22h) one at a time.
  - For each fix: read the file, make the change, run `pytest tests/ -x`, commit separately.

- [ ] **23. Commit and push Phase 3 fixes**
  - Commit: `fix(cashflow): phase 3 — analyst round 2 fixes`
  - Push to main.

---

## Phase 4: Final Validation & Ship

Acceptance criteria: 5 final analysts pass. Railway page renders correctly with screenshots.

- [ ] **24. Run 5 final validation analysts (sequential)**
  - **Final 1 — Revenue Accuracy**: Re-run Analyst 1's checks. Must PASS.
  - **Final 2 — Amazon Timing**: Re-run Analyst 2's checks. Must show exactly 2 disbursements/month.
  - **Final 3 — Balance Arithmetic**: Re-run Analyst 4's checks. All rows must balance.
  - **Final 4 — Google Sheet Comparison**: Re-run Analyst 7's checks. Must be within 30% of reference.
  - **Final 5 — Full Regression**: Run complete forecast, verify all KPIs reasonable, no crashes, no error states.
  - Write results to `ANALYST_FINAL.md`. If any FAIL, add fix tasks and loop back to Phase 3 (max 2 loops).

- [ ] **25. Push and verify on Railway**
  - Push to main.
  - Open Railway deployment in browser (Playwright).
  - Navigate to Cash Flow page.
  - Screenshot: KPI row, balance chart, first 6 weeks of weekly detail table.
  - Verify: Current Cash ~$117K, Amazon biweekly lumps, realistic expense timing, no error banners.
  - If broken, fix and re-push. Do NOT mark complete until the page is correct.

- [ ] **26. Write final status to progress.txt**
  - Summary: total tasks completed, analyst rounds run, final KPI values, known limitations.

---

## Phase 5: Frontend Design & UX Polish

Acceptance criteria: Cash Flow page renders cleanly on Railway, all interactive features work (edit cells, smart projections, upload, settings), no visual jank.

- [ ] **27. Frontend design cleanup of Cash Flow dashboard**
  - File: `views/cashflow.py`
  - Open the Cash Flow page on Railway (Playwright) and audit the full page top to bottom.
  - Fix any layout issues: KPI cards should be evenly spaced, chart should fill width, table should scroll horizontally without breaking.
  - Ensure the editable table (`st.data_editor`) renders cleanly: columns should be appropriately sized, dollar formatting consistent (`$X,XXX`), past weeks visually distinct from future weeks (e.g., slightly dimmed or different background).
  - The balance chart should have clear visual distinction between actuals (solid green line) and projections (dashed blue line). Confidence band should be subtle, not overwhelming.
  - Alert banner (if triggered) should be prominent but not obnoxious — red background, white text, clear message with escaped dollar signs (no LaTeX rendering).
  - Model Settings expander should have clean form layout — inputs aligned, labels clear.
  - Upload section should show clear feedback after import (count of new/skipped/unmapped).
  - Take screenshots after fixes to verify.

- [ ] **28. Verify and fix editable cell overrides**
  - File: `views/cashflow.py` (functions `_render_editable_table`, `_render_category_section`, `_save_edits`)
  - File: `analytics/cashflow.py` (override lookup in `build_cashflow_forecast`)
  - Test the full edit flow end-to-end on Railway:
    1. Find a future week cell in the Revenue or Expense section
    2. Change the value (e.g., set Media for next week to $15,000)
    3. Verify `st.toast` confirms the save
    4. Refresh the page — the edited value must persist (read back from `cashflow_overrides` table)
    5. Verify the edited value flows through to Net Cash Flow and Closing Balance for that week and all subsequent weeks
  - Fix any issues with: st.data_editor change detection, override key format (line_item must match category key, week_start must match format in DB), upsert SQL.
  - Past week cells MUST be read-only (disabled=True in column_config). Verify you cannot edit an actual week.
  - Test with multiple edits in the same session — all should persist independently.

- [ ] **29. Verify and fix Smart Projection reset buttons**
  - File: `views/cashflow.py` (functions `_render_smart_buttons`)
  - For every category row that has a manual override, a "Reset [Category Name]" button should appear.
  - Test the flow:
    1. First, ensure at least one category has overrides (from Task 28 or insert directly into `cashflow_overrides`)
    2. The "Manual overrides active:" caption should appear with reset buttons
    3. Click a reset button — it should DELETE all overrides for that category from `cashflow_overrides`
    4. Page should rerun and the projected (auto-calculated) values should reappear
    5. The reset button for that category should disappear (no more overrides)
  - Make sure buttons render in a clean grid (up to 4 per row), not stacked vertically.
  - If NO categories have overrides, the entire "Manual overrides active" section should be hidden.

- [ ] **30. Information hierarchy & visual scanability**
  - File: `views/cashflow.py`
  - The page should follow a clear information hierarchy — most important thing first, details on demand:
    1. **KPI row**: These are the "glanceable" numbers. The CFO looks here first. Make sure they're large, bold, and the most prominent element on the page. Current Cash should be the biggest number. Add a subtle color indicator: green if above threshold, yellow if within 20% of threshold, red if below.
    2. **Alert banner**: Only shows when something needs attention. Should feel urgent but not alarming for every page load.
    3. **Chart**: Second thing the eye goes to. Should clearly tell the story — "cash is going up" or "cash is going down" — in under 2 seconds. The today line should be obvious.
    4. **Table**: The detail layer. Only for people who want to drill in. The table should NOT dominate the page — it's reference material, not the headline.
  - Check that the page doesn't feel like a wall of numbers. There should be visual breathing room between sections.
  - The scenario and horizon controls should feel like filters, not primary actions. They should be visually secondary to the data.

- [ ] **31. Table UX — make editing intuitive**
  - File: `views/cashflow.py`
  - The editable table is the core interactive element. It needs to feel obvious:
    - **Future weeks should look editable** — slightly different background, or a subtle border, so the user knows they can click and type. Past weeks should look "locked" — dimmed or greyed out.
    - **When a cell has been manually overridden**, it should have a visual indicator (e.g., bold text, small dot, or different background color) so the CFO can see at a glance which numbers are their inputs vs the model's projections.
    - **The "Smart Projection" reset button** should be per-row, clearly labeled, and only visible when that row has overrides. Something like a small "Reset to auto" link or icon next to the row label, not a separate section below.
    - **Total rows** (Total Inflows, Total Outflows, Net Cash Flow, Closing Balance) should be visually separated from editable rows — heavier border, bold, maybe a different background. They should feel like "results" not "inputs."
    - **Column headers** (week dates) should be sticky so they stay visible when scrolling down through expense rows.
    - **The current week column** should have a subtle highlight (vertical stripe) so you can immediately see "this is where we are."
  - Test the scroll experience: horizontal scrolling through 13+ weeks should feel smooth. The row labels (category names) should be sticky/frozen on the left.

- [ ] **32. Chart polish — make it tell a story**
  - File: `views/cashflow.py`, function `_build_balance_chart()`
  - The chart should answer "are we going to be okay?" in one glance:
    - **Actuals should feel solid and trustworthy** — thicker line, filled markers, solid color.
    - **Projections should feel like estimates** — thinner dashed line, lighter color, the confidence band reinforces "this is a range, not a promise."
    - **The min cash threshold line** should be prominent — dashed red, clearly labeled. It's the "danger zone."
    - **If the projection crosses below the threshold**, that intersection point should be visually emphasized (maybe a red dot or annotation: "Week 9: Below $100K").
    - **Hover tooltips** should show: week date, closing balance, net cash flow for that week. Not just the raw number.
    - **Remove chart clutter**: no unnecessary gridlines, no excessive legend entries, no Plotly watermark. Clean and minimal.
  - Consider adding a subtle background shading: green zone (above threshold), yellow zone (within 20% of threshold), red zone (below threshold). This makes the danger area viscerally obvious without reading numbers.

- [ ] **33. Mobile & narrow viewport check**
  - File: `views/cashflow.py`
  - Streamlit is often viewed on laptops with narrow browser windows, or occasionally on tablets/phones.
  - Open the Cash Flow page and resize the browser to 1024px wide (common laptop). Verify:
    - KPI cards don't overflow or stack weirdly
    - Chart is still readable
    - Table horizontal scroll works without breaking the layout
    - Controls (scenario, horizon) don't wrap to a third row
  - Resize to 768px (iPad). The page should still be usable, even if the table requires scrolling.
  - Fix any overflow, text truncation, or broken layout at narrow widths.

- [ ] **34. Loading states & error handling UX**
  - File: `views/cashflow.py`
  - The forecast takes a few seconds to build (multiple DB queries). During that time:
    - Show a `st.spinner('Building forecast...')` that covers the main content area, not just a tiny loading indicator.
    - If the forecast fails (DB error, missing data), show a clear error message with actionable advice: "No bank transactions found. Upload a CSV in the section below." — NOT a raw Python traceback.
    - If data is stale (balance_freshness_date > 14 days), show a warning badge next to "Current Cash": "Data is 18 days old — upload fresh transactions."
  - Test the empty state: what does the page look like with zero transactions in the DB? It should guide the user to upload, not show a broken chart and empty table.

- [ ] **35. Final visual verification on Railway**
  - Push all Phase 5 changes to main.
  - Open Railway deployment in Playwright.
  - Navigate to Cash Flow page.
  - Screenshot the full page in sections: KPI row, chart, revenue table section, expense table section, totals row, settings expander, upload expander.
  - Verify everything renders correctly with real data — no empty tables, no NaN values, no broken formatting.
  - Test: change scenario from Base to Conservative — numbers should update, chart should shift down.
  - Test: change horizon from 13 weeks to 52 weeks — table should expand, chart should extend.
  - Test: edit a cell, refresh, confirm persistence. Click a reset button, confirm revert.
  - This is the final task. Mark complete only when the page looks production-ready.
