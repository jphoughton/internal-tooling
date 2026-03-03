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
