---
title: Dead-Letter Queue Operations
type: concept
tags:
  - reliability
  - integration
  - automation
  - workflow
  - operations
  - mid-level-architecture
  - best-practices
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Dead-Letter Queue Operations

A dead-letter queue (DLQ) is where events go when in-flight retries are exhausted. In the worst common implementation, it's a write-only graveyard nobody monitors — events pile up, customers complain, the team discovers months later that 3% of webhooks have been silently dropped. In the right implementation, it's a queue for **human review with explicit tooling** to inspect, replay, or drop with audit trail.

This node is the operational side: what tooling DLQs need, who watches them, how replay works. For the durability layer that feeds the DLQ, see `[[relatedTo::Inbox Pattern for Durable Event Delivery]]`. For the retry policy that decides what becomes DLQ-bound, see `[[relatedTo::Retry Policy Design for Distributed Operations]]`.

## What a DLQ IS

- A bounded queue of events that **failed terminally** despite a bounded retry policy.
- A signal that human attention is needed — fix a bug, contact upstream, make a business decision.
- An audit trail of what failed with enough state to replay after a fix.

## What a DLQ is NOT

- A dump for events you don't want to process (use a filter).
- A retry queue (retries should happen before DLQ).
- A bug tracker (DLQ events are concrete failures; bugs are diagnoses).
- A FinOps tool.

## Required operational tooling

Any DLQ needs four ops commands. Skip them and the DLQ becomes the graveyard pattern.

```
dlq list [--provider X] [--since T]            # show pending DLQ events
dlq inspect {event_id}                         # full payload + error history
dlq replay {event_id}                          # mark for re-processing
dlq drop {event_id} "reason: ..."              # archive with rationale
```

Can be CLI, web admin page, or Slack bot — the surface matters less than the existence. **Every DLQ event should be touched by a human within hours of arrival**, not days.

### `list`
Returns event_id, provider, event_type, received_at, attempts, last_error (truncated), age. Filters by provider, type, age. Sort by age desc — oldest first; they're most urgent.

### `inspect`
Returns full payload, signed headers, complete error history per attempt, downstream state at last attempt, links to related logs/traces. Goal: operator sees everything needed to decide without grepping.

### `replay`
Resets `status='received'`, `attempts=0`, `last_error=null`, `next_retry_at=null`. Logs the human action: who, when, why ("replayed after fixing bug X in commit abc123"). Worker picks it up on next scan.

**Replay safety**: assumes the underlying handler is idempotent (see `[[relatedTo::Idempotency Patterns for Automation Workflows]]`). If your handler is non-idempotent and you replay an event whose side effects partially succeeded, you'll double-spend. Fix is in the handler, not the DLQ tooling.

### `drop`
Sets `status='dropped'`, records `dropped_at`, `dropped_by`, `drop_reason`. **Mandatory rationale** (tool rejects empty strings). Auditable forever. Common drop reasons: "duplicate of {other_event_id}", "stale: customer cancelled order yesterday", "upstream sent malformed payload; logged in their tracker", "irrelevant: pre-migration data".

## Replay vs. drop decision

```
Is the failure caused by a bug we've fixed?
├─ Yes → replay
└─ No
   ├─ Is the upstream sender at fault (malformed)?
   │   ├─ Yes → drop with "upstream bug, see {ticket}"; notify upstream
   │   └─ No
   ├─ Is the event still relevant (business state hasn't moved on)?
   │   ├─ Yes → escalate (bug not fixed yet)
   │   └─ No → drop with "stale, no longer relevant"
   └─ Missing context to decide?
       └─ Escalate to domain expert; never drop in ambiguity
```

Conservative pattern: when in doubt, escalate. Dropped events are gone; escalated events stay in DLQ until decided.

## Audit logging — non-negotiable

Every replay and drop logged with: event_id, provider, action, operator (user ID not service account), timestamp, rationale (required, min length), linked context (PR, ticket).

Purposes: postmortems (months later, audit trail shows what was processed manually); compliance (financial/healthcare/regulated require provable handling); pattern discovery (`dlq audit history --by-rationale` surfaces categories that justify upstream fixes).

## Alert thresholds

| Alert | Threshold | Action |
|---|---|---|
| `dlq_depth > N` | Workflow-specific (10 low-volume, 100 high-volume) | Page; investigate <1h |
| `dlq_growth_rate > X/hour` | Faster-than-baseline ingestion | Page; likely deploy issue or upstream outage |
| `dlq_oldest_age > 24h` | Events sitting unhandled | Triage reminder |

Don't alert on every DLQ insertion — at scale, occasional permanent failures are normal. Alert on accumulation.

## Dashboard requirements

- Current depth by provider + event type.
- Depth trend (last 7 days).
- Top error classes (which failure modes dominate).
- Oldest events (triage focus list).
- Resolution rate (replays + drops per day vs. new arrivals).

If arrivals consistently outpace resolutions, the team is falling behind; alert separately and escalate.

## Retention

After triage:
- **Replayed** → moved to inbox `received` status; deleted from DLQ view (audit log retains).
- **Dropped** → archived to `*_dropped` table for forensic retention (90-365 days per compliance).
- **Untriaged after N days** → escalation alert. Eventually auto-archive `status='abandoned'` after configurable window (e.g. 30 days), preserving the row for audit.

Don't auto-purge silently. Compliance and postmortems need the trail.

## Per-source DLQ shapes

- **Webhook receivers** — per-provider DLQ. Stripe DLQ = revenue impact; GitHub DLQ = developer workflow impact. Different ops contexts.
- **Queue consumers** — typically one DLQ per consumer group / topic. SQS has native `RedrivePolicy`; Kafka has dead-letter topics.
- **Workflow engine activities** — Temporal/Inngest track failures per-activity in workflow state; the "DLQ" is "workflows in `Failed` state".
- **Cron / scheduled jobs** — usually logged failures with no formal DLQ; add one when the job has retry semantics.

## Reconciling with upstream

When DLQ events surface upstream bugs (malformed payloads, missing fields), close the loop: file a ticket with the upstream including request IDs and example payloads → track on your side → on upstream fix, replay affected events → document the resolution in your runbook. Converts DLQ noise into upstream quality improvements.

## Anti-patterns

- "DLQ ops are someone else's job" — until nobody owns it and the queue overflows.
- No mandatory rationale on drop — silent dropping is the worst case.
- Replay without verifying the bug fix shipped — same failure, same DLQ.
- DLQ with no max depth (pathological cases blow your DB).
- Replay handler that isn't idempotent — fix the design.
- Treating DLQ depth as a SLO. Depth is a signal; the right metric is **time-to-triage**.

## Links

- [[relatedTo::Inbox Pattern for Durable Event Delivery]]
- [[relatedTo::Retry Policy Design for Distributed Operations]]
- [[relatedTo::Idempotency Patterns for Automation Workflows]]
- [[relatedTo::Webhook Security Checklist]]
- [[relatedTo::Automation Workflow Design Framework]]
- [[relatedTo::Workflow Engine Tradeoffs 2026]]

## References

- AWS SQS DLQ: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html
- Stripe webhook best practices: https://stripe.com/docs/webhooks/best-practices
