# Analyst Round 2 — Cash Flow Model Validation

**Started:** 2026-03-02
**Data source:** Railway PostgreSQL (production)
**Forecast run:** `build_cashflow_forecast(start_date=today-4w, weeks=20, scenario='base')`

---

## Analyst 1 — Revenue Projection Accuracy

**Date:** 2026-03-02

### 1. DTC Revenue: Waterfall vs Actuals

| Metric | Value |
|--------|-------|
| Waterfall projected (Mar 2026) | $144,807/month gross |
| Actual Shopify avg (Sep-Feb, 6 months) | ~$121,000/month gross |
| Waterfall / Actual ratio | 1.20x (20% above recent average) |
| DTC payout ratio (auto-calibrated) | 0.984 (seed: 0.94) |
| Projected DTC cash inflow (Mar) | ~$131K/month ($32.8K/week) |
| Actual DTC bank deposits (Jan-Feb avg) | ~$116K/month |
| Cash projection / Actual deposits ratio | 1.13x (13% above) |

**Analysis:** The waterfall model projects $145K/month gross DTC revenue for March, ~20% above the 6-month average of $121K. This gap is driven by the **media spend plan inputs**, which show $75K/month planned spend — significantly higher than actual media debits of $24-65K/month (per VALIDATION_BASELINE). The waterfall mechanically converts media spend into new customers via ROAS, so overstated media spend inputs directly inflate DTC revenue projections.

The gap widens dramatically in future months as planned media ramps:

| Month | Media Plan | Waterfall DTC Gross | vs Recent Avg |
|-------|-----------|-------------------|---------------|
| Mar 2026 | $75K | $144,807 | +20% |
| Apr 2026 | $85K | $165,871 | +37% |
| May 2026 | $95K | $189,127 | +56% |
| Jun 2026 | $130K | $220,841 | +83% |
| Jul 2026 | $150K | $245,662 | +103% |

**Seasonal indices ARE being applied:** March index = 0.857, June index = 1.095. The Jun/Mar ratio in the waterfall (1.52x) reflects both seasonal ramp AND media ramp, not just seasonality. Seasonality alone would give ~1.28x.

**DTC payout ratio:** Auto-calibrated to 0.984 from EWMA of actual deposits/revenue over 12 weeks. Recent months show ~97% actual payout ratio (Jan: 96.4%, Feb: 97.7%). The auto-calibration is working correctly and tracks reality closely.

**Verdict: FAIL (MEDIUM severity)**
- Near-term (4 weeks): DTC cash projections are 13% above recent actuals — within acceptable range but slightly optimistic
- Medium-term (3-6 months): Projections diverge 37-103% from recent run-rate due to aggressive media spend plan inputs
- **Root cause:** Media spend plan inputs ($75-190K/month) vs actual media spend ($24-65K/month). This is an INPUT problem, not a code/algorithm bug
- **Recommendation:** Either (a) update media_spend table to reflect actual planned spend, or (b) add a validation warning when media plan exceeds trailing actual by >50%

### 2. Amazon Revenue: Forecast Table vs Actuals

| Month | Forecast Table | Actual Gross (DB) | Ratio |
|-------|---------------|-------------------|-------|
| Jan 2026 | N/A (not in table) | $144,563 | — |
| Feb 2026 | $150,000 | $133,820 | 1.12x (12% over) |
| Mar 2026 | $151,470 | $6,723 (partial) | — |
| Apr 2026 | $175,000 | — | — |
| May 2026 | $200,000 | — | — |
| Jun 2026 | $220,000 | — | — |

**Analysis:** February is the only month with both forecast and full-month actuals. The forecast overstates by 12%. More concerning: the table ramps to $200-220K by summer. If February's $134K is representative, the summer forecasts could be 50%+ optimistic.

**Amazon payout ratio:** Auto-calibration returns `None` (falls back to seed 0.62) because no Amazon deposits are mapped to the `amazon_revenue` category in `cashflow_transactions`. The seed value of 0.62 validates against Feb actual: $83K deposits / $134K gross = 62.0%. The seed is correct but auto-calibration is non-functional due to the mapping gap.

