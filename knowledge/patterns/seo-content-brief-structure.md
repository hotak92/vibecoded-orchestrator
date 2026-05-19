---
title: SEO Content Brief Structure
type: pattern
tags:
  - seo
  - content
  - marketing
  - briefs
  - editorial
  - mid-level-architecture
  - ai-overviews
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# SEO Content Brief Structure

A content brief is the deliverable that lets a writer (human or LLM) draft an SEO-ranked piece without making strategic decisions during writing. In 2026 — with AI Overviews eating top-funnel clicks and E-E-A-T behavioural signals dominating ranking — a brief that skips intent classification, AIO likelihood, or the angle produces commodity content that doesn't rank. The structure below is the minimum-viable brief.

## The 10-section brief

A complete brief is a single markdown file with these 10 sections, in order:

1. **Intent classification** — informational / commercial / transactional / navigational + supporting SERP evidence
2. **AIO likelihood** — high / medium / low + strategy implication
3. **SERP analysis** — what's currently ranking top-3 to top-5, why, what's missing
4. **The angle** — what your piece does that the top results don't
5. **Outline** — H1, H2-as-question, H3-as-sub-question, with per-section word targets
6. **Internal links** — 5–10 suggestions (real URLs or marked `[NEEDS: cluster page on X]`)
7. **External links** — 3–5 authoritative sources for citations
8. **On-page metadata** — title tag (≤ 60 chars), meta description (≤ 155 chars), URL slug
9. **Schema markup** — JSON-LD snippets for Article + FAQ + HowTo / Product / Review
10. **Promotion plan** — 5-item plan (internal links, newsletter, social, earned, community)

Skipping any one section degrades the brief's reliability. The writer ends up making a strategic decision during writing, and the piece's intent alignment slips.

## Intent classification (the foundational decision)

You cannot write to rank without knowing intent. The four:

- **Informational** — `what is X`, `how does X work`, `X explained`. User wants to learn.
- **Commercial** — `best X`, `X vs Y`, `X for [use case]`, `X reviews`. User researching to buy.
- **Transactional** — `buy X`, `X pricing`, `X coupon`, `X login`. User ready to act.
- **Navigational** — `X.com`, branded queries. User knows where they're going.

**Mixing intent is the #1 SEO writing mistake.** A "best CRM" article (commercial) that opens with 800 words of "what is a CRM" (informational) will not rank in 2026 — Google rewards intent alignment, and AIO eats the front-loaded informational fluff.

**To classify**: examine top 10 organic results.
- 10 listicles → commercial intent
- 10 long-form explainers → informational intent
- Mix → mixed-intent SERP (rare and hard to rank against)
- Verify against PAA (People Also Ask) — what does Google show as sibling questions?

## AIO likelihood (the 2026 wrinkle)

AI Overviews appear on ~30–50% of US informational queries (varies by vertical; YMYL queries lower, how-to queries higher). Strategy differs by tier:

| AIO likelihood | Query type                  | Strategy implication                                          |
|----------------|-----------------------------|---------------------------------------------------------------|
| **High**       | "what is X" / generic info  | Re-evaluate: is this query worth chasing? Click-through is down 30–60%. If yes: write to BE the citation source — TL;DR up top, H2-as-question structure, original data. Goal: appear *inside* the AIO with branded citation, not below it where clicks are dead. |
| **Medium**     | Some informational mix      | Standard SEO best practices apply. Add original angle, E-E-A-T signals, comparison tables. |
| **Low**        | Commercial / transactional / navigational | AIO usually doesn't render. Standard SEO works. Focus on comparison + persona + use-case angles (these are AIO-resistant). |

**Estimating AIO likelihood**:
- Best: search the actual SERP — does AIO render today? That's the source of truth.
- Fallback: estimate from query type. `what is X` = ~80% AIO probability. `best X for Y` = ~20%.

## SERP analysis (do this before outlining)

Read the top 3–5 organic results. For each, capture:

- Word count
- Author / publication credibility (E-E-A-T signal)
- Date of last update (Google rewards freshness in many verticals)
- Format (listicle, long-form, comparison, tutorial)
- Unique angle / data / original sources
- **What they're MISSING** — this is where your piece wins

If you can't access the SERP (no WebFetch, user didn't paste URLs), say so. Don't guess what's ranking — the angle depends on accurate competitive context.

## The angle (the part that gets you cited / ranked)

In 2026, the question is not "can I write a comprehensive piece" — it's "what unique angle do I bring that the top 5 don't already have." Common angles that work:

- **Original data** — survey of 200 customers, scrape of 1,000 listings, internal analytics
- **First-person experience** — "I did X for 90 days, here's what happened"
- **Contrarian take** — "Everyone says X. We did Y and got better results"
- **Updated post-event** — "What changed after Google's Feb 2024 update"
- **Specific persona/use-case** — "Best CRM for solo agency owners" (vs generic "best CRM")
- **Comparison the SERP is missing** — `X vs Y vs Z` when 2 of those don't currently rank against each other

If the brief doesn't have an angle, the writer produces a commodity piece. Always specify the angle; never leave it as "find an angle when you write."

## Outline structure (the 2026 standard)

