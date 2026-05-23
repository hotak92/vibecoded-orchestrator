---
name: saas-metrics-health-check
description: Performs a diagnostic on a SaaS product's revenue and retention metrics from a CSV or spreadsheet of customers/subscriptions/events. Computes MRR, ARR, churn (gross/net), LTV, CAC, payback period, quick ratio, and cohort retention; flags anomalies; identifies the single highest-leverage fix. Invoked when the user asks "are my SaaS metrics healthy", "compute LTV for my data", "is my churn high", "review my MRR breakdown", or shares a billing/customer CSV.
keywords: [MRR, ARR, churn, LTV, CAC, quick ratio, cohort retention, SaaS metrics, "SaaS pricing"]
model: opus
effort: high
---

# SaaS Metrics Health-Check (Opus)

**Purpose**: Turn a messy customer/subscription CSV into a defensible SaaS health report with a single recommended action.

**Model**: Opus 4.7.

## When to invoke

Use this skill when the user:
- Hands over a billing export (Stripe, Lemon Squeezy, Paddle) and asks for a diagnostic
- Asks "is my churn high" or "what should LTV/CAC be at my stage"
- Wants a monthly metrics dashboard for the first time
- Is preparing a fundraise update or YC application and needs defensible numbers
- Shares MRR figures and asks "is this healthy"

Don't use this skill for:
- "What is MRR" — point to a tutorial; this skill assumes founder-literacy
- Real-time dashboards — this is a one-shot diagnostic; for ongoing tracking recommend Baremetrics / ChartMogul / a self-built dashboard
- Pre-revenue products (<5 paying customers) — sample size too small; do customer interviews instead

## What this skill does

### 1. Data validation

Given a CSV, first verify:

- **What's in the file**: customers, subscriptions, charges, invoices, events?
- **Date range covered** and granularity (daily/monthly)
- **Currency** (assume one; if multi-currency flag for normalisation)
- **Missing fields** that block analysis (no churn timestamps, no plan info, no MRR field — request them)
- **Deduplication issues** (test customers, refunded charges, $0 transactions)

If the data is unusable, stop and ask for the right export. Don't fabricate fields.

Common providers and their exports:
- **Stripe**: Dashboard → Reports → Billing Overview, or `/v1/subscriptions` API with `expand[]=customer`
- **Lemon Squeezy**: Dashboard → Reports → CSV export
- **Paddle**: Subscriptions list export with status + MRR

### 2. Core metric calculation

Compute and report with the **exact definitions** used:

- **MRR** (Monthly Recurring Revenue): sum of monthly-normalized active subscription value. Annual plans divided by 12.
- **ARR**: MRR × 12 (display only; not an operationally distinct number)
- **New MRR**: MRR from subscriptions created this month
- **Expansion MRR**: MRR added via upgrades from existing customers
- **Contraction MRR**: MRR lost via downgrades (still subscribed, lower plan)
- **Churned MRR**: MRR lost via cancellations
- **Net New MRR** = New + Expansion − Contraction − Churned
- **Gross MRR Churn %** = Churned MRR / MRR at start of month
- **Net MRR Churn %** = (Churned + Contraction − Expansion) / MRR at start of month
  - Negative is great (expansion > churn)
- **Logo (customer) churn %** = Customers churned / Customers at start of month
- **Quick Ratio** = (New + Expansion) / (Churned + Contraction)
- **ARPU** = MRR / Active customers
- **LTV** (simple) = ARPU / Gross Monthly Churn
  - Caveat: assumes churn is constant; over-estimates LTV when churn declines with tenure (it usually does — earliest customers churn fastest). Report alongside cohort-LTV (sum of revenue across a closed cohort) when data permits.
- **CAC** = Acquisition spend / New paying customers (ask user for the spend figure)
- **LTV : CAC ratio** = LTV / CAC (target >3 for SaaS health)
- **CAC Payback (months)** = CAC / (ARPU × Gross Margin)
  - Solo founders: assume Gross Margin = 0.85 unless told otherwise (most SaaS run 70–90%)

### 3. Cohort retention table

Build a cohort table:
- Rows: signup month
- Columns: months since signup (M0, M1, M2, ..., M12)
- Cells: % of cohort still paying

This is the single most important table in SaaS metrics — it surfaces:
- Whether retention is improving over time (compare row-by-row at fixed column)
- Whether onboarding has a high M1 drop-off (M0→M1 cliff)
- Whether there's a delayed-activation curve (low M1 but high M6)

### 4. Diagnosis

Compare measured values against stage-appropriate benchmarks:

