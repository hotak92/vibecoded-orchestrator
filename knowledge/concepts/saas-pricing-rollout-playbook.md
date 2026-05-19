---
title: SaaS Pricing Rollout Playbook
type: concept
tags: [saas, pricing, rollout, grandfathering, experiment, founder, business, mid-level-architecture, active]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# SaaS Pricing Rollout Playbook

## Why Rollout Is Its Own Discipline

A pricing change is a deployment. Knowing **what** the new prices should be is half the work; **how** you ship them is the other half. Botched rollouts can leak more revenue than the price miscalibration they're trying to fix:

- Existing customers find out via a third-party tweet and churn in protest
- New signups grandfather onto the wrong tier and you can't fix it without re-billing
- Pricing-page SEO cited a number that doesn't exist anymore
- The new pricing is measured against the wrong baseline and rolled back unnecessarily

This playbook treats a pricing change like a release: a target audience, a measurement plan, a rollback criterion, a comms sequence. Pair with `[[buildsOn::SaaS Pricing Psychology for Solo Founders]]` (which decides *what* to change).

## Who Gets The New Prices

The single most important rollout decision: **who pays the new price**.

### Option 1 — New signups only (grandfather existing)

Default for almost every indie pricing change.

- New visitors see new prices from cutover day forward
- Existing paying customers keep their current price indefinitely (or for a stated grandfathering window)
- Lowest-risk: zero churn from the change itself; you measure pure conversion/ARPU on the new pricing without confounding signal

Indie-relevant: this is almost always the right choice under $25K MRR. The existing-customer revenue is too important to risk for a directional ARPU improvement on new signups.

### Option 2 — Grandfather with sunset window

Existing customers keep old price for N months, then migrate.

- N is typically 6–12 months
- Communicated up-front in a personal email with the exact migration date
- Offer an annual prepay at the old price as a goodwill gesture (locks them in 12 more months, signals fairness)
- Risk: vocal churn from the announcement (5–15% of long-tenured customers, usually self-selecting low-engagement)

Use when the old pricing was meaningfully under-priced and the loss can't be sustained.

### Option 3 — Immediate migration (everyone, now)

The "rip the band-aid off" approach. Rarely the right call.

