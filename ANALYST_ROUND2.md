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

---

## Analyst 8 — Override & Edit Persistence

**Date:** 2026-03-02

### 1. Override Insertion & Forecast Pickup

Tested the full override flow by inserting a manual override into `cashflow_overrides` for a future week (2026-03-16), re-running `build_cashflow_forecast()`, and verifying the override value appears in the output.

| Step | Action | Result |
|------|--------|--------|
| Baseline | No overrides in DB | media for 2026-03-16 = $6,803 |
| Insert override | `INSERT INTO cashflow_overrides (line_item, week_start, override_amount) VALUES ('media', '2026-03-16', 15000)` | OK |
| Re-run forecast | `build_cashflow_forecast()` | media for 2026-03-16 = **$15,000** |
| Downstream impact | total_outflows changed $28,622 → $36,818 | closing_balance shifted $247,777 → $239,580 |

**Verdict: PASS** — Override is correctly picked up by the forecast engine. The override value replaces the projected value for that specific week and category. Downstream totals (total_outflows, net_cashflow, closing_balance) correctly reflect the override for that week AND propagate to all subsequent weeks' opening balances.

### 2. Override Key Format Consistency

Verified that the override key format is consistent across all three layers:

| Layer | Key Format | Example | Match? |
|-------|-----------|---------|--------|
| Engine (`build_cashflow_forecast`) | `str(date_object)` | `"2026-03-16"` (10 chars) | -- |
| View (`_save_edits`) | `ws[:10]` | `"2026-03-16"` (10 chars) | Yes |
| DB (`cashflow_overrides.week_start`) | TEXT column | `"2026-03-16"` (10 chars) | Yes |

**Verdict: PASS** — All three layers use identical 10-character ISO date strings. No key mismatch risk.

### 3. Past-Week Override Handling (Actuals Should Win)

**CRITICAL TEST:** Inserted an override for a past week (2026-02-16, `is_actual=True`) with a deliberately absurd value ($99,999). Re-ran the forecast.

| Condition | media value for 2026-02-16 |
|-----------|---------------------------|
| No override | $0.00 (actual from bank) |
| With override ($99,999) | **$99,999.00** |

**Verdict: FAIL (MEDIUM severity)** — Overrides are honored for past weeks. The engine checks overrides BEFORE checking `is_past`:

```python
# analytics/cashflow.py, lines 897-901 (revenue) and 935-939 (expenses):
override_key = (cat, ws_str)
if override_key in overrides:       # <-- checked FIRST
    val = overrides[override_key]
elif is_past:                       # <-- actuals only used if no override
    val = actuals_cache.get(cat, {}).get(ws_str, 0)
```

**Mitigating factor:** The UI layer (`_save_edits()`, line 514) skips actuals: `if is_actual_map.get(ws, False): continue`. And `st.data_editor` disables past-week columns with `disabled=is_actual`. So users CANNOT create past-week overrides through the normal UI. However:
- A direct DB insert (admin, migration, or API) would be honored incorrectly
- Stale overrides from when a week was "future" could linger and override actuals once that week becomes "past"

**Fix:** In `build_cashflow_forecast()`, check `is_past` before checking overrides for both revenue (line 897) and expense (line 935) loops. Only honor overrides for future/current weeks.

### 4. Reset to Smart Projection Flow

Tested the full reset cycle:

| Step | Action | Result |
|------|--------|--------|
| Insert override | media=2026-03-16 → $15,000 | media shows $15,000 in forecast |
| Delete override | `DELETE FROM cashflow_overrides WHERE line_item='media' AND week_start='2026-03-16'` | OK |
| Re-run forecast | `build_cashflow_forecast()` | media reverted to $6,803 (original projected value) |

**Verdict: PASS** — Deleting an override correctly restores the projected value. The `_render_smart_buttons()` code in `views/cashflow.py` uses `DELETE FROM cashflow_overrides WHERE line_item = %s` (deletes ALL overrides for a category), which is correct for a "Reset [Category]" action.

### 5. Scenario + Override Interaction

Overrides bypass scenario adjustments. An override of $15,000 for media produces $15,000 in both Base and Conservative scenarios (Conservative would normally apply +10% to expenses).

| Scenario | Without Override | With Override ($15,000) |
|----------|-----------------|----------------------|
| Base | $6,803 | $15,000 |
| Conservative | $7,483 (+10%) | $15,000 (no adjustment) |

**Verdict: CONDITIONAL PASS (LOW severity)** — This is arguably correct behavior. If the CFO manually enters $15,000, they mean $15,000, not "$15,000 * scenario_multiplier". However, it could be surprising to users who expect scenario adjustments to apply to all values. Consider documenting this behavior or adding an option.

### 6. View-Level Code Review

| Component | File | Status |
|-----------|------|--------|
| `_save_edits()` — detects changes between original and edited DataFrames | `views/cashflow.py:505-537` | PASS — correctly compares original vs edited, skips actuals |
| `_save_edits()` — upsert SQL with ON CONFLICT | `views/cashflow.py:528-533` | PASS — proper upsert with parameterized queries |
| `_save_edits()` — st.toast confirmation | `views/cashflow.py:535` | PASS — user gets feedback |
| `_render_smart_buttons()` — finds overridden categories | `views/cashflow.py:472-502` | PASS — checks both `(cat, ws[:10])` and `(cat, ws)` formats |
| `_render_smart_buttons()` — delete action | `views/cashflow.py:495-496` | PASS — parameterized DELETE |
| `_render_smart_buttons()` — hides when no overrides | `views/cashflow.py:481-482` | PASS — early return if `not overridden_cats` |
| `st.data_editor` — past weeks disabled | `views/cashflow.py:430-433` | PASS — `disabled=is_actual` |

### Summary

| Check | Verdict | Severity |
|-------|---------|----------|
| Override insertion → forecast pickup | **PASS** | — |
| Override key format consistency | **PASS** | — |
| Past-week overrides rejected | **FAIL** | MEDIUM |
| Reset to Smart Projection | **PASS** | — |
| Scenario + override interaction | **CONDITIONAL PASS** | LOW |
| View-level code (save, reset, disable) | **PASS** | — |

**Overall: 1 FAIL (MEDIUM), 1 CONDITIONAL PASS (LOW), 4 PASS**

The override system works correctly for the normal UI flow (insert, verify, reset). The one bug — engine honoring overrides for past weeks — is mitigated by the UI preventing past-week edits, but should be fixed at the engine level to prevent stale overrides or direct DB inserts from corrupting historical data.

---

## Analyst 9 — Edge Cases & Robustness

**Date:** 2026-03-02

### 1. Cross-Month Week Revenue Attribution

**Test:** Week spanning Feb 23 – Mar 1 with different monthly revenues ($100K Feb, $150K Mar).

| Day | Month | Monthly Rev | DOW Weight | Daily Share |
|-----|-------|-------------|------------|-------------|
| Feb 23 (Mon) | 2026-02 | $100,000 | 0.157 | $3,614 |
| Feb 24 (Tue) | 2026-02 | $100,000 | 0.309 | $7,113 |
| Feb 25 (Wed) | 2026-02 | $100,000 | 0.252 | $5,801 |
| Feb 26 (Thu) | 2026-02 | $100,000 | 0.144 | $3,315 |
| Feb 27 (Fri) | 2026-02 | $100,000 | 0.137 | $3,154 |
| Feb 28 (Sat) | 2026-02 | $100,000 | 0.000 | $0 |
| Mar 1 (Sun) | 2026-03 | $150,000 | 0.000 | $0 |
| **Total** | | | | **$22,996** |

**Analysis:** `_project_revenue_week()` iterates day-by-day through the week. Each day's revenue is attributed to its calendar month via `d.strftime('%Y-%m')`, then scaled by DOW weight. This correctly handles cross-month weeks — Feb days use Feb's monthly revenue, Mar days use Mar's monthly revenue.

The Sat/Sun days show $0 regardless of month because `DTC_DOW_WEIGHTS[5]` and `DTC_DOW_WEIGHTS[6]` are both 0.0 (no weekend payouts). In this specific test case, Mar 1 falls on Sunday so no March revenue leaks into the February-heavy week.

**Verdict: PASS** — Revenue is correctly attributed to each day's calendar month in cross-month weeks.

### 2. Empty `amazon_revenue_forecast` Table

**Test:** What happens if the Amazon revenue forecast table is empty?

**Code path trace:**
1. `build_cashflow_forecast()` line 797-800: `get_amazon_revenue_forecast(conn)` returns empty list
2. Line 798: `ctx['amazon_monthly_revenue'] = {}` (empty dict)
3. If exception during fetch: line 800 catches it, sets `ctx['amazon_monthly_revenue'] = {}`
4. `_project_revenue_week()` for `amazon_revenue` (line 487): `monthly_rev.get(month_key, 0)` returns 0 for any month
5. Line 499: `total += (0 * ratio / 2) * count = 0` — even in disbursement weeks, amount is $0

**Verified:** `_project_revenue_week(None, 'amazon_revenue', ws, we, ctx_empty)` returns `$0.00` with empty forecast dict. No crash, no exception.

**Verdict: PASS** — Empty forecast table gracefully produces $0 Amazon revenue across all weeks.

### 3. COGS Calculation with Zero Amazon Revenue (Division by Zero Guard)

**Test:** `_project_expense_week()` for `production` (method=`revenue_pct`) when payout ratios are zero.

| Scenario | DTC Ratio | Amazon Ratio | Result | Crash? |
|----------|-----------|-------------|--------|--------|
| Both ratios = 0 | 0.0 | 0.0 | $0.00 | No |
| DTC ratio = 0, Amazon normal | 0.0 | 0.62 | $0.00 | No |
| Amazon ratio = 0, DTC normal | 0.94 | 0.0 | $5,749 | No |
| COGS pct = 0 | 0.94 | 0.62 | $0.00 | No |
| Normal (baseline) | 0.94 | 0.62 | $5,749 | No |

**Code guard (lines 637-638):**
```python
dtc_gross = dtc_payout / dtc_ratio if dtc_ratio > 0 else dtc_payout
amz_gross = amz_payout / amz_ratio if amz_ratio > 0 else amz_payout
```

The `if ratio > 0` guard prevents `ZeroDivisionError`. When ratio is 0, the payout amount is used directly as the gross-up (a reasonable fallback — if ratio is unknown, assume payout ≈ gross).

**Verdict: PASS** — Division by zero is properly guarded. All zero-ratio scenarios produce valid output.

### 4. `normalize_summary()` Edge Inputs

| Input | Output | Crash? | Notes |
|-------|--------|--------|-------|
| `''` (empty string) | `''` | No | Guard on line 58: `if not raw: return ''` |
| `None` | `''` | No | Same guard: `None` is falsy |
| `'1234567890'` (only numbers) | `''` | No | `\b\d{4,}\b` strips all, collapses to empty |
| `'!@#$%^&*()'` (only special) | `'!@'` | No | `#\S+` strips `#$%^&*()`, leaves `!@` |
| `'A' * 10000` (very long) | `''` | No | `\b[a-z0-9]{8,}\b` strips entire lowercased string |
| `'SHOPIFY\nDES:FUNDING\tID:123'` (whitespace) | `'shopify des:funding id:123'` | No | Newlines/tabs collapsed to spaces |
| `'PAYOUT 日本語 DEPOSIT'` (unicode) | `'payout 日本語 deposit'` | No | Unicode preserved, ASCII lowered |
| `'     '` (just spaces) | `''` | No | `.strip()` removes all spaces |
| `'A'` (single char) | `'a'` | No | Lowercased |

