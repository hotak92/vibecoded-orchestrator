---
title: SaaS Metrics Math and Benchmarks
type: concept
tags: [saas, metrics, business, ltv, cac, churn, mrr, founder, mid-level-architecture, active]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# SaaS Metrics Math and Benchmarks

## Why The Definitions Matter

Two founders with the same Stripe export can compute wildly different "LTV" numbers and reach opposite conclusions. The trap is that every SaaS metric has **at least two defensible definitions** and the indie community tosses around the names without specifying which. The first job of any metrics analysis is fixing the definitions in writing, on the same page as the numbers.

This node is the cheat sheet: exact formulas, the gotchas in each, and the stage-appropriate benchmarks for comparison. It complements `[[relatedTo::SaaS Pricing Psychology for Solo Founders]]` (which is strategy) and `[[implements::Churn Taxonomy and Reduction Tactics]]` (which decomposes churn into actionable buckets).

## Core Formulas

### MRR and Its Movements

- **MRR** = sum of monthly-normalised active subscription value. **Annual plans divided by 12.** A $1,200/yr customer is $100 MRR, not $1,200 — sub this in if your dashboard inflates MRR by lumping annual upfront.
- **ARR** = MRR × 12. Display only — not operationally distinct.
- **New MRR** = MRR from subscriptions created this period
- **Expansion MRR** = MRR added via upgrades from existing customers (seat additions, tier-ups, usage growth)
- **Contraction MRR** = MRR lost via downgrades (still subscribed, lower plan)
- **Churned MRR** = MRR lost via cancellations
- **Net New MRR** = New + Expansion − Contraction − Churned

### Churn

- **Gross MRR Churn %** = Churned MRR ÷ MRR at start of period
- **Net MRR Churn %** = (Churned + Contraction − Expansion) ÷ MRR at start of period
  - Negative net churn (expansion > losses) is the signature of compounding SaaS
- **Logo (customer) Churn %** = customers churned ÷ customers at start of period
  - Logo churn ≠ MRR churn. If your big customers stay and your small ones leave, logo churn is high but MRR churn is low. Always report both.

### Quick Ratio (Acquisition Health)

- **Quick Ratio** = (New MRR + Expansion MRR) ÷ (Churned MRR + Contraction MRR)
- Interpretation: 1.0 means you're treading water. >2 = healthy growth, >4 = strong. <1 = shrinking.
- A 4-month rolling average is more useful than month-to-month (single bad month can throw it).

### LTV (Lifetime Value) — Two Definitions

- **Simple LTV** = ARPU ÷ Gross Monthly Churn
  - Caveat: assumes churn is constant. It usually isn't — early-cohort churn is higher; survivors churn less with tenure. Simple LTV **over-estimates** in early-stage products.
- **Cohort LTV** = sum of revenue across a closed cohort, normalised per acquired customer
  - More accurate but requires ≥6 months of data; useless before then.
- Report both when data permits, with definitions. If you only have simple LTV, label it "estimate, assumes flat churn."

### ARPU

- **ARPU** = MRR ÷ Active customers (paying)
- Don't average $0 free-tier users into ARPU — that's a different metric ("ARPA" for accounts, or ARPU including trials, but call it out).

### CAC and Payback

- **CAC** = total acquisition spend ÷ new paying customers (in the same period)
  - "Acquisition spend" includes: ads, sponsorships, content (proportional), affiliate payouts, paid tools attributable to growth. Excludes: founder salary, generic infrastructure.
- **LTV : CAC ratio** = LTV ÷ CAC. Target **>3** for sustainable SaaS.
  - **>10 is usually a warning sign**: under-investing in acquisition, leaving growth on the table.
- **CAC Payback (months)** = CAC ÷ (ARPU × Gross Margin)
  - Solo SaaS gross margin is typically 0.80–0.90; assume 0.85 if unknown.
  - Healthy payback: <12 months for SMB, <18 for mid-market.

### Net-Dollar Retention (NDR / NRR)

- **NDR** = (start-of-period MRR + Expansion − Contraction − Churn) ÷ start-of-period MRR
- Measured over a 12-month window for a cohort or the whole book.
- **>100% is the holy grail** — existing customers grow faster than they leave. NDR 110%+ is best-in-class SaaS.

### Magic Number (Capital-Efficient Growth)

