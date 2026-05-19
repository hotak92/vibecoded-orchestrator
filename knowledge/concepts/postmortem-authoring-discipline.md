---
title: Postmortem Authoring Discipline
type: concept
tags: [devops, sre, operations, postmortem, mid-level-architecture, incident-response, documentation]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Postmortem Authoring Discipline

The blameless framing of post-mortems (see [[relatedTo::Blameless Postmortem Methodology]]) is the *cultural* prerequisite. This node is the *authoring* discipline — how to actually write the document so that it changes future behavior rather than gathering dust. Different concern, same goal.

The author's job, in one sentence: produce a document whose action items are concrete enough that, one year from now, a new engineer can read it and understand what changed and why.

## Three Common Failure Modes

Most post-mortems fail in one of three ways:

1. **Narrative without analysis** — reads like a chronicle: "and then X happened, and then Y happened". The reader learns *what* but not *why*, and certainly not what to change.
2. **Root cause as person** — "X engineer pushed bad code" or "Y team missed the review". Stops at human, never asks why the *system* allowed the human action to cause an outage.
3. **Action items as wishes** — "improve monitoring", "be more careful with deploys". Unowned, unmeasured, ungrounded; nothing ships.

The disciplines below counter each.

## Timeline Reconstruction

The timeline is the document's spine. Build it from the scribe log captured during the incident (see [[relatedTo::SRE Incident Response Playbook]]), augmented with system data. Format:

```
2026-05-18 14:23:00 UTC — Deploy abc123 of payment-service to prod completes
2026-05-18 14:31:00 UTC — Error rate for /api/v1/charge crosses 1% (PagerDuty fires)
2026-05-18 14:33:00 UTC — On-call acknowledges; opens incident in #inc-2026-05-18-payment
2026-05-18 14:37:00 UTC — Initial hypothesis: database connection pool exhaustion
2026-05-18 14:42:00 UTC — Hypothesis ruled out: pool metrics stable
2026-05-18 14:48:00 UTC — Second hypothesis: deploy abc123 (correlates with start time)
2026-05-18 14:51:00 UTC — Rollback initiated
2026-05-18 14:54:00 UTC — Rollback complete in us-east-1
2026-05-18 14:56:00 UTC — Error rate returns to baseline
2026-05-18 15:01:00 UTC — Monitoring shows stable for 5 minutes; incident resolved
```

Reconstruction rules:

- **UTC timestamps**, always. Multiple-timezone teams misread local times constantly.
- **Distinguish observation from action**. "Error rate crossed 1%" is an observation. "Rollback initiated" is an action. Mixing them in prose loses the distinction.
- **Include the dead ends**. The 14:37 → 14:42 dead end (connection pool hypothesis) is *evidence about your monitoring story*: why did the team think pool exhaustion plausible? Often this reveals a real gap.
- **Pin the times against system data**, not memory. Commit timestamps, deploy timestamps, alert timestamps from PagerDuty/Datadog — all stable. Memory at 48 hours is unreliable.

## Contributing-Factor Analysis (NOT "Root Cause")

The phrase "root cause" implies *the* single cause. Real outages are intersections — a code bug *and* a missing test *and* a monitoring blind spot *and* a deploy-window choice. Use "contributing factors" instead, and enumerate them.

Apply the **5 Whys** technique, but with discipline:

- Stop at "system / process" — never at "person".
- A "why" answer that ends at "Bob made a mistake" is not done. Ask: why did the system let Bob's mistake reach production?

Worked example:

```
1. Why did the service return 500s?
   → Because the new code path threw a NullPointerException.
2. Why was that path not caught in testing?
   → Because the integration test suite mocked the upstream dependency,
     which never returns null in the mock.
3. Why does the real dependency return null in this case?
   → Because the partner API added a new response shape last week
     and we didn't update our parser.
4. Why didn't we notice the API change?
   → Because we don't subscribe to the partner's changelog and they
     don't notify customers of additive changes.
5. Why don't we have a contract test against the live partner API?
   → Because the partner doesn't expose a sandbox suitable for CI,
     and we never built a periodic live-probe.
```

Five contributing factors fall out naturally: (1) missing null-handling in the new code path, (2) over-mocked integration test, (3) parser tied too rigidly to known response shapes, (4) no monitoring of partner-API changes, (5) no live contract test. Each can become its own action item.

