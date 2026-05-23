---
name: seo-content-brief
description: Generates a complete SEO content brief from a target keyword and SERP context. Includes search intent, AI Overview likelihood, outline with H2/H3 questions, internal/external linking, on-page metadata (title/meta/schema), and a 2026-aware angle. Use when the user says "write me a brief for keyword X", "I'm targeting [keyword]", or "outline a post about [topic]" and they want SEO traffic.
keywords: [SEO brief, target keyword, SERP, AI Overview, search intent, E-E-A-T]
argument-hint: "[target keyword] [optional: SERP URL or top-3 competitor URLs]"
model: opus
effort: high
---

# /seo-content-brief

Produce a content brief that gives a writer everything they need to draft an SEO-ranked piece in 2026 — accounting for AI Overviews, E-E-A-T, topical authority, and the queries Google actually shows.

## Usage

```
/seo-content-brief "best CRM for solo founders"
/seo-content-brief "cold email deliverability 2026" https://example.com/competitor-article
/seo-content-brief                                   # interview first
```

If invoked with no arguments, ask before generating:

1. Target keyword (primary + 2–3 secondary)?
2. Search intent estimate: informational / commercial / transactional / navigational? (If unsure: I'll classify from the SERP — provide the SERP URL or paste top-3 results)
3. Vendor's domain (so internal-link suggestions are real, not placeholder)
4. ICP / target reader (who the article is FOR, not just "anyone who searches")
5. Existing cluster pages? (helps internal-link suggestions and topical-authority strategy)

## What this skill produces

A single markdown file containing:

1. **Intent classification** — informational / commercial / transactional / navigational + supporting evidence
2. **AIO likelihood** — high / medium / low + how to write for AIO citation (or avoid it)
3. **SERP analysis** — what's currently ranking, why, what's missing
4. **The angle** — what your piece does differently (no commodity content in 2026)
5. **Outline** — H1, H2s as questions, H3s as sub-questions, word-count target per section
6. **Internal links** — 5–10 suggestions (or placeholders for the vendor to fill)
7. **External links** — 3–5 authoritative sources (the writer should fact-check + verify)
8. **On-page metadata** — title tag (≤60 char), meta description (≤155 char), URL slug
9. **Schema markup** — appropriate types (Article + FAQ + HowTo / Product / Review)
10. **Promotion plan** — how the piece gets links, mentions, social pickup (without promo, no ranking)

## Intent classification (the foundational decision)

You can't write to rank without knowing intent. The four:

- **Informational** — "what is X", "how does X work", "X explained". User wants to learn.
- **Commercial** — "best X", "X vs Y", "X for Z use case", "X reviews". User is researching to buy.
- **Transactional** — "buy X", "X pricing", "X coupon", "X login". User is ready to act.
- **Navigational** — "X.com", "X company", branded. User knows where they're going.

Mixing intent is the #1 SEO writing mistake. A "best CRM" article (commercial) that opens with 800 words of "what is a CRM" (informational) will not rank in 2026 — Google rewards intent alignment, AIO kills the front-loaded informational fluff.

To classify:
- Look at top 10 organic results (you'll see this in the SERP analysis section)
- Match patterns: 10 listicles = commercial; 10 long-form explainers = informational; mix = mixed intent (rare, hard)
- Verify with PAA (People Also Ask) — what does Google show as sibling questions?

## AIO likelihood (the 2026 wrinkle)

AI Overviews appear on ~30–50% of US informational queries (varies by vertical). Strategy differs:

- **High AIO likelihood** (informational, generic):
  - Re-evaluate: is this query worth chasing? Click-through is down 30–60%.
  - If yes: write to BE the citation source. Lead with a TL;DR. Use clear H2-as-question structure. Use original data.
  - Goal: appear inside the AIO answer (branded citation), not below it (where clicks are dead).
- **Medium AIO likelihood** (some informational mix):
  - Standard SEO best practices apply.
  - Add original angle, E-E-A-T signals, comparison tables.
- **Low AIO likelihood** (commercial, transactional, navigational):
  - AIO usually doesn't render. Standard SEO works.
  - Focus on comparison + persona + use-case angles. These are AIO-resistant.

You estimate AIO likelihood by:
- Search the actual SERP — does AIO render? (this is the source of truth)
- Or: estimate from query type. "What is X" = ~80% AIO probability. "Best X for Y" = ~20%.

## SERP analysis (do this before outlining)

Read the top 3–5 organic results (the user provides URLs, OR you note "I need top-3 URLs to do this properly"). For each:

- Word count
- Author / publication credibility (E-E-A-T signal)
- Date of last update (Google rewards freshness in many verticals)
- Format (listicle, long-form, comparison, tutorial)
- Unique angle / data / original sources
- What they're MISSING (this is where your piece wins)

If you don't have SERP access (no WebFetch / user didn't provide URLs), say so and tell the user to paste the top 3 URLs OR a SERP screenshot. Don't guess what's ranking.

## The angle (the part that gets you cited / ranked)

In 2026, the question is not "can I write a comprehensive piece about X" — it's "what unique angle do I bring that the top 5 results don't already have."

Common angles that work:

- **Original data** — survey of 200 customers, scrape of 1,000 listings, internal analytics
- **First-person experience** — "I did X for 90 days, here's what happened"
- **Contrarian take** — "Everyone says X. We did Y and got better results"
- **Updated post-event** — "What changed after Google's Feb 2024 update"
- **Specific persona/use-case** — "Best CRM for solo agency owners" (vs generic "best CRM")
- **Comparison the SERP is missing** — "X vs Y vs Z" where 2 of those don't currently rank against each other

If the brief doesn't have an angle, the writer will produce a commodity piece. Always specify the angle, never leave it as "find an angle when you write."

## Outline structure (the 2026 standard)

```
H1: [Title — matches title tag, ≤ 60 chars]

[TL;DR — 60–80 words, answers the literal query]
[Why we wrote this — 1 sentence, establishes E-E-A-T author/experience]

H2: What is [primary keyword]? (if informational mix, 60–100 words MAX — give AIO what it needs and move on)

H2: [First commercial question — e.g., "When should you use [X]?"]
   H3: [Sub-question 1]
   H3: [Sub-question 2]
   - List or comparison table here
   - 200–400 words this section

H2: [Second commercial question — e.g., "Top [N] options compared"]
   - Comparison table (great for AIO citation)
   - 100–150 words intro
   - Then table with explicit columns

[ continues with intent-aligned H2s ]

H2: How to choose / What to look for / How to set up [X]
   - Concrete steps or framework
   - Goal: be the practical answer

H2: Common mistakes / What goes wrong
   - Specific, contrarian, with examples
   - Drives engagement signals (saves, shares)

H2: FAQs (schema-marked)
   - 4–8 questions (mine from PAA + Reddit + customer support tickets)
   - 40–80 words per answer (snippet-ready)

[Author bio — name, photo, credentials, links to LinkedIn — E-E-A-T essential]
[Last updated date]
```

Per-H2 word-count guidance keeps writers honest. Without them, sections balloon and lose intent alignment.

## Internal and external links

**Internal**: link to 5–10 of the vendor's own pages, biased toward:
- Pillar page for this cluster (always)
- 2–3 cluster pages that drill into sub-topics
- 1–2 commercial pages (pricing, product, demo) — but NOT in the first 30% of the article
- 1–2 case studies that demonstrate experience

If the vendor has no existing pages, mark internal-link suggestions as `[NEEDS: cluster page on subtopic X]` so they know what to write next for topical authority.

**External**: link to 3–5 authoritative sources:
- Original research (Gartner, Forrester, McKinsey reports — only the freely-citable parts)
- Government / standards docs (RFC, ISO, FTC)
- Authoritative practitioner blogs (well-known names in the space)
- AVOID linking to direct competitors unless absolutely necessary

External links signal that you're not a closed-loop content farm. Pages with 3–5 outbound links rank better than 0-outbound pages.

## On-page metadata

**Title tag** (60 chars max):
- Primary keyword in first 30 chars
- Brand at the end (optional, only if recognized)
- Number / specificity / promise

Examples:
- ✅ "Best CRM for Solo Founders (2026): 7 Real Comparisons"
- ✅ "Cold Email Deliverability After Google's 2024 Update"
- ❌ "The Complete Guide to CRM" (generic)

**Meta description** (155 chars max):
- Restate the primary keyword
- Promise the value
- End with implicit CTA

**URL slug**:
- 3–5 words, lowercase, hyphens
- Primary keyword + qualifier
- Examples: `/best-crm-solo-founders/`, `/cold-email-deliverability-2024/`

**Canonical**: self-referential unless multiple URLs exist
**OG title / OG description**: match meta but slightly more "social"

## Schema markup

Always: `Article` schema with `author`, `publisher`, `datePublished`, `dateModified`.

Conditionally:
- If FAQ section: `FAQPage` schema
- If step-by-step: `HowTo` schema
- If comparing products: `Product` + `Review` + `AggregateRating` (only if you actually have ratings, don't fake)
- If listicle of products: `ItemList`

Provide schema JSON-LD snippets in the brief (the writer/developer drops them in).

## Promotion plan (without this, the piece sits)

The brief should include a 5-item promotion plan:

1. Internal: link to it from 3–5 existing high-authority pages on the vendor's site
2. Newsletter: feature in next send
3. Social: 2 LinkedIn posts + 1 X thread + 1 carousel in next 14 days
4. Earned: pitch as a guest post or "data source" to 5 publications (digital PR)
5. Community: post in 1–2 relevant subreddits (only if you've earned karma there) OR 1 Hacker News if relevant

A piece with zero promo sits at position 25–40 indefinitely. A piece with 3+ external mentions in the first 30 days has a real shot at top 10.

## Output format

Write to `seo-brief-{slug}.md`:

```markdown
# SEO Brief: {Title}

**Primary keyword**: cold email deliverability 2026
**Secondary keywords**: DMARC outbound 2024, cold email warmup, Google Yahoo bulk sender
**Search volume (est.)**: 1.2K–2.4K/mo (cite tool: Ahrefs / SEMrush)
**Keyword difficulty**: 32/100 (medium)
**Intent**: Mixed informational + commercial
**AIO likelihood**: Medium — appears on ~40% of related queries

## SERP Analysis

Top 3 current rankers:
1. Postmark blog — "Email deliverability guide" — 2,800 words, 2023 last updated, author = engineering. Strong but stale on Feb 2024 update.
2. Mailchimp — "Email deliverability best practices" — 1,800 words, generic, no original data.
3. Litmus — "DMARC 101" — 1,500 words, technical but doesn't cover cold outbound specifically.

Gap: nothing top-ranking specifically addresses cold-outbound deliverability post-Feb-2024 with original sender data.

## The Angle

Original data: post-mortem of [Vendor's own] cold outbound recovery in March–April 2024.
Numbers: reply rates went 9% → 2% → 6% over 90 days.
Specific actions that mattered + ones that didn't.

This is what nothing in the top 5 currently has.

## Outline

H1: Cold Email Deliverability After Google's Feb 2024 Update (What Actually Changed)

TL;DR (80 words):
> In Feb 2024 Google and Yahoo enforced DMARC + one-click unsubscribe + sub-0.3% spam rate for bulk senders.
> Cold outbound reply rates dropped 40–70% across the industry. We did the post-mortem on [Vendor]'s own
> ramp-back: which fixes worked, which didn't, the 90-day recovery curve. If your cold email opens cratered
> in spring 2024, this is the playbook. (5-min read.)

[ ... 8 H2 sections, each with H3 sub-questions and 200–600 word target ... ]

## Internal Links (5)

- [Pillar] /b2b-outbound-playbook/ (link from H2 #2)
- [Cluster] /dkim-rotation-guide/ (link from H2 #3)
- [Case study] /how-we-recovered-9-percent-reply-rate/ (link from H2 #4)
- [Product/Demo] /book-a-call/ (link from final CTA only)
- [NEEDS — write next]: /list-hygiene-for-cold-outbound/ (link from H2 #5)

## External Links (4)

- Google Email Sender Guidelines (official)
- Yahoo Sender Best Practices (official)
- DMARC.org Deployment Guide
- Postmark blog: transactional separation pattern

## Metadata

- **Title tag**: Cold Email Deliverability 2026: What Changed (Recovery Playbook)
- **Meta description**: Google's 2024 update broke cold outbound. Here's what we did to recover reply rates, with 90-day data and the specific DMARC/list-hygiene checklist.
- **URL slug**: /cold-email-deliverability-2024/
- **OG image**: Custom (data chart of reply rate recovery)

## Schema

```json
{
  "@context": "https://schema.org",
  "@type": "Article",
  "headline": "...",
  "author": { ... E-E-A-T author bio ... },
  ...
}
```

(Plus `FAQPage` schema for the FAQ section.)

## Promotion Plan

1. Internal: link from /b2b-outbound-playbook/ within 24h of publish
2. Newsletter: lead piece in next Friday's send
3. LinkedIn: 2 text posts + 1 carousel over 7 days
4. X: 1 long-form thread Day 1, 3 atomic posts over 10 days
5. Outreach: pitch to Smartlead / Lemlist / Instantly blogs as guest data piece

## Estimated production cost

- Writer: 6–10 hours
- Editor: 1–2 hours
- Custom chart: 1 hour
- Total: ~$400–800 if outsourced, ~half day if internal
```

## Workflow

1. **Confirm the keyword + intent + vendor domain + ICP**. If missing, ASK.

2. **Pull SERP context**:
   - User-provided URLs (preferred — fetch and analyze)
   - If WebFetch available, fetch top 3
   - If neither: tell the user to paste top-3 URLs, then continue

3. **Search the KG**:
   - `hybrid_search("SEO AI Overviews E-E-A-T")` — get the 2026 rules
   - `hybrid_search("copywriting frameworks")` — apply to article structure
   - `hybrid_search("ICP buyer persona")` — make sure the brief matches the audience

4. **Classify intent** from the SERP. Don't skip this.

5. **Estimate AIO likelihood**. Adjust strategy accordingly.

6. **Find the angle**. If you can't find one in 5 minutes of thinking, ask the user "what's unique about how YOU approach this?" — the angle usually comes from their experience.

7. **Build the outline** — H2 questions, H3 sub-questions, word-count targets.

8. **Generate metadata + schema + internal/external links + promo plan**.

9. **Write the file** — one markdown file at the path the user specified (default: `seo-brief-{slug}.md`).

10. **Report back** — file path + the angle + the one external link the writer must verify before publishing. Don't dump the brief into your reply.

## What this skill is NOT

- Not a writer. You produce the brief; the user/writer produces the article.
- Not a keyword researcher. The user provides the target keyword (or asks `/keyword-research` separately — not yet built).
- Not a Google. You don't have live SERP access unless WebFetch is available and the user authorizes URLs.
- Not a backlink-build tool. You suggest a promo plan; the user executes.

## Common mistakes

- ❌ Skipping intent classification (writes the wrong piece)
- ❌ Ignoring AIO likelihood (writes top-funnel content that's eaten by AIO)
- ❌ No unique angle (commodity piece, won't rank)
- ❌ Generic metadata ("Complete Guide to X")
- ❌ Internal links to nonexistent pages without marking `[NEEDS: ...]`
- ❌ Outline with H2s as statements instead of questions (poor AIO citation, poor PAA capture)
- ❌ No FAQ section (misses easy schema win)
- ❌ Forgetting the promo plan (piece without promo doesn't rank)

## Knowledge graph access

Search before drafting:
- `hybrid_search("SEO 2026 AI Overviews E-E-A-T")` — the 2026 rules
- `hybrid_search("copywriting frameworks")` — for the section-by-section persuasion
- `hybrid_search("ICP buyer persona")` — to keep the brief audience-specific
- `hybrid_search("social platform algorithms")` — for the promo plan

## Success criteria

You succeed when:
- The writer reads the brief and starts drafting without asking a single clarifying question
- The piece has a clear angle (not commodity)
- Intent is correctly classified
- AIO strategy is explicit (write FOR the citation OR avoid the keyword)
- Metadata fits the character limits
- Internal links are real (or marked NEEDS for the topical authority gap)
- The promo plan exists and is actionable