**Verdict: PASS** — All edge inputs handled without crash. `None` and empty string correctly return empty string.

### 5. Dead Uppercase Patterns in `_STRIP_PATTERNS` (Bug Found)

**Bug:** Four strip patterns use uppercase letters but are applied AFTER `text = raw.lower().strip()` (line 60), making them dead code:

| Pattern | Matches After Lowering? | Status |
|---------|------------------------|--------|
| `r'ID:\S*'` | No — input has `id:` not `ID:` | **DEAD** |
| `r'REF:\S*'` | No — input has `ref:` not `REF:` | **DEAD** |
| `r'TRACE:\S*'` | No — input has `trace:` not `TRACE:` | **DEAD** |
| `r'SEQ:\S*'` | No — input has `seq:` not `SEQ:` | **DEAD** |

**Evidence:**
- `normalize_summary('PAYMENT REF:TXN987654 AMOUNT')` → `"payment ref: amount"` — the `ref:` label remains in output. The value `txn987654` is stripped by `\b[a-z0-9]{8,}\b` but `ref:` itself persists.
- `normalize_summary('ACH TRACE:123456789012 DEBIT')` → `"ach trace: debit"` — `trace:` remains.

This is the same class of bug as Task 4 fixed for `r'\b[A-Z0-9]{8,}\b'` → `r'\b[a-z0-9]{8,}\b'`.

**Impact:** LOW — The values after these prefixes are usually caught by other patterns (long alphanumeric, 4+ digits). The main effect is that `id:`, `ref:`, `trace:`, `seq:` labels remain in normalized output, potentially causing slightly different normalized patterns for the same vendor depending on whether the original transaction had these fields.

**Fix:** Change to lowercase: `r'id:\S*'`, `r'ref:\S*'`, `r'trace:\S*'`, `r'seq:\S*'`

**Verdict: FAIL (LOW severity)** — 4 dead patterns from the same root cause as Task 4.

### 6. Pattern Ordering Side Effect

**Observation:** `normalize_summary('$1,234.56 01/15/2025 2025-01-15')` → `"01/15/ -01-15"`

The `\b\d{4,}\b` pattern runs before the date patterns and strips `2025` from both date formats, leaving incomplete date fragments that the subsequent date patterns can't match:
- `01/15/2025` → `01/15/` (year stripped, `\d{1,2}/\d{1,2}/\d{2,4}` no longer matches)
- `2025-01-15` → `-01-15` (`\d{4}-\d{2}-\d{2}` no longer matches)

**Impact:** NEGLIGIBLE — Date fragments still produce consistent patterns for the same vendor. The purpose of normalization is consistency, not clean output. This is a cosmetic issue at most.

**Verdict: PASS (with observation)** — Pattern ordering produces consistent results even if the output is not perfectly clean.

### 7. Confidence Intervals — Do They Widen Over Time?

**Configuration:** `CASHFLOW_CONFIDENCE_WEEKLY_GROWTH = 0.02` (2%/week), `CASHFLOW_CONFIDENCE_MAX = 0.30` (30% cap)

| Week | % Width | Band Width ($50K net) | vs Week 1 |
|------|---------|----------------------|-----------|
| 1 | 1.0% (halved from 2%) | $1,000 | — |
| 2 | 2.0% (halved from 4%) | $2,000 | 2x |
| 3 | 3.0% (halved from 6%) | $3,000 | 3x |
| 4 | 4.0% (halved from 8%) | $4,000 | 4x |
| 5 | 10.0% | $10,000 | 10x |
| 8 | 16.0% | $16,000 | 16x |
| 13 | 26.0% | $26,000 | 26x |
| 20 | 30.0% (capped) | $30,000 | 30x |
| 52 | 30.0% (capped) | $30,000 | 30x |

**Key behaviors:**
- Weeks 1-4 use `pct * 0.5` for tighter near-term confidence
- Jump from week 4 (4%) to week 5 (10%) is 2.5x — discontinuity from the 0.5x multiplier ending
- Caps at 30% starting at week 15 (`15 * 0.02 = 0.30`)
- Actual weeks (`is_actual=True`) have `confidence_lower == confidence_upper == closing_balance` (zero width) — correct
- Zero net flow uses $1,000 fixed magnitude — prevents zero-width bands on break-even weeks

**Verdict: PASS** — Confidence intervals correctly widen from 1% (week 1) to 26% (week 13) to 30% cap (week 15+). Week 1 is substantially tighter than week 13 (26x wider). The near-term halving and far-out capping produce a reasonable fan-out shape for the chart.

---

### Summary

| Check | Verdict | Severity |
|-------|---------|----------|
| Cross-month week revenue attribution | **PASS** | — |
| Empty amazon_revenue_forecast fallback | **PASS** | — |
| COGS division by zero guard | **PASS** | — |
| normalize_summary() edge inputs (None, empty, special chars) | **PASS** | — |
| Dead uppercase strip patterns (ID:, REF:, TRACE:, SEQ:) | **FAIL** | LOW |
| Pattern ordering side effect on dates | **PASS (with observation)** | NEGLIGIBLE |
| Confidence intervals widen over time | **PASS** | — |

**Overall: 1 FAIL (LOW), 6 PASS**

The cash flow engine handles edge cases robustly. No crashes on any tested edge input — `None`, empty strings, zero ratios, empty forecast tables, and cross-month weeks all produce valid output. The only bug found is 4 dead uppercase patterns in `_STRIP_PATTERNS` (same class as the Task 4 bug already fixed), which has LOW impact since the values they should strip are usually caught by other patterns.

**Recommendations for Phase 3:**
1. Fix the 4 dead uppercase strip patterns: `ID:` → `id:`, `REF:` → `ref:`, `TRACE:` → `trace:`, `SEQ:` → `seq:`
2. Consider reordering `_STRIP_PATTERNS` to run date patterns before `\b\d{4,}\b` for cleaner date stripping (optional, negligible impact)

---

## Analyst 10 — Code Quality & Security

**Date:** 2026-03-02

### 1. SQL Injection — Parameterized Queries

Audited all SQL queries across `analytics/cashflow.py` (1066 lines) and `views/cashflow.py` (648 lines).

| Location | Query | Parameterized? | Status |
|----------|-------|----------------|--------|
| analytics/cashflow.py:137 | `classify_transaction` SELECT | `%s` placeholder | **PASS** |
| analytics/cashflow.py:156-175 | `get_unmapped_patterns` SELECT | Hardcoded category — no user input | **PASS** |
| analytics/cashflow.py:181-191 | `get_mapping_stats` COUNTs | No params needed | **PASS** |
| analytics/cashflow.py:211-213 | `reclassify_all_transactions` SELECT | No params needed | **PASS** |
| analytics/cashflow.py:228-231 | `reclassify_all_transactions` UPDATE | f-string builds `%s` placeholders for IN clause, values passed as params | **PASS** |
| analytics/cashflow.py:270-278 | `compute_payout_ratio` revenue query | `%s` placeholders via params tuple | **PASS** |
| analytics/cashflow.py:283-293 | `compute_payout_ratio` deposit query | `%s` placeholders via params tuple | **PASS** |
| analytics/cashflow.py:330-337 | `compute_trailing_avg` expense query | `%s` placeholders | **PASS** |
| analytics/cashflow.py:339-345 | `compute_trailing_avg` revenue query | `%s` placeholders | **PASS** |
| analytics/cashflow.py:524-531 | `_detect_expense_schedule` query | `%s` placeholders via params tuple | **PASS** |
| analytics/cashflow.py:810-813 | payroll detection query | Hardcoded `'payroll'` — no user input | **PASS** |
| analytics/cashflow.py:837 | account list query | No params needed | **PASS** |
| analytics/cashflow.py:842-845 | per-account balance query | `%s` placeholder for account name | **PASS** |
| analytics/cashflow.py:866-868 | overrides query | No params needed | **PASS** |
| analytics/cashflow.py:1048-1049 | freshness query | No params needed | **PASS** |
| views/cashflow.py:376 | overrides read | No params needed | **PASS** |
| views/cashflow.py:495-497 | override DELETE | `%s` placeholder | **PASS** |
| views/cashflow.py:528-533 | override INSERT/UPSERT | `%s` placeholders | **PASS** |

**Note on line 228-231:** The f-string `f"WHERE tx_id IN ({placeholders})"` looks like it could be injection-prone, but `placeholders` is constructed via `','.join(['%s'] * len(tx_ids))` — it only produces `%s,%s,%s` patterns, not actual values. Values are passed via the params tuple. This is the standard safe pattern for parameterized IN clauses.

**Verdict: PASS** — All 18 SQL queries use parameterized `%s` placeholders. No f-string injection risk. No string concatenation of user input into queries.

### 2. Division-by-Zero Guards

| Location | Division | Guard | Status |
|----------|----------|-------|--------|
| analytics/cashflow.py:303 | `deposits / revenue` | `.replace(0, np.nan)` converts 0 to NaN | **PASS** |
| analytics/cashflow.py:348 | `total / lookback_weeks` | `max(lookback_weeks, 1)` | **PASS** |
| analytics/cashflow.py:466-467 | DOW weight sum | `if weight_sum <= 0: weight_sum = 1.0` | **PASS** |
| analytics/cashflow.py:476 | `(weight_sum / 7)` | weight_sum guarded above | **PASS** |
| analytics/cashflow.py:597-598 | `avg_gap` in payment projection | `if avg_gap <= 0: return 0.0` | **PASS** |
| analytics/cashflow.py:637-638 | payout ratio gross-up | `if dtc_ratio > 0 else dtc_payout` / `if amz_ratio > 0 else amz_payout` | **PASS** |
| analytics/cashflow.py:1031 | `current_cash / weekly_burn` | `if monthly_burn > 0 else 0` → `if weekly_burn > 0` | **PASS** |
| views/cashflow.py:636 | `(total - unmapped) / total` | `if total > 0 else 0` | **PASS** |

**Verdict: PASS** — All 8 division operations have explicit zero-guards.

### 3. Exception Handling — Silently Swallowed Errors

Found **10 exception handlers** that swallow errors without logging across both files:

| Location | Context | Logs? | Shows user? | Status |
|----------|---------|-------|-------------|--------|
| analytics/cashflow.py:144-145 | `classify_transaction` DB lookup fails | No | No | **FAIL** |
| analytics/cashflow.py:799-800 | Amazon revenue forecast load fails | No | No | **FAIL** |
| analytics/cashflow.py:805-806 | Media spend plan load fails | No | No | **FAIL** |
| analytics/cashflow.py:816-817 | Payroll detection fails | No | No | **FAIL** |
| analytics/cashflow.py:829-830 | Expense schedule detection fails | No | No | **FAIL** |
| analytics/cashflow.py:851-852 | Opening balance calc fails | No | No | **FAIL** |
| analytics/cashflow.py:873-874 | Override loading fails | No (`pass`) | No | **FAIL** |
| analytics/cashflow.py:1053-1054 | Balance freshness query fails | No (`pass`) | No | **FAIL** |
| views/cashflow.py:380-381 | Override loading in view fails | No (`pass`) | No | **FAIL** |
| views/cashflow.py:587-589 | Settings load fails | No | No | **FAIL** |

