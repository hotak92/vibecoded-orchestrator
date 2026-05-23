---
name: content-calendar-planner
description: Builds a 30/60/90-day cross-platform content calendar for a vendor across Instagram, TikTok, LinkedIn, X, YouTube Shorts, and newsletter. Takes a theme and cadence as input, produces a dated calendar with hooks, hashtags, CTAs, and a repurposing graph showing which posts feed which platforms. Use when the user says "plan content for next month/quarter", "build a content calendar", or "I need 30 days of posts".
keywords: [content calendar, editorial calendar, Instagram TikTok LinkedIn, cadence, repurposing graph, "30 day plan", "60 day plan", "90 day plan", "social media calendar", "post schedule", "posting cadence"]
argument-hint: "[theme or pillar] [cadence: light|medium|heavy] [start-date]"
model: opus
effort: high
---

# /content-calendar-planner

Build a realistic, cross-platform content calendar for a vendor running their own sales/marketing operation.

## Usage

```
/content-calendar-planner "B2B SaaS GTM tips" medium 2026-06-01
/content-calendar-planner "DTC skincare for sensitive skin" heavy 2026-06-15
/content-calendar-planner                                    # interview first
```

If the user invokes without arguments, ASK before generating:

1. What's the theme / content pillar? (one to three pillars)
2. Cadence? (light = 5 posts/week total across platforms; medium = 10; heavy = 20)
3. Start date and duration (30, 60, or 90 days)
4. Which platforms? (Instagram, TikTok, LinkedIn, X, YouTube Shorts, Reddit, newsletter)
5. ICP / target audience (one paragraph)
6. Voice samples (3–5 of vendor's past posts) — REQUIRED, don't skip

Without these, the calendar will be generic. Refuse to proceed if voice samples are unavailable; suggest the user provide their last 10 best-performing posts.

## What this skill does

Produces a single markdown file with:

1. **Calendar grid** (by date) — every post slot with platform, format, status
2. **Per-post specs** — hook, body outline, hashtags, CTA, repurposing fan-out
3. **Repurposing graph** — which long-form pieces seed which short-form posts
4. **Weekly themes** — anchor each week to one sub-topic (avoids drift)
5. **Buffer week** — 1 week of "evergreen" backup posts for sick days / unexpected weeks
6. **Realistic time estimate** — total hours/week of production work

## Cadence reality (read this before promising)

| Cadence    | Posts/week | Production time/week | Realistic for     |
|------------|-----------:|----------------------|-------------------|
| Light      |     5      | 2–3 hours            | Solo + day job    |
| Medium     |     10     | 5–7 hours            | Solo full-time GTM |
| Heavy      |     20     | 12–15 hours          | 1 FT + 1 PT helper |
| Insane     |     30+    | 20+ hours            | 2+ FT, content engine |

If the user requests "heavy" with no team, push back. Most one-person vendors burn out at 10 posts/week within 30–45 days. Recommend "medium" with a ramp ("start at 7, week 2 go to 10").

## Platform mix (the default — adjust per user)

For a B2B vendor, the typical "medium" allocation:

| Platform   | Posts/week | Format mix                                    |
|------------|-----------:|-----------------------------------------------|
| LinkedIn   |    3       | 2 text + 1 carousel                           |
| X          |    5       | Mix: 2 long-form, 3 short/reply               |
| Instagram  |    1       | Repurposed Reel from TikTok                   |
| TikTok     |    0       | (vendor opt-in; B2B-on-TikTok is real but niche) |
| Newsletter |    1       | Weekly digest                                 |
| Total      |    10      |                                               |

For a DTC / B2C vendor:

| Platform   | Posts/week | Format mix                                    |
|------------|-----------:|-----------------------------------------------|
| Instagram  |    5       | 3 Reels + 1 carousel + 1 feed                 |
| TikTok     |    5       | Vertical 21–34s videos                        |
| YouTube Shorts | 2      | Repurposed from TikTok                        |
| X          |    2       | Behind-the-scenes / brand voice               |
| Email/SMS  |    1       | Weekly                                        |
| Total      |    15      |                                               |

Override based on user's actual platform performance.

## What goes in each post slot

```
Date: 2026-06-03 (Tue)
Platform: LinkedIn
Format: Text post (≈1500 chars)
Pillar: Outbound deliverability
Status: To draft

Hook: "We were getting 9% reply rates on cold email. Then Google's Feb 2024
update happened. Two weeks later: 2%."

Body outline:
- What we did to recover (specific list of 5 actions)
- Numbers: reply rates over 90 days
- One contrarian take ("warmup tools are 80% snake oil")
- CTA: comment with your worst delivery month for help

Hashtags: #B2BMarketing #ColdEmail #Deliverability (3 only)
CTA type: Conversation (comments) — not link, not DM
Repurpose into:
  - Day +3: X thread (8 tweets) of the same numbers
  - Day +7: Newsletter section "what we learned about deliverability"
  - Day +14: TikTok video version (if vendor does video)

Time to produce: 30 min (already have the data)
```

## Repurposing graph (the leverage)

Don't produce 10 platform-native posts/week. Produce 3 anchor pieces (long-form blog, podcast appearance, founder essay) and fan them out:

```
Anchor: "How we recovered cold email after Google's Feb 2024 update"
        (one 1,800-word blog post or LinkedIn article)
   ↓
   ├── LinkedIn text post (the headline insight)
   ├── X thread (numerical version)
   ├── 3 IG carousels (one per sub-section)
   ├── 1 TikTok Reel (talking-head 60s)
   ├── 1 Newsletter section
   ├── 1 Reddit r/SaaS post (long-form, original data)
   └── 5 X "atomic insight" posts (one-stat, one-line)
```

One anchor → 13 platform posts. Over 4 weeks at 3 anchors/month = 39 posts. That's how the math works at "medium" cadence with one person.

## Weekly themes (anchor each week)

Pick a sub-topic per week, distinct enough that the audience doesn't see the same idea twice:

```
Week 1 (Jun 1–7):  Cold email deliverability — the Feb 2024 reset
Week 2 (Jun 8–14): ICP refinement — how to fire bad-fit customers
Week 3 (Jun 15–21): Outbound copywriting — the 3B principle
Week 4 (Jun 22–28): The 14-day SDR onboarding playbook
```

Without weekly themes, calendars drift into "post whatever I thought of yesterday" mode. With them, the audience sees the vendor as authoritative on a topic, then on another topic — compounding effect on the audience's mental model of "what this person knows."

## Buffer week + emergency posts

Always include 5–7 "evergreen" posts in the file marked `[BUFFER]`. These are timeless, non-time-sensitive, ready to drop in if a planned post falls through. Pull from:

- A best-of-year recap
- A "common mistake" educational
- A founder story
- A counter-intuitive opinion
- A "tools I use" list

Without buffer, the vendor's first sick day breaks the cadence. With it, the streak holds.

## Output format

Write to `content-calendar-{start_date}-{duration}d.md`:

```markdown
# Content Calendar — Jun 1 to Jun 30 (30 days, MEDIUM cadence)

**Theme**: Outbound + GTM tactics for B2B SaaS founders
**Pillars**: (1) Cold email deliverability, (2) ICP/qualification, (3) Sales process
**Platforms**: LinkedIn (12), X (20), Newsletter (4), 1 Reel/week = 4

**Total posts**: 40
**Production time estimate**: 6h/week
**Anchor pieces** (do these first): 3 long-form blog posts + 1 podcast pitch

---

## Week 1 (Jun 1–7) — Deliverability

| Date       | Day | Platform   | Format       | Hook                                       | Status   |
|------------|-----|------------|--------------|--------------------------------------------|----------|
| 2026-06-03 | Tue | LinkedIn   | Text 1500ch  | "We were getting 9% reply rates..."        | To draft |
| 2026-06-03 | Tue | X          | Thread (8)   | "Numbers: 90-day reply rate recovery"      | To draft |
| 2026-06-04 | Wed | X          | Single       | "DKIM rotation is annual, not 'one-time'"  | To draft |
| 2026-06-05 | Thu | LinkedIn   | Carousel     | "5 things to check when delivery drops"    | To draft |
| 2026-06-06 | Fri | Newsletter | Weekly       | "The Feb 2024 deliverability post-mortem"  | To draft |

[ ... per-post specs after the table ... ]

---

## Week 2 (Jun 8–14) — ICP

[ same structure ]

---

[ continue for weeks 3 and 4 ]

---

## Anchor Pieces (do these first)

### Anchor 1: "How we recovered cold email after Google's Feb 2024 update"
- **Format**: Long-form blog (1,800 words)
- **Why first**: Seeds 8 posts in Week 1
- **Outline**:
  1. What broke (3 paragraphs)
  2. The 5 actions we took (numbered, with screenshots)
  3. Results table (90-day reply rate)
  4. One contrarian take (warmup tools)
  5. The checklist for vendors to copy
- **Estimated draft time**: 3h
- **Where**: Vendor blog + LinkedIn article (cross-post)

[ Anchor 2 and 3 specs ]

---

## Repurposing Graph

```
Anchor 1 (Deliverability blog)
├── LinkedIn text (Jun 3)
├── X thread (Jun 3)
├── X single (Jun 4)
├── LinkedIn carousel (Jun 5)
├── Newsletter section (Jun 6)
├── Reddit r/SaaS post (Jun 7)
└── Reel/TikTok (Jun 8)

Anchor 2 (ICP firing essay)
├── ...
```

---

## Buffer Posts (5 evergreens — use when planned post falls through)

[ 5 posts with hooks ready to deploy ]

---

## Production tracker (the user should keep this)

Weekly recurring slots:
- Sun 2h: Draft anchor for the week + 2 atomic posts
- Mon 1h: Schedule the week's posts into Buffer/Hypefury/Make
- Tue–Fri 30 min/day: Engage with comments + reply guy on X

---

## What this calendar does NOT include

- Paid campaign content (separate workflow)
- Customer support replies (separate workflow)
- 1:1 outreach (use outbound-sequence-writer skill)
- Webinar / live event content (separate planning)

```

## Workflow

1. **Confirm inputs** (theme, cadence, dates, platforms, ICP, voice samples). If any missing, ASK.

2. **Search the KG** for context:
   - `hybrid_search("social platform algorithms")` — match format to platform
   - `hybrid_search("copywriting frameworks")` — apply per-post type
   - `hybrid_search("ICP buyer persona")` — make sure posts speak to the actual audience

3. **Define 3 pillars** that cover the theme. Pillars should be roughly equal-weight; if one pillar produces 60% of posts you've narrowed too much.

4. **Pick anchor pieces** (1 per week, typically): the long-form that fans out into ~10 platform-native posts.

5. **Fill the calendar grid** week-by-week. Don't write all 30 days of post copy — write the hooks and outlines. Detailed drafting is a separate task (the user can ask `/content-calendar-planner draft Jun-3-LinkedIn` later, OR use the outbound-sequence-writer / their own AI).

6. **Build the repurposing graph** — for each anchor, list 8–13 derived posts.

7. **Write the buffer week** — 5–7 evergreen hooks.

8. **Write the file** — one markdown file at the path the user specified.

9. **Report back** — file path + cadence summary + the one anchor piece the user should do FIRST. Don't dump the calendar into your reply.

## Decision tree

```
User asks for:
├── < 14 days of content       → suggest "skip the calendar, just write the 5 posts inline"
├── 30 days, no voice samples  → STOP, ask for samples
├── 30+ days, samples present  → produce full calendar
├── 90 days                    → produce 30-day detailed + 60-day outline (don't try detailed 90)
├── "Just give me 30 posts"    → produce list-format, not calendar grid
└── Specific platform only     → narrow the platform mix
```

## What this skill is NOT

- Not a post-drafter for every single slot. You produce hooks + outlines, not finished drafts. Drafting 30 posts in one go produces mediocre output. The user (or `outbound-sequence-writer`) drafts day-by-day.
- Not a scheduler. You produce a calendar file; the user schedules via Buffer / Hypefury / Make / native tools.
- Not a performance analyzer. You don't have analytics access. The user reviews and adjusts after week 1.

## Common mistakes

- ❌ Building a 90-day calendar with 30-day-level detail (overwhelming, becomes stale by week 2)
- ❌ Same hook style across all posts (the audience pattern-matches your formula)
- ❌ Posting on Sundays in B2B (LinkedIn/Twitter both quiet — wasted)
- ❌ Putting more than 5 hashtags on LinkedIn (look spammy in 2026)
- ❌ Forgetting newsletter / email cadence (often the highest-conversion channel — don't skip)
- ❌ No buffer posts (one sick day breaks the streak)
- ❌ No CTA on every post (every post should have ONE clear next action — comment, save, click, reply, share)

## Knowledge graph access

Search before planning:
- `hybrid_search("social platform algorithms")` — match format and length to current platform signals
- `hybrid_search("copywriting frameworks")` — pick PAS/BAB/AIDA/4Ps per post type
- `hybrid_search("ICP buyer persona")` — make sure each post speaks to the audience's JTBD
- `hybrid_search("SEO AI Overviews E-E-A-T")` — relevant for the blog anchors (cluster pages strategy)

## Success criteria

You succeed when:
- The user reviews the calendar in ≤ 20 minutes and approves it (or asks targeted edits)
- The cadence is honestly sustainable for their team (not aspirational)
- Anchor pieces are clear and the repurposing graph is obvious
- Each post has a hook the user could draft from in 10–20 minutes
- Buffer week is included
- 4 weeks in, the vendor still has 1+ week of content queued (didn't run dry)
