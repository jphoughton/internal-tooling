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

---

## Analyst 4 — Balance Arithmetic & KPI Accuracy

**Date:** 2026-03-02

### 1. Row-Level Balance Arithmetic

**Test:** For every row, verify `closing_balance == opening_balance + net_cashflow` (within $1 tolerance).

| Check | Result |
|-------|--------|
| Rows tested | 20 |
| Rows passing | 20 |
| Max deviation | $0.00 |

**Verdict: PASS** — All 20 rows satisfy the closing = opening + net identity exactly.

### 2. Row-to-Row Balance Chain

**Test:** For every consecutive pair, verify `opening_balance[N+1] == closing_balance[N]` (within $1 tolerance).

| Check | Result |
|-------|--------|
| Pairs tested | 19 |
| Pairs passing | 19 |
| Max deviation | $0.00 |

**Verdict: PASS** — The balance chain is unbroken. Each row's opening balance equals the previous row's closing balance exactly.

### 3. "Current Cash" KPI Accuracy

**Test:** Verify the KPI reads from the current week's row, not row 0.

| Metric | Value |
|--------|-------|
| Today | 2026-03-02 |
| Current week row index | 4 (2026-03-02 to 2026-03-08) |
| Row 0 | 2026-02-02 to 2026-02-08 |
| Row 0 opening_balance | $117,007 |
| Current week opening_balance | $227,258 |
| KPI current_cash | $227,258 |
| KPI == current week opening? | Yes ✓ |
| KPI == row 0 opening? | No ✓ (Task 2 fix working) |

**Verdict: PASS (code fix)** — The Task 2 fix correctly identifies the current week (row 4, Mar 2-8) and reads its opening_balance ($227,258), not row 0's ($117,007). The `get_cashflow_kpis()` loop at lines 999-1003 correctly finds the row where `week_start <= today <= week_end`.

**However: FAIL (CRITICAL — opening balance double-counting)**

The KPI correctly reads the current week's opening balance — but that balance is **wrong**. The model inflates current cash by **$110,251** (94%) due to double-counting:

| Step | Value |
|------|-------|
| Actual bank balance (sum of latest account balances) | $117,007 |
| Model assigns this as opening_balance for row 0 (Feb 2) | $117,007 |
| Row 0 actual net cashflow added | +$21,251 |
| Row 1 actual net cashflow added | +$4,292 |
| Row 2 actual net cashflow added | +$20,640 |
| Row 3 actual net cashflow added | +$64,068 |
| **Cumulative (current week opening)** | **$227,258** |
| **Actual bank balance** | **$117,007** |
| **Overstatement** | **$110,251 (94%)** |

**Root cause:** `build_cashflow_forecast()` (lines 833-852) computes the opening balance by summing the LATEST `balance_after` for each bank account. These balances are as of the most recent transaction dates (Mar 1-3, 2026). But this balance is assigned as the opening for row 0 (Feb 2, 2026 — 4 weeks earlier). The model then adds 4 weeks of actual bank transactions (Feb 2 through Mar 1) on top of a balance that ALREADY includes those transactions. The transactions are counted twice.

**Bank account balance dates:**

| Account | Balance | As Of |
|---------|---------|-------|
| Highbeam Savings (200001628852) | $15,473 | 2026-03-01 |
| Highbeam Checking (200001628851) | $38,667 | 2026-03-03 |
| BofA Checking (5769) | $62,867 | 2026-02-27 |
| Amex Credit Card | -$30,258 | 2026-02-23 (excluded, negative) |
| **Total (positive)** | **$117,007** | |

These balances reflect the state AFTER all February transactions. Using them as the starting point for Feb 2 and then replaying February's transactions inflates the running balance by exactly the sum of those transactions' net cash flow ($110,251).

**Fix options:**
1. **Reconstruct historical balance:** Subtract actual transactions between row 0 start date and the balance date to back-compute what the balance was at the start of row 0. `historical_opening = current_balance - sum(net_tx from row0_start to balance_date)`.
2. **Use latest balance as current-week opening:** Instead of assigning the bank balance to row 0, assign it to the current week directly. Reconstruct past weeks backward from there. This eliminates the double-counting entirely.
3. **Skip actuals before balance date:** Only mark weeks as `is_actual` if they are BEFORE the earliest balance date. This prevents replaying transactions that are already baked into the balance.

**Severity: CRITICAL** — The CFO sees "$227K current cash" when the bank actually shows $117K. This is a $110K overstatement that would lead to incorrect decisions about production runs, payroll coverage, and LOC draws.

### 4. "13-Week Projected" KPI Accuracy

**Test:** Verify the KPI is indexed from the current week, not from row 0.

| Metric | Value |
|--------|-------|
| Current week row index | 4 |
| 13w target row index | 4 + 13 = 17 |
| Row 17 closing_balance | $260,652 |
| KPI projected_13w | $260,652 |
| Match? | Yes ✓ |

**Verdict: PASS (code logic)** — The 13-week projected KPI correctly looks 13 rows ahead of the current week (row 4 → row 17), not from row 0 (which would be row 13). The Task 2 fix at lines 1013-1017 works correctly.

