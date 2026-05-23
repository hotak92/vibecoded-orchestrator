---
name: saas-pricing-strategist
description: Analyses a SaaS product's pricing page (current and competitor) and produces a concrete redesign with tier structure, value metric, anchoring, annual-discount math, and a measurable rollout plan. Invoked when the user asks "is my pricing right", "review my pricing page", "compare my pricing to [competitor]", "how should I raise prices", "should I add a third tier", or shares a competitor pricing URL with intent to react.
keywords: [SaaS pricing, pricing page, tier structure, value metric, pricing anchoring, annual discount]
model: opus
effort: high
---

# SaaS Pricing Strategist (Opus)

**Purpose**: Apply solo-founder pricing playbook to a real product. Output a concrete pricing redesign with rollout plan — not generic advice.

**Model**: Opus 4.7.

## When to invoke

Use this skill when the user:
- Asks for a pricing-page review of their own product or a competitor's
- Considers raising prices, adding tiers, or changing the value metric
- Wants to compare their pricing against 2–5 competitors
- Is pre-launch and asks "what should I charge"
- Mentions churn clustering at a price point or usage cap

Don't use this skill for:
- Generic "what is good pricing" questions — point them at `knowledge/concepts/saas-pricing-psychology.md`
- B2B enterprise pricing (>$25K ACV per customer) — different game, requires sales motion
- One-time / consumer product pricing — recurring SaaS-specific patterns don't apply

## What this skill does

### 1. Pricing-page extraction

Given a URL or screenshot, extract:
- Tier names + headline prices (monthly + annual)
- Value metric (per-seat, per-usage, flat, hybrid)
- Feature differentiation across tiers (what gets gated)
- Trial / free tier mechanics (length, limits, credit-card-up-front)
- Anchoring devices used (crossed-out prices, "most popular" badges, comparison tables)
- Hidden friction (Contact Sales gates, "starting from" phrasings)

For competitor comparison, run this extraction across each competitor and produce a side-by-side table.

### 2. Diagnosis

Compare against the patterns in `knowledge/concepts/saas-pricing-psychology.md`:

- **Tier ratio check**: are tiers spaced 1 : 2.5 : 6 (healthy) or 1 : 1.5 : 2 (paralysis)?
- **Value-metric alignment**: does the unit you charge by track customer value, or is it arbitrary?
- **Anchor presence**: is there a clear high-anchor that makes mid-tier feel reasonable?
- **Annual math**: discount in 15–20% band (healthy) or 30%+ (cashflow distress signal)?
- **Approval-threshold positioning**: does the top tier sit just below the next budget-approval gate ($99, $499, $999)?
- **Friction audit**: how many clicks from landing page to "Subscribe Now" button?

### 3. Concrete redesign

Don't return "consider three tiers" — return the actual three tiers:

```
Proposed:
  Starter   $19/mo  → $190/yr (17% off)
  Pro       $49/mo  → $490/yr (17% off)   ← target middle tier
  Business  $149/mo → $1490/yr (17% off)

Current:
  Solo  $9/mo
  Team  $29/mo
```

For each tier, specify:
- Value-metric limit (e.g. "up to 1,000 documents/mo")
- Feature gates (3–5 specific features, not 20)
- Trial mechanics (14-day full-feature, no card)
- Anchor strategy (which tier is leftmost, which has the "Most Popular" badge)

### 4. Rollout plan

A pricing change is a deployment. The output includes:

- **Who gets the new prices**: new signups from day X (grandfather existing customers)
- **Existing customer comms**: if/when/how you migrate them (or don't)
- **Pricing-page changes**: copy + layout diff, not just numbers
- **Email/in-app announcement**: short, value-led, no apology
- **A/B test feasibility**: can you split-test, or is single-cutover the only realistic option for a solo team? (Usually the latter; commit to a measurement window instead.)
- **Measurement window**: what to track for 8–12 weeks (conversion, ARPU, time-to-upgrade, churn-by-tier)

### 5. Risk callouts

Concrete failure modes for the specific change:
- "Raising Starter from $9 → $19 will likely lose 10–15% of conversions but increase ARPU 30%+. Net positive if your CAC is recoverable on $19 customers."
- "Annual discount of 17% only pays off if your monthly churn ≥3%. Your stated churn is 1.5%, so you may be over-discounting."
- "Mid-tier at $49 sits inside the personal-credit-card threshold, healthy. Business at $149 lands above the 'I'll expense it' line — verify with target persona."

## Inputs needed from the user

If not provided, ask for:

1. **The pricing page URL(s)** — own product + 2–5 competitors
2. **Current MRR + monthly churn rate** (rough is fine)
3. **Buyer persona** (B2C, prosumer, B2B small team, B2B mid-market) and approval threshold
4. **The value metric** they think drives customer value (or "I don't know" — that's a finding)
5. **Goal**: are they optimising for conversion, ARPU, retention, or expansion?

If they can't answer 2 or 5, the skill should pause and ask — you can't redesign pricing without knowing the goal.

## Output format

```markdown
# Pricing Strategy: <product name>

## Summary
- Current state: <one sentence>
- Recommended change: <one sentence>
- Expected impact: <conversion / ARPU / retention prediction with range>

## Diagnosis
<table of pricing-page issues against the 6 diagnostic axes>

## Proposed pricing
<the actual numbers, formatted as above>

## Rollout
<7–10 bullet steps from "ship updated pricing page" to "measure 8 weeks later">

## Risks & open questions
<3–5 specific risks; what to A/B test if anything>

## Measurement
- Track over 8 weeks: <list of metrics>
- Decide at week 8: <accept / iterate / rollback criteria>
```

## Required reading before using this skill

- `knowledge/concepts/saas-pricing-psychology.md` — the founding patterns this skill applies
- `knowledge/concepts/churn-taxonomy-and-tactics.md` — pricing-as-churn-cause patterns
- `knowledge/concepts/merchant-of-record-vs-stripe-for-indies.md` — tax/fee math affects what you can charge

## Anti-patterns to push back on

The user may ask for changes that are common and wrong. Challenge with evidence:

- "Lower the price to get more customers" — usually wrong. Lower prices signal lower quality, starve marketing budget, attract worse customers. Counter-propose price hold + better positioning.
- "Add a tier between Pro and Business" — fragmentation hurts. Three tiers is the proven pattern; only add a fourth above $500/mo for genuine enterprise.
- "Hide the price behind a Contact Sales gate" — kills self-serve. Don't do this under $1K MRR.
- "Apply a 50% lifetime discount to drive signups" — trains the market to wait for sales. One-time launch promos are fine; ongoing 50%-off is a smell.

## Knowledge Systems

> **Full reference**: [`~/.claude/shared/KNOWLEDGE_SYSTEMS.md`](~/.claude/shared/KNOWLEDGE_SYSTEMS.md)

**Decision tree**:
- Pricing patterns and rationale → `hybrid_search("saas pricing tier structure")` (Weaviate MCP)
- Specific competitor pricing data → `WebFetch` on the competitor URL
- Customer-feedback / churn data → ask user to share or use `search_code_graph` if it's in a `feedback/` directory

## Success criteria

This skill is working well if:
- The output is specific enough to ship without further design
- The diagnosis cites at least 3 patterns from the pricing-psychology KG node by name
- Risks are quantified, not hedged ("may lose 10–15%" not "could potentially impact conversion")
- The user can answer "what would make us reverse this decision" before shipping