- **Magic Number** = (Net New ARR for quarter × 4) ÷ Sales & Marketing spend for prior quarter
- Indie variant: replace S&M with all acquisition spend.
- Interpretation: >1 = capital-efficient growth, >0.75 = acceptable, <0.5 = burning cash unproductively.

### Rule of 40 (RoR40)

- **Rule of 40** = Revenue growth rate (%) + Profit margin (%)
- A score ≥40 signals balanced growth vs profitability. Public SaaS benchmark; useful for indies primarily as a sanity-check the second they raise money or consider doing so.

## Cohort Retention Tables (The Single Most Important Table)

A cohort table is rows × columns:

- **Rows**: signup month (or week)
- **Columns**: months since signup (M0, M1, M2, …, M12)
- **Cells**: % of cohort still paying

```
            M0    M1    M2    M3    M6    M12
Jan 2026   100%  78%   72%   68%   62%   55%
Feb 2026   100%  82%   75%   71%   65%    —
Mar 2026   100%  85%   80%   76%    —     —
```

Read it three ways:
1. **M0 → M1 cliff**: bad onboarding. Most M1 drop-offs never used the product enough.
2. **Diagonal (improving rows)**: each new cohort retains better than the last — onboarding/product is improving.
3. **Plateau (M6/M12 stabilising)**: the customers who survive 6 months are likely to stay forever. That stable retention rate is the asymptotic churn rate — use it to bound LTV.

## Stage-Appropriate Benchmarks

Benchmarks are rough industry medians. Your product economics can be very different — interpret as guardrails, not goals.

| Stage (MRR) | Monthly churn (B2C) | Monthly churn (B2B SMB) | LTV:CAC | Quick Ratio |
|-------------|----------------------|--------------------------|---------|-------------|
| <$1K | "Don't worry yet" | "Don't worry yet" | n/a | n/a |
| $1K–10K | <6% | <4% | >2 | >2 |
| $10K–50K | <5% | <3% | >3 | >3 |
| $50K+ | <4% | <2% | >3 | >4 |

Stage <$1K: don't compute LTV at all. The sample is too small, your definitions aren't stable, and the time goes further into customer interviews. See `[[implements::Customer Development for Indie Founders]]`.

## Definitional Risks (How Numbers Lie)

- **Annual subscriptions inflate MRR if not divided by 12.** A $1,200/yr customer should appear as $100 MRR, not $1,200.
- **Trial-converters double-counted.** If a trialer pays on day 1, don't book it as both "trial conversion" and "new MRR" in two reports.
- **Refunded charges still in MRR.** Filter refunded transactions before summing.
- **Test customers in production data.** Always exclude internal accounts before computing churn.
- **Mixed currencies.** Don't sum USD + EUR; normalise to one currency at a fixed FX rate (note the rate used).
- **Single-month LTV at a 4-week-old product.** Refuse the request; advise customer-development instead.
- **Vanity LTV:CAC > 10.** Usually means CAC is artificially low (you're not buying any customers). Read as "under-investing in growth," not "great economics."

## The Single Recommended Action Pattern

A health-check that ends with a list of 10 things is a checklist, not a diagnosis. Force prioritisation: pick the **one** metric that's most off-benchmark, and the **single** highest-leverage fix. Examples:

- *Gross churn 8% (benchmark 4%), clusters at M2 → fix onboarding to first-value, everything else is downstream.*
- *Quick Ratio 1.3 → stop building features for 30 days, work on retention (Stripe Smart Retries, cancel survey, pause option).*
- *LTV:CAC 5.8 → triple ad spend on the lowest-CAC channel; you're under-investing.*

## Related

- `[[relatedTo::SaaS Pricing Psychology for Solo Founders]]` — pricing is upstream of ARPU and churn
- `[[implements::Churn Taxonomy and Reduction Tactics]]` — the decomposition that follows a high-churn finding
- `[[relatedTo::North-Star Metric Selection for Solo SaaS]]` — the NSM sits upstream of MRR
- `[[relatedTo::SaaS Pricing Rollout Playbook]]` — how to measure a pricing change

## Sources

- ChartMogul, *SaaS metrics reference* (https://chartmogul.com/blog/saas-metrics/) — formula conventions
- David Skok, *SaaS Metrics 2.0* (https://www.forentrepreneurs.com/saas-metrics-2/) — Quick Ratio, Magic Number origins
- Brad Feld / Jason Lemkin commentary on Rule of 40 and NDR benchmarks
- Stripe Atlas SaaS metrics guide — definitions cross-checked 2026-05-19