**Amazon weekly cash projections (next 4 weeks):**

| Week | Amazon Cash | Disbursement? |
|------|-----------|---------------|
| Mar 2-8 | $40,248 | Yes (day 8, prorated for current week) |
| Mar 9-15 | $0 | No |
| Mar 16-22 | $0 | No |
| Mar 23-29 | $46,956 | Yes (day 24) |

Monthly total: ~$87K cash. Based on $151K gross * 0.62 = $93.7K / 2 events = $46.9K each. The first event is prorated for current week ($40.2K).

**Verdict: FAIL (MEDIUM severity)**
- Near-term: Feb 12% overstated, manageable
- Summer: Forecast ramps aggressively ($175-220K) with only 2 months of data suggesting $134-145K. Could be 30-50% overstated
- **Root cause:** Amazon revenue forecast table inputs are aspirational, not data-driven
- **Recommendation:** Consider using trailing average from `daily_sku_sales` as a cap or sanity check on the forecast table values

### 3. Seasonal Indices Application

| Check | Result |
|-------|--------|
| Seasonal indices table populated? | Yes, all 12 months |
| Passed to `build_waterfall()`? | Yes (Task 3 fix confirmed) |
| March index (0.857) applied? | Yes — visible in lower March vs June projections |
| June index (1.095) applied? | Yes |
| Jun/Mar DTC ratio | 1.52x (includes seasonal + media ramp) |

**Verdict: PASS**

### 4. Projected Weekly Cash Inflows (Next 4 Weeks) — Smell Test

| Week | DTC Cash | Amazon Cash | Other | Total Inflows |
|------|---------|------------|-------|--------------|
| Mar 2-8 | $32,317 | $40,248 | $11,907 | $84,472 |
| Mar 9-15 | $32,767 | $0 | $13,891 | $46,658 |
| Mar 16-22 | $32,767 | $0 | $13,891 | $46,658 |
| Mar 23-29 | $32,767 | $46,956 | $13,891 | $93,614 |
| **4-week total** | **$130,618** | **$87,204** | **$53,580** | **$271,402** |

**vs Feb 2026 actuals:** Total credits were $218,325. The model projects $271K for the next 4 weeks — 24% above Feb actuals. The optimism comes primarily from DTC (+13%) and the aggressive Amazon forecast (+5%).

**Verdict: CONDITIONAL PASS** — near-term projections are in the right ballpark but run ~24% hot due to optimistic inputs.

### 5. Notable Observation (for Analyst 4)

**Current Cash KPI shows $227,258 but actual bank balance is $117,007.** This appears to be a double-counting issue: the opening balance is computed from the LATEST bank balance ($117K as of March 3), but this is assigned to week 1 (Feb 2, four weeks ago). Then 4 weeks of actual transaction net cash flows are added, arriving at $227K for the current week. Since the $117K already reflects February's transactions, they're being counted twice. This is flagged for **Analyst 4 (Balance Arithmetic)** to investigate.

---

### Summary

| Check | Verdict | Severity |
|-------|---------|----------|
| DTC revenue projection accuracy | **FAIL** | MEDIUM |
| Amazon revenue projection accuracy | **FAIL** | MEDIUM |
| Seasonal indices application | **PASS** | — |
| DTC payout ratio auto-calibration | **PASS** | — |
| 4-week inflow smell test | **CONDITIONAL PASS** | LOW |
| Current Cash KPI accuracy | **FLAG for Analyst 4** | HIGH |

**Key takeaway:** The revenue projection ENGINE works correctly — seasonal indices are applied, payout ratios auto-calibrate, DOW weighting distributes DTC revenue properly, and Amazon disbursement timing shows the expected biweekly pattern. However, the INPUT data (media spend plan and Amazon revenue forecast table) is optimistic relative to recent actuals, causing projections to run 13-24% hot in the near term and potentially 50-100% hot by summer. These are input/planning issues, not code bugs.

---

## Analyst 2 — Amazon Disbursement Timing

**Date:** 2026-03-02

### 1. Forecast Amazon Revenue by Week (20 weeks)

