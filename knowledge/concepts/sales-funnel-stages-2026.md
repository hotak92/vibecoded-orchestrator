---
title: Sales Funnel Stages and Bow-Tie Model 2026
type: concept
tags:
  - sales
  - funnel
  - gtm
  - lifecycle
  - bow-tie
  - retention
  - marketing
  - mid-level-architecture
  - b2b
  - b2c
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Sales Funnel Stages and Bow-Tie Model 2026

The classic "TOFU / MOFU / BOFU" funnel was built for outbound-heavy B2B in the early 2010s. It still works as a content-strategy framework but it's incomplete: the funnel doesn't end at close. The **bow-tie model** (Winning by Design, 2019, since adopted across modern revenue ops) extends the funnel through onboarding, expansion, and advocacy — and treats them as equally weighted to acquisition.

## The classic funnel (still useful for content planning)

```
TOFU — Top of Funnel       (Awareness)
  Goal: prospect learns the problem exists / discovers the category
  Content: blog posts, social, podcast appearances, SEO informational
  Metric: reach, traffic, impressions

MOFU — Middle of Funnel    (Consideration)
  Goal: prospect evaluates approaches and shortlists vendors
  Content: comparison pages, case studies, lead magnets, webinars
  Metric: leads, demo requests, signed-up trials

BOFU — Bottom of Funnel    (Decision)
  Goal: prospect picks you, signs a contract / buys
  Content: pricing pages, ROI calculators, sales calls, proposals
  Metric: opportunities, closed-won, ACV
```

Use TOFU/MOFU/BOFU for **content planning** — it forces you to map each piece to an intent stage. Use it for **funnel-stage messaging** in nurture sequences.

Do **not** use it as your operational model. It misses 60% of the customer lifecycle.

## The bow-tie model (the operational truth)

```
   ACQUISITION         |          RETENTION
                       |
TOFU → MOFU → BOFU → CLOSED → ONBOARDED → EXPANDED → ADVOCATE
                       |
       LEFT SIDE       |           RIGHT SIDE
       (lands)         |          (expands)
                       |
   <-- pre-sales --> | <-- post-sales -->
                  CONTRACT
                  (the pinch)
```

The bow-tie has two equally important sides:

- **Left side (acquisition)**: lead → opportunity → closed-won
- **Right side (retention)**: onboarded → activated → renewed → expanded → referred

Modern SaaS economics work because the right side compounds. Net Revenue Retention (NRR) > 100% means existing customers print more revenue this year than last, before any new sales. A 120% NRR business with the same sales team grows 1.2x annually with zero new logos. A 80% NRR business needs to acquire ~25% more new revenue every year just to stand still.

## The stages, named operationally

### Pre-sales (left side)

| Stage         | Definition                                        | Conversion rate (B2B SaaS, healthy) |
|---------------|---------------------------------------------------|--------------------------------------|
| Awareness     | Prospect sees you exist (ad impression, content view) | n/a (scale metric)                  |
| Engaged       | Visited site, opened email, watched video > 30s   | 2–5% of awareness                   |
| MQL           | Marketing-Qualified Lead (downloaded, signed up)  | 10–25% of engaged                   |
| SQL           | Sales-Qualified Lead (right ICP + intent)         | 20–40% of MQL                       |
| Opportunity   | Sales accepted, demo booked                       | 60–80% of SQL                       |
| Proposal      | Quoted, in negotiation                            | 40–60% of opps                      |
| Closed-won    | Signed                                            | 20–35% of proposals                 |

Compound: 100 MQL → ~25 SQL → ~17 opp → ~7 proposal → ~2 closed-won. Healthy MQL → CW conversion is 1–3% in B2B SaaS, higher in PLG (where MQL is replaced by "PQL" — Product-Qualified Lead).

### Post-sales (right side)

| Stage         | Definition                                                 |
|---------------|------------------------------------------------------------|
| Onboarded     | Successfully completed setup; integration working         |
| Activated     | Hit the activation moment (the action correlated with retention — varies by product) |
| Healthy       | Using regularly, low support load, NPS positive            |
| Expanded      | Bought more seats / upgraded tier / added module           |
| Renewed       | Re-signed annual contract                                  |
| Advocate      | Referring others, writing reviews, case-study participant  |
| Churned       | Cancelled, downgraded substantially                        |

