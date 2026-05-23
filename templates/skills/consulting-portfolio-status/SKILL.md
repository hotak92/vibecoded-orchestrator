---
name: consulting-portfolio-status
description: Turns a directory of per-client engagement files into a board-ready portfolio status report; use when preparing a weekly digest, a steering-committee briefing, or any multi-engagement roll-up
keywords: [portfolio status, weekly digest, steering committee, board-ready report, multi-engagement roll-up, "status report", "client status report", "portfolio update", "digest for clients", "weekly portfolio"]
model: opus
effort: high
argument-hint: "[engagements-directory-or-rollup-file] [--audience internal|board|partner]"
---

# Consulting Portfolio Status

Synthesises a multi-client consulting portfolio into a single document calibrated to the requested audience. Optimised for a consulting CTO who runs 5-15 concurrent engagements and needs the digest produced in the same work block they read it in.

## When this skill auto-invokes

- "Status across all engagements"
- "Weekly portfolio digest"
- "Board update from project status files"
- "What's burning across the portfolio?"
- "Roll up `<directory>` into a status report"

## When NOT to use

- Single-engagement status (just write it directly)
- Internal staffing-only review (use a capacity sheet, not this)
- Client-facing per-client communication (use `consulting-employee-impersonator` as account manager, or just write it)

## Inputs

Three shapes are supported:

1. **Per-client folder layout** — `engagements/<client-slug>/status.md` per client. The skill globs for these and reads in parallel.
2. **Single rollup file** — `portfolio.md` with one section per client. Skill parses sections.
3. **Free-form notes** — user pastes in a single block. Skill structures it.

If the input shape is ambiguous, ask once. Don't guess.

## Audience modes

`--audience internal` (default):
- Markdown, ~1-2 pages
- Burning items front-loaded
- Internal tone — explicit about money, staffing risk, client politics
- Used by: the CTO, partners, internal ops

`--audience board`:
- Narrative + numbered themes (3-5)
- One explicit ask
- Avoids jargon and intra-firm politics
- Includes commercial roll-up table
- Used by: board, steering committee, investors

`--audience partner`:
- Closer to `internal` but with explicit "what I need from you" section
- Suitable for a co-CTO, COO, or managing partner
- Used by: peers who can act on the contents

If audience isn't specified and the request is ambiguous, ask.

## What the skill produces

```markdown
# Portfolio Status — Week of {date} [{audience}]

## TL;DR
- {3-5 bullets, burning first}

## Action requested
- {only present for board/partner audience}

## Burning ({n})
### {Client}
{2-4 sentences: what, why, stakes, recommended action}

## Watching ({n})
{one-liner per engagement}

## Compounding ({n})
{one-liner per engagement}

## Quiet / Stale ({n})
{client | last update | days since}

## Commercial roll-up
{table: engagement | contract type | budget % consumed | runway weeks | margin trend}
(present for board/partner; suppressed for internal-only if data missing)

## Staffing
{table: person | utilisation % | engagements | rolloff}
(present for internal/partner; suppressed for board unless asked)

## Data gaps
- {what couldn't be filled in and which engagement it affects}

## Portfolio-level risks
- {concentration, single-points-of-failure, contractor reliance, etc.}
```

## Triage rules (must follow)

Each engagement maps to ONE bucket using these rules in order:

1. **Burning** if ANY: red commercial state (overrun, dispute, withheld payment), red technical state (production incident, broken delivery commitment), client escalation pending, milestone slipped without a recovery plan.
2. **Watching** if ANY: yellow on commercial OR technical, single point of failure with no backup, contractor rolling off without successor, key client stakeholder turnover.
3. **Compounding** if ALL: green commercial, green technical, last update <14 days, no open escalations.
4. **Quiet / Stale** if: last update >14 days. Bucket regardless of last-reported colour — old data is its own signal.

If an engagement could go in two buckets, choose the more urgent one.

## Critical thinking required

- **Downgrade wishful colour codes** — when the open-risk list contradicts the colour, re-classify and explain.
- **Surface stale data explicitly** — don't just inherit last week's colour.
- **Flag missing escalation paths** — three consecutive yellows on the same engagement is a structural problem.
- **Reconcile staffing math** — if allocated FTE > available FTE, name the gap; don't average it.
- **Refuse to invent numbers** — if a status file says "good progress" without a budget %, write `—` and add the engagement to the data-gaps section.

## Cross-client NDA boundary

This skill produces ONE document covering MULTIPLE clients. That document is internal-only by default. The skill must:

- Mark output `INTERNAL — DO NOT SHARE WITH CLIENTS` in the header when audience is `internal` or `partner`.
- For `board` audience: still internal to the board, but ensure no client confidential info (specific architectures, named bugs, named individuals at the client) leaks into themes.
- For per-client communication, the user should use a separate workflow that produces one file per client.

## Knowledge graph integration

Before producing the report:

```bash
hybrid_search("portfolio status patterns")
hybrid_search("consulting multi-tenancy isolation")
hybrid_search("client engagement lifecycle")
```

After: if you discovered a recurring portfolio-level pattern (e.g. "engagements past month 4 of fixed-price always go yellow"), suggest writing a KG node and surface the candidate text.

## Anti-patterns

- ❌ Producing a 12-page report
- ❌ Restating last week's bullets unchanged with no acknowledgement
- ❌ Mixing client-confidential detail across sections
- ❌ Inventing budget / utilisation figures
- ❌ Burying burning items below "good news" framing

## Success criteria

- Burning section is ≤5 items, each with a concrete recommended action
- A non-technical board member can read the `board` version and ask the right question
- Data gaps are surfaced explicitly
- CTO can act on the digest in the same morning they read it
