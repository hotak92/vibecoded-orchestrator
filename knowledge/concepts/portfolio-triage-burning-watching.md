---
title: Portfolio Triage (Burning / Watching / Compounding / Quiet)
type: concept
tags:
- concept
- consulting
- portfolio-management
- triage
- mid-level-architecture
- client-management
- best-practices
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Portfolio Triage (Burning / Watching / Compounding / Quiet)

The four-bucket classification model for rolling up state across a multi-engagement consulting portfolio (typically 5-15 concurrent client engagements) into a single signal a CTO or partner can act on within one work block. The buckets are mutually exclusive; every engagement is in exactly one. The model is the scaffolding for weekly digests, board / steering-committee briefings, and triage of incoming client escalations against current commitments.

## Why this matters

Without a fixed bucket model, every portfolio review reinvents the framing — sometimes by stage, sometimes by client size, sometimes by colour code. The result is reports that are inconsistent week-over-week, hard to scan, and easy to game (an engagement marked green last week stays green this week because the framing was different). A fixed four-bucket model with explicit assignment rules produces digests that are scannable, comparable across weeks, and resistant to wishful colour codes.

## The four buckets

### Burning (n)
Needs the CTO's direct attention this week. Front-loaded in every digest.

**Assignment rule** — engagement is Burning if ANY of:
- Red commercial state (overrun, dispute, withheld payment)
- Red technical state (production incident, broken delivery commitment)
- Client escalation pending
- Milestone slipped without a recovery plan

Each Burning item gets 2-4 sentences: what is happening, why now, what's at stake, recommended action.

### Watching (n)
Yellow flags that don't require action this week but will become Burning if ignored for two more weeks.

**Assignment rule** — engagement is Watching if ANY of:
- Yellow on commercial OR technical
- Single point of failure with no documented backup
- Contractor rolling off without a successor
- Key client stakeholder turnover

One-liner per engagement.

### Compounding (n)
Green and trending well. Mentioned briefly so stakeholders see the wins and so the portfolio narrative is balanced.

**Assignment rule** — engagement is Compounding if ALL of:
- Green commercial
- Green technical
- Last update <14 days
- No open escalations

One-liner per engagement.

### Quiet / Stale (n)
No recent activity. Bucket regardless of last-reported colour — old data is its own signal.

**Assignment rule** — engagement is Quiet/Stale if:
- Last update >14 days

Listed as: client | last update | days stale.

## Conflict resolution

If an engagement could go in two buckets, choose the more urgent one. Burning > Watching > Compounding > Quiet. The Quiet bucket overrides Compounding if the data is stale — old greens are not greens, they are unknowns.

## The 14-day staleness rule

If `last_updated` on a status file is older than 14 days, the engagement is Quiet/Stale regardless of its last-reported colour. Old data masquerading as current data is a structural risk: it means no one is watching, and "no news" gets misread as "no problems".

Operationally, this means the digest exposes engagements that have gone dark — and "going dark" is frequently the leading indicator of trouble. An engagement that was green 3 weeks ago and has had no update since may be coasting OR may be a crisis the responsible person hasn't reported.

## Critical-thinking discipline

The triage model exists to RESIST wishful framing. Specific discipline:

- **Downgrade wishful colour codes** — when a PM marks an engagement green but the open-risk list contradicts it, downgrade to Watching or Burning and explain why in the entry.
- **Surface stale data explicitly** — do not inherit last week's colour for engagements with no update. Move them to Quiet/Stale; "stale green" is misleading.
- **Three-yellows-in-a-row is structural** — an engagement that has been Watching for three consecutive reports is a structural problem, not a status line. Surface it to the partner as a portfolio-level concern, not as a routine yellow.
- **Reconcile staffing math** — if total allocated FTE > available FTE, name the gap; do not average it away across the portfolio.
- **Refuse to invent numbers** — if a status file says "good progress" without a budget %, write `—` and add the engagement to the data-gaps section. Inventing figures to fill the table is worse than leaving them blank.

## Output sections (digest skeleton)

```
# Portfolio Status — Week of {date}

## TL;DR
- {3-5 bullets, Burning first}

## Action requested
- {1-3 explicit asks; for board / partner audience only}

## Burning ({n})
### {Client A}
- {2-4 sentences: what, why now, stakes, recommended action}

## Watching ({n})
- {one-liner per engagement}

## Compounding ({n})
- {one-liner per engagement}

## Quiet / Stale ({n})
- {client | last update | days stale}

## Commercial roll-up
{table: engagement | contract type | budget % consumed | runway weeks | margin trend}

## Staffing roll-up
{table: person | utilisation % | engagements | rolloff}

## Conflicts & gaps
- {staffing conflicts}
- {data gaps preventing a full picture}
- {portfolio-level risks (concentration, single points of failure)}
```

## Audience calibration

The same triage produces different deliverables for different audiences (see also [[relatedTo::Consulting Multi-Tenancy Isolation]] for the NDA constraint on cross-client documents):

- **Internal weekly digest** — full detail, all four buckets, commercial roll-up included, internal tone (explicit about money, staffing risk, client politics).
- **Board / steering committee** — narrative format with 3-5 numbered themes, each tied to a metric trend, one explicit ask, no client-confidential specifics (named bugs, named individuals, specific architectures). Commercial roll-up table included.
- **Partner / co-CTO** — closer to internal but with explicit "what I need from you" section.

The Quiet bucket and the data-gaps section are present in all three audiences — visibility of what's NOT known is part of the digest's value, not a defect.

## NDA boundary (multi-client document hazard)

A portfolio digest is ONE document covering MULTIPLE clients. By default it is internal-only and labelled as such ("INTERNAL — DO NOT SHARE WITH CLIENTS"). For per-client communication, the workflow is one file per client, not extracting sections from the digest. Cross-client visibility is an NDA risk; the digest's structure must prevent it from being repurposed.

## Anti-patterns

- ❌ Inventing budget / utilisation numbers when source files don't have them
- ❌ Restating the same status report from last week without acknowledging "no change since last week" explicitly
- ❌ Producing a 12-page report when 1-2 pages would do — the digest is for scanning, not reading
- ❌ Burying Burning items below "good news" framing
- ❌ Mixing client-confidential detail across sections (cross-NDA leakage)
- ❌ Inheriting last week's colour codes without re-evaluating against current evidence
- ❌ Letting "stale green" pass as a current state

## Links

- [[implements::Client Engagement Lifecycle]] — each bucket assignment depends on the engagement's current phase
- [[relatedTo::Consulting Multi-Tenancy Isolation]] — the multi-client document is itself an isolation discipline concern
- [[relatedTo::Contractor vs Employee Management]] — staffing roll-up must surface contractor utilisation, not just employee
- [[relatedTo::Technical Debt Accounting]] — portfolio-level debt is a Watching-or-Burning signal depending on severity
- [[relatedTo::Consulting Deliverable Skepticism]] — the refuse-to-invent discipline applies acutely here