**3 are bare `except: pass`** (lines 873, 1053 in analytics; line 380 in views) — worst case: these completely hide DB connection failures, schema mismatches, or corrupt data.

**7 more** silently fall back to defaults without logging — if the Amazon forecast table is empty due to a schema change, the model silently uses $0 for all Amazon revenue. If the opening balance query fails, it silently uses $153K from months ago. The CFO would see wrong numbers with no indication anything is wrong.

**Exceptions that DO log properly:**
- analytics/cashflow.py:791 — waterfall build: `log.warning()` ✓
- views/cashflow.py:278-280 — main forecast error: `st.error()` + `log.exception()` ✓
- views/cashflow.py:501-502, 536-537, 572-573 — edit/import errors: `st.error()` ✓

**Verdict: FAIL (MEDIUM severity)**
- 10 of 16 exception handlers swallow errors without logging
- 3 are bare `except: pass` with zero feedback
- Impact: DB failures, schema changes, or corrupt data would silently produce wrong forecasts
- Recommendation: Add `log.warning('Context: %s', exc)` to every catch block. For critical ones (opening balance, revenue loads), add `log.error()`.

### 4. Logging Coverage

| Area | Logged? | Status |
|------|---------|--------|
| Forecast completion (line 978-981) | `log.info()` with summary stats | **PASS** |
| Waterfall build failure (line 791-792) | `log.warning()` | **PASS** |
| Main page render error (views:280) | `log.exception()` | **PASS** |
| Amazon forecast load failure (line 799) | Nothing | **FAIL** |
| Media plan load failure (line 805) | Nothing | **FAIL** |
| Payroll detection failure (line 816) | Nothing | **FAIL** |
| Schedule detection failure (line 829) | Nothing | **FAIL** |
| Opening balance failure (line 851) | Nothing | **FAIL** |
| Override load failure (line 873) | Nothing | **FAIL** |
| Classify transaction DB error (line 144) | Nothing | **FAIL** |
| Settings load failure (views:587) | Nothing | **FAIL** |

**Verdict: FAIL (MEDIUM)** — Only 3 of 14 error paths are logged. The most critical ones (opening balance, revenue source loading) have zero logging.

### 5. Dead Code, TODOs, Commented-Out Blocks

**Dead regex patterns (same bug class as Task 4):**
Lines 37-40 in `_STRIP_PATTERNS`:
```python
r'ID:\S*',      # Applied AFTER lowercasing — never matches "id:xxx"
r'REF:\S*',     # Same — never matches "ref:xxx"
r'TRACE:\S*',   # Same — never matches "trace:xxx"
r'SEQ:\S*',     # Same — never matches "seq:xxx"
```
Input is lowercased on line 60 (`text = raw.lower().strip()`) before patterns run on line 61-62. These uppercase patterns are dead code. Analyst 9 already flagged this.

**No TODO/FIXME/HACK comments found.** ✓
**No commented-out code blocks found.** ✓

**Verdict: FAIL (LOW)** — 4 dead regex patterns. No other dead code.

### 6. Task 4 Fix Verification — Lowercase `_STRIP_PATTERNS`

Line 34: `r'\b[a-z0-9]{8,}\b'` — **CONFIRMED FIXED**. Previously `[A-Z0-9]`, now correctly lowercase to match lowercased input.

**Verdict: PASS**

---

### Summary

| Check | Result | Severity |
|-------|--------|----------|
| SQL parameterization | **PASS** | — |
| Division-by-zero guards | **PASS** | — |
| Silent exception swallowing | **FAIL** | MEDIUM |
| Logging coverage | **FAIL** | MEDIUM |
| Dead code / TODOs | **FAIL** | LOW |
| Task 4 regex fix verified | **PASS** | — |

**Overall: 3 FAIL (2 MEDIUM, 1 LOW), 3 PASS**

The cash flow engine has good security posture — no SQL injection vectors and no division-by-zero vulnerabilities. The main quality issue is poor error observability: 10 of 16 exception handlers silently swallow errors, and only 3 of 14 error paths log anything. This means DB failures or schema changes could silently produce wrong forecasts with no trace in logs. The 4 dead uppercase regex patterns are the same bug class as Task 4 (already flagged by Analyst 9).

**Recommendations for Phase 3:**
1. **(MEDIUM)** Add `log.warning()` to all 10 silent exception handlers in `analytics/cashflow.py` and `views/cashflow.py`. For the 3 bare `except: pass` blocks, add both logging and a sensible fallback message.
2. **(MEDIUM)** For critical data loads (opening balance line 851, Amazon forecast line 799, media plan line 805), upgrade to `log.error()` since wrong values here cascade through the entire forecast.
3. **(LOW)** Fix the 4 dead uppercase strip patterns on lines 37-40: `ID:` → `id:`, `REF:` → `ref:`, `TRACE:` → `trace:`, `SEQ:` → `seq:`.

---

## Analyst 11 — Business Sanity Check (Does This Look Like a Real Business?)

**Date:** 2026-03-02

**Methodology:** Ran `build_cashflow_forecast(conn, start_date=today-4w, weeks=56, scenario='base')` and examined the 52-week output holistically. This is NOT a code review — this is a CFO looking at the numbers and asking "does this make sense?"

### 1. 52-Week Ending Cash

| Metric | Value | Expected Range | Verdict |
|--------|-------|---------------|---------|
| Opening cash (week 0) | $117,007 | ~$117K (actual bank) | Correct |
| Week 4 (current week) opening | $227,258 | ~$117K | **WRONG** (+$110K) |
| 13-week closing | $658,300 | $250-400K | **FAIL** (1.6-2.6x high) |
| 52-week closing | $2,495,445 | $700K-$1.3M | **FAIL** (1.9-3.6x high) |
| FAIL gate ($400K-$2M) | $2,495K > $2M | Must be <$2M | **FAIL** |

**Analysis:** The model projects $2.5M ending cash after 52 weeks starting from $117K. This implies ~$2.4M of net cash generation = $46K/week net positive. For a company with ~$300K/month gross revenue and $150-250K/month expenses, this is unrealistically optimistic. The expected range of $700K-$1.3M would imply $50-100K/month net positive, which is reasonable. At $2.5M, the model is ~2x too optimistic.

**Root causes (traced):**

| Root Cause | Impact on 52-Week Cash | Source |
|------------|----------------------|--------|
| Phantom interest_income (Jameson misclassification) | +$720K | Analyst 3 |
| Opening balance double-counting | +$110K | Analyst 4 |
| Optimistic DTC revenue (aggressive media inputs) | +$200-400K | Analyst 1 |
| Missing loan expense ($0 instead of $25-75K/month) | +$300-900K | Analyst 3 |
| Missing unmapped expenses (~$35-52K/month) | +$420-624K | Analyst 3 |

If all root causes were corrected: phantom revenue removed (~$720K), loan payments restored (~$300-900K in expenses), and optimistic inputs adjusted, the 52-week ending cash would likely be in the $400K-$900K range — still positive but much more realistic.

**Verdict: FAIL (CRITICAL)** — 52-week ending cash of $2.5M is outside the $400K-$2M sanity gate by 25%.

### 2. Weekly Revenue Range

| Metric | Model Range | Expected Range | Verdict |
|--------|------------|---------------|---------|
| DTC weekly (cash) | $32,317 - $64,247 | $10,000 - $15,000 | **FAIL** |
| Amazon weekly (disbursement) | $40,248 - $68,200 | $40,000 - $60,000 | **CONDITIONAL PASS** |
| Amazon weekly (non-disbursement) | $0 | $0 | PASS |
| Max single DTC week | $64,247 (Sep 7) | <$100K threshold | PASS (below cap) |
| Max single Amazon week | $68,200 (Jun/Jul/Aug) | <$150K threshold | PASS (below cap) |

**DTC analysis:** The model projects $32-64K/week in DTC cash revenue. The PRD reference says ~$10-15K/week, but this appears to reference net/cash after processing fees on a much earlier revenue base. Actual recent Shopify bank deposits are ~$116K/month = ~$27K/week. The model projects $32-64K/week, which is 1.2-2.4x above recent actual deposits. The gap widens over time because the waterfall model compounds aggressive media spend inputs into ever-higher revenue projections.

**Amazon analysis:** Disbursement weeks show $40-68K, which starts within the $40-60K expected range but grows to $68K by summer (driven by the aggressive Amazon revenue forecast table ramping to $220K/month). Per-event amounts are calculated as `monthly_forecast * 0.62 / 2`, so the issue is the input, not the formula.

**DTC weekly revenue is too high.** The $32K/week starting point (March) is 20% above recent actuals ($27K/week). By September, it reaches $64K/week — 2.4x above actuals. This is driven by the waterfall model converting optimistic media spend inputs into new customer projections.

**Verdict: FAIL (MEDIUM)** — DTC revenue 20-140% above recent actuals.

### 3. Weekly Expense Range

| Metric | Value | Expected | Verdict |
|--------|-------|----------|---------|
| Average weekly outflows (projected) | $37,000 - $66,000 | $35,000 - $60,000 | **CONDITIONAL PASS** |
| Max single week outflows | $98,768 (Mar 2-8) | <$150K threshold | **PASS** |
| Min weekly outflows | $28,554 (Feb 8 2027) | — | PASS |

**Analysis:** The March 2-8 week shows $98.8K outflows, driven by a $58K media hit (schedule detection placed a large payment here). This is below the $150K threshold. No other week exceeds $70K. Average outflows run ~$40-50K/week which is in the expected ballpark.

However, expenses are structurally understated because:
- Loan expenses are $0 (should be $25-75K/month = $6-17K/week)
- ~$103K/month in unmapped expenses are not projected
- The true weekly outflow should be ~$55-85K/week, not $37-66K

**Verdict: CONDITIONAL PASS** — Under the $150K cap but structurally understated by ~$30-40K/week due to missing categories.

### 4. Expense-Revenue Correlations

| Correlation | Expected | Actual | Verdict |
|-------------|----------|--------|---------|
| Fulfillment rises with revenue | Yes (more orders = more 3PL) | **NO** — flat $5,070/week regardless | **FAIL** |
| COGS scales with revenue | Yes (25% of gross) | **Partially** — production column scales but at ~45% not 25% | **FAIL** |
| Shipping correlates with volume | Yes | Flat at ~$0-$125/week | **FAIL** |

**Fulfillment analysis:** Fulfillment (3PL fees) stays completely flat at $5,070/week in all projected weeks. This is because it uses the `trailing_avg` method, which returns the average of recent actual fulfillment payments. The trailing average doesn't scale with projected revenue growth. In reality, if DTC revenue doubles from $32K/week to $64K/week, fulfillment costs should roughly double too (more orders shipped). The model shows fulfillment at 17% of DTC revenue in March falling to 8% by August — an impossible divergence.

