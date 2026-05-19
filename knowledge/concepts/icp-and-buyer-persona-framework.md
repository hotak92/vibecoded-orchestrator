---
title: ICP and Buyer Persona Framework
type: concept
tags:
  - sales
  - marketing
  - gtm
  - icp
  - buyer-persona
  - mid-level-architecture
  - b2b
  - b2c
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# ICP and Buyer Persona Framework

The ICP (Ideal Customer Profile) is the company-level filter; the buyer persona is the person-level filter. Vendors who conflate them write outbound that fits no one. Vendors who treat them as separate layers compound — every channel (email, ads, content, calls) reads from the same definition.

## Three layers, distinct

**ICP** (firmographics, B2B) or **target segment** (B2C demographics + life stage):
- B2B firmographics: industry, sub-vertical, employee count, revenue band, geography, funding stage, tech stack
- B2C segment: age range, income band, geography, life stage, behaviours/values
- Negative ICP: who you explicitly do NOT sell to (saves 30–50% of pipeline noise)

**Buyer persona** (the human you're emailing/messaging):
- Role / seniority (B2B) or psychographic profile (B2C)
- Goals (what they're hired to do / what they want)
- Pains (what's blocking them today)
- Watering holes (where they consume content — LinkedIn, X, Reddit r/<sub>, YouTube channels, podcasts)
- Trigger events (what makes them suddenly buy — funding round, new hire, product launch, life event)

**Jobs-to-be-done (JTBD)** (the underlying motivation):
- "When [situation], I want to [motivation], so I can [outcome]"
- Cuts across personas — two very different personas often share the same JTBD
- Most useful for messaging and product positioning

## Building the ICP (one weekend exercise)

1. List your top 10–20 best customers (revenue, retention, NPS — pick one metric)
2. List your worst 5 (churned fast, low CSAT, gave you grief)
3. For each, capture firmographics + buyer role + trigger event (if knowable)
4. Find the patterns in the "best" list that are absent in the "worst" list
5. Write the ICP as a one-paragraph filter — if a prospect doesn't pass, don't outbound them

**Example ICP filter** (B2B SaaS):
> Seed-to-Series-B SaaS companies (10–80 employees, $1M–$20M ARR), product-led, US/EU/AU, currently using HubSpot or Pipedrive, with 2+ marketing hires.

A junior SDR should be able to read this and instantly disqualify 80% of inbound leads.

## Building the buyer persona (the part most people skip)

For each persona, write a one-pager:

```
Persona: Marketing Ops Lead at PLG SaaS

Goals:
- Attribute revenue across channels
- Reduce CAC by 15% YoY
- Stop manual list-uploading between tools

Pains:
- Stitching HubSpot + Mixpanel + Stripe with brittle Zaps
- CMO asking "where's the pipeline?" weekly
- One person doing the work of three

Trigger events:
- Hired in last 90 days (looking to make a mark)
- Series B raise (budget unlocked)
- Job posting for "RevOps Manager" (overwhelmed)

Watering holes:
- LinkedIn (specifically: posts from Kyle Poyar, Emily Kramer)
- r/RevOps, r/SaaS
- The Modern CMO podcast, MKT1 newsletter

Best opening line:
"Saw you joined [Company] 60 days ago — most new mktg ops leads at PLG SaaS spend month 1 untangling HubSpot+Mixpanel. We made a 1-pager on the 3 setups that don't break. Worth a 5-min skim?"
```

## How JTBD changes the message

Same persona (Marketing Ops Lead), different JTBD = different message:

- JTBD = "prove ROI to my CMO": Lead with attribution + dashboards
- JTBD = "stop being the bottleneck": Lead with automation + self-serve
- JTBD = "level up to RevOps Director": Lead with strategic frameworks + community

Cold outbound that names the JTBD outperforms cold outbound that names features by 3–5x reply rate (cite: most outbound benchmarks 2023–2025, Lavender / Apollo / Outreach data).

## Common mistakes

- **One ICP for everything**: real vendors usually have 2–4 ICPs (e.g. "SMB self-serve" + "mid-market sales-led"). Each needs its own funnel and messaging.
- **Persona without JTBD**: you'll write generic "Hi [Name], I noticed you're a [Role]" emails that anyone could send.
- **No negative ICP**: SDRs waste cycles on out-of-ICP prospects because nobody told them to disqualify.
- **Personas built from gut, not data**: interview 5 actual customers per persona. Don't make it up.

## Tools that help

- **Clay** / **Apollo** / **Cognism** / **Ocean.io** — ICP enrichment + lookalike search
- **Common Room** / **Default** — surface trigger events from public signals
- **Customer interviews** (5–10 per persona) — still the cheapest, highest-signal input
- **Gong / Chorus call recordings** — mine pain language straight from prospect mouths

## Operational rule

Update ICP + personas every 2 quarters at minimum. Market shifts, your product shifts, the buyer's job shifts. A stale ICP is worse than no ICP — it sends confident-sounding outbound to the wrong people.

## Related

- [[relatedTo::Copywriting Frameworks for Sales and Marketing]]
- [[relatedTo::Sales Funnel Stages and Bow-Tie Model 2026]]
- [[relatedTo::Email Deliverability 2026]]

## References

- Bob Moesta, *Demand-Side Sales 101* — JTBD applied to sales
- Tony Ulwick, *Jobs-to-Be-Done: Theory to Practice* — original JTBD framework
- MKT1 newsletter (Emily Kramer / Kathleen Estreich) — ICP for early-stage B2B
- Apollo + Outreach 2025 outbound benchmark reports
