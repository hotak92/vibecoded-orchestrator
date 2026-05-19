---
title: Landing Page Conversion Audit Framework
type: concept
tags: [saas, landing-page, conversion, marketing, copy, founder, ux, mid-level-architecture, active]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Landing Page Conversion Audit Framework

## What This Is

A repeatable framework for evaluating a SaaS landing page from a **conversion-funnel** perspective — not a design review. The question is "would this convert a cold visitor in 8 seconds," not "is it pretty."

The framework's value is forcing a single-bottleneck call. Indie founders waste time polishing the second-worst axis when the worst one is gating everything; this framework surfaces the worst one and tells you to fix it first.

## Prerequisites Before Scoring

You cannot audit a landing page without knowing:

1. **The target visitor** — who the page is for, the job title/role, the problem they're trying to solve, and where they came from (Twitter, organic search, Show HN, paid ad).
2. **The goal CTA** — the single action the page is optimised for (sign up, book demo, start trial, install, buy).

If the user can't articulate either, **that itself is the finding**. The page can't be optimised until both are decided. Push back; don't audit a page with undefined objectives.

## The 8 Scoring Axes

Each axis is scored 1–5 with 1–3 sentences of evidence. Don't safe-middle the scoring — pick a side and be wrong if necessary.

### A. Hero Clarity (the "5-second test")

A cold visitor must answer "what is this and who is it for" in 5 seconds. The headline carries 80% of this load.

- 5: Unambiguous, names the audience AND the outcome ("Stop spending Sundays on invoices — for solo accountants")
- 3: Clear what it is, vague on who it's for
- 1: Jargon soup ("AI-powered platform for next-generation workflow synergy")

### B. Value Proposition Specificity

Concrete claims (numbers, named outcomes) outperform abstract ones. Quantification is trust.

- "Cuts your invoicing time by 80%" beats "Save time on invoicing"
- "Used by 12,000 indie SaaS founders" beats "Trusted by thousands"

### C. CTA Hierarchy

- Primary CTA visible without scrolling
- Primary CTA repeated 2–3× down the page
- Exactly **one** primary action per page — not "Sign up OR Book Demo OR Read Docs"
- Secondary actions (docs link, pricing) styled visually subordinate

The most common failure: competing CTAs of equal visual weight. Visitors freeze, leave.

### D. Social Proof

A hierarchy from strongest to weakest:

1. Named testimonial with role + photo + company (or named individual)
2. Logos of recognisable customers
3. Usage stats with specifics ("1,247 users last week", not "thousands")
4. Press mentions / publication logos
5. Star ratings or aggregate review counts with link to source

"1,000 users" without provenance is **worse than no claim** — sounds invented, signals desperation.

### E. Copy-to-Design Ratio

- Heavy design, thin copy: pretty but uninformative. Visitor doesn't learn what it does.
- Heavy copy, thin design: 2012 Wordpress site. Visitor doesn't trust it.
- 5 = balanced: specific copy, crisp visuals, every section earns its scroll-height.

### F. Mobile-First

The hero must work on a 375px viewport (iPhone SE width). Concrete thresholds:

- Tap targets ≥44px tall
- No horizontal scroll at any breakpoint
- Body font size ≥16px (anything smaller forces zoom)
- Hero CTA visible without scroll on the smallest target device
- Lazy-loaded images don't push CTA below the fold

### G. Page Weight and Speed

Budgets that work for landing pages:

- **Time to First Byte (TTFB)** <1s
- **Largest Contentful Paint (LCP)** <2.5s
- **Total page bytes** <2MB
- **Hero image** <500KB (most "hero images" are 3–5MB; this is almost always the highest-leverage fix when present)
- **JavaScript bundle** <300KB (compressed)

Tools: PageSpeed Insights, WebPageTest, or `curl -I` + `wc -c` for a quick byte-count.

### H. Trust Signals

For an unknown indie SaaS, trust must be earned in the layout. Stack as many as apply:

- Real founder name + photo + 2-sentence bio
- Country or city (signals a real human, not faceless SaaS)
- Linked Twitter/LinkedIn/GitHub of the founder
- Privacy policy + ToS linked in footer (legal hygiene)
- Customer count + last-updated changelog
- Status page link
- Open-source repo (if applicable) with star count

A founder photo + Twitter link converts measurably better than an anonymous "We are…" page for indies under $100K MRR.

## The Bottleneck Method

After scoring, name the **single weakest axis** and the **single highest-leverage fix**. The discipline: don't fix a 4-rated axis while a 1-rated axis sits there gating everything. Show your work — point at the axis with the lowest score, propose a specific change, predict the impact.

## Concrete Suggestions, Not Generalities

When the bottleneck is copy (axes A, B, C), produce **verbatim alternatives**, not "improve the headline." Format:

```
Current: <verbatim current copy>
Suggested A: <alternative>
Suggested B: <alternative>
Why: <one sentence of rationale tied to a scoring axis>
```

When the bottleneck is layout (axes C, E, F), describe the structural change with a before/after layout sketch — not "improve the layout."

When the bottleneck is page weight (G), name the heaviest offender by byte size and propose a replacement (e.g. "Hero JPEG is 3.2MB; convert to WebP, target <300KB").

## Anti-Patterns To Avoid In The Audit Itself

- Generic advice ("add testimonials") without specifying whose, where, and what they say
- Scoring everything 3/5 (the "safe middle")
- Listing 20 suggestions — limit to 5, ranked by effort vs impact
- Ignoring page weight in favour of copy (sometimes "page is 8MB" is the only fix that matters)
- Saying "looks great" — if conversion is bad, something is wrong; find it

## Related

- `[[relatedTo::SaaS Pricing Psychology for Solo Founders]]` — when pricing is on the page, the pricing-psychology axes apply
- `[[relatedTo::Customer Development for Indie Founders]]` — the target-visitor definition comes from customer development
- `[[relatedTo::Solo SaaS Launch Playbook]]` — landing-page audit is a prerequisite for a public launch

## Sources

- Julie Zhuo on hero-test five-second method (industry-standard usability heuristic)
- Web.dev Core Web Vitals (LCP, INP, CLS thresholds, https://web.dev/articles/vitals) — verified 2026-05-19
- Pieter Levels' Nomad List + Photo AI landing pages — exemplars of indie trust-signal density
- Stripe Atlas guides on landing page conversion (https://stripe.com/atlas/guides/atlas-pricing)