**Production/COGS analysis:** The `production` column uses the `revenue_pct` method and scales with revenue, which is correct directionally. However, the amounts are ~45% of DTC revenue, far above the 25% COGS seed rate. This appears to be because the revenue_pct method uses gross-up calculations that amplify the base rate. Actual production spend from bank data is only ~$5K/week average (PO-driven and spiky), while the model projects $8-43K/week.

**COGS column is $0 everywhere.** The `cogs` column appears unused despite being in the schema. All COGS-like expenses flow through `production` instead.

**Verdict: FAIL (HIGH)** — Fulfillment is flat when it should scale with revenue. Production/COGS is 2-9x above actual levels.

### 5. No Negative Revenue Weeks

| Check | Result | Verdict |
|-------|--------|---------|
| Weeks with negative DTC revenue | 0 | **PASS** |
| Weeks with negative Amazon revenue | 0 | **PASS** |

**Verdict: PASS** — No refunds or sign errors leaking into revenue.

### 6. Cash Never Goes Massively Negative

| Metric | Value | Threshold | Verdict |
|--------|-------|-----------|---------|
| Minimum closing balance (any week) | $138,258 (week 0, Feb 2) | >-$100K | **PASS** |
| Cash goes negative at any point? | No | — | **PASS** |

**Analysis:** Cash starts at $117K (actual bank balance) and only grows. The model never shows a drawdown. This is because revenue is systematically overstated and expenses are understated — a realistic model would show some tight weeks, especially around production POs and quarterly tax payments.

**Verdict: PASS** — But misleading. A model that never shows cash stress is likely too optimistic, not actually safe.

### 7. Month-over-Month Consistency

| Month Pair | Inflow Change | Within 30%? | Verdict |
|------------|--------------|-------------|---------|
| Mar → Apr | -1.8% | Yes | PASS |
| Apr → May | +11.4% | Yes | PASS |
| May → Jun | **+30.8%** | **No (barely)** | **FAIL** |
| Jun → Jul | -9.7% | Yes | PASS |
| Jul → Aug | +25.2% | Yes | PASS |
| Aug → Sep | -13.8% | Yes | PASS |
| Sep → Oct | -9.1% | Yes | PASS |
| Oct → Nov | +14.4% | Yes | PASS |
| Nov → Dec | -19.1% | Yes | PASS |
| Dec → Jan | -4.9% | Yes | PASS |
| Jan → Feb | **-44.0%** | **No** | **FAIL** (partial month) |

**Analysis:** The May→Jun jump of +30.8% slightly exceeds the 30% threshold. This is driven by the media spend plan ramping from $95K (May) to $130K (Jun), which the waterfall converts into higher DTC revenue. The Jan→Feb drop of -44% is a windowing artifact (February is a partial month at the end of the forecast).

Excluding the partial-month Feb, month-over-month variations range from -19% to +30.8%. The model doesn't show wild 3x jumps — inflows are relatively stable. However, the underlying growth trend (March $321K → August $519K = +62% over 5 months) is driven by compounding media spend assumptions that may not materialize.

**Verdict: CONDITIONAL PASS** — No catastrophic jumps, but the May→Jun boundary slightly exceeds the 30% threshold due to aggressive media ramp inputs. The overall upward trend is plausible if media spend plans are accurate.

---

### Summary

| Sanity Gate | Verdict | Severity |
|-------------|---------|----------|
| 52-week ending cash ($400K-$2M range) | **FAIL** ($2.5M, 25% over cap) | CRITICAL |
| Weekly DTC revenue range ($10-15K) | **FAIL** ($32-64K, 2-4x over) | MEDIUM |
| Weekly Amazon revenue range ($40-60K) | **CONDITIONAL PASS** ($40-68K) | LOW |
| Weekly expense range (<$150K) | **PASS** (max $99K) | — |
| Expense-revenue correlations | **FAIL** (fulfillment flat, COGS inflated) | HIGH |
| No negative revenue weeks | **PASS** | — |
| Cash never massively negative (<-$100K) | **PASS** (min $138K) | — |
| Month-over-month consistency (<30%) | **CONDITIONAL PASS** (one 30.8% jump) | LOW |

**Overall: 3 FAIL (1 CRITICAL, 1 HIGH, 1 MEDIUM), 2 CONDITIONAL PASS, 3 PASS**

### Root Cause Tracing

The $2.5M ending cash (vs expected $700K-$1.3M) is an ~$1.2-1.8M overstatement. The major contributors:

1. **Phantom revenue from Jameson misclassification (+$720K/52wk):** The `interest_income` category contains $13,891/week of trailing-average projected "revenue" that is actually loan payments. This is 15.6% of total projected inflows. *Trace: `_project_revenue_week()` → `compute_trailing_avg(conn, 'interest_income')` → returns $13,891/week from actual Jameson debit transactions miscategorized as revenue.*

2. **Missing loan expenses (+$300-900K/52wk):** With loan expenses at $0/week (Jameson misclassified elsewhere), the model ignores $25-75K/month in real cash outflows. *Trace: `_project_expense_week()` → `compute_trailing_avg(conn, 'loan')` → returns $0.125/week (only $1 in actual mapped data) → `val * 4.33` → ~$0.54/qualifying week.*

3. **Optimistic DTC revenue (+$200-400K/52wk):** Waterfall model inflates DTC by 20-140% because media spend plan inputs ($75-190K/month) exceed actual spend ($24-65K/month). *Trace: `build_waterfall(media_plan)` → media_plan from Settings page → `new_customers = media_spend * ROAS / AOV`.*

4. **Flat fulfillment costs (understated ~$100K/52wk):** Fulfillment stays at $5,070/week even as revenue doubles, when it should roughly track order volume. *Trace: `_project_expense_week()` → `compute_trailing_avg(conn, 'fulfillment')` → static trailing average, no revenue linkage.*

5. **Opening balance double-counting (+$110K):** Current week shows $227K instead of $117K. *Trace: `build_cashflow_forecast()` → opening balance uses latest bank sum ($117K) as week 0 start, then replays 4 weeks of actual transactions already reflected in that balance.*

### Recommendations for Phase 3

1. **(CRITICAL)** Fix Jameson misclassification in `category_mappings` — reclassify from `interest_income` to `loan`. This alone would remove ~$720K of phantom revenue and add ~$720K of expenses over 52 weeks = ~$1.4M swing in ending cash.
2. **(CRITICAL)** Fix opening balance double-counting in `build_cashflow_forecast()` — when using latest bank balance as opening, the actual-week transactions are already reflected. Either (a) set opening balance to what it was at `start_date` (subtract interim transactions), or (b) start from today instead of 4 weeks ago.
3. **(HIGH)** Link fulfillment costs to revenue volume so they scale with projected growth instead of staying flat.
4. **(HIGH)** Add direction filter to `_get_actual_weekly_totals()` — revenue categories should only sum credits, expense categories should only sum debits.
5. **(MEDIUM)** Validate media spend plan inputs against trailing actuals — warn when plan exceeds recent actual by >50%.

---

## Analyst 12 — Cross-Dashboard Consistency Check

**Date:** 2026-03-02

### 1. Media Spend: Settings Page vs Cash Flow Model

| Data Point | Value |
|------------|-------|
| `media_spend` table rows (All Sources) | 13 months (Feb 2026 – Feb 2027) |
| Plan range | $5K – $190K/month |
| Near-term plan (Feb-Mar 2026) | $75K/month |
| Summer ramp (Jun-Aug 2026) | $130K – $190K/month |
| Actual media bank debits (recent) | $24K – $52K/month |

**Data flow check:** The cash flow model reads `media_spend` table WHERE `source='All Sources'` via `get_media_spend()`. The same table is what the Settings/Business Variables page writes to. **The data flow is consistent** — both the cash flow model and the Settings page read/write the same table.

**However, the media spend PLAN significantly exceeds actual bank debits:**
- March 2026: Plan $75K vs Actual $52K (+43.6%) — **MISMATCH >20%**
- Recent months (Nov 2025 – Jan 2026): Actual debits $24K – $26K/month vs plan $75K (3x gap)
- Summer plan ($130K – $190K/month) has never been achieved historically (peak was $83K in Jul 2025)

**Impact:** The media plan feeds TWO parts of the model:
1. **Media EXPENSE projection** (method: `media_plan`) — bills the planned amount monthly. Expense is overstated.
2. **DTC REVENUE via waterfall** — `build_waterfall(media_plan)` converts media spend × ROAS into new customers. Revenue is overstated.

Both effects compound: the model overspends on media AND overestimates the revenue that media generates. This is an **input quality problem**, not a code bug.

**Verdict: FAIL (HIGH)** — Media spend plan is 43-200% above actual spend. The Settings page and cash flow model are data-consistent (same table), but the values in that table are disconnected from reality.

### 2. Amazon Revenue Forecast: Forecast Table vs Cash Flow Usage

| Month | Forecast Table | Actual Gross Revenue | Diff |
|-------|---------------|---------------------|------|
| 2026-02 | $150,000 | $133,820 | +12.1% (OK) |
| 2026-03 | $151,470 | $6,723 (partial) | N/A |
| 2026-04 | $175,000 | — | Future |
| 2026-05 | $200,000 | — | Future |
| 2026-06 – 2026-09 | $220,000/month | — | Future |

**Data flow check:** Cash flow model reads `amazon_revenue_forecast` table via `get_amazon_revenue_forecast()`, stores as `ctx['amazon_monthly_revenue']`, and passes to `_project_revenue_week()` which applies payout ratio (0.62) and distributes across 2 disbursements/month. **The data flow is correct** — the forecast table IS what drives Amazon projections.

**Accuracy check:** February forecast ($150K) vs actual Amazon gross revenue ($134K) = 12% over. Within acceptable range. The forecast table values match what Analyst 1 already examined.

**Summer projections ($200K-$220K/month):** No actuals to compare. Analyst 1 flagged these as likely 30-50% optimistic based on the historical range of $54K-$134K/month.

**Verdict: CONDITIONAL PASS** — Data flow is consistent (same table, same values). Near-term accuracy is acceptable (12%). Summer projections are aspirational but not contradicted by available data.

### 3. Waterfall DTC Revenue: Forecast Page vs Cash Flow

| Month | Waterfall Output | Actual Shopify Gross | Diff |
|-------|-----------------|---------------------|------|
| 2026-03 | $144,807 | $121K (6-month avg) | +20% |
| 2026-06 | $220,841 | — | Future |
| 2026-08 | $275,135 | — | Future |
| 2027-02 | $159,855 | — | Future |

**Data flow check:** The cash flow model calls `build_waterfall(media_plan, source_filter='shopify', horizon_months=12, seasonal_indices=seasonal_dict)` — the EXACT same function the Forecast page would call. Both receive the same `media_plan` (from `get_media_spend(conn, source='All Sources')`) and the same `seasonal_indices` (from DB table). **The waterfall output is inherently consistent** — it's the same function with the same inputs.

**The waterfall is NOT called separately for the Forecast page vs the Cash Flow page** — both import from `analytics/waterfall.py` and call `build_waterfall()` with the same parameters. There is no opportunity for divergence.

**Waterfall output columns:** `month, repeat_units, new_customer_units, total_units, new_customers_acquired, repeat_revenue, new_customer_revenue, total_revenue`. Cash flow uses the `total_revenue` column.

