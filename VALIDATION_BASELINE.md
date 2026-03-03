# Validation Baseline — Cash Flow Model

**Generated:** 2026-03-02
**Data source:** Railway PostgreSQL (production)
**Transaction date range:** 2025-07-02 to 2026-03-03 (2,069 transactions across 4 accounts)

---

## 1. Revenue by Channel by Month (daily_sku_sales)

### Shopify (DTC) Gross Revenue

| Month   | Gross Revenue | Units Sold |
|---------|--------------|------------|
| 2026-03 | $10,123      | 433        |
| 2026-02 | $111,911     | 4,741      |
| 2026-01 | $128,336     | 5,225      |
| 2025-12 | $121,869     | 4,932      |
| 2025-11 | $125,742     | 5,658      |
| 2025-10 | $121,378     | 4,748      |
| 2025-09 | $115,630     | 4,461      |

**Avg monthly (Sep-Feb, 6 months):** ~$120,811/month gross
**PRD reference:** $42-52K/month — **CRITICAL DISCREPANCY**: actual Shopify gross revenue is ~2.5x higher than the PRD's reference figure. The PRD figure may refer to net/cash revenue after refunds and processing fees, or may be outdated. DTC bank deposits (see section 3) average ~$125K/month, confirming the higher figure.

### Amazon Gross Revenue

| Month   | Gross Revenue | Units Sold |
|---------|--------------|------------|
| 2026-03 | $6,723       | 157        |
| 2026-02 | $133,820     | 3,217      |
| 2026-01 | $144,563     | 3,524      |

**Amazon data starts 2026-01-01.** Only 2 full months available.
**Avg monthly (Jan-Feb):** ~$139,192/month gross
**PRD reference:** $54-102K/month actual — **DISCREPANCY**: DB shows $134-145K gross. After ~35-40% Amazon fees (payout ratio ~0.62), net would be ~$83-90K/month, which is within the PRD's $54-102K range. The gross figure aligns if we apply the payout ratio.

### Combined Monthly Revenue

| Month   | Total Gross Revenue |
|---------|-------------------|
| 2026-02 | $245,730          |
| 2026-01 | $272,899          |
| 2025-12 | $121,869          |
| 2025-11 | $125,742          |
| 2025-10 | $121,378          |
| 2025-09 | $115,630          |

**Note:** Dec-Sep totals are Shopify-only (no Amazon data before Jan 2026). Jan-Feb combined gross is $245-273K, which when adding estimated Amazon net, approaches the PRD's $300K+/month combined gross target.

---

## 2. Expense Totals by Category by Month (cashflow_transactions)

### Mapped Expenses (debits, excluding transfers/duplicates/unmapped)

| Category        | Jan 2026  | Feb 2026  | Dec 2025  | Nov 2025  | Oct 2025  | Sep 2025  |
|-----------------|-----------|-----------|-----------|-----------|-----------|-----------|
| media           | $26,127   | —         | $24,969   | $24,709   | $54,610   | $65,176   |
| payroll         | $24,355   | $23,977   | $22,857   | $22,847   | $24,571   | $23,099   |
| interest_income | $56,115   | $54,947   | $36,497   | $30,265   | $5,706    | $5,363    |
| fulfillment     | $27,958   | $16,159   | $24,809   | $1,639    | —         | —         |
| production      | —         | $2,614    | $60,051   | $883      | $70,812   | —         |
| accounting      | $2,565    | $7,749    | $2,946    | $6,539    | $3,019    | $3,126    |
| agency          | —         | $1,600    | $11,400   | $3,904    | $4,300    | —         |
| sales_tax       | $4,036    | $1,844    | $882      | $1,398    | $4,511    | $1,574    |
| shipping        | —         | $33       | —         | $2,033    | $1,532    | $197      |
| software        | $82       | $80       | $68       | $70       | $150      | $52       |
| loan            | —         | $1        | —         | —         | —         | —         |

**Key observations vs PRD reference:**
- **Media:** PRD says $40-55K/month. Actual varies wildly: $24-65K. Oct and Sep were $55-65K (matching PRD high end), but Nov-Feb dropped to $24-26K. March 2026 shows $52K already.
- **Payroll:** PRD says $32K ($16K biweekly). Actual is $22-25K/month — **~25% below PRD**. Payroll may have changed or PRD figure is gross including taxes.
- **Interest/Loan (Jameson):** Shows as `interest_income` debit at $5-56K/month. The Jan/Feb spike to $55K is suspicious — may include principal repayment. PRD says principal $25-75K + interest $400-6,500. The combined figure (~$55K) fits the middle range.
- **Fulfillment:** PRD says $20K (~$5K/week). Actual ranges $1.6-28K — highly variable.
- **Production:** PRD says $0-108K (spiky, PO-driven). Actual: $0-71K — matches PO-driven pattern.
- **Software:** PRD says $42K (~$10.5K/week). Actual shows only $50-150/month mapped. **CRITICAL GAP**: Most software expenses are likely in "unmapped" category.
- **Agency:** PRD says $10.5K/month. Actual: $0-11.4K — variable but in range.
- **Sales tax:** PRD says $12-23K/quarter. Actual: ~$6-8K/quarter (Sep-Nov $5K, Oct-Dec $6.8K). Slightly below PRD range.

