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

- [ ] **3. Fix missing DTC seasonality in waterfall call**
  - File: `analytics/cashflow.py`, function `build_cashflow_forecast()` (line 747)
  - Bug: `build_waterfall(media_plan, source_filter='shopify', horizon_months=12)` is called without `seasonal_indices`. The function accepts it (confirmed: `def build_waterfall(media_plan, source_filter=None, horizon_months=12, seasonal_indices=None)`), but we're not passing it.
  - Fix: Load seasonal indices from DB and pass them:
    ```python
    seasonal_df = read_sql('SELECT month_num, index_value FROM seasonal_indices', conn)
    seasonal_dict = dict(zip(seasonal_df['month_num'], seasonal_df['index_value'])) if not seasonal_df.empty else None
    wf = build_waterfall(media_plan, source_filter='shopify', horizon_months=12, seasonal_indices=seasonal_dict)
    ```
  - Verify: March revenue (seasonal index 0.98) should be ~12% lower than June revenue (index 1.10). If seasonal_indices table is empty, the model should still work (graceful None handling).

- [ ] **4. Fix normalize_summary dead regex pattern**
  - File: `analytics/cashflow.py`, line 34
  - Bug: Pattern `r'\b[A-Z0-9]{8,}\b'` uses uppercase character class but input is lowercased on line 60 before patterns run. Dead code — never matches.
  - Fix: Change line 34 to `r'\b[a-z0-9]{8,}\b'`.
  - Verify: `normalize_summary("SHOPIFY DES:FUNDING ID:ABC12345678 INDN:Josh")` should strip `abc12345678` after lowercasing. Before this fix it wouldn't.

- [ ] **5. Fix reclassify_all_transactions O(M*N) performance**
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

- [ ] **6. Fix current-week DTC proration to use DOW weights**
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

- [ ] **9. Commit and push all Phase 1 fixes**
  - Run `pytest tests/ -x` to verify no regressions.
  - Commit: `fix(cashflow): phase 1 — Amazon overcounting, KPI accuracy, seasonality, perf`
  - Push to main.

---

## Phase 2: Validate Against Google Sheet (10 Sequential Analysts)

Acceptance criteria to exit Phase 2: All 10 analysts complete. Findings written to `ANALYST_ROUND2.md`. Phase 3 tasks generated from any FAILs.

Each analyst runs SEQUENTIALLY so it can build on the previous analyst's findings. Each analyst must read `analytics/cashflow.py`, `views/cashflow.py`, and any prior analyst output before starting.

### Tasks

- [ ] **10. Research — build VALIDATION_BASELINE.md**
  - Query the Railway database (use `from db import get_db, read_sql`) to extract:
    - Revenue by channel by month (last 6 months) from `daily_sku_sales`
    - Expense totals by category by month from `cashflow_transactions`
    - Amazon revenue forecast table values
    - Media spend table values
    - Current bank account balances
    - Count of mapped vs unmapped transactions
  - Cross-reference against the Google Sheet reference data in this PRD (see "Reference Data" section above).
  - Write `VALIDATION_BASELINE.md` with all findings. This file is the source of truth for all analysts.

- [ ] **11. Analyst 1 — Revenue Projection Accuracy**
  - Read `VALIDATION_BASELINE.md` and `analytics/cashflow.py`.
  - Validate: Does the DTC monthly revenue from `build_waterfall()` roughly match actual Shopify revenue in `daily_sku_sales`? Is it in the $42-52K/month range? Are seasonal indices being applied?
  - Validate: Does the Amazon monthly revenue from `amazon_revenue_forecast` table match reality? The DB shows $54-102K/month actual Amazon revenue — if the forecast table says $150-200K, that's a critical discrepancy.
  - Calculate the projected weekly DTC and Amazon cash inflows for the next 4 weeks. Do they pass the smell test against the reference data?
  - Write findings (PASS/FAIL with specifics) and append to `ANALYST_ROUND2.md`.

- [ ] **12. Analyst 2 — Amazon Disbursement Timing**
  - Read prior analyst findings in `ANALYST_ROUND2.md`.
  - Run `build_cashflow_forecast()` and extract all `amazon_revenue` column values for 13 weeks.
  - Count disbursement events per month. Must be exactly 2 per month.
  - Cross-reference against actual Amazon deposits in `cashflow_transactions` (category='amazon_revenue') — when did real disbursements land?
  - Verify the fix from Task 1 eliminated the overcounting bug.
  - Append findings to `ANALYST_ROUND2.md`.

- [ ] **13. Analyst 3 — Expense Completeness & Timing**
  - Read prior analyst findings.
  - For each of the 12 expense categories: compare projected 13-week total against actual 13-week total from `cashflow_transactions`.
  - Flag any category where projected vs actual differs by >25%.
  - Verify expense timing: payroll should show in 2 weeks per month (not all 4), media in ~1 week per month, taxes in 1 week per quarter. Check that gap weeks show $0 for scheduled expenses.
  - Append findings to `ANALYST_ROUND2.md`.

