---
title: Cold Outbound Sequence Architecture
type: pattern
tags:
  - sales
  - marketing
  - outbound
  - email
  - linkedin
  - sequence
  - cadence
  - mid-level-architecture
  - b2b
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Cold Outbound Sequence Architecture

A cold outbound sequence is a multi-touch campaign across one or two channels (typically cold email + LinkedIn DM) designed to convert a defined ICP from "never heard of you" to a booked meeting. The architecture below — touches, cadence, ask-laddering, reply branches — is what consistently works in 2026 after deliverability tightening and the death of generic personalization.

## The default 7-touch shape (over 21 days)

The shape generalises to most B2B outbound. Override per ICP and per channel mix; do not collapse below 4 touches (single-touch outbound rarely produces meetings in 2026 — saturated channels need persistence).

| Touch | Day | Channel  | Goal                                       | Word target | Framework        | CTA type                |
|-------|----:|----------|--------------------------------------------|------------:|------------------|-------------------------|
| 1     |   1 | Email    | Open the conversation; segment-level relevance | < 75    | PAS or 3B        | Easy yes/no OR doc offer |
| 2     |   4 | Email    | Re-frame value, different angle            | < 60        | BAB              | Same as T1 OR artefact  |
| 3     |   7 | LinkedIn DM | Soft, conversational, human-not-sequence | 2–3 sentences | (no framework — human voice) | Connect OR question |
| 4     |  10 | Email    | Provide value, not pitch                   | < 80        | FAB or 4Ps       | Soft meeting ask        |
| 5     |  14 | Email    | Direct meeting ask, break-up framing       | < 60        | (direct)         | Book call OR "not interested" reply |
| 6     |  18 | LinkedIn DM (optional) | Permission-based ping        | < 40 (msg)  | (human voice)    | Channel-switch question |
| 7     |  21 | Email (optional) | Closing the loop                  | < 30        | (direct)         | Implicit (reply or don't) |

**Why this shape works**:
- Asking for the meeting in Touch 1 drops reply rate 3–5x (premature ask). The ladder starts tiny.
- Touch 4 is statistically the highest-reply touch — it shows up after the prospect has registered the sender's existence (touches 1–3) and arrives with value, not a pitch.
- Touch 5's "break-up" framing ("last email — should I stop?") triggers loss-aversion replies more than any other framing.
- Touch 7 is closure-as-courtesy; ~5–10% of replies come from it.

## Subject line patterns (≤ 45 chars, ≤ 6 words)

Mobile clients show ~35 characters before truncation. Optimise for that constraint. Patterns that work in 2026:

- **Lowercase, no punctuation**: `quick thought on your hubspot setup`
- **First name as subject**: `Maria`
- **Question subject**: `is this the right ask?`
- **Specific reference**: `your tweet about RevOps`
- **One-word power subject**: `thoughts?` / `fit?`
- **Open loop**: `the thing about HubSpot Mixpanel`

**Avoid** (all trip filters or read as sequencer-bait):
- ALL CAPS
- Multiple punctuation marks (`!!!`, `??`)
- `Re:` prefixes when not actually replying
- Emojis (deliverability hit — Gmail/Outlook penalise)
- "open this!" / "act fast" / "limited time"
- Numbered subjects (`[#1] Hey Maria`)

## Personalization quality bar

The 2022–2024 "first-line personalization" arms race produced terrible templated openers (`I noticed you're a [Role] at [Company]`). In 2026 these are worse than no personalization — they signal "automated sequence" within the first reading second. A real personalization references ONE of:

- A specific post they wrote on LinkedIn/X (with paraphrased takeaway, NOT "great post")
- A specific company event (raise, hire, product launch, podcast appearance) within the last 90 days
- A specific job-posting on their site (signals what they're building)
- A specific customer of theirs you noticed (B2B research)
- Something contextual and natural (shared city, alma mater) — only if it doesn't feel stalker-ish

**If you can't find a real angle in 5 minutes, switch to segment-level relevance instead:**
> "Seed-stage SaaS founders we've talked to in the last 30 days all hit X problem at the same point..."

Segment-level relevance reads as honest research; bad first-name-and-role variables read as spam.

## Ask-laddering (the core principle)

Touch 1's CTA must be smaller than touch 5's CTA. The ladder:

| Touch | CTA size       | Example                                              |
|-------|----------------|------------------------------------------------------|
| 1     | Tiny           | "Want me to send the 1-pager?"                       |
| 2     | Tiny           | "Reply yes/no — should I send the case study?"       |
| 3     | Social         | "Mind if I connect?" (LinkedIn)                      |
| 4     | Soft meeting   | "Worth a 15-min chat about this?"                    |
| 5     | Direct meeting | "Tuesday 2pm work, or should I stop emailing?"       |
| 6     | Channel-switch | "Wasn't sure which channel works better — DM ok?"   |
| 7     | None / implicit | "Closing the loop — anything to follow up on later?" |

Going heavy in touch 1 ("book a 30-min demo") is the single most common cold-outbound mistake. Reply rates drop 3–5x vs the same content with a yes/no opener.

## Reply branches (always include)

A sequence without reply branches is a sequence that breaks the moment someone replies. The seven essential branches:

- **Positive — meeting interest**: short, confirmatory, ONE qualifying question before booking. "Great — are you the one who'd actually use this, or scoping for someone? Want to bring the right context."
- **Objection — price**: don't defend; acknowledge, ask for context. "What budget would make this an obvious yes? We've been honest with founders about which tier fits which stage."
- **Objection — timing ("not now")**: ask when, mark a calendar follow-up. "Makes sense — Q3? Q4? I'll put a placeholder and ping you then."
- **Objection — already have a vendor**: ask what's working/not. "Smart — what's the current stack? Most teams switch when X breaks, curious if you're seeing any of that yet."
- **Negative — not interested / unsubscribe**: honour immediately, polite one-line acknowledgement. "All good — taking you off the list. If anything changes, you know where to find me."
- **Out-of-office**: snooze 7 days, no reply.
- **Wrong person**: ask for the right contact. "Apologies for the misdirect — would you mind pointing me at the right person on your team?"

Missing any one of these branches degrades the entire sequence; the seven cover ~90% of real-world reply types.

## Deliverability respect (the sequencer dependency)

The writer of the sequence doesn't send, but the sequencer does. Drafts must respect 2026 deliverability rules:

- Plain text or text-with-1-small-inline-image — image-heavy emails get spam-foldered in cold outbound
- ONE link maximum in cold outbound (a single calendar link or doc link)
- No spam-trigger phrases — "guaranteed", "100% free", "click here now", "act fast", "limited time", excessive `!!!`
- Plain-text-friendly HTML — should look identical with HTML disabled
- No tracking pixels on touch 1 — pixels on first contact are a small but real spam signal (some sequencers auto-add; instruct the user to disable)
- List-Unsubscribe header present (the sequencer should add it — flag if not)
- Send from a **warmed subdomain**, never the vendor's primary domain (a single bad sequence kills your password-reset emails if you don't separate)

Full deliverability rules: `knowledge/concepts/email-deliverability-2026.md`.

## Volume + sending discipline

Per inbox / per day:

- Week 1 of new domain: 10–20 sends/day (mostly warmup pool)
- Week 2: 30–50/day
- Week 3: 60–100/day
- Week 4+: 150–250/day max per inbox; most agencies cap at 30–50/inbox/day for safety

Across N inboxes on M domains: total daily volume = N × M × cap. A solo founder with 2 domains × 3 inboxes × 30/day = 180 cold sends/day = ~3600/month. Above that requires more infrastructure.

## Timing windows (default; override per timezone)

Detected from prospect location (LinkedIn / Apollo enrichment):

- US: Tue/Wed/Thu, 8–10am local
- EU: Tue/Wed/Thu, 7–9am local
- AU: Tue/Wed/Thu, 8–10am local
- Avoid Mondays (lowest open rates) and Fridays after lunch (low intent)
- Avoid weekends in B2B entirely

## Suppression list (the discipline)

The sequencer must suppress, before any send:

- Existing customers
- Current open opportunities
- Competitor employees (don't email the competition's CEO by accident)
- Anyone who previously unsubscribed (CAN-SPAM + GDPR requirement)
- Anyone who replied "not interested" in a prior sequence
- Anyone the vendor has had > 3 prior touches with in the last 90 days (saturation)

## Calibration: edit rate by sequence number

- First sequence the assistant produces: ~30% of touches need edits before send
- Second sequence: ~15% edits
- Third+ sequence: < 10% edits if voice samples were representative

If edits stay > 20% after sequence 3, the voice samples weren't representative — request more or different samples (the vendor's actual best-performing past outbound, not their default style).

