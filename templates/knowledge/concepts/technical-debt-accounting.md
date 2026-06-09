---
title: Technical Debt Accounting
type: concept
tags:
- concept
- consulting
- technical-debt
- engineering-management
- mid-level-architecture
- best-practices
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Technical Debt Accounting

A framework for quantifying, communicating, and reducing technical debt in a way that non-technical stakeholders (boards, partners, clients) can act on. Without accounting, debt becomes an engineering grumble that doesn't get budgeted for. With accounting, it becomes a line item that loses to other line items honestly, on its own merits.

## Why this matters

"We have tech debt" loses every budget negotiation against "we have a customer complaint" or "we have a sales opportunity". Without accounting, engineering trades velocity for stability silently — until stability breaks and the bill arrives all at once. With accounting, the firm (and the client) can decide whether to pay the debt down, refinance it, or accept it.

## The four debt classes

Not all debt is the same. Treating it as one category leads to "we have tech debt" framing. Treating it as four lets the firm prioritise.

### 1. Deliberate debt
Decisions made knowingly, with a clear rationale, expected to be paid down later.
**Example**: "Ship the MVP without horizontal scaling; the next architecture iteration will replace the monolith if we get past 100 customers."
**Cost shape**: Bounded. Known. Documentable.
**Action**: Track the trigger that justifies pay-down (in the example: passing 100 customers). When trigger fires, raise the budget.

### 2. Inadvertent debt
Decisions made without realising they were creating debt — usually because the team didn't know better at the time.
**Example**: "We used the wrong database for time-series data; query latency is now untenable."
**Cost shape**: Surprising. Grows as data grows.
**Action**: Once identified, treat as deliberate going forward — name the trigger, name the eventual fix.

### 3. Bit-rot debt
Code that was correct when written but is now out of date because the environment moved.
**Example**: "Framework is three major versions behind; new hires can't onboard onto it; security patches stopped."
**Cost shape**: Continuous, accelerating.
**Action**: Budget for ongoing maintenance, not for one-time pay-down. Bit-rot doesn't stay paid.

### 4. Discovered debt
Debt that was always there but invisible until something surfaced it (incident, audit finding, new requirement, security disclosure).
**Example**: "Penetration test surfaced that authentication has been broken for 18 months."
**Cost shape**: Unknown until measured. Frequently larger than expected.
**Action**: Treat as an unplanned project, not as a maintenance task. Get the budget; don't squeeze it into ongoing work.

## Quantification approaches

### Time-to-fix estimate
**How**: For each debt item, estimate engineer-days to fix.
**Pros**: Direct, intuitive.
**Cons**: Engineers under-estimate by 2-3x reliably. Multiply by a discipline-calibrated factor.

### Interest-rate framing
**How**: Estimate the velocity tax — "this debt costs us 1 day/sprint in slowness, 2 days/sprint in bug fixes traceable to it".
**Pros**: Compounding pattern resonates with finance audience.
**Cons**: Hard to attribute; requires measurement discipline.

### Customer-impact mapping
**How**: For each debt item, list the customer outcomes it blocks ("we can't sell to enterprise because no SSO", "page load > 3s, churn risk").
**Pros**: Translates engineering concern into commercial concern.
**Cons**: Requires honest assessment of which debt items actually affect customers — many don't.

### Replacement-cost framing
**How**: "Building this from scratch would cost X. Maintaining as-is for one more year costs Y. Refactor costs Z."
**Pros**: Reads like a finance proposal.
**Cons**: Triggers "let's rewrite" instinct; most rewrites are net losses.

In practice, USE TWO approaches for any significant debt item: time-to-fix for the engineering plan, and customer-impact OR interest-rate for the stakeholder conversation.

## Communicating debt to non-technical stakeholders

A board / partner / client is not asking "how much code do you want to rewrite". They're asking "how does this affect the business".

### The three-question test
For any debt item the firm wants to raise budget for, answer:

1. **What happens if we do nothing for 6 months?** (specific consequence, not "things get worse")
2. **What does fixing it cost?** (engineer-weeks + opportunity cost)
3. **What does NOT fixing it cost?** (estimated customer impact, incident risk, recruiting impact)

If any of the three can't be answered concretely, the firm isn't ready to raise the budget yet. Go back to estimation.

### The board-deck framing
- ❌ "Engineering wants to refactor the payment service"
- ✅ "Payment service is 4 years old; SOC 2 audit findings will accumulate if not modernised by Q3; estimated risk: re-audit cost + 1 enterprise deal at risk"

### The client framing
For consulting engagements, debt accumulated during the build is the firm's responsibility to surface to the client.

- ❌ Hide it; ship; let client discover it during handover
- ✅ Add a "debt register" to the handover document with the four classes, the trigger conditions, the recommended remediation timeline

The client will respect the honesty far more than they'll resent the news. Hidden debt discovered post-handover is how relationships end.

## Anti-patterns

- ❌ **Lumping all debt into one number** — "we have 6 months of tech debt" doesn't help anyone prioritise
- ❌ **Pure engineer-day estimates without business framing** — finance can't fund what they can't translate
- ❌ **Big-bang rewrites disguised as debt pay-down** — most "let's just rewrite it" projects are net negative
- ❌ **Ignoring bit-rot until the framework is unsupported** — patching at end-of-life is 5x more expensive
- ❌ **"We'll fix it after the next deal closes"** — the next deal never closes the debt; the debt comes due first

## Tracking template

Per debt item:

```yaml
id: TD-001
title: Authentication service uses deprecated JWT library
class: bit-rot         # deliberate | inadvertent | bit-rot | discovered
introduced: 2024-03
discovered: 2026-02   # may equal introduced if known at the time
quantify_engineer_days: 12
quantify_velocity_tax_per_sprint: 0.5 days
quantify_customer_impact: "blocks SOC 2 type II evidence; one enterprise deal at risk"
trigger_to_fix: "SOC 2 audit Q3 2026 OR enterprise deal commit"
recommendation: pay-down before Q3
status: open | in-progress | paid-down | accepted
last_reviewed: 2026-05-19
```

Maintained in a debt register (a file in the repo, a Jira project, or a Notion page — anywhere it's reviewed monthly). The accounting discipline matters more than the tool.

## Links

- [[implements::Client Engagement Lifecycle]] — debt accrues in phase 4 (build), is paid or surfaced in phase 5 (handover), and reappears in phase 7 (steady state)
- [[relatedTo::Consulting CTO Portfolio Coordinator]] — debt across the portfolio is a portfolio-level signal
- [[relatedTo::Consulting Multi-Tenancy Isolation]] — isolation gaps are themselves a class of debt
