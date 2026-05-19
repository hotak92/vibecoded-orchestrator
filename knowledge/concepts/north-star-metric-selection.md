---
title: North-Star Metric Selection for Solo SaaS
type: concept
tags: [saas, metrics, business, growth, founder, mid-level-architecture, active]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# North-Star Metric Selection for Solo SaaS

## What a North-Star Metric Is (And Isn't)

A North-Star Metric (NSM) is the **single number** that captures whether your product is delivering value at scale. It's a forcing function: when there's friction between two decisions, the NSM is the tie-breaker.

It is **not**:
- MRR (output, not input — lags by weeks)
- Signups (vanity — most fail to activate)
- Page views (further from value than signups)
- DAU (proxy without commitment — doesn't distinguish loved from tolerated)

A good NSM has three properties:
1. **Closely tied to revenue** — moving the NSM up moves revenue up, with a lag of weeks not months
2. **Reflects customer value** — high NSM means customers got what they came for
3. **Actionable** — you can tie a specific change in product/marketing/support to it

## The Standard NSM Pattern

For most SaaS products, the NSM is some variant of:

> **"Number of [users] who [performed the core value action] in [time window]"**

Examples calibrated to product category:

| Product type | North-Star Metric |
|--------------|---------------------|
| Email tool (Mailchimp-style) | Weekly active senders |
| Async comms (Slack-style) | Weekly active sending messages in 2+ channels |
| Notes / wiki | Weekly active users editing 3+ docs |
| Analytics / dashboards | Weekly active accounts viewing a dashboard with 1+ data source |
| Dev tools | Weekly active accounts running the CLI / API ≥3 times |
| AI tools | Weekly active accounts completing a generation with output downloaded/copied |
| Booking / scheduling | Weekly meetings booked through the tool |
| E-commerce SaaS | Weekly active stores processing ≥1 order |

Note the pattern: **frequency × commitment × outcome**. Weekly (not daily — most SaaS isn't daily-use), with a quality bar (sending, editing, viewing — not just logging in), tied to the core value loop.

## Why "Active Sender" Beats "Active User"

The single biggest mistake in NSM design is using a low-bar event. "User logged in" tells you nothing — they might have logged in to cancel.

Choose an event that:
- **Requires effort** (the user has to *do* the thing, not just open the page)
- **Maps to a value moment** (the thing they came to your product for)
- **Is uncommon enough to filter noise** (if 95% of accounts do it weekly, it's not differentiating)

If you can only pick one event, pick the one that, when a customer does it, makes them ~10x more likely to still be a customer in 90 days. That's your **activation event**, and the count of accounts hitting it weekly is your NSM.

## NSM vs MRR — Why You Need Both

MRR is the outcome. NSM is the leading indicator. They diverge in instructive ways:

| NSM trend | MRR trend | Interpretation |
|-----------|-----------|----------------|
| ↑ | ↑ | Healthy growth |
| ↑ | flat | New users activating but not paying — pricing/packaging issue |
| flat | ↑ | Existing customers paying more (expansion); new acquisition stalled |
| ↑ | ↓ | Activation up, but churn higher — onboarding fine, retention broken |
| ↓ | ↑ | Customers paying but using less — silent churn coming in 60–90 days |
| ↓ | ↓ | Crisis. Stop building features, find the leak. |

The "↓ NSM, ↑ MRR" cell is the dangerous one — MRR looks fine, but the next quarter will be bad. NSM gives you 60–90 days of warning.

## Counter-Metrics: Avoid Goodhart's Law

If you only optimise the NSM you'll game it. A counter-metric prevents that.

Example pairing:
- NSM: weekly active senders in an email tool
- Counter: spam complaint rate, list-bounce rate

If you let users send to lists they shouldn't, NSM goes up but the business dies. Counter-metric guards against that.

Other useful counter pairings:
- Activation rate ↔ paid conversion rate (don't let "activated" creep down-funnel)
- Feature adoption ↔ feature retention (a tutorial drives first use but not real use)
- API calls ↔ error rate (more calls aren't valuable if half fail)

## Cohort vs Aggregate

Always look at the NSM **per signup cohort**, not just aggregate. Aggregate hides survivorship bias — your "growing active user count" might be entirely driven by the long tail of pre-2024 cohorts still using the product, while new cohorts are failing to activate.

Minimum slicing:
- By signup week (or month if low-volume)
- By acquisition channel (paid vs organic vs referral — they behave differently)
- By plan tier (free vs paid often diverge)

A cohort table with each row = signup week, each column = weeks-since-signup, cells = % of cohort still hitting NSM event. Read columns to see retention curves; read rows to compare cohorts. If column 8 is consistently higher than column 4, you have a delayed-activation product. If it's consistently lower, you have a churn-curve.

## Choosing Your NSM: A Practical Process

For a solo founder shipping a product currently in the $0–10K MRR range:

1. **List 5 events** customers can do in your product
2. For each event, ask: "If a new customer did this in week 1, how much more likely are they to still pay in month 3?" (Use whatever data you have — even 30 customers is enough to see signal)
3. Pick the event with the strongest 3-month-retention lift
4. Define the NSM as "weekly accounts doing this event"
5. Display this number on a single dashboard you check daily
6. Pair with 1 counter-metric

Re-evaluate every 6 months. As the product evolves, the activation event may shift (e.g. early Notion = pages created; mature Notion = team workspaces).

## Related

- `[[implements::SaaS Pricing Psychology for Solo Founders]]`
- `[[relatedTo::Churn Taxonomy and Reduction Tactics]]`

## Sources

- Sean Ellis (Growth Hackers) original framing of NSM circa 2017 — concept has stabilised since
- *Reforge* and *Lenny Rachitsky* essays on NSM selection in product-led growth contexts
- Wikipedia, *Customer lifetime value* (https://en.wikipedia.org/wiki/Customer_lifetime_value) — verified 2026-05-19, ties NSM to LTV mechanics