| Week       | Amazon Rev | Disbursement? | Actual? |
|------------|-----------|---------------|---------|
| 2026-02-02 | $0        | No            | YES     |
| 2026-02-09 | $0        | No            | YES     |
| 2026-02-16 | $0        | No            | YES     |
| 2026-02-23 | $0        | No            | YES     |
| 2026-03-02 | $40,248   | Yes (day 8)   | no      |
| 2026-03-09 | $0        | No            | no      |
| 2026-03-16 | $0        | No            | no      |
| 2026-03-23 | $46,956   | Yes (day 24)  | no      |
| 2026-03-30 | $0        | No            | no      |
| 2026-04-06 | $54,250   | Yes (day 8)   | no      |
| 2026-04-13 | $0        | No            | no      |
| 2026-04-20 | $54,250   | Yes (day 24)  | no      |
| 2026-04-27 | $0        | No            | no      |
| 2026-05-04 | $62,000   | Yes (day 8)   | no      |
| 2026-05-11 | $0        | No            | no      |
| 2026-05-18 | $62,000   | Yes (day 24)  | no      |
| 2026-05-25 | $0        | No            | no      |
| 2026-06-01 | $0        | No            | no      |
| 2026-06-08 | $68,200   | Yes (day 8)   | no      |
| 2026-06-15 | $0        | No            | no      |

**Observation:** Actual weeks (Feb 2-23) all show $0 because Amazon deposits are unmapped in `cashflow_transactions` (category='unmapped'). The model correctly shows $0 actuals for those weeks since there are no categorized Amazon credits.

### 2. Disbursement Events Per Month (Forecast Output)

| Month   | Events | Weeks                          | Total    |
|---------|--------|--------------------------------|----------|
| 2026-03 | 2      | Mar 2, Mar 23                  | $87,203  |
| 2026-04 | 2      | Apr 6, Apr 20                  | $108,500 |
| 2026-05 | 2      | May 4, May 18                  | $124,000 |
| 2026-06 | 1*     | Jun 8                          | $68,200  |

*June shows 1 event because the 20-week window ends before June 24. This is a windowing artifact, not a bug.

**Verdict: PASS** — Exactly 2 disbursement events per month within the forecast window.

### 3. Pre-computed Schedule Verification

The `_build_amazon_disbursement_schedule()` function (Task 1 fix) produces:

| Week       | Disbursement For | Count |
|------------|-----------------|-------|
| 2026-02-02 | 2026-02         | 1     |
| 2026-02-23 | 2026-02         | 1     |
| 2026-03-02 | 2026-03         | 1     |
| 2026-03-23 | 2026-03         | 1     |
| 2026-04-06 | 2026-04         | 1     |
| 2026-04-20 | 2026-04         | 1     |
| 2026-05-04 | 2026-05         | 1     |
| 2026-05-18 | 2026-05         | 1     |
| 2026-06-08 | 2026-06         | 1     |

Each disbursement is assigned to exactly ONE week. No week has count > 1. No double-counting.

**Events per calendar month:** Feb: 2 ✓, Mar: 2 ✓, Apr: 2 ✓, May: 2 ✓, Jun: 1 (window truncation)

**Verdict: PASS** — Task 1 fix eliminates the overcounting bug.

### 4. Actual Amazon Deposits from Bank Data

All 51 Amazon-related transactions in `cashflow_transactions` are **unmapped** (category='unmapped'). This means:
- The model cannot auto-calibrate Amazon payout ratio from bank actuals (confirmed by Analyst 1)
- Actual weeks show $0 Amazon revenue because no transactions are categorized as `amazon_revenue`

**Actual Amazon disbursements (credits > $1,000):**

