---
title: Solo SaaS Launch Playbook
type: concept
tags: [saas, launch, marketing, comms, founder, product-hunt, show-hn, release, mid-level-architecture, active]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Solo SaaS Launch Playbook

## What This Covers

A single release deserves coordinated multi-channel comms. Solo founders waste hours rewriting the same content for different audiences, or worse — ship a launch with only a tweet and wonder why nobody noticed. This playbook covers the **channel-specific conventions, narrative spine, and T-1/T-0/T+1 schedule** that turn one release into one coordinated launch.

Scope: a public release (v0.X, new feature, new product, milestone). Not covered: ongoing brand-voice marketing, paid acquisition campaigns, customer support comms.

## Pre-Flight: Is This Worth A Launch?

If the change list is **mostly internal** (refactors, infra, perf-without-user-visible-effect), skip the launch sequence. Ship a changelog entry only and save the launch energy for the next user-facing release.

Heuristic: at least one of these must be true:
- A new user-visible feature that solves a stated pain
- A workflow that used to take N steps now takes 1
- A breaking change that needs proactive comms
- A milestone worth a number ($X MRR, N users, v1.0)

If none apply, write the changelog and move on. A "we launched!" tweet about a refactor erodes future-launch credibility.

## The Narrative Spine

Every channel orbits one story arc:

1. **What was the problem?** — one sentence of the pain that existed before
2. **What did you ship?** — one sentence of what changed
3. **Why it matters to a user opening the email** — one sentence of the new capability

Almost all release-comm failure comes from skipping step 1. "We shipped X" is feature-talk. "You can now do Y, which used to take Z" is benefit-talk. Lead with the user benefit; demote feature lists.

## Channel-Specific Conventions

The same story needs different phrasing per channel. Channels reward different reading patterns.

### Changelog (CHANGELOG.md)

- Versioned heading + date (e.g. `## v0.5.0 — 2026-05-19`)
- Three sub-sections: Added / Changed / Fixed (skip empty ones)
- Each bullet: present-tense, user-visible, links to PR if available
- Length: 5–20 bullets total
- Ignore "fix typo in test" — internal noise stays out

### Release-Notes Blog Post (300–700 words)

- Title: specific and benefit-led, **not** "v0.5 Release Notes" (which nobody clicks)
- Hero paragraph: problem → solution → outcome
- 2–4 sections, each one feature explained with a 1-line gif/screenshot suggestion
- "What's next" closing paragraph (direction only — no concrete commitments)
- Single CTA: try it, read docs, or reply if you have feedback

### Tweet / X Thread (5–9 tweets)

- **Tweet 1**: headline hook. Not "v0.5 is out!" — lead with the user benefit
- **Tweet 2**: the "before" pain (visualised if possible — screenshot of the painful workflow)
- **Tweets 3–6**: one feature per tweet, with a concrete example each
- **Penultimate**: link to release notes / blog
- **Final**: soft ask (try it, RT, reply)

Per-tweet rules: ≤270 chars, line breaks for readability, no hashtags except 1–2 in the final tweet. No emojis unless the brand voice already uses them.

### LinkedIn Post (150–250 words)

- Hook line that works as the link preview (first 150 chars)
- Story-mode: what we shipped, why, who it's for
- More professional tone than tweets, more narrative arc
- One CTA at the end

### In-App Announcement Banner (40–80 words)

- One sentence: what's new and what to try
- One CTA button label (2–4 words)
- Dismissable + non-blocking — don't force-modal a feature announcement

### Customer Email (150–250 words)

- Subject line A + Subject line B (for A/B testing)
- Personal-sounding opening ("Hey — we shipped X today…")
- The user benefit in 2–3 sentences
- One CTA to an in-app link (not a marketing landing page)
- Sign-off in founder's name

### Show HN (Hacker News)

- Title: `Show HN: <product> – <one-line description>` — under 80 chars
- First comment (the actual pitch): 80–150 words, **conversational, tech-detail honest**, link to GitHub if open-source
- Tone: HN readers smell marketing-speak instantly. Be specific, technical, candid about limitations
- Timing: weekday 8–10am Pacific is conventional, but **more important: be online for the next 4 hours to reply to comments**. A Show HN that goes hot and the founder is asleep loses the ranking battle.