- Only justified if old pricing is catastrophically wrong (you're losing money per customer)
- Expect 10–30% churn in the announcement month
- Requires extensive comms — personal email, in-app banner, social, blog
- Reserve for major repricing events (e.g. shifting from flat to usage-based)

## Single-Cutover vs A/B Testing

The textbook answer is "A/B test prices." The solo-founder reality is different:

- A/B tests on pricing require **statistically meaningful traffic** — typically 1000+ visitors per variant per week to detect a 10% conversion lift in <8 weeks
- Most indies under $25K MRR don't have that traffic
- A/B tests on price also fragment your support ("why is mine $19 and theirs $29?") and complicate refund/upgrade flows
- Tooling for true price A/B tests is heavier than indies often want (custom Stripe integration, dynamic pricing pages)

Realistic default for solo SaaS: **single cutover with a measurement window**. Commit to the new price for 8–12 weeks; measure; decide accept/iterate/rollback. Trade statistical rigour for shipping speed — you'll be running 5 pricing experiments in the time a "proper" A/B test runs once.

When A/B testing IS appropriate: post-$100K MRR with a marketing-site CMS that supports variants, or pre-launch when you have no committed customers and can freely show different prices to different visitors.

## Measurement Window: What To Track For 8–12 Weeks

The window must be long enough to see retention impact, not just conversion impact.

Track:

- **Conversion rate**: % of visitors to pricing page → paid signup (split by tier)
- **ARPU**: average revenue per paying user (tier mix shifts)
- **Time-to-upgrade**: days from signup to upgrade (does new tier ladder work?)
- **Churn-by-tier**: monthly cancellation rate within each tier (did the new mid-tier attract worse customers?)
- **Mix of new vs upgrade revenue**: are new customers buying the mid-tier, or are upgrades doing the work?

Decision triggers at week 8:
- **Accept**: net new MRR matches or exceeds projection
- **Iterate**: directionally right but the wrong tier ratios; tweak before next cutover
- **Rollback**: conversion dropped >30%, or churn spiked >2× baseline

Don't decide at week 2 — the signal is too noisy and you'll thrash.

## The Communication Sequence

### For grandfathered changes (new signups only)

Light touch — existing customers may never need to know:

1. **Pricing page updates** the day of cutover
2. **Changelog entry** ("Updated pricing for new customers; existing customers unchanged")
3. **No customer email** — you don't want to alarm grandfathered customers
4. **Update SEO-cited mentions** (your About page, footer, etc.)

### For sunset-window changes

Heavy comms, all up-front:

1. **Pre-announce personally** to your top 20% revenue customers (manual email, 1 sentence asking "any concerns?")
2. **Customer-wide email** with the date, the new price, the why, the offer to lock-in annual at old price
3. **In-app banner** for affected customers for 4 weeks
4. **Blog post** explaining the why (positioning, costs, product value added)
5. **Follow-up email** at T-30 days reminding people of the migration

### For immediate migration

Rare, treated as a critical incident with comms:

1. Decide quietly internally first
2. Announce simultaneously across email, blog, social — never let the first signal be a tweet from a confused customer
3. Be online for replies for the next 48 hours
4. Offer a personal pause/refund path for anyone who responds badly

## Tone For The Customer Email

Hard rules:

- **Lead with the change**, not the rationale. ("We're updating our pricing.")
- **State the new price plainly.** Don't bury it.
- **Explain why in 1–2 sentences**, not 5 paragraphs. Acceptable reasons: "to keep investing in X," "to match the value the product now delivers." Unacceptable: "to align with industry standards" (translates as "we want more money and have no specific reason").
- **No apology, no over-justification.** Pricing changes are normal business. Apologetic tone signals you don't believe in the change.
- **Make the action clear** — what does the customer need to do? (Usually: nothing.)

Banned phrasings: "we're excited to announce", "this allows us to continue serving you", "we hope you understand". They read as PR copy.

## Risk Callouts To Surface Before Shipping

Concrete failure modes for the specific change:

- *"Raising Starter from $9 → $19 will likely lose 10–15% of conversions but increase ARPU 30%+. Net positive if your CAC is recoverable on $19 customers."*
- *"Annual discount of 17% only pays off if monthly churn ≥3%. Your stated churn is 1.5%, so you may be over-discounting."*
- *"Mid-tier at $49 sits inside the personal-credit-card threshold — healthy. Business at $149 lands above the 'I'll expense it' line — verify with target persona."*
- *"Removing the cheapest tier filters out the worst-fit customers (good) but caps your top-of-funnel volume (also good or bad depending on growth stage)."*

Specific numbers, not vague hedges.

## Grandfathering Rules (The Operational Detail)

If grandfathering, decide:

- **Forever or sunset?** Stripe and most billing systems support "lock in the price as of X date" indefinitely; build that flag now even if you don't intend a sunset.
- **What if they upgrade?** Usually a grandfathered customer who upgrades to a higher tier pays the new price for that tier (else they're partially grandfathered into a tier they never signed up for).
- **What if they downgrade?** Usually they lose the grandfather (the price they're paying no longer corresponds to the tier they're on).
- **What if they cancel and resubscribe?** Almost always: they lose grandfather. State this in the price-lock email so it's not a surprise.
- **What about pause/resume features?** Usually grandfather survives a pause. Test the flow before shipping.

Document these rules **in your pricing page footnote** as well as your billing system. Future-customer-support thanks you.

## Anti-Patterns

- **Re-pricing every quarter** — confuses customers, breaks SEO of price-mention pages, signals indecision
- **Apologising in the announcement** — undermines the change
- **A "limited time" discount on the launch of new pricing** — trains buyers to wait for sales, kills urgency
- **Forgetting non-pricing-page mentions** — your homepage, FAQ, comparison pages, and blog all probably reference the old price
- **Measuring on conversion-only at week 2** — too early, too noisy
- **Surprise migration of existing customers** — pure churn-bait; always grandfather or pre-announce

## Related

- `[[buildsOn::SaaS Pricing Psychology for Solo Founders]]` — decides what to change
- `[[implements::Churn Taxonomy and Reduction Tactics]]` — pricing changes can trigger voluntary churn
- `[[relatedTo::SaaS Metrics Math and Benchmarks]]` — measurement-window metrics
- `[[relatedTo::Solo SaaS Launch Playbook]]` — major pricing changes often coincide with launches

## Sources

- Patrick McKenzie writings on pricing rollouts and grandfathering (Kalzumeus / patio11)
- Profitwell / Paddle research on price-change retention impact
- Indie founder retrospectives (Indie Hackers, Twitter case studies) on grandfathering outcomes
- Stripe billing documentation on price locking and proration (https://docs.stripe.com/billing) — verified 2026-05-19