| Date       | Amount   | Day |
|------------|---------|-----|
| 2025-07-25 | $114,852 | 25  |
| 2025-08-08 | $46,309  | 8   |
| 2025-08-22 | $39,249  | 22  |
| 2025-09-05 | $48,557  | 5   |
| 2025-09-19 | $49,023  | 19  |
| 2025-10-03 | $42,346  | 3   |
| 2025-10-17 | $44,868  | 17  |
| 2025-10-31 | $47,147  | 31  |
| 2025-11-14 | $35,940  | 14  |
| 2025-11-28 | $35,941  | 28  |
| 2025-12-12 | $32,104  | 12  |
| 2025-12-26 | $36,075  | 26  |
| 2026-01-09 | $36,061  | 9   |
| 2026-01-23 | $38,856  | 23  |
| 2026-02-06 | $39,283  | 6   |
| 2026-02-20 | $43,605  | 20  |

**Key findings from actual data:**
- **Perfect 14-day cycle**: Average gap = 14.0 days exactly, range 14-14 days
- **Early window actual days**: 3, 5, 6, 8, 9, 12, 14 (range: day 3-14)
- **Late window actual days**: 17, 19, 20, 22, 23, 25, 26, 28, 31 (range: day 17-31)
- **October 2025 had 3 disbursements** (days 3, 17, 31) — happens when the biweekly cycle puts a late disbursement from the prior cycle and an early one in the same month
- **Average per disbursement**: $47,025 (range: $32K-$115K)

### 5. Model Timing vs Actual Timing

| Aspect | Model | Actual |
|--------|-------|--------|
| Frequency | Biweekly (day 8 and 24) | Biweekly (every 14 days exactly) |
| Early window | Fixed day 8 | Range day 3-14 |
| Late window | Fixed day 24 | Range day 17-31 |
| Events/month | Always exactly 2 | Usually 2, occasionally 3 (Oct) |
| Cycle approach | Calendar-based (fixed days) | Rolling 14-day cycle |

**Timing accuracy:** The model's fixed day 8/24 is a reasonable simplification. Real Amazon disbursements follow a rolling 14-day cycle that drifts across the calendar month. This means:
- Some months the model will place the disbursement in the wrong week by ±1 week
- October-like months with 3 actual disbursements will be underrepresented (model always shows 2)
- Over a quarter, the total amount is correct even if per-week timing is off

### 6. 13-Week Disbursement Summary

| Metric | Value |
|--------|-------|
| Projected weeks | 13 |
| Months covered | 3 (Mar, Apr, May) |
| Non-zero Amazon weeks | 6 |
| Expected (2/month × 3) | 6 |
| Max single-week Amazon | $62,000 |
| Double-count check | PASS (no week exceeds 1.8× expected per-event amount) |

**Per-event amounts check:**
- March: $151K × 0.62 / 2 = $46,956/event — matches actual avg of $41K (Feb)
- April: $175K × 0.62 / 2 = $54,250/event — slightly above recent actuals
- May: $200K × 0.62 / 2 = $62,000/event — above recent actuals (aspirational forecast input)

### 7. Observations for Later Analysts

1. **Amazon mapping gap (for Analyst 3/10):** All 51 Amazon bank transactions are unmapped. This includes both credits (disbursements, ~$40-115K each) and debits (Seller Central fees, ~$5K each). Mapping these would enable auto-calibration of the Amazon payout ratio.

2. **Rolling cycle vs fixed day (for Phase 3):** The model could be more accurate by tracking the last known Amazon disbursement date and projecting forward every 14 days, instead of using fixed day 8/24. This would correctly handle months with 3 disbursements and reduce week-level timing error. **Severity: LOW** — the quarterly total is correct regardless.

---

### Summary

| Check | Verdict | Severity |
|-------|---------|----------|
| Exactly 2 disbursements per month | **PASS** | — |
| Task 1 fix (no overcounting) | **PASS** | — |
| No double-counting in any week | **PASS** | — |
| Per-event amounts reasonable | **PASS** | — |
| Timing alignment with actuals | **CONDITIONAL PASS** | LOW |
| Amazon transactions mapped in bank data | **FLAG** | MEDIUM |

**Key takeaway:** The Task 1 fix (`_build_amazon_disbursement_schedule`) works correctly — each disbursement is assigned to exactly one week, exactly 2 per month, with no overcounting. The original bug (2.6 events/month) is eliminated. Per-event amounts scale correctly with the forecast table values and payout ratio. The main gap is that all 51 Amazon bank transactions remain unmapped, preventing auto-calibration and causing actual weeks to show $0 Amazon revenue. The fixed day 8/24 approach is a reasonable simplification of Amazon's actual rolling 14-day cycle, with ±1 week timing variance that averages out quarterly.