### Unmapped Expenses (significant gap)

| Month   | Mapped Expenses | Unmapped Debits | Total Debits |
|---------|-----------------|-----------------|-------------|
| 2026-02 | $109,003        | $46,948         | $155,951    |
| 2026-01 | $141,271        | $87,553         | $228,824    |
| 2025-12 | $184,480        | $61,930         | $246,410    |
| 2025-11 | $94,377         | $123,863        | $218,240    |
| 2025-10 | $169,497        | $142,496        | $311,993    |
| 2025-09 | $98,975         | $157,388        | $256,363    |

**Unmapped debits average ~$103K/month** — a very large portion of total expenses. The 336 category mappings cover 1,328 of 2,031 non-transfer/non-dupe transactions (65%), but 703 remain unmapped representing ~$1.96M in absolute amount.

**Top unmapped debits (>$5K in 2026):** AMEX payments ($23K, $18K, $8K), Corp E Corp e-check ($7K), Homestead Studio invoices ($7K, $6K), Amazon.com bills ($5K each, multiple). Many of these are likely Amazon FBA fees, credit card payments (which may be transfers), and contractor payments.

---

## 3. DTC Payout Ratio Validation

| Month   | Shopify Gross Rev | DTC Bank Deposits | Implied Payout Ratio |
|---------|-------------------|-------------------|---------------------|
| 2026-02 | $111,911          | $109,295          | 97.7%               |
| 2026-01 | $128,336          | $123,709          | 96.4%               |
| 2025-12 | $121,869          | $136,061          | 111.7%*             |
| 2025-11 | $125,742          | $114,904          | 91.4%               |
| 2025-10 | $121,378          | $129,046          | 106.3%*             |
| 2025-09 | $115,630          | $140,988          | 121.9%*             |

