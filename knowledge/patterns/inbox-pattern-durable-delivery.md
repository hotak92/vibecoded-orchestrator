---
title: Inbox Pattern for Durable Event Delivery
type: pattern
tags:
  - patterns
  - reliability
  - webhooks
  - integration
  - automation
  - workflow
  - mid-level-architecture
  - postgres
  - queues
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Inbox Pattern for Durable Event Delivery

The inbox pattern turns "at-least-once delivery from an external source" (webhook, message queue, scheduled poll) into "we process each event exactly once and survive crashes". It's the structural backbone of any production webhook receiver or queue consumer. Dual to the **outbox pattern** which solves the symmetric problem on the sending side.

## What it solves

Without an inbox, three failure modes lose events silently:

1. **Receive, ACK 200, crash before processing** → event lost; sender thinks it succeeded.
2. **Receive, partially process, crash mid-flight** → side effects partially applied, no recovery state.
3. **Receive, process, ACK 200, sender retries due to network blip** → duplicate processing.

With an inbox: the persisted record is the source of truth; the ACK is purely a delivery signal; processing is asynchronous and crash-tolerant.

## Three components

```
Sender → POST /webhook → Receiver
                          │
                          ├─ Verify (HMAC, schema)
                          ├─ INSERT INTO inbox (...) ON CONFLICT DO NOTHING  ← atomic
                          ├─ Return 200                                       ← ACK
                          └─ Enqueue event_id for worker                      ← async
                                              │
                                              ▼
                                          Worker pool
                                              ├─ Claim row (FOR UPDATE SKIP LOCKED)
                                              ├─ Process (transactional side effects)
                                              ├─ Mark completed; on failure retry or DLQ
```

**Critical ordering**: persist BEFORE ACK. ACK first and crash = event gone. Persist first and crash = sender retries, `ON CONFLICT DO NOTHING` catches the duplicate.

## Inbox schema (Postgres)

```sql
CREATE TABLE webhook_inbox (
    event_id        text PRIMARY KEY,           -- provider's event ID
    provider        text NOT NULL,
    event_type      text NOT NULL,
    raw_payload     jsonb NOT NULL,
    signed_headers  jsonb NOT NULL,             -- for replay debugging
    received_at     timestamptz NOT NULL DEFAULT now(),
    processed_at    timestamptz,
    status          text NOT NULL DEFAULT 'received',
                    -- received | processing | completed | failed | dlq
    attempts        int NOT NULL DEFAULT 0,
    last_error      text,
    next_retry_at   timestamptz
);
CREATE INDEX webhook_inbox_pending_idx
    ON webhook_inbox (status, next_retry_at)
    WHERE status IN ('received', 'failed');
```

Choices: `event_id` PRIMARY KEY = automatic dedup via `ON CONFLICT`; use provider's ID (Stripe `event.id`, GitHub `X-GitHub-Delivery`), not a generated UUID. `raw_payload` as JSONB for replay-from-original. `signed_headers` captured for debugging. Partial index on pending so scans don't slow as completed rows accumulate.

## Receiver: persist-before-ACK

```python
@app.post("/webhook/{provider}")
async def receive(provider, request):
    raw = await request.body()
    meta = verify_signature(provider, raw, request.headers)
    event = parse_provider_event(provider, raw)
    result = await db.execute(
        """
        INSERT INTO webhook_inbox (event_id, provider, event_type, raw_payload, signed_headers)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (event_id) DO NOTHING
        RETURNING event_id
        """,
        event.id, provider, event.type, raw, dict(request.headers),
    )
    is_new = result.rowcount == 1
    response = Response(status_code=200)
    if is_new:
        await enqueue_for_processing(event.id)   # best-effort
    return response
```

Invariants: INSERT commits before `return Response(...)`. `ON CONFLICT DO NOTHING` makes the receiver idempotent at the inbox layer. The enqueue is best-effort — if the queue is unavailable, the worker's periodic scan picks up `status='received'` rows anyway.

## Worker: claim, process, complete