## Action-Item Categorization

Categorize every action item by *where in the failure path* it intervenes:

| Category | Purpose | Time horizon |
|---|---|---|
| **Prevent** | Stop the same cause from recurring | Weeks |
| **Detect** | Notice sooner if it does recur | Days–weeks |
| **Mitigate** | Reduce blast radius / recovery time | Weeks–months |
| **Document** | Update runbook so the next responder finds the answer fast | Days |

A healthy post-mortem has items in *at least three* categories. If everything is "Prevent", you're missing detection/mitigation depth-in-defense. If everything is "Document", you're not actually fixing anything.

Worked example continuing from above:

| Category | Item | Owner | Due |
|---|---|---|---|
| Prevent | Add null check in `PaymentParser.parseResponse()` with regression test | @alice | 2026-05-22 |
| Prevent | Replace upstream-dep mock with a contract-test against partner's recorded responses | @bob | 2026-06-01 |
| Detect | Alert on partner API response-shape drift via JSON schema validation | @carol | 2026-06-08 |
| Detect | Alert on /charge error-rate above 0.1% (currently 1%) | @alice | 2026-05-25 |
| Mitigate | Add circuit-breaker around partner API call so failures don't 500 the whole charge flow | @bob | 2026-06-15 |
| Document | Update payment-service runbook: section on partner API changes, link to schema-drift alert | @carol | 2026-05-22 |

**Action-item shape**:

- Owner is a *named person*, not a team. Teams don't ship action items.
- Due date is a *specific date*, not "Q3" or "ASAP".
- The deliverable is *verifiable* — someone can check "is this done?" without arguing.
- Tracked in the same system as other engineering work (Jira, GitHub Issues, Linear). A separate "post-mortem tracker" guarantees forgotten items.

## What the Document Must Contain

Minimum sections, in order:

1. **Summary** (2–4 sentences) — what happened, how long, customer impact. The TL;DR for someone who won't read the rest.
2. **Impact** — quantified: error count, duration, customers affected, revenue impact if known, SLO budget consumed.
3. **Timeline** — as above.
4. **Contributing factors** — as above, enumerated.
5. **What went well** — yes, really. Worth recording: fast detection, clean rollback, good comms. These are reproducible behaviors; name them so they stay reproducible.
6. **What went poorly** — gaps, delays, dead ends. Don't soften.
7. **Where we got lucky** — the bits that worked by accident and might not next time. These are often the most important section because they're invisible action items.
8. **Action items** — categorized and owned, as above.
9. **Supporting data** — graphs, log excerpts, commit links. Embedded or linked.

## Anti-Patterns

- **The single root cause** — "the cause was a missing null check". No. The cause was a missing null check *and* a test gap *and* a monitoring gap *and* a deploy-without-canary.
- **Naming individuals as causes** — "alice's deploy caused the outage". The deploy caused symptoms; the *system that let the deploy ship with a bug* caused the outage. Talk about the system.
- **Future-tense lessons** — "we will be more careful". Not an action item. "Add canary deploys to payment-service by 2026-06-01, owned by @bob" is an action item.
- **Action items without exit criteria** — "Improve monitoring." When is this done? Replace with "Add alert on metric X with threshold Y; close ticket when alert is firing in staging."
- **Burying the customer impact** — leadership reads the Summary. If the customer impact isn't quantified there, they won't find it.
- **Skipping the read-out** — the document is necessary but not sufficient. A 30-minute review meeting with the team and adjacent teams produces lateral learning that the document alone cannot.

## Cadence

- **Draft within 48 hours**. Memory degrades fast.
- **Review meeting within 1 week**. Author presents, peers ask questions, action items are finalized and assigned in the meeting.
- **Follow-up audit within 30 days**. Walk through the action items: which shipped, which slipped, why. Slipped items either get re-committed or explicitly closed-as-won't-fix.
- **Quarterly trend review**. Aggregate categories across post-mortems: are detection items consistently slipping? Are prevent items consistently shipped but new failures of the same shape recur? The patterns are organizational, not per-incident.

[[relatedTo::Blameless Postmortem Methodology]] [[relatedTo::SRE Incident Response Playbook]] [[relatedTo::SLI Selection Methodology]] [[relatedTo::USE, RED and Four Golden Signals]]