---

## Analyst 3 — Expense Completeness & Timing

**Date:** 2026-03-02

### 1. 13-Week Actual vs Projected Expense Comparison

**Actual period:** 2025-12-01 to 2026-03-01 (13 weeks of bank data)
**Projected period:** 2026-03-02 to 2026-05-31 (13 weeks of projections)

| Category | Actual 13wk | Proj 13wk | Ratio | Diff % | Method | Verdict |
|----------|------------|-----------|-------|--------|--------|---------|
| media | $51,096 | $129,500 | 2.53x | +153% | media_plan | **FAIL** |
| payroll | $71,189 | $89,880 | 1.26x | +26% | biweekly_schedule | **FAIL** |
| loan | $1 | $2 | 2.73x | +173% | schedule | **FAIL** |
| fulfillment | $68,926 | $68,836 | 1.00x | -0.1% | trailing_avg | **PASS** |
| production | $62,665 | $128,913 | 2.06x | +106% | revenue_pct | **FAIL** |
| sales_tax | $6,763 | $4,137 | 0.61x | -39% | quarterly_detect | **FAIL** |
| software | $230 | $212 | 0.92x | -8% | trailing_avg | **PASS** |
| shipping | $33 | $1,626 | 48.9x | +4792% | trailing_avg | **FAIL** |
| agency | $14,100 | $11,566 | 0.82x | -18% | trailing_avg | **PASS** |
| accounting | $18,977 | $9,707 | 0.51x | -49% | trailing_avg | **FAIL** |
| insurance | $0 | $12,857 | inf | N/A | trailing_avg | **FAIL** |
| other_expense | $0 | $6,429 | inf | N/A | trailing_avg | **FAIL** |
| **TOTAL** | **$293,980** | **$463,665** | **1.58x** | **+58%** | | **FAIL** |

**9 of 12 categories FAIL** (>25% deviation). Total projected outflows are 58% above actual outflows.

### 2. Root Cause Analysis — Schedule Detection Overrides Methods

**CRITICAL BUG (HIGH severity):** In `_project_expense_week()` (analytics/cashflow.py, lines 614-699), the schedule detection from bank actuals ALWAYS takes priority over the method-based projection (media_plan, biweekly_schedule, quarterly_detect). The code flow is:

```
1. Check method == 'revenue_pct' → only COGS exits here
2. Check pre-detected schedule from actuals → if has_data AND finds payments, RETURN
3. FALLBACK: method-based projection (media_plan, biweekly_schedule, etc.)
```

Step 2 short-circuits step 3 for any category with bank transaction history. This means the carefully designed timing methods (biweekly payroll, monthly media, quarterly taxes) are never reached when schedule detection has data.

**Impact by category:**

#### Media (153% over)
- Schedule detection: avg_gap=1.9 days, frequency='daily' (contaminated by small daily TikTok $250 payments mixed with large monthly Meta $25-52K payments)
- Result: `_project_next_payments()` walks forward every 1.9 days from last payment, projecting ~$5-7K/week evenly spread
- Expected behavior (media_plan method): $75K lump in one week per month (weeks where day >= 25)
- Neither matches reality well — actual media spend is $17K/month in bank debits (most goes on AMEX), while the plan table says $75K/month
- The schedule detection produces $130K/13wk, a middle ground but driven by wrong logic

#### Payroll (26% over)
- Schedule detection: avg_gap=9.4 days, frequency='weekly' (Justworks has 3 payments/month: biweekly ~$9.6K + smaller ~$4.7K benefits/tax payment around 6th-8th)
- Result: Payroll shows up in ALL 16 projected weeks ($5,454-$7,458 each), not just 2 per month
- Expected behavior (biweekly_schedule method): $0 in non-payroll weeks, ~$7,458 in 2 weeks per month (around 10th and 25th)
- Actual pattern: 3 payments per month (10th ~$9.6K, 24th-27th ~$9.6K, 6th-8th ~$4.7K) — $24K/month actual vs $30K/month projected

