---
title: Idempotency Patterns for Automation Workflows
type: pattern
tags:
  - patterns
  - integration
  - automation
  - workflow
  - reliability
  - mid-level-architecture
  - REST-API
  - webhooks
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
status: active
---

# Idempotency Patterns for Automation Workflows

Idempotent operations produce the same result whether invoked once or many times with the same input. In automation workflows that span multiple systems with at-least-once delivery (webhooks, queues, retries), idempotency is what separates "works in dev" from "doesn't double-charge customers in production".

## Where idempotency is non-negotiable

- **Webhook receivers** — providers retry on any non-2xx, sometimes on timeouts; same payload may arrive 2-5 times.
- **Message queue consumers** — RabbitMQ, SQS, Kafka, Pub/Sub all default to at-least-once delivery.
- **Workflow engine retries** — Temporal, Airflow, n8n, Inngest all retry failed activities/nodes.
- **API clients on transient failures** — connection resets, 502/503/504, timeouts.
- **User-triggered actions with side effects** — "Submit payment" clicked twice; doubled tab; back-button.

## The idempotency-key pattern

A unique key per logical operation, supplied by the caller (or derived from the payload), stored server-side with the result.

```python
# Client supplies a key (Stripe-style)
POST /api/v1/charges
Idempotency-Key: chg_2026-05-19_user-1234_order-789
Body: {"amount": 1000, "currency": "USD", ...}

# Server logic:
# 1. Lookup key in store
# 2. If present + same payload → return cached response
# 3. If present + different payload → 409 Conflict (key reuse with mismatched body)
# 4. If absent → process, store {key, request_hash, response, status}, return
```

**Key generation strategies** (pick one and document it):

| Strategy | When | Tradeoff |
|---|---|---|
| Client-supplied UUID per logical action | User-initiated requests, retries by same client | Client must persist + reuse on retry |
| Hash of (user_id, action_type, business_id) | Server-derived from payload | Collision risk if business_id is reused |
| Hash of full canonical payload | Best-effort dedup without client cooperation | Different bodies → different keys, even for "same" semantic op |
| Event ID from upstream (e.g. Stripe event.id, GitHub X-GitHub-Delivery) | Webhook receivers | Trust the source; verify HMAC first |

## Storage choices

| Store | TTL | Fit |
|---|---|---|
| Redis (SET NX EX) | Hours-days | Default for sub-millisecond lookup, eventual loss acceptable |
| Postgres unique index | Forever | When you also need audit trail; transaction-safe |
| DynamoDB conditional write | 24-48h with TTL | Multi-region, high-write workloads |
| Workflow engine state (Temporal, Inngest) | Workflow lifetime | When the engine owns the operation already |

**TTL guidance**: keep keys at least as long as the longest expected retry window of the upstream system. Stripe retries webhooks for 72 hours; GitHub for 8 hours; SQS visibility timeout is configurable but max 12 hours; Kafka consumer can replay arbitrarily far. **24 hours is a safe default**; 7 days if you have headroom.

## Collision and reuse handling

Three failure modes to design for:

1. **Same key, same payload, different time** → return cached response (success path).
2. **Same key, different payload** → reject with 409 Conflict. The client is reusing a key incorrectly; do NOT silently process the second payload.
3. **Same key, still processing** → return 409 with `Retry-After`, or block briefly (advisory lock). The caller may have retried before the first call completed.

```python
def handle_with_idempotency(key: str, payload: dict, handler):
    canonical = canonical_hash(payload)
    record = store.get(key)
    if record:
        if record.status == "completed":
            if record.request_hash != canonical:
                raise IdempotencyConflict("key reused with different payload")
            return record.response
        if record.status == "in_progress":
            raise IdempotencyInFlight(retry_after_seconds=5)
    # Atomic claim
    if not store.set_nx(key, {"status": "in_progress", "request_hash": canonical}, ttl=86400):
        raise IdempotencyInFlight(retry_after_seconds=5)
    try:
        response = handler(payload)
        store.set(key, {"status": "completed", "request_hash": canonical, "response": response}, ttl=86400)
        return response
    except Exception:
        store.delete(key)  # Allow retry
        raise
```

The `delete-on-error` step is important: a transient failure should not poison the key for 24 hours. Only persist the "completed" record on success.

## Idempotency inside the handler (the harder problem)

The key-store pattern protects you at the entrypoint, but your handler likely calls multiple downstream systems. Each one needs its own idempotency story:

- **Payment provider** — Stripe/Adyen have their own `Idempotency-Key` header; thread your key through.
- **Email/SMS sender** — most providers accept a `message_id`; pass `idem_key + ":email"`.
- **Database writes** — use `INSERT ... ON CONFLICT DO NOTHING` or `MERGE`, never naked `INSERT` on retry-prone paths.
- **External system without idempotency support** — wrap each side effect in `WHERE NOT EXISTS (SELECT 1 FROM audit_log WHERE op_id = ?)`.

A retry that successfully charged the customer but failed before sending the receipt email must not double-charge on the next retry — but it CAN re-send the email if the email sender's own dedup window has elapsed. Map your handler's steps to a transaction or a saga (see `[[relatedTo::Saga Pattern]]`).

## Anti-patterns

- **"We never retry, so we don't need idempotency"** — your users will (double-clicks), your load balancer will (connection drops), the network will (TCP RST).
- **Idempotency by checking "does this row already exist?"** — race condition between SELECT and INSERT. Use unique constraints + ON CONFLICT instead.
- **Auto-generated key per-attempt** — defeats the purpose. The key must be stable across retries of the SAME logical operation.
- **No TTL** — your idempotency store grows forever and slows down.
- **Storing only the key, not the response** — second call has to redo the work to know what to return.

## Links

- [[relatedTo::Webhook Security Checklist]]
- [[relatedTo::Saga Pattern]]
- [[relatedTo::Workflow Engine Tradeoffs 2026]]
- [[buildsOn::Exactly-Once vs At-Least-Once Delivery]]

## References

- [Stripe's idempotency design](https://stripe.com/docs/api/idempotent_requests)
- [AWS Lambda idempotency utility](https://docs.powertools.aws.dev/lambda/python/latest/utilities/idempotency/)
- [Temporal Workflow Determinism](https://docs.temporal.io/workflows#deterministic-constraints) — workflows themselves are idempotent by virtue of replay
