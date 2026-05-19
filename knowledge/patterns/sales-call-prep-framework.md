---
title: Sales Call Prep Framework
type: pattern
tags:
  - sales
  - calls
  - discovery
  - demo
  - negotiation
  - meddic
  - spin
  - sandler
  - mid-level-architecture
  - b2b
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Sales Call Prep Framework

A one-pager prep doc for an upcoming sales call. The framework below is designed to produce a usable brief in 10 minutes from public inputs (LinkedIn profile, company website, last message exchange) — enough that a vendor with 15 minutes before a 2pm call walks in confident, with the right pitch, the right questions, the right objection responses, and the right ask.

## The brief in 10 sections

A complete call-prep doc has 10 sections in this order, structured for a 5-minute pre-call skim:

1. **TL;DR (90-second read)** — what to know if you only have 90 seconds before the call
2. **The 60-second pitch** — what to say if asked "so what does your company do?", customised to this prospect
3. **Company context** — what they do, scale, recent events (funding, hires, launches)
4. **The buyer (this person)** — role, tenure, prior companies, what they post about, likely JTBD
5. **Buying-committee map** — who else is likely involved (titles + how to surface them)
6. **Likely pains** — 3–5 specific pains based on company + role (not generic)
7. **Discovery questions** — 8–12 ranked open → specific → consequence → qualification
8. **Objection cheat sheet** — 5–7 most likely objections + the vendor's strongest response
9. **Red flags** — signs this is not a fit, or signs the prospect is wasting time
10. **The ask + the close** — 3 graduated next-step asks (best/solid/worst case)

## Call-type adaptation

The brief structure changes by call type. Auto-detect from inputs, or ask:

| Call type      | Heavy on                                                 | Light on                          |
|----------------|----------------------------------------------------------|-----------------------------------|
| **Discovery**  | Pains, qualification, multi-thread questions             | Demo plan, pricing                |
| **Demo**       | "Show this not that" based on use case; demo flow        | Open-ended discovery questions    |
| **Negotiation**| Procurement context, multi-thread, BATNA, deal structure | Surface-level discovery (already done) |
| **Renewal**    | Usage data summary, expansion angles, churn-risk signals | Cold-discovery questions          |
| **Churn-save** | What the actual complaint is, alternatives, rescue ask   | Standard demo flow                |
| **Partnership**| Strategic fit, value exchange, co-marketing potential    | Buy-cycle discovery               |

If the call type isn't specified, default to **discovery**.

## The 60-second pitch (customised)

When the prospect asks "so what does your company do?", the vendor needs a 60-second pitch *specific to this prospect*, not the generic homepage version. Structure:

```
[Identify the customer segment you typically serve — match it to THIS prospect]
   "We solve [problem] for [customer segment] like yours."

[Tell the story most customers tell you when they arrive — name a specific pain]
   "The story most [their role] tell us is the same: [specific pain]."

[Show the resolution in concrete terms]
   "We [verb] those [data sources/workflows/whatever] in [time] and produce [outcome]."

[Establish proof / scale]
   "We've done this for [N] teams in [comparable segment to theirs]."
```

If the vendor's $14M-ARR Series-B target is on the call, the 60-second pitch names "Series-A-to-C B2B SaaS in $5M–$50M ARR" — not "we work with all kinds of businesses." Specificity converts.

## Likely-pain inference (the high-value part)

Generic pains ("they probably want to grow") are useless. Specific pains, inferred from company + role + recent signals, move the conversation. Examples of strong inference:

- **"Series B + 80 employees + just hired a Head of RevOps"** → likely pain: ops is firefighting, no single source of truth across HubSpot+Stripe+Mixpanel, CEO asking "how are we doing" weekly with stale data
- **"DTC skincare brand + 3yr old + Shopify + just launched a new product line"** → likely pain: CAC creeping post-iOS-14, need LTV via subscription or repeat, attribution confusion
- **"Solo founder + 2yr old SaaS + manual outbound to 100/wk"** → likely pain: outbound stops scaling at one person, can't afford an SDR yet, list quality dropping

If you can't infer specific pain, your inputs aren't rich enough. Ask for their recent LinkedIn post or last email — generic pains won't move the call.

## Discovery questions — the SPIN / Sandler / MEDDIC ladder

A vendor doesn't need 30 questions. They need 8–12 ranked questions used as a *menu*, not a script. The ladder (loosely SPIN-derived, with MEDDIC qualification):

**Situation (1–2 questions)** — current state, scale
- "Walk me through your current [X] setup — what's working, what isn't?"
- "How many [Y] are you doing per [time period] right now?"

**Problem (2–3 questions)** — pains, frustrations
- "What's the part of [X] that's annoying right now?"
- "When did this become a problem worth solving?"

**Implication (2–3 questions)** — cost of inaction (the heart of the conversation)
- "What happens if you don't fix this in the next 6 months?"
- "Who else on your team feels this pain?"
- "When the numbers don't match across tools, what's the workflow to reconcile? How long does it take?"

**Need-payoff (1–2 questions)** — what success looks like
- "If we waved a magic wand and fixed [Y], what would change?"
- "How would your job look in 90 days if this worked?"

**Qualification (2–3 questions)** — process, budget, timing (MEDDIC)
- "Who else is involved in the decision?" (the multi-threading question)
- "What's the budget conversation look like at your stage?"
- "What's your decision timeline?"

