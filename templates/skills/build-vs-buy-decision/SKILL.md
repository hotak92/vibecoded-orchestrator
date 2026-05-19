---
name: build-vs-buy-decision
description: Helps a solo founder or small team decide whether to build a feature in-house, buy/integrate a SaaS, or defer it. Considers MRR stage, team capacity, vendor lock-in risk, ongoing maintenance cost, and the "default to building" indie bias. Invoked when the user asks "should we build or buy [feature]", "is it worth integrating [tool]", "should we replace [vendor] with our own implementation", or "what's the cheapest way to ship [capability]".
model: opus
effort: high
---

# Build-vs-Buy Decision (Opus)

**Purpose**: Apply a calibrated framework — not generic advice — to a specific build-or-buy choice. Output is a recommendation with explicit assumptions, not a "depends" wishy-washy answer.

**Model**: Opus 4.7.

## When to invoke

Use this skill when the user:
- Compares building vs integrating a vendor for a specific capability
- Asks "is X worth $Y/mo" for a SaaS tool
- Wants to know whether to replace a current SaaS with an in-house version (rebuild)
- Frames the decision around team size, runway, or focus

Don't use this skill for:
- Pure technology comparisons (React vs Vue) — use `architect` skill
- Vendor selection within a category (Stripe vs Lemon Squeezy) — point to specific KG nodes
- "Build vs no-code" framing — that's a different question (no-code is usually a "buy" wearing a "build" disguise)

## Core principle

The indie founder bias is **default to building**. This is wrong roughly 70% of the time. Building has hidden ongoing costs (maintenance, security patches, edge cases, team-knowledge-debt) that are invisible until you're already committed.

The skill's job: surface those hidden costs and force a deliberate trade-off, not let the user default into building because it sounds fun.

## The Decision Framework

Score the decision across **six axes**. Each axis is 1–5.

### Axis 1: Distance from core product

How close is this capability to your **unique value proposition**?

- **5 — IS the core product**: build (your moat is here)
- **4 — directly user-facing core flow**: lean build
- **3 — supports core but generic**: depends on other axes
- **2 — internal tool, doesn't differentiate**: lean buy
- **1 — pure infrastructure**: buy

The lower the score, the more you should buy. If the capability is generic (auth, email sending, file storage, billing, error tracking, search), it doesn't differentiate — buying lets you spend the saved time on things that do.

### Axis 2: Time-to-ship cost

How long does building take, **realistically**? Indie founders systematically under-estimate by 2–3x.

- **5 — >1 month** of solo work to a v1: very high build cost
- **4 — 2–4 weeks**: high
- **3 — 1–2 weeks**: moderate
- **2 — 2–5 days**: low
- **1 — <1 day**: trivial; build it

Combine with: what could you ship instead in that time? If you'd ship a customer-requested feature instead, that's the real opportunity cost.

### Axis 3: Ongoing maintenance cost

The under-considered axis. How much will this **cost you per month, forever**?

- **5 — high**: needs security patches, monitoring, on-call (auth, payments, anything network-facing)
- **4 — high**: external API integration that breaks when the API changes
- **3 — moderate**: occasional bugfixes, minor schema migrations
- **2 — low**: stable internal logic
- **1 — trivial**: write once, forget

Solo-founder reality: every line of in-house code is a future debugging session. The maintenance cost is paid in your scarcest resource (attention) for as long as the product lives.

### Axis 4: Vendor lock-in risk

How locked-in does the vendor make you?

- **5 — extreme lock-in**: data lives only there, no export, proprietary APIs (e.g. Airtable for production data)
- **4 — high**: open formats but heavy migration cost (Auth0, Stripe Billing)
- **3 — moderate**: standard APIs but pricing power (most SaaS)
- **2 — low**: easy to swap (Postmark / Resend / Mailgun for email)
- **1 — trivial**: thin wrapper, providers interchangeable (S3-compatible storage)

Lock-in is the real risk of "buy." Mitigate by choosing vendors with **open formats, exportable data, and a competitive market** — even if more expensive.

### Axis 5: Vendor cost trajectory

What's the cost going to look like in 18 months?

- **5 — usage-priced, scales with growth**: at $50K MRR you'll be paying $5K/mo for it
- **4 — flat with high tier-jumps**: pricing cliffs at usage limits
- **3 — flat, reasonable scaling**: predictable
- **2 — flat, generous limits**: predictable
- **1 — open-source / free / one-time**: zero trajectory risk

For capabilities priced as % of revenue (Stripe at 2.9%, Lemon at 5%), the trajectory is built-in but the alternative (build your own) is rarely cheaper at any plausible scale — payments specifically should almost never be built.

### Axis 6: Capability risk if you build it badly

What's the downside if your in-house version is mediocre?