### Product Hunt

- Opens at 00:01 PT on the day; submission earlier is fine but the launch window starts then
- Tagline: ≤60 chars, benefit-led
- Description: ≤260 chars, plain language, no superlatives
- Maker's first comment: 100–200 words; the story of why you built it; one specific feature to demo
- Gallery image captions: one sentence each
- Be online all day to reply — Product Hunt rewards engagement throughout the day, not just at launch

### Newsletter (Optional, 600–1200 words)

- Longer-form version of the blog post — more storytelling
- "How the idea came up" angle, "what we learned" angle
- Room for 2–3 "small things" not big enough for the blog
- Send the same day as blog, or one day later

## The T-1 / T-0 / T+1 Schedule

A launch is a deployment with comms attached. The schedule:

**T-1 day** (the day before):
- In-app banner queued (deploys with launch build)
- Changelog merged in the release PR
- Blog post scheduled to publish
- Tweet thread, LinkedIn post drafted and queued
- Customer email written, in send tool, scheduled

**T-0** (launch day):
- 09:00 local: blog publishes, tweet thread posts, LinkedIn post goes live
- 09:15: customer email sends
- 10:00: Show HN posted (founder online to reply)
- 00:01 PT: Product Hunt visible (if running PH)
- Throughout the day: reply to every comment within ~30 minutes

**T+1 day**:
- Capture quotes from good responses (testimonial pipeline for next launch)
- Retweet/quote the best engagement
- Send a "thank you" follow-up to power users who replied
- Update the in-app banner (or remove)

## Voice-Matching

A launch package must sound like the founder, not like a launch consultant. Before writing any new copy, **read 2–3 of the founder's prior posts** (release notes, blog, Twitter). Match:

- Sentence length and rhythm
- Formality level
- Punctuation tics (em-dash vs hyphen, semicolon usage)
- Emoji density (most indies use zero; a few use them heavily)
- Hashtag use (default: zero, except 1–2 in the final tweet of a thread)
- Signature style (sign-off pattern, "thanks for reading" closers, etc.)

Default if no reference provided: direct, specific, no marketing hype, no emojis, no exclamation marks.

## Banned Phrases (Indie Brand Voice Hygiene)

- "Revolutionise" / "revolutionary"
- "Game-changer" / "game-changing"
- "Industry-leading"
- "Synergy" / "synergistic"
- "We're excited to announce" (as the first words of anything)
- "Unleash the power of…"
- Emoji-front headlines (🚀 sounds startup-bro to most audiences)
- Inventing customer testimonials (hard rule — if no real quote, leave it out)
- Overpromising "coming soon" features

## Cross-Channel Consistency Check

Before delivering:
- Headline benefit is **the same** across channels (different phrasing, same idea)
- Numbers match exactly across channels (no "5x faster" in tweet and "3x faster" in blog)
- CTAs point to the same destinations
- No internal jargon leaked into customer-facing copy

## Anti-Patterns

- Same exact copy in tweet and LinkedIn — different platforms, different conventions
- Feature lists with no "so what" — every feature needs a 1-line user benefit
- Show HN written in marketing tone — readers smell it; conversational + technically honest only
- Launching when you're not online to respond — the engagement window is the first 4 hours
- "We're excited" openings (cut every time)

## Related

- `[[relatedTo::Landing Page Conversion Audit Framework]]` — landing page is the launch's destination; audit it first
- `[[relatedTo::SaaS Pricing Psychology for Solo Founders]]` — pricing-page changes often coincide with launches
- `[[relatedTo::Customer Development for Indie Founders]]` — launch is also a discovery moment (replies = customer signals)

## Sources

- Pieter Levels' transparent launch threads (Nomad List, Photo AI, Remote OK)
- DHH and Jason Fried, *Rework* / *It Doesn't Have To Be Crazy at Work* — concrete-claim copy patterns
- Show HN community norms (https://news.ycombinator.com/showhn.html) — verified 2026-05-19
- Product Hunt maker guide (https://www.producthunt.com/launch) — timing rules verified 2026-05-19