- [ ] **14. Analyst 4 — Balance Arithmetic & KPI Accuracy**
  - Read prior analyst findings.
  - Verify for every row: `closing_balance == opening_balance + net_cashflow` (within $1 tolerance).
  - Verify: `opening_balance[row N+1] == closing_balance[row N]`.
  - Verify "Current Cash" KPI shows the current week's opening balance, not row 0's.
  - Verify "13-Week Projected" is indexed from current week, not from 4 weeks ago.
  - Verify "Monthly Burn" only uses actual (is_actual=True) rows.
  - Append findings to `ANALYST_ROUND2.md`.

- [ ] **15. Analyst 5 — Payout Ratio Calibration**
  - Read prior analyst findings.
  - Compute actual DTC payout ratio: sum of `cashflow_transactions` (category='dtc_revenue', direction='credit') / sum of `daily_sku_sales` (source='shopify') for the same period (with 3-day lag offset).
  - Compute actual Amazon payout ratio: same approach with 21-day lag.
  - Compare against the model's auto-calibrated ratios from `compute_payout_ratio()`.
  - If ratios differ by >5% from the reference values (DTC ~0.94, Amazon ~0.62), flag it.
  - Append findings to `ANALYST_ROUND2.md`.

- [ ] **16. Analyst 6 — Seasonality Impact**
  - Read prior analyst findings.
  - Check if `seasonal_indices` table has data. If empty, flag as FAIL (Task 3 won't work).
  - If populated: verify March revenue forecast uses index ~0.98 and June uses ~1.10. The ratio between June and March projected DTC revenue should be approximately 1.12x.
  - If seasonality appears flat across all months, the fix from Task 3 didn't work.
  - Append findings to `ANALYST_ROUND2.md`.

- [ ] **17. Analyst 7 — Google Sheet Comparison**
  - Read all prior analyst findings and `VALIDATION_BASELINE.md`.
  - Compare our model's 13-week output against the Google Sheet reference data:
    - Total monthly revenue: should be $250-350K/month (DTC $42-52K + Amazon $54-102K + other)
    - Total monthly expenses: should be roughly $150-250K/month (media + payroll + loan + fulfillment + software + rest)
    - Net monthly cash flow: should be positive $50-150K/month based on the Google Sheet
    - 13-week closing balance: should be higher than opening if net positive
  - Flag any metric that's >30% off from the Google Sheet model's range.
  - Append findings to `ANALYST_ROUND2.md`.

- [ ] **18. Analyst 8 — Override & Edit Persistence**
  - Read prior findings.
  - Test the override flow: insert a manual override into `cashflow_overrides` table for a future week, re-run `build_cashflow_forecast()`, verify the override value appears in the output DataFrame instead of the projected value.
  - Test that past-week overrides are NOT honored (actuals should always win for past weeks). Actually check the code logic for this.
  - Test the "Reset to Smart Projection" flow: deleting from `cashflow_overrides` should restore the projected value.
  - Append findings to `ANALYST_ROUND2.md`.

- [ ] **19. Analyst 9 — Edge Cases & Robustness**
  - Read prior findings.
  - Test: week spanning two months (does revenue get attributed to the right month for each day?).
  - Test: what happens if `amazon_revenue_forecast` table is empty? Should fall back to trailing average, not crash.
  - Test: COGS calculation when Amazon revenue is $0 for a week (division by zero guard on payout ratio gross-up).
  - Test: `normalize_summary()` with edge inputs: empty string, None, string of only numbers, string with only special characters.
  - Test: confidence intervals — do they widen over time? Is week 1 tighter than week 13?
  - Append findings to `ANALYST_ROUND2.md`.

- [ ] **20. Analyst 10 — Code Quality & Security**
  - Read prior findings.
  - Check all SQL queries in `analytics/cashflow.py` use parameterized queries (%s placeholders), not f-strings.
  - Check for division-by-zero guards on all ratio calculations.
  - Check that no exceptions are silently swallowed (bare `except: pass`).
  - Check logging coverage: are errors logged before being caught?
  - Check for any remaining dead code, TODO comments, or commented-out blocks.
  - Verify `_STRIP_PATTERNS` regex (Task 4 fix) now uses lowercase character class.
  - Append findings to `ANALYST_ROUND2.md`.

- [ ] **21. Generate Phase 3 fix tasks**
  - Read `ANALYST_ROUND2.md` from top to bottom.
  - For each FAIL or issue with severity HIGH or CRITICAL, append a new task to this PRD at the bottom of Phase 3 with: file, bug description, fix, verify.
  - If ALL 10 analysts PASSed, write "ALL PASS — skipping Phase 3" and skip to task 24.
  - Update `progress.txt`.

---

## Phase 3: Fix Round 2 Issues

Acceptance criteria: All dynamically-generated tasks complete. Code committed and pushed.

- [ ] **22. Implement all Phase 3 fixes**
  - Work through each task appended by task 21, one at a time.
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
  - This is the last task.