```
H1: [Title — matches title tag, ≤ 60 chars]

[TL;DR — 60–80 words, answers the literal query]
[Why we wrote this — 1 sentence, establishes E-E-A-T author/experience]

H2: What is [primary keyword]?
   (If informational mix: 60–100 words MAX — give AIO what it needs, move on)

H2: [First commercial question — e.g., "When should you use [X]?"]
   H3: [Sub-question 1]
   H3: [Sub-question 2]
   - List or comparison table here
   - 200–400 words this section

H2: [Second commercial question — e.g., "Top [N] options compared"]
   - Comparison table (great for AIO citation)
   - 100–150 words intro, then explicit-column table

[ continue with intent-aligned H2s ]

H2: How to choose / What to look for / How to set up [X]
   - Concrete steps or framework

H2: Common mistakes / What goes wrong
   - Specific, contrarian, with examples (drives engagement signals)

H2: FAQs (schema-marked)
   - 4–8 questions (mine from PAA + Reddit + customer support tickets)
   - 40–80 words per answer (snippet-ready)

[Author bio — name, photo, credentials, LinkedIn link — E-E-A-T essential]
[Last updated date]
```

**H2-as-question, not H2-as-statement** — AIO models scan for `What is X`, `How to X`, `Why X`, `X vs Y` patterns. Statement H2s capture less PAA real estate and rarely get cited.

**Per-H2 word-count guidance keeps writers honest.** Without it, sections balloon and lose intent alignment.

## Internal + external links

**Internal** (5–10 suggestions):
- Pillar page for this cluster (always)
- 2–3 cluster pages drilling into sub-topics
- 1–2 commercial pages (pricing, product, demo) — but NOT in the first 30% of the article
- 1–2 case studies demonstrating experience

If the vendor has no existing page for a sub-topic, mark `[NEEDS: cluster page on X]` so the topical-authority gap is visible.

**External** (3–5 suggestions):
- Original research (Gartner, Forrester, McKinsey — only the freely-citable parts)
- Government / standards docs (RFC, ISO, FTC)
- Authoritative practitioner blogs in the space
- **Avoid linking to direct competitors** unless strictly necessary

External links signal you're not a closed-loop content farm. Pages with 3–5 outbound links rank better than 0-outbound pages.

## On-page metadata (hard character limits)

**Title tag** (≤ 60 chars):
- Primary keyword in first 30 chars
- Brand at end (optional, only if recognised)
- Number / specificity / promise

Examples:
- ✅ "Best CRM for Solo Founders (2026): 7 Real Comparisons"
- ✅ "Cold Email Deliverability After Google's 2024 Update"
- ❌ "The Complete Guide to CRM" (generic, no number, no specificity)

**Meta description** (≤ 155 chars):
- Restate the primary keyword
- Promise the value
- End with implicit CTA

**URL slug**:
- 3–5 words, lowercase, hyphens
- Primary keyword + qualifier
- Examples: `/best-crm-solo-founders/`, `/cold-email-deliverability-2024/`

**Canonical**: self-referential unless multiple URLs exist
**OG title / OG description**: match meta but slightly more "social"

## Schema markup (selection table)

| Content type             | Schema types                                            |
|--------------------------|---------------------------------------------------------|
| All articles (baseline)  | `Article` with `author`, `publisher`, `datePublished`, `dateModified` |
| Has FAQ section          | `FAQPage`                                               |
| Step-by-step content     | `HowTo`                                                 |
| Product comparison       | `Product` + `Review` + `AggregateRating` (only if real ratings) |
| Listicle of products     | `ItemList`                                              |
| Recipe / DIY             | `Recipe`                                                |
| Local business / service | `LocalBusiness` + `Service`                             |

Provide schema JSON-LD snippets in the brief — the writer/developer drops them in.

## Promotion plan (without this, the piece sits)

Every brief includes a 5-item promotion plan:

1. **Internal** — link to it from 3–5 existing high-authority pages on the vendor's site within 24h of publish
2. **Newsletter** — feature in next send
3. **Social** — 2 LinkedIn posts + 1 X thread + 1 carousel in next 14 days
4. **Earned** — pitch as guest post or data source to 5 publications (digital PR)
5. **Community** — post in 1–2 relevant subreddits (only if karma earned there) OR 1 Hacker News if relevant

A piece with zero promo sits at position 25–40 indefinitely. A piece with 3+ external mentions in the first 30 days has a real shot at top 10.

## Common brief mistakes

- ❌ Skipping intent classification (writes the wrong piece)
- ❌ Ignoring AIO likelihood (writes top-funnel content that AIO eats)
- ❌ No unique angle (commodity piece, won't rank)
- ❌ Generic metadata ("Complete Guide to X")
- ❌ Internal links to nonexistent pages without `[NEEDS: ...]` marker
- ❌ H2s as statements instead of questions (poor AIO citation, poor PAA capture)
- ❌ No FAQ section (misses easy schema win)
- ❌ Forgetting the promo plan (piece without promo doesn't rank)
- ❌ Hero shot of the product on the page hero (show outcome, not UI)

## What a brief is NOT

- Not a draft — the brief produces the skeleton, the writer (or LLM) drafts the meat
- Not keyword research — the target keyword is an input to the brief
- Not a SERP scraper — if the assistant can't access the live SERP, ask the user to paste top-3 URLs
- Not a backlink builder — the brief includes a promo plan; the user executes

## Related

- [[relatedTo::SEO in 2026 — AI Overviews, E-E-A-T, Topical Authority]]
- [[relatedTo::Copywriting Frameworks for Sales and Marketing]]
- [[relatedTo::ICP and Buyer Persona Framework]]
- [[relatedTo::Content Repurposing Graph]]

## References

- Google Search Quality Rater Guidelines (E-E-A-T definitions)
- schema.org — vocabulary for all schema types
- Aleyda Solís — practitioner blog on SEO in the AIO era
- Backlinko / Brian Dean — content brief templates and data studies
- Clearscope / Surfer / Frase documentation — on-page optimisation NLP signals
