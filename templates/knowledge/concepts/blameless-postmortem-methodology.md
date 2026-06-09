---
title: Blameless Post-Mortem Methodology
type: concept
tags: [SRE, incident-response, postmortem, process, devops, infrastructure, mid-level-architecture, culture]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Blameless Post-Mortem Methodology

## Definition

A **blameless post-mortem** is a structured retrospective written after a production incident that focuses on *systemic contributing factors* rather than individual mistakes. The premise: a system that allows a human action to cause outage is itself the failure. The document's purpose is to drive *durable corrective action* — changes to code, process, alerting, and architecture — not to assign blame. Canonical reference: Google SRE Book, *Postmortem Culture* — https://sre.google/sre-book/postmortem-culture/

## Why Blameless

When engineers fear blame, they:
- Wait longer to declare incidents (data lost)
- Omit context from timelines ("I bypassed the deploy gate because...")
- Resist root-cause questions
- Don't share lessons learned

When engineers trust the process, they:
- Declare incidents early (less damage)
- Provide full timelines including their own actions
- Engage with "why was that easy to get wrong?" questions
- Share findings widely

**Blameless ≠ accountabilityless.** Action items still have owners. Repeat mistakes still trigger process changes. The distinction: the post-mortem document doesn't name individuals as the cause; it names the system, the gap in tooling, the missing guardrail.

## When to Write One

A post-mortem is required when any of:
- User-visible impact > N minutes (set N per org; typically 5–15 for high-tier services)
- Data loss or corruption (any duration)
- Security incident
- Near-miss that would have been severe (called a "near-miss post-mortem"; valuable for learning before customers feel it)
- Recurring small issue (third occurrence triggers a deep-dive)

Low-impact incidents may use a short-form template; high-severity ones use the full document.

## Document Structure

```markdown
# [INCIDENT-2026-05-18-01] API 500s during deploy

**Status**: Draft | Review | Final
**Severity**: SEV-1 / SEV-2 / SEV-3
**Started**: 2026-05-18 14:23 UTC
**Detected**: 2026-05-18 14:28 UTC (5m later)
**Mitigated**: 2026-05-18 14:51 UTC
**Resolved**: 2026-05-18 16:10 UTC
**Author**: <author>
**Reviewers**: <peers from affected teams>

## Summary (3 sentences)
What happened, who was affected, what was done. Plain language, no jargon.

## Impact
- Users affected: ~12,400 (15% of EU traffic)
- Requests failed: 184,000 over 28 minutes
- Revenue impact: ~$X estimated (if known)
- Data loss: none / [details]
- SLO budget consumed: 4.2% of monthly availability budget

## Timeline (UTC, append-only during incident)
14:23 — Deploy of api@v2.18.0 begins via Argo CD
14:25 — Canary 10% rollout completes; success rate normal
14:27 — Full rollout progresses to 100%
14:28 — PagerDuty fires "APIErrorBudgetBurnFast" (multi-burn-rate)
14:29 — On-call (Sam) acknowledges, opens #incident-api channel
14:31 — IC declared by Sam; comms lead: Jamie
14:33 — Status page set to "investigating"
14:38 — Rollback initiated to api@v2.17.4
14:42 — Rollback complete; error rate begins falling
14:51 — Error rate back to baseline; incident mitigated
15:10 — Root cause hypothesized (see below)
16:10 — Status page set to "resolved" after monitoring stable for 1h

## Detection
- How was it detected? (alert, customer report, dashboard glance, monitoring)
- Was it detected by the alert that *should* have caught it?
- Time-to-detect (TTD): 5m

## Contributing Factors (NOT "root cause" singular)
Use Five Whys *as a starting point*, but expect multiple converging factors:

1. **Direct trigger**: Schema migration in v2.18.0 added NOT NULL constraint without backfill;
   in-flight requests using cached connection pool hit the new constraint and 500'd.
2. **Why did the migration pass review?** PR description didn't mention the constraint;
   diff was 300 lines, reviewer skimmed.
3. **Why did the canary not catch it?** Canary stage ran for 90 seconds against 10% traffic.
   The bug only fires for requests that landed on stale connections (older than 60s).
   At 10% × 90s, almost no stale connections existed yet.
4. **Why didn't the database integration tests catch it?** Test fixture inserts complete rows;
   migration test only runs against empty tables.
5. **Why didn't anything block the deploy?** No automated check for "DDL changes that could fail
   on in-flight rows" — relies on author/reviewer recognising the pattern.

## What Went Well
- TTD of 5m is well within target (10m). The multi-burn-rate alert fired correctly.
- Rollback via Argo CD took 4m from decision to complete.
- Comms lead posted status page update within 5m of IC declaration.

## What Went Poorly
- Canary stage duration is too short to catch connection-pool-related issues.
- DDL migrations aren't gated separately from code deploys.
- No pre-prod environment with realistic connection-pool warmth.

## Where We Got Lucky
- Incident happened at 14:00 UTC, not 02:00 UTC; on-call response was fast.
- Affected region was EU, lower-volume than US-East at that hour.

## Action Items
Format: title — owner — due — type — ticket

| # | Title | Owner | Due | Type | Status |
|---|---|---|---|---|---|
| 1 | Extend canary stage to 5m minimum | @alice | 2026-05-25 | prevent | INFRA-3041 |
| 2 | Add CI check flagging NOT NULL DDL on non-empty tables | @bob | 2026-06-01 | prevent | DB-2104 |
| 3 | Document "migration safety" runbook | @sam | 2026-05-22 | mitigate | DOC-410 |
| 4 | Update PR template to require migration safety checklist | @bob | 2026-05-23 | prevent | INFRA-3042 |

**Type** is one of: `prevent` (stops recurrence), `detect` (catches it earlier), `mitigate` (reduces impact).
Aim for at least one of each type. A post-mortem with only `prevent` items is fragile;
prevention always eventually fails, so detection and mitigation matter equally.

## Lessons
1. Connection-pool warmth makes canary insufficient for some classes of bug.
2. DDL changes deserve a different review path than code changes.
3. The blast radius of a 90-second canary is ~10% of usual traffic — not safe for high-RPS services.

## References
- Argo CD application: <link>
- Grafana dashboard at time of incident: <link with time range>
- PR that introduced the bug: <link>
- Chat log export: <link>
```