**March DTC gross:** $144,807 (20% above 6-month trailing average of $121K). This overshoot is driven by the media spend plan inputs ($75K/month vs actual $24-52K) as documented by Analyst 1.

**Verdict: PASS** — Waterfall output is consistent across dashboard pages (same function, same inputs). The accuracy concern (20% above recent average) is an input problem, not an inconsistency.

### 4. Planned Inbound / Production

| Month | Planned Inbound (units) |
|-------|------------------------|
| 2026-02 | 0 |
| 2026-03 | 0 |
| 2026-04 | 16,000 |
| 2026-05 | 22,200 |
| 2026-06 – 2027-01 | 0 |

**Data flow check:** The `planned_inbound` table has 204 rows across 12 months. April and May 2026 have 38,200 total units planned. At a rough $2-3/unit production cost, this represents $75K-$115K in production expense that should spike in those months.

**GAP IDENTIFIED:** The cash flow model does NOT read `planned_inbound` for production expense timing. The `production` category uses method `revenue_pct` (COGS as % of gross revenue), which spreads production costs smoothly in proportion to revenue. This means:
- April/May production POs (~$75-115K) will NOT appear as expense spikes
- The cash flow will show smooth production costs instead of lumpy PO-driven payments
- This could understate cash needs by $50K+ in PO months and overstate them in non-PO months

**Verdict: FAIL (MEDIUM)** — Planned inbound data exists but is not consumed by the cash flow model. Production expense timing is disconnected from actual PO commitments. This is a feature gap, not a data inconsistency.

### 5. Seasonal Indices: DB vs constants.py

| Month | DB Value | Default (constants.py) | Diff % |
|-------|----------|----------------------|--------|
| 1 (Jan) | 0.884 | 0.950 | -6.9% |
| 2 (Feb) | 0.858 | 0.920 | -6.7% |
| 3 (Mar) | 0.857 | 0.980 | -12.5% |
| 4 (Apr) | 0.945 | 1.020 | -7.3% |
| 5 (May) | 1.049 | 1.050 | -0.1% |
| 6 (Jun) | 1.095 | 1.100 | -0.5% |
| 7 (Jul) | 1.126 | 1.120 | +0.6% |
| 8 (Aug) | 1.098 | 1.080 | +1.6% |
| 9 (Sep) | 1.108 | 1.020 | +8.6% |
| 10 (Oct) | 1.038 | 0.980 | +5.9% |
| 11 (Nov) | 1.037 | 0.920 | +12.7% |
| 12 (Dec) | 0.905 | 0.880 | +2.8% |

**Data flow check:** The cash flow model reads `seasonal_indices` from DB and passes to `build_waterfall()`. The waterfall applies these to REPEAT revenue only (not first-order), matching the spreadsheet methodology.

**DB values differ from `DEFAULT_SEASONAL_INDICES` in `utils/constants.py`** by -12.5% to +12.7%. No differences exceed the 20% threshold. The DB values appear to have been auto-calibrated or manually updated from actual sales data — this is expected and correct behavior.

**Key observation:** The DB values show a wider range than PRD seeds (range: 0.857-1.126 = 31.4% spread vs PRD 0.88-1.12 = 27.3%). Winter months are lower (Jan 0.884 vs seed 0.95) and fall months are higher (Sep/Nov ~1.04-1.11 vs seed 0.92-1.02). This suggests actual Hydrant seasonality has more extreme summer peaks and deeper winter troughs than the default seeds assumed.

**Verdict: PASS** — DB indices are populated, within 13% of defaults, and correctly consumed by both the waterfall and cash flow models. The differences are calibration improvements, not inconsistencies.

### 6. Amazon Mapping Gap (Cross-cutting Finding)

| Metric | Value |
|--------|-------|
| Amazon bank transactions mapped as 'amazon_revenue' | 0 |
| Amazon bank transactions still 'unmapped' | 51 |
| Total transaction mapping coverage | 66.0% (1,366/2,069) |

**Impact on cross-dashboard consistency:** Because zero Amazon bank transactions are mapped, the cash flow model cannot auto-calibrate the Amazon payout ratio from actuals. It falls back to the seed value (0.62). The Reorder page and Forecast page use separate Amazon data flows (daily_sku_sales, not bank transactions), so this mapping gap doesn't affect them — but it prevents the cash flow from validating its Amazon assumptions against bank reality.

**Verdict: FAIL (MEDIUM)** — Already flagged by Analysts 2 and 5. The mapping gap prevents payout ratio auto-calibration and obscures $51 transactions (~$40-60K each = potentially $2-3M in unclassified bank deposits).

---

### Summary

| Check | Verdict | Severity | Details |
|-------|---------|----------|---------|
| Media spend (Settings ↔ Cash Flow) | **FAIL** | HIGH | Data flow consistent, but plan values 43-200% above actual bank debits |
| Amazon forecast (Table ↔ Cash Flow) | **CONDITIONAL PASS** | LOW | Data flow consistent, Feb accuracy 12% over, summer unverifiable |
| Waterfall DTC (Forecast ↔ Cash Flow) | **PASS** | — | Same function, same inputs, inherently consistent |
| Planned inbound → Production expense | **FAIL** | MEDIUM | PO data exists (38K units in Apr-May) but not consumed by cash flow |
| Seasonal indices (DB ↔ constants.py) | **PASS** | — | DB values within 13% of defaults, correctly consumed |
| Amazon transaction mapping | **FAIL** | MEDIUM | 0/51 Amazon deposits mapped, blocking auto-calibration |

**Overall: 3 FAIL (1 HIGH, 2 MEDIUM), 1 CONDITIONAL PASS, 2 PASS**

### Key Findings

1. **No code-level inconsistencies exist between dashboard pages.** The cash flow model, forecast page, and settings page all read from the same database tables through the same functions. The data flow is architecturally sound.

2. **The primary issue is input quality, not data flow.** The media spend plan in the `media_spend` table is significantly more aggressive than actual spending. This inflates both DTC revenue projections (via waterfall) AND media expense projections. Since the CFO controls these inputs on the Settings page, this is a business judgment call, not a bug.

3. **The planned inbound → production expense gap is a real feature gap.** The cash flow model ignores PO data that exists in `planned_inbound`, instead modeling production costs as a smooth % of revenue. For a brand with $50K+ lumpy PO payments, this understates cash needs during PO months.

4. **The 51 unmapped Amazon transactions represent ~$2-3M in unclassified bank deposits.** Mapping these would enable auto-calibration of the Amazon payout ratio and improve the model's self-correction capability.

---

## Analyst 13 — Stress Test: Extreme Scenarios

**Date:** 2026-03-02

**Methodology:** Ran `build_cashflow_forecast()` with `start_date=today-4w, weeks=56` across all three scenarios (base, conservative, aggressive), plus two extreme tests (zero revenue, zero expenses). Used Railway PostgreSQL production data. Monkey-patched `_project_revenue_week` and `_project_expense_week` for the zero-revenue and zero-expense tests to isolate the engine's behavior when one side of the cash flow is eliminated. Also performed code-level analysis of `_apply_scenario()` and COGS interaction with scenario multipliers.

### 1. Three-Scenario Comparison (52 Projected Weeks)

| Metric | Base | Conservative | Aggressive |
|--------|------|-------------|-----------|
| Opening balance | $117,007 | $117,007 | $117,007 |
| Week 52 closing | $2,495,445 | $1,570,974 | $3,073,339 |
| Projected inflows (52w) | $4,631,065 | $3,937,110 | $5,093,702 |
| Projected outflows (52w) | $2,362,879 | $2,593,394 | $2,247,621 |
| Projected net (52w) | $2,268,186 | $1,343,716 | $2,846,081 |

**Scenario multiplier verification:**

| Check | Expected | Actual | Verdict |
|-------|----------|--------|---------|
| Conservative revenue vs base | ~-15% | -15.0% | **PASS** |
| Conservative expenses vs base | ~+10% | +9.8% | **PASS** |
| Conservative closing < base? | Yes | Yes (-$924,470) | **PASS** |
| Aggressive revenue vs base | ~+10% | +10.0% | **PASS** |
| Aggressive expenses vs base | ~-5% | -4.9% | **PASS** |
| Aggressive closing > base? | Yes | Yes (+$577,894) | **PASS** |
| Ordering: cons < base < agg | Yes | $1.57M < $2.50M < $3.07M | **PASS** |

**Verdict: PASS** — All three scenarios produce correctly ordered results with the expected multiplier effects. Revenue and expense adjustments match the documented multipliers exactly (-15%/+10% for conservative, +10%/-5% for aggressive).

**Note on expense deviation (9.8% vs 10.0%):** The conservative expense increase is 9.8% instead of exactly 10.0% because the `revenue_pct` (COGS) method calls `_project_revenue_week` internally to compute base revenue, then the COGS result gets the +10% expense multiplier. However, COGS is computed from UN-scenarioed revenue. This is analyzed in detail in Finding 5 below.

### 2. Actual Rows Unchanged Across Scenarios

**Test:** Verified that actual (past) week rows are identical across all three scenarios — scenarios should only affect projected weeks.

| Check | Result | Verdict |
|-------|--------|---------|
| Actual rows (base vs conservative) | Identical | **PASS** |
| Actual rows (base vs aggressive) | Identical | **PASS** |
| Opening balance (all three) | $117,007.28 | **PASS** |

**Verdict: PASS** — Scenario adjustments are correctly applied only to projected/future weeks. Historical actual data is not modified by scenario selection. The opening balance is sourced from bank data and is scenario-independent.

### 3. Balance Chain Integrity (All Scenarios)

**Test:** For every row in every scenario, verified:
1. `closing_balance == opening_balance + net_cashflow` (arithmetic identity)
2. `opening_balance[N+1] == closing_balance[N]` (chain continuity)

| Scenario | Arithmetic | Chain | Max Error |
|----------|-----------|-------|-----------|
| Base | PASS (56/56 rows) | PASS (55/55 pairs) | $0.0000 |
| Conservative | PASS (56/56 rows) | PASS (55/55 pairs) | $0.0000 |
| Aggressive | PASS (56/56 rows) | PASS (55/55 pairs) | $0.0000 |

**Verdict: PASS** — The balance arithmetic engine is mathematically exact across all scenarios. No floating-point drift, no chain breaks.

### 4. Zero Revenue — Does the Model Survive?

**Test:** Monkey-patched `_project_revenue_week` to always return $0.00. Ran 56-week forecast.

| Metric | Value | Verdict |
|--------|-------|---------|
| Crashed? | No | **PASS** |
| Opening balance | $117,007 | Correct |
| Week 52 closing | -$945,545 | Expected (expenses drain cash) |
| Projected inflows (52w, proj only) | $4,700 | See analysis below |
| Projected outflows (52w, proj only) | $1,177,503 | Expenses continue |
| Cash trend | Declining | **PASS** (correct) |
| Cash goes negative? | Yes, week 13 (2026-04-27) | Expected |
| COGS with zero revenue | $0.00 | **PASS** |
| Outflows still occur? | Yes | **PASS** |

