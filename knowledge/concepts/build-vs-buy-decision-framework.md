---
title: Build vs Buy Decision Framework for Indies
type: concept
tags: [saas, build-vs-buy, vendor, infrastructure, founder, decision-framework, business, mid-level-architecture, active]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Build vs Buy Decision Framework for Indies

## The Core Bias

The indie founder default is **build it**. This is wrong roughly 70% of the time. Building feels productive, sounds fun, and has visible artefacts; buying feels like spending money on something you "could just do yourself." Both intuitions mislead.

Building has hidden ongoing costs — maintenance, security patches, edge cases, team-knowledge-debt — that are invisible until you're already committed. The first 80% of any in-house system takes a week; the next 20% takes the next two years.

This framework forces those hidden costs into the open. The output is a defensible decision, not a "depends" wishy-washy answer.

## The 6 Scoring Axes

Each axis is scored 1–5. The total (6–30) maps to a recommendation.

### Axis 1 — Distance From Core Product

How close is this capability to your **unique value proposition**?

- **5 — IS the core product**: build (your moat is here)
- **4 — directly user-facing core flow**: lean build
- **3 — supports core but generic**: depends on other axes
- **2 — internal tool, doesn't differentiate**: lean buy
- **1 — pure infrastructure**: buy

If the capability is generic (auth, email sending, file storage, billing, error tracking, search), it doesn't differentiate — buying lets you spend the saved time on things that do.

### Axis 2 — Time To Ship

How long does building take, **realistically**? Indie founders systematically under-estimate by 2–3×.

- **5** — >1 month of solo work to a v1: very high build cost
- **4** — 2–4 weeks: high
- **3** — 1–2 weeks: moderate
- **2** — 2–5 days: low
- **1** — <1 day: trivial, build it

Combine with **opportunity cost**: what could you ship instead in that time? If you'd ship a customer-requested feature instead, that's the real cost.

### Axis 3 — Ongoing Maintenance

The under-considered axis. Maintenance is paid in your scarcest resource (attention) for as long as the product lives.

- **5** — needs security patches, monitoring, on-call (auth, payments, anything network-facing)
- **4** — external API integration that breaks when the API changes
- **3** — occasional bugfixes, minor schema migrations
- **2** — stable internal logic
- **1** — write once, forget

Every line of in-house code is a future debugging session. Front-load this cost: estimate hours/year of maintenance × your hourly rate, treat as part of the "build" cost.

### Axis 4 — Vendor Lock-In Risk

The real risk of "buy." Mitigate by choosing vendors with open formats, exportable data, and a competitive market — even if more expensive.

- **5** — extreme: data lives only there, no export, proprietary APIs (e.g. Airtable for production data)
- **4** — high: open formats but heavy migration cost (Auth0, Stripe Billing)
- **3** — moderate: standard APIs but pricing power (most SaaS)
- **2** — low: easy to swap (Postmark / Resend / Mailgun for email)
- **1** — trivial: thin wrapper, providers interchangeable (S3-compatible storage)

### Axis 5 — Vendor Cost Trajectory

What does the cost look like in 18 months?

- **5** — usage-priced, scales with growth: at $50K MRR you'll be paying $5K/mo for it
- **4** — flat with high tier-jumps: pricing cliffs at usage limits
- **3** — flat, reasonable scaling: predictable
- **2** — flat, generous limits: predictable
- **1** — open-source / free / one-time: zero trajectory risk

For capabilities priced as % of revenue (Stripe at 2.9%, Lemon Squeezy at 5%), trajectory is built-in but the build alternative is rarely cheaper at any plausible scale.

### Axis 6 — Capability Risk

What's the downside if your in-house version is mediocre?

- **5** — catastrophic: payment errors, security breaches, data loss
- **4** — high: lost trust, customer churn (auth, billing, deliverability)
- **3** — moderate: minor inconvenience (search ranking, recommendations)
- **2** — low: slightly worse UX (UI components)
- **1** — none: a hobby-quality version is fine (internal admin)

The asymmetry of a security/payment screw-up means buying is usually right when capability risk is 4+. "Saved money" on building auth → 6 months later discover a password-reset hole → lose 50 customers' trust.

## Scoring Rule

Sum the six axes (range 6–30):

