---
title: Customer Development for Indie Founders
type: concept
tags: [saas, customer-development, business, growth, founder, mom-test, jtbd, mid-level-architecture, active]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Customer Development for Indie Founders

## The Core Problem

Indie founders bias toward shipping over talking. The result: products that solve problems the founder finds interesting, sold to customers who don't exist in the size needed for a sustainable business. Customer development is the disciplined practice of talking to potential and actual customers in a way that surfaces real information instead of lies that sound like compliments.

This note distils the operationally useful patterns from three traditions:
1. **The Mom Test** (Rob Fitzpatrick) — how to ask questions that don't fish for compliments
2. **Jobs-to-be-Done** (Christensen, Ulwick) — what people *hire* products to do
3. **Continuous discovery** (Teresa Torres) — weekly cadence, not one-shot

## The Mom Test in 3 Rules

The book's framing: even your mom will lie to you if you ask "do you think my idea is good?" To get truth instead of politeness:

1. **Talk about their life, not your idea**
   - ❌ "Would you use a tool that does X?"
   - ✅ "Walk me through the last time you did X."

2. **Ask about specifics in the past, not generics about the future**
   - ❌ "Would you pay for this?"
   - ✅ "When you last had this problem, what did you do? How much did you spend?"

3. **Talk less, listen more**
   - The founder talking is the founder pitching. The customer talking is the founder learning.

The litmus test: if you couldn't have learned the same thing from someone who'd never seen your idea, you ran a good interview.

## JTBD: What People Hire You to Do

Christensen's framing — customers don't buy products, they "hire" them to make progress in their lives. The right question is:

> "What was happening in your life that made you start looking for a solution?"

The structure that surfaces a useful JTBD statement:

> When [situation], I want to [motivation], so I can [expected outcome].

Example for a writing tool:
> When my client sends edits at midnight and I'm too tired to format them properly, I want to paste them in and get clean markdown back, so I can ship the post before sleeping.

Notice what JTBD captures that personas don't:
- The **triggering situation** (when, not who)
- The **motivation** (what makes this urgent now)
- The **outcome** (the success criterion, not the feature list)

A persona says "freelance writers, 25–40, remote." A JTBD says "the moment they want a clean version of messy input fast." The second tells you what to build and how to position.

## Three Interview Types, Three Purposes

| Type | When | Length | Goal |
|------|------|--------|------|
| **Problem interview** | Pre-product or major pivot | 30–45 min | Validate problem exists and is painful enough to pay for |
| **Solution interview** | Pre-launch or new feature | 30 min | Validate the proposed solution matches the problem |
| **Churn / cancel interview** | Post-cancel | 15–20 min | Why did they leave; could anything have saved them |

The most underused is the **cancel interview**. Indies skip it because it's painful. It's the cheapest research you'll ever do — the person already gave you their honest opinion by leaving.

## The Pain × Frequency × Willingness-to-Pay Matrix

Cluster what you learn from interviews into three axes:

- **Pain**: 1 (annoying) → 5 (blocks their work)
- **Frequency**: 1 (yearly) → 5 (daily)
- **WTP**: 1 ("nice to have, $0") → 5 ("would pay 10x current alternative")

The fundable triangle is **pain ≥ 4, frequency ≥ 3, WTP ≥ 3**. Below that, you're building a hobby. Above pain 4 frequency 5 WTP 5 you're building a real business.

After 10 interviews, plot the problems you heard on this grid. The top-right cluster is where to ship.

## How Many Interviews Is Enough?

Standard answer from research literature: **~10 interviews** uncover 80% of the major themes (saturation point). 20 doubles your confidence. Beyond 30 with diminishing returns.

Practical indie cadence:
- **Week 1**: 5 interviews. Pattern-match. Form initial hypothesis.
- **Week 2**: 5 more, targeting the apparent JTBD more narrowly. Confirm or pivot.
- **Ongoing**: 1–2 customer conversations per week, indefinitely.

The mistake is "I did 20 interviews 6 months ago." Customer reality drifts. The discipline is continuous, not one-shot.

## Sourcing Interview Subjects

Solo founders' most common blocker: "I don't have anyone to talk to."

In order of quality (highest signal first):
1. **Current paying customers** — call your top 5 today
2. **Recent cancellers** — email them the day after they cancel
3. **People who signed up but never paid** — what stopped them?
4. **Cold outreach in target segments** — LinkedIn DM, Reddit DM, Twitter DM. Offer a $50 gift card for 30 minutes. ~10% reply rate is normal.
5. **Communities** (Indie Hackers, niche subreddits, Discord servers) — post asking for 20-minute calls. Lower quality, but free.
6. **Friends-of-friends** in target segments — last resort; bias toward politeness

Avoid: friends and family who match your target demo. They lie to be supportive.

## The Cancel Interview Script (15 Minutes)

A specific high-ROI template:

1. "Thanks for trying us — I'd love to learn from your experience. Mind a 15-minute call?"
2. "When you signed up, what were you hoping to accomplish?"
3. "How did that go in the first week?"
4. "What made you decide to cancel?"
5. "What did you switch to, if anything?"
6. "Is there anything we could have done that would have made you stay?"
7. "Would you mind if I follow up if we ship X feature in the future?"

Q4 + Q5 are the two most useful. The "switched to" answer tells you who your real competitor is — often it's spreadsheets or "I just stopped tracking it," not the company you thought.

## Analysing Interview Notes

After 10+ interviews, pattern-match by:

1. **Clustering** verbatim quotes by theme (use sticky notes, a spreadsheet, or an AI tool)
2. **Frequency** — how many interviewees mentioned this unprompted?
3. **Severity** — how strongly did they describe the pain? "Annoying" vs "the worst part of my week"
4. **Recency** — did this come up in the last 5 interviews, or only the first 5? (Drift signal)

The output is a **prioritised problem list**, not a feature list. Features are downstream of which problems you commit to solving.

## Common Failure Modes

- **Leading questions**: "Don't you think X is annoying?" — Will produce yes regardless of truth.
- **Hypotheticals about the future**: "Would you pay?" — Lies. Ask about past purchases instead.
- **Talking to one segment only**: 10 conversations with one customer type doesn't generalise.
- **Stopping after 3 interviews because it "feels clear"** — confirmation bias; you only hear what fits your hypothesis.
- **Not writing it down**: memory is reconstructive; you'll remember the bits that confirmed your view.

## Related

- `[[implements::North-Star Metric Selection for Solo SaaS]]`
- `[[relatedTo::Churn Taxonomy and Reduction Tactics]]`
- `[[relatedTo::SaaS Pricing Psychology for Solo Founders]]`

## Sources

- Rob Fitzpatrick, *The Mom Test* (book)
- Clayton Christensen, *Competing Against Luck* (JTBD foundational text)
- Wikipedia, *Jobs-to-be-Done* — https://en.wikipedia.org/wiki/Jobs-to-be-Done — verified 2026-05-19
- Teresa Torres, *Continuous Discovery Habits* (book) — weekly cadence framing