#### Sales Tax (39% under)
- Schedule detection: avg_gap=4.9 days, frequency='daily' (many small state tax remittances throughout the month)
- Result: $234-467/week spread evenly
- Expected behavior (quarterly_detect method): Large lump ($18K+) in Jan, Apr, Jul, Oct
- Actual pattern is mixed: frequent small payments ($234 each) plus occasional larger quarterly payments
- The quarterly_detect method would produce ~$4K in one Q2 week (Apr 13-19) but $0 in others; schedule detection produces $4.1K spread evenly. Neither perfectly matches but the quarterly lump is more accurate for the large payments.

### 3. Jameson Loan Misclassification (CRITICAL)

**CRITICAL BUG (HIGH severity):** Jameson loan principal + interest payments ($5K-$76K/month, accelerating) are categorized as `interest_income` in `category_mappings`, which is a **REVENUE** category. This means:

| Month | Jameson Payment | Direction | Category (Wrong) | Should Be |
|-------|----------------|-----------|-------------------|-----------|
| Feb 2026 | $54,947 | debit | interest_income | loan |
| Jan 2026 | $56,115 | debit | interest_income | loan |
| Dec 2025 | $36,497 | debit | interest_income | loan |
| Nov 2025 | $30,265 | debit | interest_income | loan |
| Oct 2025 | $5,706 | debit | interest_income | loan |
| Sep 2025 | $5,363 | debit | interest_income | loan |
| Aug 2025 | $5,571 | debit | interest_income | loan |
| Jul 2025 | $76,462 | debit | interest_income | loan |
| **Total** | **$270,926** | | | |

**Double impact:**
1. **Revenue inflated:** `_get_actual_weekly_totals()` does NOT filter by direction. Jameson debit payments ($36-56K/month) are summed positively as "interest income" revenue in actual weeks. Weeks with Jameson payments show ~$55K of phantom revenue.
2. **Expenses understated:** The `loan` category shows $1 actual and ~$0 projected because the real loan payments are categorized elsewhere. The model thinks there are no loan expenses.
3. **Combined effect:** Revenue overstated by ~$55K/month AND expenses understated by ~$55K/month = ~$110K/month swing in net cash flow. Over 13 weeks this is a ~$330K cumulative error.

**Code issue (secondary):** `_get_actual_weekly_totals()` (analytics/cashflow.py, lines 368-394) sums ALL amounts for a category without filtering by direction. For revenue categories, only credits should count; for expense categories, only debits should count. Without this guard, any misclassified transaction in the wrong direction inflates actuals instead of reducing them.

### 4. Categories With No Actuals Using Seed Defaults

Three categories have NO bank transaction data and fall back to seed defaults from `CASHFLOW_SEED_DEFAULTS`:

| Category | Seed ($/week) | Proj 13wk | Actual 13wk | Issue |
|----------|--------------|-----------|-------------|-------|
| insurance | $1,000 | $12,857 | $0 | Likely on AMEX or unmapped |
| other_expense | $500 | $6,429 | $0 | Catch-all with no mapped transactions |
| loan | $7,500 | $0* | $1 | *Schedule method returns $0 because trailing_avg is $0 |

For insurance and other_expense, the trailing average returns $0 (no mapped transactions), so the seed defaults kick in: $1,000/week and $500/week respectively. Over 13 weeks this adds ~$19K of projected expenses that don't appear in actuals.

**Note on loan:** The seed default for loan is $7,500/week (~$32K/month), but the 'schedule' method only pays in weeks where day >= 25. With trailing avg of $0 and seed of $7,500, the method computes `val = 0` (trailing avg takes priority over seed only when > 0, but here `avg = 0` so `val = seed = 7500`... wait, actually the code says `val = avg if avg > 0 else seed`). Let me recheck...