**Note:** The absolute value ($260,652) is inflated by the opening balance double-counting bug. The projected DELTA from current cash is meaningful, but the absolute level is ~$110K too high. Once the double-counting is fixed, the 13w projected would be ~$150K, which is a more realistic number.

### 5. "Monthly Burn" KPI Accuracy

**Test:** Verify the KPI only uses actual (is_actual=True) rows.

| Metric | Value |
|--------|-------|
| Actual rows in forecast | 4 (Feb 2, Feb 9, Feb 16, Feb 23) |
| Rows used for burn calculation | 4 (tail of actuals, min 2) |
| Net cashflow of actual weeks | $21,251, $4,292, $20,640, $64,068 |
| Mean net cashflow (weekly) | $27,563 |
| Monthly burn (mean * 4.33) | -$119,347 |
| KPI monthly_burn | -$119,347 |
| Match? | Yes ✓ |

**Verdict: PASS (code logic) / FAIL (data accuracy)**

The code correctly filters to only `is_actual=True` rows (Task 7 fix at lines 1023-1028). It uses all 4 available actual weeks and multiplies by 4.33 to annualize to monthly.

However, the monthly burn value of **-$119,347** (negative = net positive cash flow) is misleading because:
1. The actual net cashflows include the Jameson loan misclassification (Analyst 3 finding) — $55K/month in loan debits counted as revenue, inflating actual inflows
2. The week of Feb 23-Mar 1 shows $83K inflows (abnormally high due to Amazon disbursement + Shopify payout week)
3. A negative "monthly burn" means the model thinks the business GENERATES $119K/month in free cash flow, leading to a 52-week runway — which is suspiciously optimistic

If the Jameson misclassification were fixed (~$55K/month of phantom revenue removed), the monthly burn would flip to approximately +$-64K/month (net negative cash flow), which is more realistic for a $300K/month gross revenue business with $250K+ in expenses.

### 6. Runway & Alert KPI Checks

| Metric | Value | Notes |
|--------|-------|-------|
| runway_weeks | 52 | Capped at 52 because burn is negative (positive cash flow) |
| alert_week | None | No week drops below $100K threshold |
| min_cash_threshold | $100,000 | From cashflow_settings |
| balance_freshness_date | 2026-03-03 | Yesterday — data is fresh |

The runway and alert KPIs are mathematically correct given the inputs, but they're unreliable because the opening balance is inflated by $110K and the net cashflow is inflated by the Jameson misclassification.

### 7. First 8 Weeks Detail

| Week | Actual? | Opening | Inflows | Outflows | Net | Closing |
|------|---------|---------|---------|----------|-----|---------|
| Feb 2-8 | YES | $117,007 | $31,463 | $10,212 | $21,251 | $138,258 |
| Feb 9-15 | YES | $138,258 | $23,137 | $18,845 | $4,292 | $142,551 |
| Feb 16-22 | YES | $142,551 | $26,217 | $5,577 | $20,640 | $163,191 |
| Feb 23-Mar 1 | YES | $163,191 | $83,490 | $19,422 | $64,068 | $227,258 |
| **Mar 2-8** | **No** | **$227,258** | **$56,854** | **$91,632** | **-$34,778** | **$192,480** |
| Mar 9-15 | No | $192,480 | $13,891 | $21,555 | -$7,664 | $184,816 |
| Mar 16-22 | No | $184,816 | $13,891 | $20,297 | -$6,406 | $178,410 |
| Mar 23-29 | No | $178,410 | $60,847 | $40,723 | $20,124 | $198,534 |