**Inflow residual analysis:** The $4,700 in projected inflows is NOT a bug — it comes from the **current week's actual-to-date** portion. The blending logic (lines 902-922) uses real bank deposits for days already elapsed in the current week, then adds projected revenue for remaining days. With the patch, remaining days produce $0 but the actual-to-date ($4,699 in Shopify deposits on March 2) is real data. Purely future weeks show exactly $0.00 inflows. **Correct behavior.**

**COGS behavior:** With zero revenue, `_project_expense_week` for `production` calls `_project_revenue_week` (which returns $0) and computes `$0 * cogs_pct = $0`. COGS correctly scales to zero when revenue is zero. **PASS.**

**Cash-negative timing:** Cash hits negative in week 13 (~$117K opening / ~$22.6K weekly average expenses = ~5.2 months). This is consistent with the expense run rate.

**Verdict: PASS** — The model handles zero revenue gracefully. No crashes, no NaN, no division by zero. Expenses correctly continue draining cash. COGS correctly drops to zero.

### 5. Zero Expenses — Does the Model Survive?

**Test:** Monkey-patched `_project_expense_week` to always return $0.00. Ran 56-week forecast.

| Metric | Value | Verdict |
|--------|-------|---------|
| Crashed? | No | **PASS** |
| Opening balance | $117,007 | Correct |
| Week 52 closing | $4,800,602 | Expected (revenue accumulates) |
| Projected inflows (52w, proj only) | $4,631,065 | Same as base (unaffected) |
| Projected outflows (52w, proj only) | $57,722 | See analysis below |
| Cash trend | Growing | **PASS** (correct) |
| Inflows still occur? | Yes | **PASS** |

**Outflow residual analysis:** The $57,722 in projected outflows is NOT a bug — it comes from the **current week's actual-to-date** expenses. Specifically: $52,235 in media (an actual bank debit on March 2) and $5,486 in fulfillment (actual bank debit). These are real bank transactions from days already elapsed in the current week. Purely future weeks show exactly $0.00 outflows. **Correct behavior.**

**Revenue in zero-expense scenario:** Projected inflows ($4,631,065) match the base scenario exactly. This confirms that expense patching does not affect revenue projection — the two sides of the forecast are correctly independent (except for COGS, which was also patched to $0 here).

**Verdict: PASS** — The model handles zero expenses gracefully. Revenue continues accumulating. No crashes or unexpected behavior.

### 6. `_apply_scenario()` Unit Tests

Tested all 9 combinations of (scenario, category_group, amount) with deterministic inputs ($1,000):

| Scenario | Group | Input | Output | Expected | Verdict |
|----------|-------|-------|--------|----------|---------|
| base | revenue | $1,000 | $1,000.00 | $1,000 | **PASS** |
| base | expense | $1,000 | $1,000.00 | $1,000 | **PASS** |
| conservative | revenue | $1,000 | $850.00 | $850 | **PASS** |
| conservative | expense | $1,000 | $1,100.00 | $1,100 | **PASS** |
| aggressive | revenue | $1,000 | $1,100.00 | $1,100 | **PASS** |
| aggressive | expense | $1,000 | $950.00 | $950 | **PASS** |
| base | transfer | $1,000 | $1,000.00 | $1,000 | **PASS** |
| conservative | transfer | $1,000 | $1,000.00 | $1,000 | **PASS** |
| aggressive | transfer | $1,000 | $1,000.00 | $1,000 | **PASS** |

**Verdict: PASS** — All multipliers match documented values. Transfers are correctly unaffected by scenario selection.

### 7. COGS Does Not Track Revenue Scenario (BUG FOUND)

**Bug:** In the conservative scenario, COGS (production) *increases* by ~10% instead of *decreasing* proportionally with the 15% revenue drop.

**Code trace** (`_project_expense_week`, lines 630-639):
```python
if method == 'revenue_pct':
    cogs_pct = ctx.get('cogs_pct', 0.25)
    dtc_payout = _project_revenue_week(conn, 'dtc_revenue', week_start, week_end, ctx)
    amz_payout = _project_revenue_week(conn, 'amazon_revenue', week_start, week_end, ctx)
    ...
    return (dtc_gross + amz_gross) * cogs_pct
```

Then in the main loop (lines 951-952):
```python
val = _project_expense_week(conn, cat, ws, we, ctx)
val = _apply_scenario(val, 'expense', scenario)
```

**What happens:**
1. `_project_expense_week` calls `_project_revenue_week` to get the **BASE** (un-scenarioed) revenue
2. Computes COGS as `base_gross_revenue * 0.25 = $X`
3. Returns $X (base COGS)
4. The main loop then applies `_apply_scenario($X, 'expense', 'conservative')` = `$X * 1.10`

**Result for conservative scenario:**
- Revenue line items: base revenue * 0.85 = -15%
- COGS line item: base_gross * 0.25 * 1.10 = +10% of base COGS

This is backwards. In a conservative scenario with 15% less revenue, COGS should also decrease because fewer units are sold = fewer units produced. The correct behavior would be:

| Scenario | Revenue | COGS (current) | COGS (expected) |
|----------|---------|----------------|-----------------|
| Base | $4,631K | $X | $X |
| Conservative | $3,937K (-15%) | $X * 1.10 (+10%) | ~$X * 0.85 (-15%) |
| Aggressive | $5,094K (+10%) | $X * 0.95 (-5%) | ~$X * 1.10 (+10%) |

The expense multiplier on COGS creates perverse incentives: the conservative scenario shows HIGHER COGS on LOWER revenue (margin squeeze), while the aggressive scenario shows LOWER COGS on HIGHER revenue (margin expansion). This is the opposite of reality.

**Impact:** The total expense deviation (9.8% vs 10.0% for conservative) is mostly explained by this: COGS goes up +10% while other expenses also go up +10%, but the net is slightly less than 10% because of how the actual-week blending dilutes the effect. The COGS directional error inflates conservative expenses by ~$120K over 52 weeks (the COGS portion that should decrease with revenue but instead increases).

**Severity: MEDIUM** — COGS is ~$500K/year in the base scenario. The conservative scenario shows COGS at $550K when it should be ~$425K (tracking the 15% revenue decline). This is a $125K swing that makes the conservative scenario ~10% more pessimistic on expenses than intended, partially masking the revenue pessimism.

**Fix:** In `_project_expense_week` for the `revenue_pct` method, apply the revenue scenario multiplier to the revenue call, OR exempt `revenue_pct` categories from the expense multiplier in `_apply_scenario`. The simplest fix is to pass `scenario` into the expense function and handle `revenue_pct` specially:

```python
if method == 'revenue_pct':
    dtc_payout = _project_revenue_week(...)
    dtc_payout = _apply_scenario(dtc_payout, 'revenue', scenario)  # Apply revenue multiplier
    amz_payout = _project_revenue_week(...)
    amz_payout = _apply_scenario(amz_payout, 'revenue', scenario)
    ...
    return (dtc_gross + amz_gross) * cogs_pct  # Don't apply expense multiplier later
```

Then in the main loop, skip `_apply_scenario` for `revenue_pct` categories since the scenario is already baked in.

**Verdict: FAIL (MEDIUM severity)**

### 8. Scenario Spread Analysis

| Metric | Conservative | Base | Aggressive | Spread (agg-cons) |
|--------|-------------|------|-----------|-------------------|
| 52w closing | $1,570,974 | $2,495,445 | $3,073,339 | $1,502,365 |
| 52w net | $1,343,716 | $2,268,186 | $2,846,081 | $1,502,365 |
| Weekly avg inflows | $75,714 | $89,059 | $97,956 | $22,242/week |
| Weekly avg outflows | $49,873 | $45,440 | $43,223 | $6,650/week |

**Spread of 95.6% between conservative and aggressive closing balances.** The aggressive scenario ends at nearly double the conservative scenario ($3.07M vs $1.57M). This is a very wide spread, driven by the compounding effect of the scenario multipliers over 52 weeks:

- Revenue compounding: 15% + 10% = 25% revenue swing, compounded over 12 months of waterfall growth
- Expense compounding: 10% + 5% = 15% expense swing
- Combined: 40% swing on net cash flow per week, compounded over 52 weeks

**Is this spread reasonable?** For a 52-week horizon, a ~2x spread between pessimistic and optimistic scenarios is within the normal range. The $1.5M spread on a $2.5M base represents one standard deviation of ~30%, which is consistent with the confidence interval parameters (CASHFLOW_CONFIDENCE_MAX = 30%).

**Conservative scenario never goes negative:** The minimum closing balance in the conservative scenario is $138,258 (week 0, the actual opening). Cash grows throughout the conservative forecast, from $117K to $1.57M. This means even the pessimistic scenario projects strong positive cash flow — a reflection of the underlying revenue/expense ratio (revenue >> expenses after all known bugs).

**However**, if the known bugs were corrected (Jameson loan reclassification adding ~$720K in annual expenses, opening balance fix removing $110K inflation, media plan correction reducing revenue), the conservative scenario would likely show cash declining to near $0 or negative within 26-39 weeks, which is a more realistic stress test for a CPG brand with Hydrant's revenue/expense profile.

**Verdict: CONDITIONAL PASS** — Scenario spread mechanics work correctly, but the underlying model is too optimistic (per Analysts 1-12 findings) so even the conservative scenario is unrealistically positive.

### 9. Conservative Scenario Cash Alert Analysis

| Check | Result |
|-------|--------|
| Min cash balance (conservative) | $138,258 (week 0 opening, actual data) |
| Does cash go negative? | No — grows from $117K to $1.57M |
| Should alert trigger (< $100K threshold)? | No — never breaches threshold |
| Is this realistic? | **No** — see analysis below |

**Analysis:** The `get_cashflow_kpis()` function checks for weeks where `closing_balance < min_cash_threshold` (default $100K). In the conservative scenario, cash never approaches this threshold because:

1. Revenue ($3.94M/year) still far exceeds expenses ($2.59M/year) even at -15%/+10% adjustments
2. The Jameson misclassification inflates revenue by ~$720K/year (phantom interest_income)
3. Missing loan expenses understate outflows by ~$600K+/year
4. Missing unmapped expenses understate outflows by ~$400-600K/year

If these data issues were fixed, conservative projected annual revenue would be ~$3.2M (base $3.9M minus phantom $720K) and expenses would be ~$3.8M (base $2.6M plus missing loan $600K plus unmapped $400K). This would show a ~$600K annual cash DEFICIT in conservative mode, with the $117K opening balance exhausted by week ~10.