Actually looking at the output: loan projects $0 for most weeks. This is because the schedule method:
1. Gets trailing avg = $0 (no mapped loan transactions)
2. Falls back to seed = $7,500
3. Then only pays in weeks where day >= 25
4. But the actual projected values round to $0 — this needs investigation. The schedule fallback may have `val * 4.33` to convert to monthly, but with `seed = 7500`, it would be $7500 * 4.33 = $32,475 in qualifying weeks.

Upon re-examining the output: loan shows "<-- HIT" for weeks where day >= 25 but the dollar values displayed as $0. This is likely because the schedule detection returned no data, then the 'schedule' method fallback computes: trailing avg = $0, seed = $7,500, `val = 0 if 0 > 0 else 7500 = 7500`, then `return 7500 * 4.33 = 32,475` for qualifying weeks. But the forecast showed $0... Let me check if the issue is a floating point display problem or if the loan schedule code has a different path.

Wait, I see: the output shows `loan=$         0  <-- HIT` — the "<-- HIT" triggers when loan > 0, and the display shows $0. This must mean the value is very small (like $0.09, rounding to $0 in display). The total over 13 weeks was $2, confirming essentially zero.

The issue is that the schedule method falls through to the fallback code path (lines 687-694):
```python
elif method == 'schedule':
    avg = compute_trailing_avg(conn, category, lookback_weeks=8)
    seed = CASHFLOW_SEED_DEFAULTS.get(category, 0)
    val = avg if avg > 0 else seed
    if week_end.day >= 25 or week_start.day >= 25:
        return val * 4.33
    return 0.0
```

But this code is NEVER reached because the schedule detection check happens first. Since `_schedule_loan` has `has_data=False`, the schedule check is skipped... and then it should fall through to the method-based check. Let me trace again:

Looking at the code:
```python
schedule = ctx.get(sched_key)  # _schedule_loan
if schedule and schedule.get('has_data'):
    ... # skipped because has_data=False
```
Then it falls through to:
```python
if method == 'media_plan': ...
elif method == 'biweekly_schedule': ...
elif method == 'quarterly_detect': ...
elif method == 'schedule':
    avg = compute_trailing_avg(conn, category, lookback_weeks=8)
    ...
```

So loan DOES reach the 'schedule' method. But `compute_trailing_avg` for loan returns ~$0/week (only $1 actual). `val = 0 if 0 > 0 else 7500 = 7500`. Wait, avg=$0.02 maybe? If avg is a tiny positive number, then `val = avg = 0.02`, then `val * 4.33 = 0.09`. That would explain the $0 display.

Actually, `compute_trailing_avg` returns `total / max(lookback_weeks, 1)`. Total loan debits = $1 over 8 weeks = $0.125/week. Since $0.125 > 0, `val = 0.125`, then in qualifying weeks: `0.125 * 4.33 = $0.54`. Over 13 weeks, ~2 qualifying weeks → ~$1.08 ≈ $2 (matches the projected $2!).

So the loan seed default of $7,500 is never used because the trailing average ($0.125) is technically > 0. This is a subtle bug: a $1 transaction prevents the $7,500 seed from activating.

### 5. Expense Timing Pattern Verification

#### Payroll Timing: **FAIL**
- **Expected:** 2 payments per month (~10th and ~25th), $0 in other weeks
- **Actual bank pattern:** 3 payments per month (6th-8th ~$4.7K, 10th ~$9.6K, 24th-27th ~$9.6K)
- **Model output:** Payroll in ALL 16 projected weeks ($5,454-$7,458 each) — no $0 weeks
- Schedule detection classifies as 'weekly' (avg_gap 9.4d) and distributes $23.6K monthly / 4.33 = $5,454/week as floor, with $7,458 from _project_next_payments on specific weeks
- The biweekly_schedule method (checks for days 9-11 and 24-26) is never reached

#### Media Timing: **FAIL**
- **Expected:** ~1 large payment per month near end of month
- **Actual bank pattern:** 1 large Meta payment ($22-52K) near month end + frequent small TikTok ($250/day)
- **Model output:** Media in ALL 16 projected weeks ($5,102-$6,803 each) — no monthly lump pattern
- Schedule detection's daily frequency (avg_gap 1.9d) distributes evenly
- The media_plan method is never reached