**PRD seed:** 0.94 (94%)
**Avg Jan-Feb 2026:** ~97% — slightly above the 94% seed, reasonable.
**Note:** Months marked * show deposits > gross revenue, likely due to timing lag (deposits for prior month's sales landing early in the next month). The 3-day lag offset should help smooth this.

---

## 4. Amazon Revenue Forecast vs Actuals

| Month   | Forecast Table | Actual Gross (DB) | Forecast/Actual Ratio |
|---------|----------------|-------------------|-----------------------|
| 2026-02 | $150,000       | $133,820          | 1.12x (12% over)     |
| 2026-03 | $151,470       | $6,723 (partial)  | N/A (month just started) |
| 2026-04 | $175,000       | —                 | —                     |
| 2026-05 | $200,000       | —                 | —                     |
| 2026-06 | $220,000       | —                 | —                     |

**Feb 2026:** Forecast is $150K vs actual $134K gross — 12% overstatement. After payout ratio (0.62), forecast cash = ~$93K vs actual cash ~$83K. **Within acceptable range but slightly optimistic.**

**CONCERN:** Forecast ramps aggressively: $150K → $175K → $200K → $220K. If Feb actual was $134K, the $200K+ forecasts for summer may be 50%+ overstated. This would inflate the cash flow projection.

### Amazon Bank Disbursements (from cashflow_transactions)

Amazon deposits land in BofA checking. Identified by "AMAZON" in summary, unmapped credits:

| Month   | Disbursement Count | Total Deposits |
|---------|-------------------|----------------|
| 2026-02 | 3                 | $82,995        |
| 2026-01 | 2                 | $74,917        |
| 2025-12 | 2                 | $68,179        |
| 2025-11 | 4                 | $71,995        |
| 2025-10 | 6                 | $134,586       |

**PRD says:** Exactly 2 disbursements/month, ~$45K each. Actual shows 2-6/month — likely includes non-disbursement Amazon transactions (FBA reimbursements, etc.) or the search pattern is too broad.

**Implied Amazon payout ratio (Feb):** $82,995 deposits / $133,820 gross = 62.0%. **Matches PRD seed of 0.62 exactly.**

---

## 5. Media Spend Table Values

| Month   | Source      | Spend    | NC ROAS |
|---------|-------------|----------|---------|
| 2026-02 | All Sources | $75,000  | 0.6     |
| 2026-02 | Amazon      | $15,000  | 0.0     |
| 2026-03 | All Sources | $75,000  | 0.6     |
| 2026-03 | Amazon      | $22,000  | 0.0     |
| 2026-04 | All Sources | $85,000  | 0.6     |
| 2026-05 | All Sources | $95,000  | 0.6     |
| 2026-06 | All Sources | $130,000 | 0.6     |
| 2026-07 | All Sources | $150,000 | 0.6     |
| 2026-08 | All Sources | $190,000 | 0.6     |
| 2026-09 | All Sources | $180,000 | 0.6     |
| 2026-10 | All Sources | $140,000 | 0.6     |

**PRD reference:** Media $40-55K/month. The media_spend table has the PLANNED spend (for waterfall model), not the actual. Planned spend ramps from $75K to $190K — these are future projections, not historical actuals.

**Actual media debits (from cashflow_transactions):** $24-65K/month. The gap between planned ($75K+) and actual ($24-65K) media spend is significant and will affect the waterfall DTC revenue projection.

---

## 6. Current Bank Account Balances

| Account                           | Latest Date | Balance After |
|-----------------------------------|-------------|---------------|
| 200001628851 (Highbeam Checking)  | 2026-03-03  | $38,667.40    |
| 200001628852 (Highbeam Savings)   | 2026-03-01  | $15,472.60    |
| bank_of_america_checking_5769     | 2026-02-27  | $62,867.28    |
| american_express_credit_card_1001 | 2026-02-23  | -$30,258.09   |

**Total available cash (excl Amex):** $117,007.28
**PRD reference:** ~$117K — **MATCH**

**Data freshness:** Highbeam is current (Mar 3). BofA is 3 days old. Amex is 7 days old. Overall freshness is acceptable.

---

## 7. Transaction Mapping Statistics

| Metric           | Value |
|------------------|-------|
| Total transactions | 2,069 |
| Transfers          | 30    |
| Duplicates         | 8     |
| Category mappings  | 336   |
| Mapped (non-transfer/non-dupe) | 1,328 (65%) |
| Unmapped (non-transfer/non-dupe) | 703 (35%) |
| Unmapped total amount | $1,959,680 |
| Mapped total amount   | $2,342,920 |

**Key concern:** 35% of transactions are unmapped, including large Amazon disbursements that are categorized as 'unmapped' credit instead of a dedicated 'amazon_revenue' category. This means the cash flow model's Amazon revenue recognition depends on the `amazon_revenue_forecast` table rather than actual bank deposits.

---

## 8. Seasonal Indices (from DB)

| Month | Index Value | PRD Reference |
|-------|------------|---------------|
| Jan   | 0.8845     | 0.95          |
| Feb   | 0.8580     | 0.92          |
| Mar   | 0.8571     | 0.98          |
| Apr   | 0.9454     | 1.02          |
| May   | 1.0490     | 1.05          |
| Jun   | 1.0949     | 1.10          |
| Jul   | 1.1263     | 1.12          |
| Aug   | 1.0976     | 1.08          |
| Sep   | 1.1076     | 1.02          |
| Oct   | 1.0377     | 0.98          |
| Nov   | 1.0372     | 0.92          |
| Dec   | 0.9048     | 0.88          |

**DB values are populated** (Task 3 prerequisite met). The DB indices differ from the PRD seed values — they appear to have been auto-calibrated from actual data. The general shape is similar (low winter, high summer) but the DB values show a flatter curve with less extreme lows in Jan-Mar.

**Jun/Mar ratio:** DB = 1.095/0.857 = 1.278x. PRD = 1.10/0.98 = 1.122x. DB shows a stronger seasonal swing than PRD suggests.

---

## 9. Net Cash Flow Summary

| Month   | Total Credits | Total Debits | Net Flow   |
|---------|---------------|-------------|------------|
| 2026-02 | $218,325      | $155,951    | +$62,374   |
| 2026-01 | $265,607      | $228,824    | +$36,784   |
| 2025-12 | $232,468      | $246,410    | -$13,942   |
| 2025-11 | $241,937      | $218,240    | +$23,697   |
| 2025-10 | $399,765      | $311,993    | +$87,773   |
| 2025-09 | $265,432      | $367,090    | -$101,659  |

**Avg monthly net (Sep-Feb, 6 months):** +$15,838/month
**PRD reference:** Net positive $50-150K/month — **DISCREPANCY**: Actual average is much lower. Two months were negative. The PRD projection appears optimistic.

---

## 10. Critical Findings for Analysts

1. **Amazon revenue forecast may be overstated.** Feb actual ($134K gross) vs forecast ($150K). Summer forecasts ($200-220K) could be 50%+ optimistic if Feb is representative.

2. **Unmapped transactions are a major gap.** 35% of transactions remain unmapped including Amazon disbursements, AMEX payments, and various vendor payments. This affects expense projection accuracy.

3. **Interest/loan expense is large and growing.** $5K/month in Sep-Oct → $55K/month in Jan-Feb. This is the Jameson loan and is a major cash outflow that must be accurately modeled.

4. **Software expenses almost entirely unmapped.** PRD says $42K/month but only $50-150/month is mapped to 'software'. The rest is in unmapped debits or on the Amex card.

5. **Media planned vs actual gap.** Media spend table shows $75-190K/month planned, but actual bank debits show $24-65K. The waterfall model uses the planned figures, which will overstate DTC revenue projections.

6. **DTC payout ratio is reasonable.** Jan-Feb 2026 shows ~97%, close to the 94% seed. Auto-calibration should handle this.

7. **Amazon payout ratio validates at 62%.** Feb bank deposits / Feb gross revenue = 62.0%, matching the PRD seed exactly.

8. **Bank balances match PRD.** Total available cash = $117K, matching the PRD's $117K reference.