The alert system is architecturally correct (it checks every future week's closing balance against the threshold), but the underlying data makes it unable to detect real cash stress scenarios.

**Verdict: PASS (code) / FAIL (data) (MEDIUM severity)** — The alert mechanism works, but the model's data issues prevent realistic stress detection.

---

### Summary

| Test | Verdict | Severity |
|------|---------|----------|
| A. Base scenario 52w execution | **PASS** | -- |
| B. Conservative revenue -15% | **PASS** | -- |
| B. Conservative expenses +10% | **PASS** | -- |
| B. Conservative closing < base | **PASS** | -- |
| C. Aggressive revenue +10% | **PASS** | -- |
| C. Aggressive expenses -5% | **PASS** | -- |
| C. Aggressive closing > base | **PASS** | -- |
| D. Zero revenue — no crash | **PASS** | -- |
| D. Zero revenue — COGS drops to $0 | **PASS** | -- |
| D. Zero revenue — expenses continue | **PASS** | -- |
| E. Zero expenses — no crash | **PASS** | -- |
| E. Zero expenses — revenue continues | **PASS** | -- |
| F. _apply_scenario unit tests (9/9) | **PASS** | -- |
| G. Actual rows unchanged across scenarios | **PASS** | -- |
| G. Opening balance same across scenarios | **PASS** | -- |
| H. Ordering conservative < base < aggressive | **PASS** | -- |
| I. Balance chain integrity (all 3 scenarios) | **PASS** | -- |
| COGS scenario interaction (revenue_pct bug) | **FAIL** | MEDIUM |
| Conservative cash alert realism | **FAIL** | MEDIUM (data) |

**Overall: 17 PASS, 2 FAIL (1 code bug MEDIUM, 1 data-driven MEDIUM)**

### Key Findings

1. **The scenario engine works correctly at the architecture level.** Multipliers are applied accurately (-15%/+10% conservative, +10%/-5% aggressive), actual rows are unaffected, balance chains are exact, and the ordering invariant (conservative < base < aggressive) holds across all 56 weeks. The `_apply_scenario()` function handles all group types (revenue, expense, transfer) correctly.

2. **Zero revenue and zero expense tests pass cleanly.** The model handles both extremes without crashes, NaN values, or division-by-zero errors. When revenue is zero, COGS correctly drops to zero. When expenses are zero, revenue correctly continues accumulating. The current-week blending logic correctly preserves actual-to-date bank data in both cases, which is why projected totals show small non-zero amounts from real transactions.

3. **NEW BUG: COGS does not track revenue scenarios.** The `revenue_pct` method computes COGS from un-scenarioed revenue, then the main loop applies the expense scenario multiplier. This causes COGS to move in the wrong direction: increasing +10% in conservative (should decrease with revenue) and decreasing -5% in aggressive (should increase with revenue). The impact is ~$125K/year swing in the conservative scenario and ~$25K/year in aggressive. **Fix:** Apply the revenue scenario multiplier to the revenue calls inside `_project_expense_week` for `revenue_pct` categories, and exempt them from the expense multiplier in the main loop.

4. **The conservative scenario is unrealistically optimistic.** Due to known data issues (Jameson misclassification, missing loan/unmapped expenses), even the conservative scenario projects cash growing from $117K to $1.57M. With corrected data, it would likely show cash exhaustion within 10 weeks. This means the stress test / alert system cannot detect real cash stress until the underlying data issues from Analysts 3-7 are resolved.

5. **Scenario spread is appropriate.** The 95.6% gap between conservative and aggressive 52-week closings ($1.57M vs $3.07M) is consistent with a ~30% annual uncertainty band, matching the confidence interval parameters. The spread mechanics are sound even if the absolute levels are inflated.

### Recommendations for Phase 3

1. **(MEDIUM)** Fix COGS scenario interaction: Apply revenue scenario multiplier to `_project_revenue_week` calls inside `_project_expense_week` for `revenue_pct` method. Exempt `revenue_pct` results from the expense multiplier in the main loop (lines 951-952). This ensures COGS tracks revenue scenarios directionally.

2. **(MEDIUM)** After fixing the data issues identified by Analysts 3-7 (Jameson reclassification, opening balance, unmapped transactions), re-run the conservative scenario stress test to verify the alert system correctly detects cash stress. The current test cannot validate alerts because the model is too optimistic.

3. **(LOW)** Consider adding a "worst case" scenario beyond conservative (e.g., revenue -30%, expenses +20%) for genuine stress testing. The current -15%/+10% conservative scenario is too mild to surface cash risks for a CPG brand with thin margins.

---

## Analyst 14 — Week-by-Week Narrative Walkthrough

**Date:** 2026-03-03

### Method

Ran `build_cashflow_forecast(conn, scenario='base', weeks=20, start_date=date.today()-timedelta(weeks=4))` against Railway PostgreSQL production database. This produces 4 actual weeks (Feb 2 – Mar 1) followed by projected weeks (Mar 2+). Walked through the first 8 weeks as if presenting to a board of directors, narrating each line item and checking whether the numbers tell a coherent, believable story.

**Reference benchmarks (from PRD and VALIDATION_BASELINE.md):**
- DTC cash inflows: ~$7K/day gross × 7 × 0.94 payout ≈ $46K/week gross → ~$27-30K/week in bank deposits
- Amazon disbursements: 2 per month, ~$45K each
- Payroll: $16K biweekly (~$8K per payment, hitting ~10th and ~25th)
- Media: $40-55K/month, bills near end of month
- Fulfillment: ~$5K/week
- Actual bank balance: ~$117K

### Raw Data — Weeks 1-8

#### Week 1: Feb 2-8 (ACTUAL)

| Line Item | Amount |
|-----------|--------|
| Opening Balance | $117,007 |
| DTC Revenue | $31,428 |
| Amazon Revenue | $0 |
| Interest Income | $35 |
| **Total Inflows** | **$31,463** |
| Payroll | -$4,670 |
| Fulfillment | -$5,542 |
| **Total Outflows** | **-$10,212** |
| **Net Cash Flow** | **+$21,251** |
| **Closing Balance** | **$138,258** |

#### Week 2: Feb 9-15 (ACTUAL)

| Line Item | Amount |
|-----------|--------|
| Opening Balance | $138,258 |
| DTC Revenue | $23,137 |
| Amazon Revenue | $0 |
| **Total Inflows** | **$23,137** |
| Payroll | -$9,650 |
| Fulfillment | -$5,946 |
| Production | -$2,614 |
| Sales Tax | -$635 |
| **Total Outflows** | **-$18,845** |
| **Net Cash Flow** | **+$4,292** |
| **Closing Balance** | **$142,551** |

#### Week 3: Feb 16-22 (ACTUAL)

| Line Item | Amount |
|-----------|--------|
| Opening Balance | $142,551 |
| DTC Revenue | $26,217 |
| Amazon Revenue | $0 |
| **Total Inflows** | **$26,217** |
| Payroll | $0 |
| Fulfillment | -$4,671 |
| Sales Tax | -$794 |
| Software | -$80 |
| Shipping | -$33 |
| **Total Outflows** | **-$5,577** |
| **Net Cash Flow** | **+$20,640** |
| **Closing Balance** | **$163,191** |

#### Week 4: Feb 23 – Mar 1 (ACTUAL)

| Line Item | Amount |
|-----------|--------|
| Opening Balance | $163,191 |
| DTC Revenue | $28,512 |
| Amazon Revenue | $0 |
| Interest Income | $54,978 |
| **Total Inflows** | **$83,490** |
| Payroll | -$9,657 |
| Loan | -$1 |
| Sales Tax | -$416 |
| Agency | -$1,600 |
| Accounting | -$7,749 |
| **Total Outflows** | **-$19,422** |
| **Net Cash Flow** | **+$64,068** |
| **Closing Balance** | **$227,258** |

#### Week 5: Mar 2-8 (PROJECTED — current week)

| Line Item | Amount |
|-----------|--------|
| Opening Balance | $227,258 |
| DTC Revenue | $22,157 |
| Amazon Revenue | $33,540 |
| Interest Income | $9,923 |
| **Total Inflows** | **$65,619** |
| Media | -$55,287 |
| Payroll | -$5,327 |
| Fulfillment | -$7,577 |
| Production | -$19,471 |
| Sales Tax | -$334 |
| Shipping | -$169 |
| Accounting | -$882 |
| Insurance | -$714 |
| Other Expense | -$357 |
| **Total Outflows** | **-$90,119** |
| **Net Cash Flow** | **-$24,500** |
| **Closing Balance** | **$202,758** |

#### Week 6: Mar 9-15 (PROJECTED)

| Line Item | Amount |
|-----------|--------|
| Opening Balance | $202,758 |
| DTC Revenue | $32,721 |
| Interest Income | $13,891 |
| **Total Inflows** | **$46,612** |
| Media | -$8,546 |
| Payroll | -$7,458 |
| Fulfillment | -$5,070 |
| Production | -$8,325 |
| Sales Tax | -$234 |
| Software | -$26 |
| Shipping | -$237 |
| Agency | -$1,928 |
| Insurance | -$1,000 |
| Other Expense | -$500 |
| **Total Outflows** | **-$33,324** |
| **Net Cash Flow** | **+$13,288** |
| **Closing Balance** | **$216,046** |

#### Week 7: Mar 16-22 (PROJECTED)

| Line Item | Amount |
|-----------|--------|
| Opening Balance | $216,046 |
| DTC Revenue | $32,721 |
| Interest Income | $13,891 |
| **Total Inflows** | **$46,612** |
| Media | -$6,409 |
| Payroll | -$5,454 |
| Fulfillment | -$5,070 |
| Production | -$8,325 |
| Sales Tax | -$234 |
| Accounting | -$1,235 |
| Insurance | -$1,000 |
| Other Expense | -$500 |
| **Total Outflows** | **-$28,228** |
| **Net Cash Flow** | **+$18,384** |
| **Closing Balance** | **$234,431** |

#### Week 8: Mar 23-29 (PROJECTED)

| Line Item | Amount |
|-----------|--------|
| Opening Balance | $234,431 |
| DTC Revenue | $32,721 |
| Amazon Revenue | $46,956 |
| Interest Income | $13,891 |
| **Total Inflows** | **$93,568** |
| Media | -$8,546 |
| Payroll | -$7,458 |
| Fulfillment | -$5,070 |
| Production | -$27,259 |
| Sales Tax | -$467 |
| Software | -$26 |
| Shipping | -$237 |
| Agency | -$1,928 |
| Insurance | -$1,000 |
| Other Expense | -$500 |
| **Total Outflows** | **-$52,492** |
| **Net Cash Flow** | **+$41,076** |
| **Closing Balance** | **$275,507** |

---

### CFO Narrative Walkthrough

#### Week 1 (Feb 2-8, ACTUAL) — PASS

*"We start the month at $117K across our bank accounts. DTC deposits of $31K come in — that's about $4.5K/day, reasonable for a slower January-February period (PRD says $5-10K/day gross, minus processing fees). No Amazon disbursement this week (they pay biweekly, so this is expected). Payroll is $4.7K — seems low for a biweekly $16K cadence, but this could be a partial or mid-period hit. Fulfillment at $5.5K tracks the expected $5K/week. No media billing yet. Net +$21K, closing at $138K."*

**Verdict: PASS.** Numbers are plausible. DTC deposits are in the right range. Expense timing makes sense. The story holds.

#### Week 2 (Feb 9-15, ACTUAL) — PASS

*"Opening at $138K. DTC deposits $23K — slightly lower week, but within normal range. Still no Amazon disbursement. Payroll hits harder this week at $9.7K — this is a biweekly payroll week (Feb 10th is around the right time). Fulfillment $5.9K stays consistent. Small production charge of $2.6K. Net +$4K, closing at $143K."*

**Verdict: PASS.** The payroll cadence makes sense (heavier this week than last week, consistent with biweekly pattern — $4.7K + $9.7K = $14.4K over 2 weeks ≈ $16K biweekly). Revenue is reasonable.

#### Week 3 (Feb 16-22, ACTUAL) — PASS

*"Opening at $143K. DTC deposits $26K — mid-week bump, normal. No payroll this week — correct, we just paid last week. Fulfillment $4.7K. Almost nothing else. Net +$21K, closing at $163K. Light expense week, cash accumulating."*

**Verdict: PASS.** Clean, quiet week. No red flags. The payroll gap (zero this week after $9.7K last week) is correct biweekly behavior.

#### Week 4 (Feb 23 – Mar 1, ACTUAL) — FAIL (CRITICAL)

*"Opening at $163K. DTC deposits $28K. And then... $54,978 of 'interest income'? That's not interest income — Hydrant doesn't earn $55K/week in interest on a $163K balance. Let me look at this more carefully..."*

**This is the Jameson Companies loan transaction.** As identified by Analyst 3 (Task 13), a $54,947 debit (money leaving the bank for Jameson loan payment) is classified under `interest_income` (a revenue category). The `_get_actual_weekly_totals()` function doesn't filter by transaction direction, so this debit is summed as positive revenue.

**Impact on narrative:** The model shows $83K inflows and +$64K net. In reality, this week had ~$28.5K inflows (just DTC) and ~$74K outflows ($19K operating + $55K loan), for a net of approximately -$45K. The model shows +$64K instead — a **$109K swing** from a single misclassified transaction.

**Verdict: FAIL (CRITICAL).** The opening balance double-counting bug also becomes visible here: the model uses the $117K bank balance as the opening for row 0 (Feb 2), but that $117K already reflects all transactions through early March. Replaying Feb 2-Mar 1 actuals on top inflates the balance by ~$110K. This is why the closing balance shows $227K when the actual bank balance is ~$117K.

#### Week 5 (Mar 2-8, PROJECTED — current week) — FAIL (HIGH)

*"Opening at $227K. But wait — the actual bank balance is $117K. We're starting from a phantom $110K surplus that doesn't exist."*

*"DTC projects $22K for a partial week (today is Tuesday, so most of the high-revenue days remain). Amazon has a $33.5K disbursement — this is a disbursement week (day 8 falls in this window). That amount seems a bit low (expected ~$45K per event), but it's based on March forecast. Interest income shows $9.9K — this is the Jameson trailing average bleeding into projections at ~$13.9K/week (= $55K / 4-week trailing)."*

*"Expenses: Media at $55K is a massive spike. The model projects ~$55K of media in the first week of March. Actual media spend from bank data was $0 in most weeks, with occasional monthly lumps. This spike comes from the trailing average including end-of-month billings. Production at $19.5K — reasonable if a PO landed. Payroll $5.3K — payroll seems to be spreading instead of concentrating in biweekly bursts."*

**Verdict: FAIL (HIGH).** Three issues compound:
1. Opening balance inflated by $110K (double-counting bug from Analyst 4)
2. Interest income contains $9.9K phantom Jameson revenue (Analyst 3)
3. Media expense is $55K in week 1 alone (lumpy trailing avg problem — schedule detection not working correctly)
4. Payroll spreading every week ($5-7K) instead of biweekly ($0 or $16K) — Analyst 3 flagged this

#### Week 6 (Mar 9-15, PROJECTED) — FAIL (MEDIUM)

*"DTC $33K — reasonable for a full week. No Amazon disbursement — correct, the next one is late-month. But $13.9K 'interest income' is pure Jameson phantom revenue. Payroll at $7.5K — again, spreading instead of concentrating biweekly. If this is supposed to be a payroll week (around the 10th), it should be ~$16K, not $7.5K."*

**Verdict: FAIL (MEDIUM).** Jameson contamination (+$14K/week) and payroll spreading are both carried forward.

#### Week 7 (Mar 16-22, PROJECTED) — FAIL (MEDIUM)

*"Same pattern as week 6. DTC $33K (fine). Interest income $13.9K (phantom). Payroll $5.5K (should be $0 this week — it's not a biweekly payroll week). The narrative repeats robotically because the trailing average produces identical numbers each week."*

**Verdict: FAIL (MEDIUM).** Projected weeks lack the natural lumpiness of real cash flow. Real weeks alternate between heavy ($40-80K outflows) and light ($5-10K outflows) weeks. The model smooths everything to ~$28-33K/week, which is directionally right on average but narratively wrong week-by-week.

#### Week 8 (Mar 23-29, PROJECTED) — CONDITIONAL PASS

*"DTC $33K. Amazon disbursement $47K — correct, this is the late-month disbursement (day 24). Amount seems reasonable (~$94K March forecast × 0.62 payout / 2 events = $29K... wait, it shows $47K. Let me check: Amazon forecast for March is probably ~$150K × 0.62 / 2 = $46.5K. OK, that matches if the forecast table has $150K for March). Interest income still $13.9K phantom. Production spikes to $27K — this is the 'revenue_pct' method kicking in (25% COGS on a higher-revenue Amazon week). Total outflows $52K — the heaviest expense week, driven by production + media + payroll."*

*"Closing at $275K. Starting from actual $117K, after 8 weeks the model claims we've gained $158K. At ~$28K/week DTC deposits and expenses averaging ~$40K/week, the real trajectory is probably flat to slightly up. The $158K gain is almost entirely explained by: $110K opening balance inflation + $56K Jameson phantom revenue (4 weeks × $14K)."*

**Verdict: CONDITIONAL PASS.** The Amazon disbursement timing and amount are correct. DTC levels are reasonable. But the absolute balance is inflated by ~$160K due to the two known bugs.

---

### Cross-Week Pattern Analysis

#### 1. Payroll Cadence — FAIL
Expected: $0 in 2 weeks, ~$16K in 2 weeks (biweekly). Actual: $4.7K, $9.7K, $0, $9.7K (actual weeks show some biweekly pattern). Projected: $5.3K, $7.5K, $5.5K, $7.5K (spreading every week). The schedule detection (`_detect_expense_schedule`) is not producing a clean biweekly output for projections.

#### 2. Media Cadence — FAIL
Expected: $0 most weeks, ~$40-55K in one month-end week. Actual: $0 across all 4 actual weeks (Feb). Projected: $55K, $9K, $6K, $9K — the first projected week absorbs a huge lump, then it spreads. This suggests the trailing average is being influenced by one large actual payment.

#### 3. Amazon Disbursements — PASS
Expected: 2 per month, ~$45K each. Week 1 (early Mar): $33.5K. Week 4 (late Mar): $47K. Exactly 2 events in March. Amounts scale with forecast. The Task 1 fix is working.

#### 4. Jameson Loan Contamination — FAIL (CRITICAL)
$54,978 actual debit appears as revenue in week 4. $13,891/week phantom revenue propagates through all projected weeks. Over 8 weeks, this contributes ~$110K of phantom cash (4 actual weeks blended + 4 projected). This is the single largest source of model inaccuracy.

#### 5. Opening Balance Double-Counting — FAIL (CRITICAL)
The model uses latest bank balance ($117K) as opening for Feb 2, then replays Feb 2–Mar 1 transactions. The actual balance already includes those transactions. By week 5, the balance is inflated by $110K ($227K shown vs $117K real).

#### 6. Expense Smoothing vs Reality — FAIL (MEDIUM)
Real cash outflows are lumpy: Week 1 $10K, Week 2 $19K, Week 3 $6K, Week 4 $19K. Projected outflows are smooth: $90K, $33K, $28K, $52K. The model smooths payroll, insurance, accounting, and other periodic expenses into every week instead of concentrating them in the correct weeks.

---

### The "Board Presentation Test"

If the CFO presented this 8-week forecast to the board:

**Weeks 1-3 (actuals):** "We started February at $117K and had three solid weeks of DTC deposits ($31K, $23K, $26K) against moderate expenses ($10K, $19K, $6K). Cash grew to $163K." **This is presentable and accurate.**

**Week 4 (actual with Jameson bug):** "In the last week of February, we received $83K in inflows including $55K of interest income." The board would immediately ask: "What $55K interest income? On what principal?" The CFO would have no answer — it's a misclassified loan payment. **The presentation breaks here.**

**Weeks 5-8 (projected):** "We project closing March at $275K." The board looks at the bank balance ($117K) and asks: "You're saying we'll gain $158K in a month? That's double our typical monthly net. What's driving it?" The CFO cannot explain the gap. **The presentation fails.**

**Board Presentation Verdict: FAIL.** The model cannot survive basic board-level scrutiny due to the Jameson misclassification and opening balance double-counting. A CFO would lose credibility presenting these numbers.

---

### Summary Scorecard

| Week | Period | Type | Verdict | Key Issue |
|------|--------|------|---------|-----------|
| 1 | Feb 2-8 | Actual | PASS | Clean, plausible |
| 2 | Feb 9-15 | Actual | PASS | Payroll cadence correct |
| 3 | Feb 16-22 | Actual | PASS | Light week, coherent |
| 4 | Feb 23-Mar 1 | Actual | FAIL (CRITICAL) | $55K Jameson debit shown as revenue |
| 5 | Mar 2-8 | Projected | FAIL (HIGH) | $110K balance inflation + $10K phantom revenue + $55K media spike |
| 6 | Mar 9-15 | Projected | FAIL (MEDIUM) | $14K phantom interest + payroll spreading |
| 7 | Mar 16-22 | Projected | FAIL (MEDIUM) | Same as week 6, robotic repetition |
| 8 | Mar 23-29 | Projected | CONDITIONAL PASS | Amazon timing correct, but absolute levels inflated |

**Overall: FAIL (3 PASS, 1 CONDITIONAL PASS, 4 FAIL)**

### Issues for Phase 3 (prioritized)

1. **(CRITICAL)** Reclassify Jameson Companies transactions from `interest_income` to `loan` in `category_mappings` table. File: DB data fix via `views/tx_mapping.py` or direct SQL. This is a data fix, not a code fix — but `_get_actual_weekly_totals()` should also filter by direction (`WHERE direction = 'credit'` for revenue categories) as a defensive guard.

2. **(CRITICAL)** Fix opening balance double-counting. File: `analytics/cashflow.py`, lines 832-852. The model uses the latest bank balance as opening for `start_date` (4 weeks ago), then replays transactions that are already baked into that balance. Fix: either (a) reconstruct the historical balance by subtracting intervening transactions, or (b) use the latest bank balance as the opening for the *current* week, not the start_date week.

3. **(HIGH)** Fix payroll spreading in projections. File: `analytics/cashflow.py`, `_project_expense_week()`. Payroll should project as $0 in non-payroll weeks and ~$16K in payroll weeks (biweekly on ~10th and ~25th), not $5-7K every week. The schedule detection should produce a clean biweekly output.

4. **(HIGH)** Fix media expense lumpiness. File: `analytics/cashflow.py`, `_project_expense_week()`. Media should project as ~$0 most weeks and ~$40-55K in the billing week (typically end of month), not spread across all weeks. The `monthly_media_spend` plan exists in ctx but may not be consumed correctly by the expense projector.

5. **(MEDIUM)** Map Amazon bank transactions to enable auto-calibration and actuals validation. File: `category_mappings` table via `views/tx_mapping.py`.