#### Sales Tax Timing: **FAIL**
- **Expected:** Quarterly lump in Jan, Apr, Jul, Oct
- **Actual bank pattern:** Frequent small state tax remittances ($234 avg, every 5 days) + periodic larger payments
- **Model output:** Sales tax in ALL 16 projected weeks ($234-$467 each) — no quarterly pattern
- Schedule detection's daily frequency distributes evenly
- The quarterly_detect method is never reached
- Note: The actual sales tax pattern is itself unusual — frequent small payments rather than quarterly lumps

#### Fulfillment Timing: **PASS**
- Trailing average distributes ~$5.3K/week evenly, which matches the actual pattern of weekly fulfillment invoices
- Schedule detection confirms weekly frequency (avg_gap 7.2d)

#### Production/COGS Timing: **CONDITIONAL PASS**
- revenue_pct method correctly checked BEFORE schedule detection
- COGS appears only in weeks with Amazon disbursements (when Amazon adds to gross revenue), which creates a lumpy pattern
- But the 25% rate applied to grossed-up revenue produces $10-27K/week vs actual production spend of ~$5K/week average (PO-driven)
- The revenue_pct method is conceptually right but produces amounts 2x above actual production costs

### 6. Missing Expense Categories (Unmapped Gap)

From VALIDATION_BASELINE: **$103K/month in unmapped debits** including AMEX payments, Amazon FBA fees, contractor payments, and software subscriptions. The expense projection only covers mapped categories, missing:

| Missing Category | Estimated Monthly | Impact |
|-----------------|------------------|--------|
| AMEX payments (likely media, software, misc) | ~$15-25K | Expenses understated |
| Amazon FBA fees | ~$5-10K | Expenses understated |
| Software (on AMEX/unmapped) | ~$10K | Expenses understated |
| Contractor payments | ~$5-7K | Expenses understated |
| **Total unmapped expenses** | **~$35-52K/month** | **Expenses 25-35% understated** |

This is a data/mapping issue, not a code bug — but it means the expense projections are structurally incomplete.

---

### Summary

| Check | Verdict | Severity |
|-------|---------|----------|
| Schedule detection overrides method-based timing | **FAIL** | HIGH |
| Jameson loan misclassified as interest_income | **FAIL** | CRITICAL |
| _get_actual_weekly_totals no direction filter | **FAIL** | HIGH |
| Payroll timing (should be biweekly, shows weekly) | **FAIL** | MEDIUM |
| Media timing (should be monthly, shows daily) | **FAIL** | MEDIUM |
| Sales tax timing (should be quarterly, shows daily) | **FAIL** | LOW |
| Fulfillment trailing avg | **PASS** | — |
| Production/COGS revenue_pct scaling | **CONDITIONAL PASS** | LOW |
| Insurance/other_expense phantom projections | **FAIL** | LOW |
| Accounting understated 49% | **FAIL** | MEDIUM |
| Shipping overstated 4792% | **FAIL** | LOW |
| Unmapped expenses ($103K/month) | **FLAG** | HIGH |
| Total outflows accuracy (58% over) | **FAIL** | HIGH |

**Key takeaways:**

1. **Schedule detection override is the #1 code bug.** The pre-detected schedule from bank actuals always takes priority over the method-based projection logic (biweekly_schedule, media_plan, quarterly_detect). This causes payroll to spread weekly, media to spread daily, and sales tax to spread daily — destroying the intended timing patterns that make the model realistic. **Fix:** For categories with specific methods (media_plan, biweekly_schedule, quarterly_detect, schedule), skip the schedule detection and go directly to the method-based projection.

2. **Jameson misclassification is the #1 data bug.** $271K in loan payments categorized as revenue (interest_income) causes ~$110K/month swing in net cash flow. **Fix:** Reclassify Jameson transactions from interest_income to loan in category_mappings. Also add a direction filter to `_get_actual_weekly_totals()` to prevent debit transactions from inflating revenue categories.

3. **Unmapped transactions represent ~$103K/month in invisible expenses.** Until mapping coverage improves from 65% to >90%, expense projections will structurally understate reality. This is a data completeness issue, not a code bug.