| Score | Recommendation |
|-------|----------------|
| 6–12 | **Buy / integrate** — case for building is weak across the board |
| 13–18 | **Buy unless ≥3 strong build signals** — usually buy |
| 19–24 | **Genuine trade-off** — apply tiebreakers |
| 25–30 | **Build** — likely core product or trivial |

A single axis at 5 doesn't override the rest. If "core product distance" is 5 but everything else screams buy, you've probably mis-scoped the question — the *capability* is core, but the *implementation detail* you're considering building isn't.

## Tiebreakers For The 13–24 Band

When the framework is ambiguous:

1. **MRR stage**:
   - <$5K MRR: buy almost everything except the core product
   - $5–25K MRR: buy by default; build only when maintenance is genuinely low
   - $25K+ MRR: you can afford to build more (engineering hire likely on horizon)

2. **Runway**: if <6 months, **never build** anything that delays a revenue-positive ship. Buy and iterate.

3. **Team capacity now**: if you're already context-switching across 4+ areas, buying is almost always right.

4. **Reversibility**: if switching from buy → build later costs <2 weeks, start by buying. Many "build it" decisions made early become "we have technical debt and no time to migrate" later.

## Category Defaults (Indie Patterns)

Based on observed solo-founder outcomes:

| Category | Default | Why |
|----------|---------|-----|
| Payments / billing | **Buy** (Stripe / Lemon Squeezy / Polar) | Capability risk 5, near-impossible to ship as well |
| Authentication | **Buy** initially (Clerk, Supabase Auth, Auth0) | Capability risk 5; build only with a specific reason |
| Transactional email | **Buy** (Resend, Postmark, Loops) | Deliverability is a deep specialty |
| Analytics | **Buy** (PostHog self-host or cloud; Plausible) | Free or cheap, instant value |
| Error tracking | **Buy** (Sentry free tier) | Hours/week saved |
| Support chat | **Buy** (Plain, Crisp) or just email | Building chat = forever debugging |
| Content (blog, docs) | **Buy/use SaaS or static site** (MDX + GitHub) | No upside to in-house CMS |
| Marketing site | **Build** (positioning, voice matters) | Default templates undersell |
| Onboarding flow | **Build** | Tightly coupled to your product |
| Core product features | **Build** | This is your business |
| Admin tools | **Buy** if Retool/Forest works; else minimal build | Maintenance burden underestimated |
| Status page | **Buy** (Instatus, Statuspage) | Cheap; signal of professionalism |
| Search | **Build with a service** (Meilisearch, Algolia, Typesense) | Pure build is hard, pure buy is expensive at scale |

## Anti-Patterns To Push Back On

- "It's cheaper to build" — compute the **real** maintenance cost (your hourly rate × hours/year, capitalised over the product's life) and add to the build cost.
- "We need full control" — control is rarely the real reason. Usually "building is more fun." Challenge: what specifically can't you do with the vendor that matters **now**?
- "I'll just spin up a quick version" — quick versions of auth/payments/email are how indies get hacked or sued.
- "Open-source is free" — your time isn't. Self-hosting OSS is "buy" with much higher implementation cost.
- "We can switch later if it doesn't work" — true for thin wrappers, false for things deeply integrated (auth, billing, data store). Make the irreversibility explicit before deciding.

## Decision Review Trigger

Every build-vs-buy decision should be revisited when **one specific thing changes**: a vendor's price doubles, you hit $25K MRR, the vendor announces a deprecation, the integration breaks for a week. Write the trigger into the decision so future-you doesn't re-litigate it weekly.

## Related

- `[[relatedTo::Merchant of Record vs Stripe for Indies]]` — specific buy decision for payments
- `[[relatedTo::SaaS Pricing Psychology for Solo Founders]]` — pricing the resulting product
- `[[relatedTo::SaaS Metrics Math and Benchmarks]]` — MRR-stage tiebreakers come from metrics

## Sources

- Jason Fried / DHH, *Rework* (chapter on outsourcing) — opportunity-cost framing
- Patrick McKenzie (patio11) writings on solo-SaaS infrastructure decisions
- Joel Spolsky, *In Defense of Not-Invented-Here Syndrome* (counterpoint — read alongside)
- Indie Hackers community surveys of build-vs-buy outcomes 2024–2026