| Stage (MRR) | Monthly churn (B2C) | Monthly churn (B2B SMB) | LTV:CAC | Quick Ratio |
|-------------|----------------------|--------------------------|---------|-------------|
| <$1K | "Don't worry yet" | "Don't worry yet" | n/a | n/a |
| $1K–10K | <6% | <4% | >2 | >2 |
| $10K–50K | <5% | <3% | >3 | >3 |
| $50K+ | <4% | <2% | >3 | >4 |

(Benchmarks are rough industry medians; individual product economics vary.)

Flag:
- Any metric **>2x worse than benchmark** as a "blocker"
- **Anomalies** in cohort table (one bad month, one cliff)
- **Definitional risks** (e.g. if there's no annual subscription handling, churn % will be wrong)

### 5. The single recommended action

End the report with **one** prioritised action — not a list of 10 things. The point of a diagnostic is forcing prioritisation.

Examples of well-formed recommendations:
- "Your gross churn is 8% (benchmark 4%) and clusters at month 2. The onboarding sequence is your highest-leverage fix. Audit the signup → activation → first-value flow this week; everything else is downstream."
- "Quick ratio of 1.3 means acquisition barely outruns churn. Stop building features. Spend next 30 days on retention — turn on Stripe Smart Retries (involuntary churn), add a cancel survey (voluntary), add a pause-instead-of-cancel option."
- "LTV:CAC is 5.8 — you're under-investing in acquisition. Triple your ad spend on the channel with lowest CAC; you have 4 months of runway in the math even at 50% efficiency degradation."

## Inputs needed from the user

If not provided, ask for:

1. **The data export** (CSV preferred; can also work from numbers pasted as a table)
2. **Currency** and whether the numbers are normalised
3. **Approx monthly acquisition spend** (ads + content + tools attributable) — needed for CAC
4. **Approximate gross margin** if unusual — needed for CAC payback
5. **Customer segment** (B2C / prosumer / B2B SMB / B2B mid) — sets benchmark band
6. **Time horizon of the data** — anything <3 months is too short for meaningful LTV

If they don't have export data, the skill should not invent numbers — instead pivot to "let's set up the tracking first" and recommend a 1-line dashboard.

## Output format

```markdown
# SaaS Health-Check: <product name>

## Headline
- MRR: $X
- Net MRR growth (last 3 mo avg): $Y/mo (Z%)
- Gross monthly churn: A%
- Quick ratio: B
- LTV:CAC: C
- Overall verdict: <Healthy / Acceptable / Concerning / Crisis>

## Metrics (with definitions)
<table: metric, value, benchmark, status>

## Cohort retention
<table or text-rendered grid>

## Anomalies
- <flagged issue 1, with month + size>
- <flagged issue 2>

## What changed vs last period
<if multi-period data was given>

## Single recommended action
<one paragraph; the highest-leverage next move>

## Open questions / data gaps
<things that would sharpen the diagnosis if available>
```

## Data hygiene rules

- **Never fabricate**: if a metric requires data you don't have, mark it `n/a — need X`
- **Show your work**: state the definition used for each metric (LTV is especially ambiguous)
- **Flag double-counting**: if the CSV mixes successful and refunded charges, separate them
- **Currency**: don't sum across currencies without normalisation; flag if mixed

## Required reading before using this skill

- `knowledge/concepts/churn-taxonomy-and-tactics.md` — for interpreting churn breakdowns
- `knowledge/concepts/saas-pricing-psychology.md` — pricing as a churn cause
- `knowledge/concepts/north-star-metric-selection.md` — the NSM is upstream of revenue metrics

## Anti-patterns to push back on

- "Just give me the LTV" with a 4-week-old product — refuse; LTV is meaningless before 6+ months of churn data
- "Compare me to <Notion / Stripe / Slack>" — those are post-product-market-fit benchmarks, irrelevant for an indie
- "I want to look good for investors" — the diagnostic isn't a marketing exercise; if the metrics are weak, the right move is to fix them before the deck, not to spin them
- LTV:CAC > 10 — usually means under-investing in acquisition, not "great economics"

## Knowledge Systems

> **Full reference**: [`~/.claude/shared/KNOWLEDGE_SYSTEMS.md`](~/.claude/shared/KNOWLEDGE_SYSTEMS.md)

**Decision tree**:
- SaaS-metric definitions and patterns → `hybrid_search("SaaS metrics LTV churn definition")` (Weaviate MCP)
- Stage-appropriate benchmarks → consult the table above + KG nodes
- Data parsing → standard Python via Bash; pandas if available, csv module if not

## Success criteria

This skill is working well if:
- Every metric has the definition used printed next to the value
- The recommendation is one action, not a list
- Anomalies are flagged with month + magnitude, not "high"
- Output is short enough to read in 3 minutes
- The founder can take the recommendation and start work the same day