The vendor picks the 5–7 they'll actually use given how the conversation flows. If the prospect opens up on questions 1–2, skip the rest of that section and move forward.

## Buying-committee map (multi-threading)

A B2B deal has 3–8 stakeholders. Mapping them up front lets the vendor ask the multi-thread question naturally:

| Person                | Role           | Influence       | How to surface       |
|-----------------------|----------------|-----------------|----------------------|
| Maria Chen (champion) | Head of Mktg Ops | Champion       | Already on the call  |
| [Probable] Sarah Lee  | CMO            | Decision maker | "Who'd I be presenting to alongside you?" |
| [Probable] David Park | CFO            | Budget gate    | "When does finance get involved at Acme?" |
| [Maybe] VP Eng        | Integration    | Sign-off       | "How does your eng team usually evaluate vendors?" |

Surface unmentioned committee members during discovery — "If this goes anywhere, who else would be in the decision?" Single-threaded deals close at 50–60% lower rates than multi-threaded ones (every modern sales methodology).

## Red flags — when to disqualify politely

Sometimes a call is a waste. Help the vendor spot it in the first 10 minutes:

- **No clear pain** — they're researching, not buying
- **No process owner on the call** — wrong person; needs multi-thread
- **"Send me pricing and I'll get back" before any value discovery** — usually a polite no
- **"We need a custom proposal" without budget signal** — fishing for ideas
- **Long silences, distracted, takes another call** — not their priority
- **No follow-up question on anything you say** — not actually engaged
- **Promised demo to a junior who won't introduce a decision-maker** — sales-cycle stall

If 2+ red flags appear in the first 10 minutes, end politely: "Sounds like we should reconnect in 30 days when this is more urgent — does that work?" Don't burn a half-hour on a call that won't close.

## The ask + the close (graduated)

Every call ends with a specific next step proposed BY the vendor. Provide 3 graduated asks the vendor picks from based on call signal:

- **Best case ask**: "Want to set up the next call with you + [their boss] in the next 10 days?"
- **Solid case ask**: "I'll send a 1-pager summarising what we discussed — when can we reconnect to walk through it?"
- **Worst case ask**: "Let's set a follow-up in 30 days — by then you'll know if you want to do anything here."

**NEVER end on**: "let us know!" / "shoot me an email when you have a sec." / "feel free to reach out". These are zero-commitment closes that produce zero follow-up. Always propose a specific next step with a specific timeframe.

## Post-call discipline

Within 24 hours of the call, the vendor sends:

1. A 5-min Loom (or written summary) of the discussion, specific to their stack
2. ONE specific reference customer (same segment, 2-line outcome)
3. The follow-up calendar link with a proposed slot

Update CRM:
- Stage progression (e.g. Discovery → Demo Scheduled if positive)
- Champion identified
- Pain captured in their words
- Next step + date

The 24-hour follow-up is the single highest-leverage post-call action. Delays beyond 48 hours measurably drop conversion to the next stage.

## Data sources (public info only)

The brief can pull from:
- **LinkedIn profile** — About, recent posts, prior companies
- **Company website** — About, Pricing, Customers, Blog, Job Postings
- **Last email/reply** — the most informative single source the user can paste
- **Past CRM notes** — paste from the CRM if applicable
- **Funding / hiring data** — Crunchbase, company "About" page, news mentions

**Do NOT**: scrape, infer private data, guess about family or personal life. Public-information-only is both an ethical and a legal constraint (privacy regulations).

## Common brief mistakes

- ❌ Generic pains ("they probably want to grow revenue")
- ❌ Generic questions ("what's keeping you up at night?")
- ❌ 20 hypothetical objections instead of the 5–7 most likely
- ❌ No multi-thread question in the discovery list
- ❌ No specific next step in the close
- ❌ Speculating about personal info from social media
- ❌ Forgetting the TL;DR (vendor has 90 seconds before the call)
- ❌ "Let me know if you have any questions!" as the close

## Success criteria

A brief succeeds when:
- The vendor reads it in 5 minutes and walks into the call confidently
- The likely-pain inference visibly lands with the prospect in the first 10 minutes
- The discovery questions get used (not just listed in the doc)
- The objection cheat sheet contains the actual objection they heard, not a hypothetical
- The vendor knows the specific next step to propose
- Post-call, the vendor can say "the brief was right about pain #1"

## Related

- [[relatedTo::ICP and Buyer Persona Framework]]
- [[relatedTo::Sales Funnel Stages and Bow-Tie Model 2026]]
- [[relatedTo::Copywriting Frameworks for Sales and Marketing]]
- [[relatedTo::Sales Objection Handling Library]]
- [[relatedTo::CRM and RevOps Stack 2026]]

## References

- Neil Rackham — *SPIN Selling* (Situation/Problem/Implication/Need-payoff)
- Jack Napoli — *MEDDIC* methodology (Metrics/Economic-buyer/Decision-criteria/Decision-process/Identify-pain/Champion)
- David Sandler — Sandler Selling System (pain-funnel discovery)
- Chris Voss — *Never Split the Difference* (negotiation tactics for high-stakes calls)
- Armand Farrokh, Nick Cegelski — *Cold Calling Sucks (And That's Why It Works)* (modern outbound + discovery)
- Gong.io / Chorus.ai research — call-analysis benchmarks on what wins deals