## Rules for the Author

1. **No names in causal language.** Don't write "Alice deployed v2.18.0"; write "v2.18.0 was deployed". Don't write "Bob approved the PR"; write "the PR was approved with skim review". The system permitted the action; that's the post-mortem's interest.
2. **Past tense, declarative.** "The deploy failed because X". Not "the deploy would have succeeded if Y".
3. **Distinguish events from hypotheses.** Anything in the timeline must be sourced (log, dashboard, chat message). Anything inferred goes in *Contributing Factors* or *Lessons*.
4. **Action items must be specific and assigned.** "Improve testing" isn't an action item. "Add a CI check that fails if a migration adds NOT NULL without DEFAULT or backfill — owner @bob, by 2026-06-01" is.
5. **No "operator error" or "human error" as a contributing factor.** If a human action was sufficient to cause the incident, the question is "why was that action available, unguarded, in production?".

## Common Failure Modes of Post-Mortems

- **Post-mortem theater**: written, filed, never read again. Counter with quarterly post-mortem review meetings.
- **Action items languish**: track them in the same backlog as features, with the same prioritization rituals.
- **"Root cause" framed as a single thing**: incidents are almost always multi-cause. Force yourself to enumerate at least 3 contributing factors.
- **Defensive rewrites in review**: peers asking for changes that exonerate themselves. Author has final say; reviewers can add comments.
- **No nearby-miss culture**: organisations only write post-mortems for actual incidents miss the cheapest learnings.

## Related Concepts

- [[implements::SLO Error Budgets and Multi-Window Multi-Burn-Rate Alerts]]
- [[relatedTo::USE, RED, and Four Golden Signals — Observability Method Selection]]
- [[buildsOn::GitOps Progressive Delivery — Argo CD vs Flux, Canary, Blue-Green]]

## References

- Google SRE Book, *Postmortem Culture: Learning from Failure*: https://sre.google/sre-book/postmortem-culture/
- Google SRE Workbook, *Postmortem Culture*: https://sre.google/workbook/postmortem-culture/
- Etsy debriefing guide, *Beyond Blame*: https://extfiles.etsy.com/DebriefingFacilitationGuide.pdf
- John Allspaw, *Blameless PostMortems and a Just Culture*: https://www.etsy.com/codeascraft/blameless-postmortems/
- PagerDuty's incident response documentation: https://response.pagerduty.com/