```python
async def process_pending():
    while True:
        async with db.transaction():
            events = await db.fetch(
                """
                SELECT * FROM webhook_inbox
                WHERE status IN ('received', 'failed')
                  AND attempts < 5
                  AND (next_retry_at IS NULL OR next_retry_at <= now())
                ORDER BY received_at LIMIT 10
                FOR UPDATE SKIP LOCKED
                """
            )
            for e in events:
                await db.execute("UPDATE webhook_inbox SET status='processing', attempts=attempts+1 WHERE event_id=$1", e.event_id)

        # Process OUTSIDE the claiming transaction — don't hold locks during work
        for e in events:
            try:
                await dispatch(e)
                await db.execute("UPDATE ... SET status='completed', processed_at=now() ...")
            except RetryableError as ex:
                next_retry = now() + exp_backoff(e.attempts)
                await db.execute("UPDATE ... SET status='failed', last_error=$2, next_retry_at=$3 ...")
            except PermanentError as ex:
                await db.execute("UPDATE ... SET status='dlq', last_error=$2 ...")
                await alert_dlq(e)

        if not events: await asyncio.sleep(POLL_INTERVAL_SECONDS)
```

Key choices: `FOR UPDATE SKIP LOCKED` for many-workers-no-contention; LIMIT 10 batched; process OUTSIDE claiming transaction (don't hold row locks during real work); attempt cap + `next_retry_at` for bounded exponential retry (see `[[relatedTo::Retry Policy Design for Distributed Operations]]`); explicit `RetryableError` vs `PermanentError` for classification.

## State machine

```
received → processing → completed
                    ↘ failed → (next_retry_at) → received
                    ↘ dlq
```

## Integration with workflow engines

When using Temporal/Inngest/Step Functions: the inbox table still exists as the durability layer. The worker, instead of doing side effects directly, triggers the workflow:

```python
await temporal_client.start_workflow(
    ProcessWebhookEvent.run, event.event_id,
    id=f"webhook-{event.event_id}",   # natural idempotency key
    task_queue="webhooks",
)
```

Inbox + engine combines the inbox's durability guarantee with the engine's exactly-once semantics. The inbox is the audit-of-record; the engine is the orchestrator.

## Observability + DLQ

Per-event lifecycle: `event_id`, `provider`, `event_type`, `received_at`, `claimed_at`, `completed_at` (or `dlq_at`), `attempts`, per-attempt error class + duration.

Key metrics + alerts: `inbox.pending{provider}` gauge (alert >1000 for >5min = worker stuck); `inbox.dlq_depth{provider}` gauge; `inbox.processing_latency` p99; `inbox.signature_failures` counter (alert >0.1%).

DLQ needs operational tooling (list/inspect/replay/drop) — see `[[relatedTo::Dead-Letter Queue Operations]]`.

## Comparison with alternatives

| Approach | Crash safety | Dedup |
|---|---|---|
| **Inbox + worker** | Full | Built-in via PK |
| Synchronous handler | None | Manual |
| Pure queue (SQS/Rabbit) | Partial | Limited dedup window |
| Workflow engine direct | Full | Engine ID |

Inbox is the simplest pattern with full crash safety. For complex post-receive logic, layer a workflow engine on top.

## Anti-patterns

- ACK before persistence — silently loses events on crash.
- No `ON CONFLICT` on event_id — receiver isn't idempotent.
- Holding claiming transaction during processing — blocks other workers.
- No `next_retry_at` — failed events retry immediately, hammer downstream.
- Unbounded retries (no `attempts < N`) — events loop forever; never reach DLQ.
- DLQ without ops tooling (the graveyard pattern).
- No partial index on pending — scans slow as completed rows accumulate.

## Links

- [[relatedTo::Webhook Security Checklist]]
- [[relatedTo::Idempotency Patterns for Automation Workflows]]
- [[relatedTo::Retry Policy Design for Distributed Operations]]
- [[relatedTo::Dead-Letter Queue Operations]]
- [[relatedTo::Saga Pattern for Distributed Workflows]]
- [[relatedTo::Workflow Engine Tradeoffs 2026]]

## References

- Microservices Patterns (Chris Richardson) — Inbox / Outbox
- Postgres `FOR UPDATE SKIP LOCKED`: https://www.postgresql.org/docs/current/sql-select.html
- Stripe webhook best practices: https://stripe.com/docs/webhooks/best-practices
