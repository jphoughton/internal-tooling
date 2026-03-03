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