- **5 — catastrophic**: payment errors, security breaches, data loss (don't build these)
- **4 — high**: lost trust, customer churn (auth, billing, deliverability)
- **3 — moderate**: minor inconvenience (search ranking, recommendations)
- **2 — low**: slightly worse UX (animation library, UI components)
- **1 — none**: a hobby-quality version is fine (internal admin)

Indies often "save money" by building auth → 6 months later discover a password-reset hole → lose 50 customers' trust. The asymmetry of a security/payment screw-up means buying is usually right when capability risk is 4+.

## Scoring & Decision Rule

Sum the six axes (range: 6–30).

| Score | Recommendation |
|-------|----------------|
| 6–12 | **Buy / integrate** — the case for building is weak across the board |
| 13–18 | **Buy unless ≥3 strong build signals** — usually buy |
| 19–24 | **Genuine trade-off** — apply tiebreakers (MRR stage, team capacity, runway) |
| 25–30 | **Build** — likely core product or trivial |

Important: a single axis at 5 doesn't override the rest. If "core product distance" is 5 but everything else screams buy, you've probably mis-scoped the question — the *capability* is core, but the *implementation detail* you're considering building isn't.

## Tiebreakers for the middle band (13–24)

When the framework is ambiguous, decide based on:

1. **MRR stage**:
   - <$5K MRR: buy almost everything except the core product
   - $5–25K MRR: still buy by default, build only where #3 maintenance is genuinely low
   - $25K+ MRR: you can afford to build more (engineering hire likely on horizon)

2. **Runway**: if <6 months runway, **never build** anything that delays a revenue-positive ship. Buy and iterate.

3. **Team capacity now**: if you're already context-switching across 4+ areas, buying is almost always right.

4. **Reversibility**: if you can switch from buying to building later with <2 weeks of work, start with buying. Many "build it" decisions made early become "we have technical debt and no time to migrate" later.

## Categories with strong default recommendations

Based on observed solo-founder patterns, the **default for** specific categories:

| Category | Default | Why |
|----------|---------|-----|
| Payments / billing | **Buy** (Stripe / Lemon Squeezy / Polar) | Capability risk 5, lock-in 3, near-impossible to ship as well |
| Authentication | **Buy** initially (Clerk, Supabase Auth, Auth0) | Capability risk 5; build only if you have a specific reason |
| Transactional email | **Buy** (Resend, Postmark, Loops) | Deliverability is a deep specialty |
| Analytics | **Buy** (PostHog self-host or cloud; Plausible) | Free or cheap, instant value |
| Error tracking | **Buy** (Sentry free tier) | Hours/week saved |
| Support chat | **Buy** (Plain, Crisp, Help Scout) or just email | Building chat = forever debugging |
| Content (blog, docs) | **Buy/use SaaS or static site** (MDX + GitHub) | No upside to in-house CMS |
| Marketing site | **Build** (it's positioning, your voice matters) | Default templates undersell |
| Onboarding flow | **Build** | Tightly coupled to your product |
| Core product features | **Build** | This is your business |
| Admin tools | **Buy** if Retool/Forest works; otherwise minimal build | Maintenance burden underestimated |
| Status page | **Buy** (Instatus, Statuspage) | Cheap; signal of professionalism |
| Search | **Build** with a service (Meilisearch, Algolia, Typesense self-host) | Pure build is hard, pure buy is expensive at scale |

## Output format

```markdown
# Build-vs-Buy: <capability> for <product>

## Question
<one-sentence reframe>

## Scoring
| Axis | Score | Reasoning |
|------|-------|-----------|
| 1. Distance from core | X/5 | <one line> |
| 2. Time to ship | X/5 | <one line, with realistic estimate> |
| 3. Ongoing maintenance | X/5 | <one line> |
| 4. Vendor lock-in | X/5 | <one line> |
| 5. Vendor cost trajectory | X/5 | <one line, 18-month projection> |
| 6. Capability risk | X/5 | <one line> |
| **Total** | **XX/30** | |

## Recommendation
**Buy / Build / Mixed**: <one sentence>

## Specific recommendation
- If buy: which vendor(s), at what tier, integration estimate
- If build: scope of v1, what to defer, monthly maintenance budget

## Decision review trigger
"Re-open this decision when: <specific event, e.g. 'we hit $25K MRR' or 'vendor raises price by 50%'>"

## What changes the answer
<2–3 conditions that would flip the recommendation>
```

## Anti-patterns to push back on

- "It's cheaper to build" — usually wrong. Compute the real maintenance cost (your hourly rate × hours/year) and add to the build cost.
- "We need full control" — control is rarely the real reason; it's usually "building is more fun." Challenge: what specifically can't you do with the vendor that matters now?
- "I'll just spin up a quick version" — quick versions of auth/payments/email are how indies get hacked or sued.
- "Open-source is free" — your time isn't. Self-hosting open-source is "buy" with a much higher implementation cost.
- "We can switch later if it doesn't work" — true for thin wrappers, false for things deeply integrated (auth, billing, data store). Make the irreversibility explicit before deciding.

## Knowledge Systems

> **Full reference**: [`~/.claude/shared/KNOWLEDGE_SYSTEMS.md`](~/.claude/shared/KNOWLEDGE_SYSTEMS.md)

**Decision tree**:
- Build-vs-buy reasoning patterns → `hybrid_search("build vs buy SaaS")` (Weaviate MCP)
- Vendor landscape for a specific category → `WebFetch` competitor pricing pages
- Indie founder community sentiment → check Indie Hackers, Hacker News (`web-explorer` agent)

## Success criteria

This skill is working well if:
- The decision comes out as Buy/Build/Mixed with a single sentence
- The scoring table makes the reasoning auditable
- The "what would flip this" section is genuinely actionable
- The user can defend the decision to a co-founder or advisor in 2 minutes
- A decision-review trigger is set so the question doesn't get re-litigated weekly