**Activation is the most important right-side metric.** Examples by category:
- Slack: 2,000 messages sent in a workspace
- Dropbox: 1 file uploaded from 1 device
- Notion: 5 docs created in 7 days
- HubSpot: 50 contacts imported + 1 email sent

If you don't know your activation event, ask: "What did the users who renewed do in their first 14 days that the users who churned did not?" That's it.

## B2C variations

The funnel shrinks: Awareness → Consideration → Purchase → Repeat. Stages compress to hours/days (e-commerce) instead of weeks/months (B2B). But the bow-tie still applies:

- Right side in B2C = LTV (Lifetime Value), driven by repeat purchase rate, AOV (Average Order Value), referrals
- Acquisition CAC matters less when LTV / CAC > 3x and payback < 12 months
- Loyalty programs, post-purchase email/SMS flows, referral programs ARE the right side

The principle is identical: don't optimize only the acquisition motion.

## Attribution across the funnel

The two extremes (first-touch and last-touch) are both wrong, but for different reasons.

- **First-touch**: gives all credit to the awareness channel — undervalues mid-funnel nurture, never closes the loop on what actually converted
- **Last-touch**: gives all credit to the final channel (often "direct" or "branded search") — undervalues everything that built the awareness

Modern multi-touch options:

- **U-shaped (Position-based)**: 40% first-touch, 40% last-touch, 20% spread across middle touches
- **W-shaped**: 30% first-touch, 30% lead-creation, 30% opportunity-creation, 10% middle
- **Time-decay**: weights touches by recency
- **Data-driven (GA4)**: machine-learned weights from your actual conversion data — best if you have volume (>500 conversions/month)

Vendors who can't afford an MMM (Marketing Mix Model) should run **incrementality tests** instead: pause a channel for 2–4 weeks, measure baseline conversions, compare. Most channels' "attributed" pipeline overstates true incremental by 30–60%.

## The "funnel is dead" debate (and why it's not)

A few influential GTM voices argue the funnel doesn't model how people actually buy (Gartner B2B research: buyers do 5–7 parallel touches, loop back and forth, involve 6–10 stakeholders in any given B2B purchase). They're directionally right. The funnel is a stylized model, not reality.

But the funnel still works as:

- A **content-mapping framework** (every piece maps to a stage)
- A **conversion-rate benchmark** (where are you leaking?)
- A **diagnostic tool** ("low SQL → opp = qualification problem; low opp → proposal = discovery problem; low proposal → close = pricing/value problem")

Use it as a model, not as gospel. If you find yourself optimizing the funnel diagram instead of the customer outcome, you've lost the plot.

## Diagnostic by funnel stage (the practical use)

| Symptom                                    | Likely root cause                                       |
|--------------------------------------------|---------------------------------------------------------|
| Low MQL volume                             | TOFU reach (paid + content + SEO not enough)            |
| MQL → SQL drop                             | Bad ICP fit / bad lead form / mis-targeted top funnel  |
| SQL → Opp drop                             | Qualification process broken; SDRs not booking          |
| Opp → Proposal drop                        | Discovery weak; not finding the buying committee        |
| Proposal → Close drop                      | Pricing mismatch; or sales not closing; or wrong ICP    |
| Closed-won but low onboarded               | Onboarding process broken; setup friction               |
| Onboarded but low activated                | Wrong activation event chosen; product gap              |
| High churn at month 3–4                    | Activation didn't stick; usage habit didn't form        |
| High churn at renewal                      | No expansion conversation; no business outcome shown    |
| No expansion                               | CSM not having QBR; no expansion playbook               |
| No advocacy                                | No NPS / referral program; not asking happy customers   |

Map your funnel to this table once a quarter. Fix the biggest gap before anything else.

## Related

- [[relatedTo::ICP and Buyer Persona Framework]]
- [[relatedTo::Copywriting Frameworks for Sales and Marketing]]
- [[relatedTo::SEO in 2026 — AI Overviews, E-E-A-T, Topical Authority]]
- [[relatedTo::Email Deliverability 2026]]

## References

- Winning by Design — *Bow-Tie Model* (Jacco van der Kooij, 2019)
- David Skok — *SaaSMetrics 2.0* (NRR, activation, cohort analysis)
- Gartner — *Future of Sales* research series (buyer behaviour)
- HubSpot State of Marketing reports (annual, conversion benchmarks)
- OpenView Partners — *PLG benchmarks* (PQL vs MQL)
- ChartMogul / Maxio — SaaS retention benchmarks by ARR band