## Common architecture mistakes

- ❌ Same opener pattern in all 7 touches — boring after touch 2; rotate openers
- ❌ Asking for the meeting in touch 1 (3–5x reply-rate drop)
- ❌ Stuffing 3 CTAs in one email ("reply, click here, OR book a call")
- ❌ Using the prospect's full name in body ("Maria Chen, I noticed...") — first name only
- ❌ Reusing the same case study across all touches — rotate proof points
- ❌ Drafts longer than the spec ("just one more sentence" — no, cut)
- ❌ Forgetting the unsubscribe branch (illegal in CAN-SPAM, GDPR territories)
- ❌ Variables that read as such: `I see you're working on great things at {{company}}`

## Related

- [[relatedTo::ICP and Buyer Persona Framework]]
- [[relatedTo::Copywriting Frameworks for Sales and Marketing]]
- [[relatedTo::Email Deliverability 2026]]
- [[relatedTo::Sales Funnel Stages and Bow-Tie Model 2026]]
- [[relatedTo::Sales Objection Handling Library]]

## References

- Josh Braun, Jason Bay, Will Allred — modern 3B (Brevity/Bluntness/Basic) school
- Apollo, Outreach, Salesloft — 2024–2025 outbound benchmark reports (reply rates by touch, by industry)
- Smartlead, Instantly, Lemlist — sequencer documentation on warmup + cadence
- Lavender — AI email coaching dataset (subject-line + opener performance)
- *Cold Calling Sucks (And That's Why It Works)* — Armand Farrokh, Nick Cegelski (modern outbound multi-channel playbook)