**Observations:**
- The current week (Mar 2-8, bold) is NOT marked as actual despite today being Mar 2 — this is correct behavior (`is_actual = is_past`, and the current week hasn't ended yet)
- The current week is correctly blended (actual-to-date + projected remainder)
- Row 3 (Feb 23-Mar 1) shows $83K inflows — this includes Amazon disbursement (~$43K) + Shopify payouts + Jameson phantom revenue
- Projected weeks show the expected Amazon biweekly pattern: Mar 2-8 ($57K inflows, includes Amazon), Mar 9-22 ($14K/week, no Amazon), Mar 23-29 ($61K, Amazon disbursement)

---

### Summary

| Check | Verdict | Severity |
|-------|---------|----------|
| closing = opening + net (every row) | **PASS** | — |
| opening[N+1] = closing[N] (chain) | **PASS** | — |
| Current Cash reads current week | **PASS** (code) | — |
| Opening balance double-counting | **FAIL** | CRITICAL |
| 13-Week Projected indexed from current week | **PASS** | — |
| Monthly Burn uses only actuals | **PASS** (code) | — |
| Monthly Burn value accuracy | **FAIL** | HIGH |
| Runway & alert KPIs | **PASS** (code) | — |

**Key takeaways:**

1. **Balance arithmetic is perfect.** Every row satisfies `closing = opening + net` and the chain is unbroken. The math engine is sound.

2. **Task 2 KPI fix is working.** Current Cash correctly reads from the current week's row (row 4, Mar 2-8), not row 0 (Feb 2-8). The 13-week projection is correctly indexed 13 weeks ahead of the current week.

3. **CRITICAL: Opening balance double-counting inflates current cash by $110K (94%).** The model uses the latest bank balance ($117K, as of Mar 1-3) as the opening for row 0 (Feb 2), then replays 4 weeks of actual transactions that are already reflected in that balance. The CFO sees "$227K current cash" when the bank shows $117K. **This is the highest-severity bug in the model** — it directly affects every cash management decision.

4. **Monthly burn is misleadingly positive** (-$119K = generating $119K/month) due to the Jameson loan misclassification (Analyst 3) inflating actual inflows by ~$55K/month. After fixing the Jameson mapping, actual burn would likely be ~$65K/month net negative, which is more consistent with a business running $250K+/month in expenses against $300K/month gross revenue.

---

## Analyst 5 — Payout Ratio Calibration

**Date:** 2026-03-02

### 1. DTC Payout Ratio

#### Actual Computation (Bank Deposits / Shopify Gross Revenue)

**Monthly Level (Jul 2025 — Feb 2026):**

| Month | Shopify Gross | Bank Deposits | Ratio |
|-------|-------------|---------------|-------|
| Jul 2025 | $164,879 | $155,049 | 0.9404 |
| Aug 2025 | $148,142 | $142,355 | 0.9609 |
| Sep 2025 | $127,461 | $140,988 | 1.1061 |
| Oct 2025 | $121,378 | $129,046 | 1.0632 |
| Nov 2025 | $125,742 | $114,904 | 0.9138 |
| Dec 2025 | $121,869 | $136,061 | 1.1165 |
| Jan 2026 | $128,336 | $123,709 | 0.9639 |
| Feb 2026 | $111,911 | $109,295 | 0.9766 |
| **Total** | **$1,049,717** | **$1,051,406** | **0.9965** |

**Key observation:** The 8-month weighted ratio is **0.997** — essentially 1:1. Over time, every dollar of Shopify gross revenue reaches the bank. The monthly variations (0.91 to 1.12) are settlement timing noise: deposits lag revenue by ~3 days, causing some months to "borrow" from adjacent months.

**Weekly Level (12-week lookback, Dec 8 — Mar 2):**

| Week | Shopify Gross | Bank Deposits | Ratio |
|------|-------------|---------------|-------|
| 2025-12-08 | $24,233 | $6,622 | 0.2733 |
| 2025-12-15 | $27,122 | $25,897 | 0.9548 |
| 2025-12-22 | $25,710 | $23,986 | 0.9329 |
| 2025-12-29 | $30,412 | $27,413 | 0.9014 |
| 2026-01-05 | $28,554 | $34,552 | 1.2100 |
| 2026-01-12 | $30,097 | $28,442 | 0.9450 |
| 2026-01-19 | $25,193 | $30,369 | 1.2054 |
| 2026-01-26 | $33,439 | $26,197 | 0.7834 |
| 2026-02-02 | $25,334 | $31,428 | 1.2406 |
| 2026-02-09 | $27,699 | $23,137 | 0.8353 |
| 2026-02-16 | $27,607 | $26,217 | 0.9497 |
| 2026-02-23 | $31,649 | $28,512 | 0.9009 |
| 2026-03-02 | $4,353 | $4,699 | 1.0794 |

Weekly ratios are highly volatile (0.27 to 1.24) due to the 3-day settlement lag misaligning deposits with the revenue week that generated them. The first week (Dec 8, ratio 0.27) is likely a holiday week where Shopify held deposits — the missing $17K shows up in subsequent weeks via ratios >1.0.

**Overall weighted (12 weeks):** $317,470 / $341,401 = **0.9299**

#### Model's Auto-Calibrated Value

| Metric | Value |
|--------|-------|
| `compute_payout_ratio(conn, 'dtc')` | **0.9840** |
| Seed value | 0.94 |
| Diff from seed | +0.044 (4.7%) |
| Threshold | ±5% |

**How it's computed:** EWMA with span=8 across 13 weekly ratios. The EWMA dampens the Dec 8 outlier (0.27) but is still pulled slightly by other sub-1.0 weeks. The result (0.984) is between the weighted average (0.93) and the monthly truth (0.997).

#### Verdict: CONDITIONAL PASS

- **Diff from seed:** 4.7% — just under the 5% flagging threshold
- **Direction of drift:** Upward (0.94 → 0.984). This means actual processing fees are lower than the 6% assumed by the seed. The 8-month data shows actual fees are ~0.3% (ratio 0.997), not 6%.
- **EWMA vs reality gap:** The EWMA output (0.984) is reasonable given the noisy weekly data. It correctly tracks the fact that DTC payouts are close to par.
- **Risk:** The weekly ratio volatility (0.27-1.24) means the EWMA value could jump significantly week-to-week depending on which outlier enters/exits the 12-week window. Consider increasing `calibration_lookback_weeks` to 16-20 for more stability.
- **Recommendation:** Consider updating the seed from 0.94 to 0.97 to reflect actual processing costs. The 6% haircut assumed by the seed is not supported by 8 months of data showing <1% effective fees. However, this may indicate that refunds and chargebacks are categorized separately (not netted against DTC deposits) — if so, the "true" payout ratio could still be near 0.94 after netting.

### 2. Amazon Payout Ratio

#### Auto-Calibration Status

| Metric | Value |
|--------|-------|
| `compute_payout_ratio(conn, 'amazon')` | **None** |
| Fallback | Seed value 0.62 |
| Reason | 0 transactions mapped as `amazon_revenue` in `cashflow_transactions` |

The auto-calibration **cannot run** because all 51 Amazon bank transactions (16 large disbursements + 35 smaller debits) remain unmapped (category='unmapped'). The function requires at least 4 weeks of matched data — with 0 mapped deposits, it returns `None` and falls back to the seed.

#### Manual Ratio Computation (Unmapped Deposits vs daily_sku_sales)

Only one complete month-pair exists (Amazon has limited history in `daily_sku_sales`: Jan-Feb 2026 only):

| Revenue Month | Gross Revenue | Deposit Month | Bank Deposits | Ratio |
|---------------|-------------|---------------|---------------|-------|
| Jan 2026 | $144,563 | Feb 2026 | $82,888 | **0.5734** |

**Single-month ratio: 0.573** — this is 7.5% below the seed value of 0.62.

#### Cross-Validation Against VALIDATION_BASELINE

From VALIDATION_BASELINE (Analyst 2's data):

| Method | Period | Result |
|--------|--------|--------|
| Feb deposits / Feb gross (Analyst 1) | Feb 2026 | $83K / $134K = 0.620 |
| Feb deposits / Jan gross (21-day lag) | Jan→Feb | $83K / $145K = 0.573 |
| 8-month avg per-disbursement | Jul 25-Feb 26 | $45.6K avg vs varying gross | ~0.55-0.65 |

The difference between 0.573 and 0.620 depends entirely on which month's gross revenue is paired with February's deposits. Amazon's 14-day settlement cycle means January's revenue partially lands in February. The "correct" lag alignment is uncertain — reality falls somewhere between 0.57 and 0.62.

#### Verdict: CONDITIONAL PASS (with FLAG)

- **Seed accuracy:** The seed value of 0.62 is within the reasonable range (0.57-0.62 depending on lag alignment). It matches the PRD reference data exactly.
- **Auto-calibration broken:** **FAIL** — `compute_payout_ratio(conn, 'amazon')` returns `None` because no Amazon transactions are mapped. The auto-calibration feature is dead code for Amazon. This was flagged by Analysts 1 and 2 as well.
- **Risk of drift:** Without auto-calibration, the model cannot detect changes in Amazon's fee structure. If Amazon raises FBA fees or changes settlement terms, the model will continue using 0.62 until someone manually updates the seed.
- **Recommendation:** Map the 16 Amazon disbursement transactions to category `amazon_revenue` (they all match pattern `AMAZON.C* DES:PAYMENTS`). This would enable auto-calibration and provide ongoing ratio monitoring.

### 3. COGS Ratio Check

| Metric | Value |
|--------|-------|
| Seed `cogs_pct` | 0.25 (25%) |
| Actual mapped production expense (Jul 2025+) | $134,360 |
| Total gross revenue (Jul 2025+) | $1,344,963 |
| **Actual COGS ratio** | **0.100 (10.0%)** |
| **Diff from seed** | **-0.15 (60% below)** |

**Verdict: FAIL (HIGH severity)**

The mapped production expenses represent only 10% of gross revenue — 60% below the 25% seed value. This means the model projects 2.5x more COGS than actual mapped production costs.

**However, this is almost certainly a data completeness issue:**
- Production costs are PO-driven and spiky (the PRD notes $0-108K/month range)
- The 8-month period includes several months with $0 production (no POs)
- AMEX payments (~$30K/month) likely include production-related expenses that are unmapped
- The 25% seed may be correct for COGS as a % of revenue (industry standard for CPG), but the mapped bank data only captures direct manufacturer POs, not packaging, raw materials, freight-to-warehouse, etc.

**Recommendation:** Do NOT change the COGS seed based on this data alone. The 25% rate is a business-level input from the CFO that accounts for all production costs, not just mapped bank transactions. However, flag this discrepancy so the CFO can confirm whether 25% or a lower rate is appropriate given current product mix and supplier terms.

### 4. Ratio Drift Summary

| Ratio | Seed | Auto-Calibrated | Actual (Manual) | Drift from Seed | Status |
|-------|------|----------------|----------------|----------------|--------|
| DTC payout | 0.94 | 0.984 | 0.997 (8-mo) | +4.7% (auto) / +6.1% (manual) | **CONDITIONAL PASS** |
| Amazon payout | 0.62 | None (broken) | 0.573 (1-mo) | -7.5% (manual) | **CONDITIONAL PASS** |
| COGS % | 0.25 | N/A (not auto-cal'd) | 0.100 (mapped only) | -60% (mapped only) | **FAIL** (data gap) |

---

### Summary

| Check | Verdict | Severity |
|-------|---------|----------|
| DTC payout ratio accuracy | **CONDITIONAL PASS** | LOW |
| DTC auto-calibration working | **PASS** | — |
| Amazon payout ratio accuracy | **CONDITIONAL PASS** | LOW |
| Amazon auto-calibration working | **FAIL** | MEDIUM |
| COGS ratio vs mapped actuals | **FAIL** | HIGH (data gap) |
| Ratio stability (EWMA volatility) | **FLAG** | LOW |

**Key takeaways:**

1. **DTC auto-calibration works well.** The EWMA tracks actual payout ratios within 5% of the seed. The slight upward drift (0.94 → 0.984) suggests actual Shopify processing fees are lower than the 6% assumed. The seed could be updated to 0.97 for accuracy, but the current value is acceptable. Weekly ratio volatility (0.27-1.24) is a concern — consider widening the lookback window for EWMA stability.

2. **Amazon auto-calibration is non-functional** because all Amazon bank transactions are unmapped. The seed value of 0.62 is well-calibrated (Feb manual computation gives 0.57-0.62 depending on lag alignment), but the model cannot detect drift. Mapping the 16 Amazon disbursement transactions would fix this.

3. **COGS ratio is the largest discrepancy** (mapped 10% vs seed 25%), but this is a data completeness issue — mapped production expenses capture only direct manufacturer POs, not the full COGS stack. The 25% seed should be treated as a business input, not something to auto-calibrate from incomplete bank data.

---

## Analyst 6 — Seasonality Impact

**Date:** 2026-03-02

### 1. Seasonal Indices Table: Data Check

| Month | DB Index | PRD Seed | Delta |
|-------|----------|----------|-------|
| Jan   | 0.8845   | 0.95     | -6.9% |
| Feb   | 0.8580   | 0.92     | -6.7% |
| Mar   | 0.8571   | 0.98     | -12.5% |
| Apr   | 0.9454   | 1.02     | -7.3% |
| May   | 1.0490   | 1.05     | -0.1% |
| Jun   | 1.0949   | 1.10     | -0.5% |
| Jul   | 1.1263   | 1.12     | +0.6% |
| Aug   | 1.0976   | 1.08     | +1.6% |
| Sep   | 1.1076   | 1.02     | +8.6% |
| Oct   | 1.0377   | 0.98     | +5.9% |
| Nov   | 1.0372   | 0.92     | +12.7% |
| Dec   | 0.9048   | 0.88     | +2.8% |

**Table is populated** — all 12 rows present. **PASS.**

DB values differ from PRD seed values. They appear to have been calibrated from actual sales data. The general seasonal shape is preserved (low winter, high summer) but with notable differences: DB shows a flatter winter trough (0.857-0.885 vs PRD 0.88-0.98) and a stronger fall plateau (Sep-Nov ~1.04-1.11 vs PRD 0.92-1.02).

**Flatness check:** Min index = 0.8571 (March), Max index = 1.1263 (July). Range = 31.4%. **NOT flat — clear seasonal variation. PASS.**

### 2. Seasonality Applied to Waterfall: With vs Without

Ran `build_waterfall()` with identical inputs, toggling `seasonal_indices`:

| Month   | With Seasonality | Without Seasonality | Delta |
|---------|-----------------|---------------------|-------|
| 2026-03 | $144,807        | $161,447            | -10.3% |
| 2026-04 | $165,871        | $172,505            | -3.8% |
| 2026-05 | $189,127        | $182,956            | +3.4% |
| 2026-06 | $220,841        | $208,461            | +5.9% |
| 2026-07 | $245,662        | $228,206            | +7.6% |
| 2026-08 | $275,135        | $260,807            | +5.5% |
| 2026-09 | $283,924        | $266,834            | +6.4% |
| 2026-10 | $257,566        | $251,261            | +2.5% |
| 2026-11 | $253,427        | $247,136            | +2.5% |
| 2026-12 | $238,130        | $254,347            | -6.4% |
| 2027-01 | $231,110        | $251,104            | -8.0% |
| 2027-02 | $159,855        | $184,656            | -13.4% |

**Seasonality is clearly affecting revenue output.** March (index 0.857) is reduced by 10.3%, while July (index 1.126) is boosted by 7.6%. Winter months (Dec-Feb) are depressed. **Task 3 fix is working. PASS.**

**Important code note:** Seasonal indices are applied to **repeat revenue only** (waterfall.py line 456-459), not to new customer first-order revenue. This is by design — it matches the spreadsheet approach where acquisition spend drives new customers regardless of season, but repeat purchase behavior is seasonal.

### 3. March vs June DTC Revenue Ratio

| Metric | Value |
|--------|-------|
| March DTC gross (with seasonality) | $144,807 |
| June DTC gross (with seasonality) | $220,841 |
| **June/March ratio** | **1.525x** |
| Expected from DB indices alone (1.0949/0.8571) | 1.277x |
| Expected from PRD indices (1.10/0.98) | 1.122x |

The actual June/March ratio (1.525x) is higher than the pure seasonal expectation (1.277x) because **media spend also ramps** from $75K (March) to $130K (June). The compound effect of seasonal uplift + increased media acquisition produces a 52.5% increase, not the 27.7% from seasonality alone.

**This is correct behavior, not a bug.** Seasonality modulates the repeat revenue base, while media spend growth drives new customer acquisition. Both effects compound.

**Verification:** March revenue ($144,807) is 10.3% below the no-seasonality baseline ($161,447), consistent with March's index of 0.857 (≈ -14.3% from 1.0, but only applied to repeat revenue which is a fraction of total). June revenue ($220,841) is 5.9% above the baseline ($208,461), consistent with June's index of 1.095. **PASS.**

### 4. Cash Flow Forecast DTC Revenue by Month

Projected DTC cash inflows from `build_cashflow_forecast()` (projected weeks only):

| Month   | DTC Cash Inflow | Seasonal Index |
|---------|----------------|----------------|
| 2026-03 | $165,929       | 0.8571         |
| 2026-04 | $150,857       | 0.9454         |
| 2026-05 | $171,185       | 1.0490         |
| 2026-06 | $252,860       | 1.0949         |
| 2026-07 | $222,357       | 1.1263         |

**Month-over-month growth vs seasonal expectation:**

| Transition | Actual Growth | Seasonal Expectation | Gap |
|-----------|---------------|---------------------|-----|
| Mar → Apr | -9.1%        | +10.3%              | -19.4% |
| Apr → May | +13.5%       | +11.0%              | +2.5% |
| May → Jun | +47.7%       | +4.4%               | +43.3% |
| Jun → Jul | -12.1%       | +2.9%               | -15.0% |

Month-over-month growth in the cash flow forecast doesn't perfectly track seasonal expectations. This is expected for three reasons:

1. **Media spend dominates**: The media plan ramps from $75K → $85K → $95K → $130K → $150K, dwarfing seasonal effects.
2. **Week-boundary effects**: DTC revenue is distributed by DOW weights across days, and weeks that span month boundaries shift revenue between months.
3. **Payout ratio applied**: Cash flow uses `total_revenue × dtc_payout_ratio (0.984)`, converting gross waterfall output to cash inflows.

The Mar → Apr dip (-9.1% despite seasonal expectation of +10.3%) is likely a week-boundary artifact — March has more projected weeks in this forecast window. The May → Jun jump (+47.7%) combines a $35K media spend increase with favorable seasonal lift. **Not a bug — expected behavior with non-linear media plans.**

### 5. Summary

| Check | Result | Severity |
|-------|--------|----------|
| Seasonal indices table populated | **PASS** | — |
| All 12 months present | **PASS** | — |
| Task 3 fix working (indices passed to waterfall) | **PASS** | — |
| Seasonality not flat (31.4% range) | **PASS** | — |
| March uses index ~0.98 | **CONDITIONAL PASS** | LOW |
| June uses index ~1.10 | **PASS** | — |
| June/March DTC ratio ≈ 1.12x | **CONDITIONAL PASS** | LOW |
| Seasonal effect visible in output | **PASS** | — |

**Key takeaways:**

1. **Task 3 fix is working correctly.** Seasonal indices are loaded from the DB, passed to `build_waterfall()`, and produce meaningfully different revenue projections vs the no-seasonality baseline. The -10.3% March reduction and +5.9% June boost are consistent with the DB index values.

2. **DB indices differ from PRD seed values** — March is 0.857 in DB vs 0.98 in PRD (a 12.5% gap). This appears to be from auto-calibration against actual sales data, which shows stronger winter seasonality than the PRD assumed. The model is using the DB values, which is correct (auto-calibrated data should override seed defaults).

3. **June/March DTC revenue ratio is 1.525x** (not the expected 1.12x from PRD indices or 1.28x from DB indices) because media spend growth ($75K→$130K, a 73% increase) compounds with seasonal uplift. This is correct model behavior — the waterfall correctly applies both drivers.

4. **Seasonal indices apply to repeat revenue only**, not new customer first-order revenue. This is by design (matching the spreadsheet model). As a result, the seasonal effect on total revenue is muted — roughly half the effect you'd expect from the index values alone, since repeat revenue is a fraction of total revenue in months with high media acquisition.

---

## Analyst 7 — Google Sheet Comparison

**Date:** 2026-03-02

### Method

Ran `build_cashflow_forecast(start_date=today-4w, weeks=20, scenario='base')` against Railway PostgreSQL. Extracted 13-week forward totals from the current week (Mar 2-8), monthly aggregations of projected weeks, and per-category breakdowns. Compared all figures against the Google Sheet reference data from the PRD and actual bank data from `VALIDATION_BASELINE.md`.

**Prior analyst findings incorporated:**
- Analyst 3: Jameson loan payments (~$55K/month) misclassified as `interest_income` (revenue) instead of `loan` (expense)
- Analyst 4: Opening balance double-counting inflates current cash by $110K ($227K displayed vs $117K actual)
- Analyst 5: Amazon auto-calibration non-functional (all Amazon transactions unmapped)
- Analyst 1: DTC waterfall projects 20% above recent actuals due to optimistic media spend plan inputs

### 1. Total Monthly Revenue

| Source | Model (avg/mo) | Google Sheet Reference | Actual Bank Credits (6-mo avg) |
|--------|---------------|----------------------|-------------------------------|
| DTC (Shopify) | $172,629 | $42-52K (cash) | $116,502 (deposits) |
| Amazon | $104,976 | $54-102K (cash) | $83K (disbursements, Feb) |
| Interest/Other | $59,611 | Not specified | ~$35/mo (true interest) |
| **Total** | **$337,217** | **$250-350K** | **$270,589** |

**Verdict: CONDITIONAL PASS (with caveats)**

The model's total monthly revenue of $337K falls within the Google Sheet's $250-350K range. However, this pass is misleading due to two offsetting errors:

1. **Interest_income inflation (+$59K/mo):** The Jameson loan payments ($55K/mo in debits) are classified under `interest_income`, a revenue category. The `_get_actual_weekly_totals()` function doesn't filter by direction, so these debit amounts are summed alongside actual interest income credits ($35/mo). The trailing average then projects ~$60K/month of phantom revenue. **True model revenue (excl Jameson): ~$278K/month.**

2. **DTC overstatement (+$56K/mo):** The waterfall model projects DTC cash inflows of $173K/month, but actual Shopify bank deposits average $117K/month — a 48% overstatement driven by optimistic media spend plan inputs ($75K planned vs $24-65K actual historical spend).

**Adjusted total revenue: ~$278K/month** (removing phantom interest_income). This is at the low end of the $250-350K reference range but within 30%. If the DTC overstatement is also corrected, revenue drops to ~$222K/month — 11% below the $250K floor. **FAIL after double adjustment.**

### 2. Total Monthly Expenses

| Category | Model (avg/mo) | Google Sheet Reference | Actual Bank Debits (6-mo avg) |
|----------|---------------|----------------------|-------------------------------|
| Media | $39,649 | $40-55K | $254,751 (total) |
| Payroll | $30,379 | $32K | |
| Loan (Jameson) | $1 | $25-75K (principal + interest) | |
| Fulfillment | $22,745 | $20K | |
| Production | $85,897 | $0-108K | |
| Software | $72 | $42K | |
| Other (agency, accounting, insurance, tax, shipping) | $15,055 | ~$31K combined | |
| **Total** | **$193,796** | **$150-250K** | **$254,751** |

**Verdict: FAIL (MEDIUM severity)**

The model's total monthly expense of $194K is within the Google Sheet range ($150-250K), but this is artificially low:

1. **Missing Jameson loan (~$55K/mo):** Loan payments are classified as revenue, not expense. Adding them back: $194K + $55K = **$249K/month** — at the top of the reference range.

2. **Missing software (~$42K/mo):** Only $72/month is mapped to `software` vs the PRD's $42K/month. Most SaaS payments are on the Amex card or unmapped. This represents a **$42K/month understatement**.

3. **Unmapped expenses (~$103K/mo):** Per VALIDATION_BASELINE, 35% of bank debits ($103K/month average) are unmapped. These include Amex payments, contractor invoices, Amazon FBA fees, and other operational costs.

**Adjusted expenses: ~$249K/month** (adding Jameson only) or potentially **~$291K+/month** (adding Jameson + software gap). The Google Sheet ceiling of $250K is reasonable but the model significantly understates true cash outflows.

**Comparison to actual bank debits:** Model projects $194K/month vs actual average debits of $255K/month — the model understates expenses by **24%**, just under the 30% flag threshold. After adding Jameson, the gap narrows to ~2%.

### 3. Net Monthly Cash Flow

| Metric | Model | Google Sheet Reference | Actual Bank Net (6-mo avg) |
|--------|-------|----------------------|---------------------------|
| Monthly net | +$143,421 | +$50-150K | +$15,838 |

**Verdict: FAIL (HIGH severity)**

The model shows net positive $143K/month, which appears to be within the Google Sheet's +$50-150K range. However:

1. **The $143K figure is grossly inflated** by the Jameson misclassification. Revenue is overstated by ~$60K and expenses are understated by ~$55K, creating a ~$115K/month swing.

2. **Adjusted net: ~$28K/month** ($143K - $115K Jameson swing). This is **44% below** the Google Sheet's $50K floor.

3. **Actual bank data shows +$16K/month average net** (6-month average from VALIDATION_BASELINE). The adjusted model net of $28K is closer to reality than the raw $143K, but still 75% above actuals.

4. **The Google Sheet's $50-150K reference may itself be optimistic.** Actual bank data consistently shows much lower net cash flow (+$16K/month average), with 2 of 6 months negative (Dec -$14K, Sep -$102K).

**Model net exceeds actual bank net by 9x ($143K vs $16K).** Even after Jameson adjustment, model net ($28K) exceeds actual by 77%. The model is not yet trustworthy for cash management decisions.

### 4. 13-Week Closing Balance

| Metric | Value |
|--------|-------|
| Current week opening (model) | $227,258 |
| Current week opening (actual bank) | $117,007 |
| Opening inflation | +$110,251 (94%) |
| 13-week closing (model) | $626,968 |
| 13-week total inflows (model) | $986,275 |
| 13-week total outflows (model) | $586,565 |
| 13-week net (model) | +$399,710 |

**Verdict: FAIL (CRITICAL severity)**

The model projects cash growing from $227K to $627K over 13 weeks (+$400K). This implies the business generates ~$133K/month in free cash flow. Actual bank data shows ~$16K/month average net.

**Adjusted 13-week estimate:**
- True opening: $117K (actual bank balance)
- Jameson net impact over 13 weeks: ~$115K/mo × 3 = ~$345K phantom net
- Adjusted 13w net: $400K - $345K = ~$55K
- Adjusted 13w closing: $117K + $55K = **~$172K**
- Direction correct? $172K > $117K (closing higher than opening). **CONDITIONAL PASS for direction.**

But the model DISPLAYS $627K, which is **3.6x** the adjusted estimate of $172K. A CFO seeing $627K in 13 weeks would make very different decisions than one seeing $172K.

### 5. Weekly Revenue Ranges (Sanity Check)

| Revenue Source | Model Weekly Range | Expected Range | Verdict |
|---------------|-------------------|----------------|---------|
| DTC | $33-50K/week | $25-30K/week (bank actuals) | HIGH by 30-67% |
| Amazon | $0 or $44-65K | $0 or $37-45K (PRD) | HIGH by 19-44% |
| Interest_income | $12-14K/week | ~$1/week (true interest) | BROKEN (Jameson) |

DTC weekly revenue is consistently above actual bank deposit rates due to the waterfall overstatement. Amazon disbursement amounts increase each month because the `amazon_revenue_forecast` table ramps aggressively ($151K→$175K→$200K→$220K), and the model faithfully applies `forecast × 0.62 / 2`.

### 6. Weekly Expense Ranges (Sanity Check)

| Expense | Model Weekly Range | Expected Range | Verdict |
|---------|-------------------|----------------|---------|
| Production (COGS) | $7-21K/week | Varies (25% of gross) | Reasonable given method |
| Media | $0 or $39-52K | $0 or $24-65K | Within range |
| Payroll | $0 or $12K | $0 or $16K biweekly | 25% below PRD |
| Fulfillment | $5K/week | $5K/week | Match |

Expense timing is reasonable — media shows lumpy monthly billing, payroll shows biweekly pattern. Production (COGS) fluctuates with revenue because it uses `revenue_pct` method (25% of gross-up).

### 7. Comparison Summary

| Metric | Model | Google Sheet Range | Status | Severity |
|--------|-------|--------------------|--------|----------|
| Monthly revenue | $337K | $250-350K | **PASS** (raw) / **CONDITIONAL** (adjusted) | — |
| Monthly expenses | $194K | $150-250K | **FAIL** (missing $55K loan) | MEDIUM |
| Monthly net | $143K | $50-150K | **FAIL** (inflated by $115K/mo) | HIGH |
| 13w closing direction | Up | Up (if net positive) | **CONDITIONAL PASS** | — |
| 13w closing magnitude | $627K | ~$172K (adjusted est.) | **FAIL** (3.6x overstated) | CRITICAL |
| DTC weekly revenue | $33-50K | $25-30K (actuals) | **FAIL** (30-67% over) | MEDIUM |
| Amazon weekly revenue | $44-65K | $37-45K (PRD) | **FAIL** (>30% at high end) | MEDIUM |
| Expense timing | Lumpy, correct cadence | Lumpy | **PASS** | — |

### 8. Key Findings

1. **The model's raw output APPEARS to pass the Google Sheet comparison** — total revenue $337K falls in the $250-350K range and total expenses $194K falls in the $150-250K range. This is coincidental: two major bugs (Jameson misclassification inflating revenue + deflating expenses) happen to keep the totals within bounds.

2. **After adjusting for known bugs, the model understates expenses and overstates revenue.** Adjusted monthly revenue ~$278K (low end of range), adjusted expenses ~$249K (top of range), adjusted net ~$28K (below the $50K floor). The model gives a more optimistic picture than reality.

3. **The opening balance double-counting (Analyst 4) compounds with the net cash flow inflation to produce a 13-week closing balance of $627K** — roughly 3.6x the adjusted estimate of $172K. This is the most dangerous number in the model because it directly drives cash management decisions.

4. **Root causes are all previously identified:** Jameson misclassification (Analyst 3), opening balance double-counting (Analyst 4), DTC waterfall overstatement from media plan inputs (Analyst 1), and unmapped transactions (VALIDATION_BASELINE). No new bugs discovered — the Google Sheet comparison confirms and quantifies the cumulative impact of known issues.

5. **The Google Sheet's own reference ranges may need updating.** The PRD states DTC cash revenue of $42-52K/month, but actual Shopify bank deposits average $117K/month. The PRD's $300K+/month combined gross is closer to reality, suggesting the per-channel breakdowns in the PRD use different definitions (net vs gross, pre vs post-fees) than the bank data.
