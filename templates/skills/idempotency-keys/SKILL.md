---
name: idempotency-keys
description: Designs the idempotency strategy for a state-changing operation - key derivation, storage choice, TTL, collision handling, and threading through downstream side effects. Use when adding a new endpoint with side effects, hardening an existing one, or designing a workflow that survives retries
short_desc: idempotency strategy for state-changing endpoints
keywords: ["idempotency key", "dedup token", "duplicate prevention", "idempotent endpoint", "prevent duplicates", "safe to retry", "idempotency design", "design for retries", "handle duplicate requests"]
model: opus
effort: high
---

# Idempotency Keys (Opus)

**Purpose**: Design the idempotency strategy for any state-changing operation — webhook handlers, payment endpoints, workflow steps, message-queue consumers. Decides: where the key comes from, where it's stored, for how long, what happens on collision, how it threads through downstream side effects.

**Model**: Opus 4.7

## When to invoke autonomously

Invoke when:
1. **Designing a new endpoint with side effects** (charge, send, create, update).
2. **Hardening an existing endpoint** that's been hit by duplicate-processing bugs.
3. **Workflow step design** where the step calls a non-idempotent external API.
4. **Message queue consumer** that needs to survive replays.
5. **User asks "what's our idempotency strategy for X?"** — produce a concrete spec.

**Don't invoke for**:
- Read-only operations (no idempotency needed).
- Workflows where the engine already guarantees exactly-once (Temporal workflows ARE idempotent by replay; activities still need it).

## Usage

```
/idempotency-keys design for [endpoint or operation]
/idempotency-keys audit [path to handler]
/idempotency-keys derive key from [payload shape]
```

## Decision sequence

The skill walks through six questions in order:

### Q1: Who supplies the key?

```
Who initiates this operation?
├─ External client (our SDK / direct caller) → client supplies UUID per logical action
├─ Webhook receiver → use provider's event ID (Stripe event.id, GitHub X-GitHub-Delivery, etc.)
├─ Message queue consumer → use message ID + producer-supplied dedupe ID
├─ Workflow step → use {workflow_id}:{step_name}:{deterministic_param}
└─ Internal cron job → use {job_name}:{scheduled_time} (truncated to slot)
```

**Reject silently**: a key generated server-side per-attempt. That defeats the purpose.

### Q2: What's the canonical request hash?

To detect "same key, different payload" collisions, compute a stable hash of the request:

```python
def canonical_hash(payload: dict) -> str:
    # Sort keys, drop irrelevant volatile fields, hash
    stable = {k: v for k, v in sorted(payload.items()) if k not in {"timestamp", "client_request_id"}}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
```

**Decide which fields are "the same operation"** — usually `customer_id`, `amount`, `action_type`. Timestamps, IPs, request IDs are usually noise.

### Q3: Where do you store keys?

| Store | TTL practical | When |
|---|---|---|
| Redis (`SET NX EX`) | hours-days | Sub-ms lookup, OK with eventual loss, single-region |
| Postgres unique index on idempotency key | days-forever | When you also need audit trail; transaction-safe |
| DynamoDB conditional write | 24-48h | Multi-region, high write rate, native TTL |
| Workflow engine state | workflow lifetime | When the engine owns the operation (Temporal, Inngest) |
| Memcached | minutes | Don't — too short and lossy for most idempotency |

**Default**: Redis with `SET NX EX 86400` (24h) for in-memory, plus a Postgres audit row for billing/payment ops where audit is non-negotiable.

### Q4: What's the TTL?

Rule: TTL ≥ longest expected retry window of the upstream system.

| Source | Retry window | Suggested TTL |
|---|---|---|
| Stripe webhooks | 72h | 7 days |
| GitHub webhooks | 8h | 24h |
| SQS standard | 12h max visibility timeout | 24h |
| Kafka with manual commits | unbounded (consumer can replay) | 30 days |
| Internal HTTP retry policy | 5 min | 1h |
| User-driven UI retry | minutes (page reload) | 24h |

**Don't**: store forever. Your idempotency store will grow without bound and slow down. Audit logs go to Postgres; the idempotency cache itself is ephemeral.

### Q5: What happens on collision?

Three distinct collision types:

| Collision | What it means | Response |
|---|---|---|
| Same key, same payload, completed | Legitimate retry | Return cached response, status 200 |
| Same key, same payload, still in_progress | Concurrent retry | 409 Conflict + `Retry-After`, OR advisory lock + wait briefly |
| Same key, DIFFERENT payload | Bug or attack | 422 Unprocessable Entity, log loudly, DO NOT process the second payload |
| Same key, completed, but TTL expired | Replay outside window | New request — process. Document this in TTL choice. |

```python
def acquire_or_replay(key: str, request_hash: str, ttl: int = 86400) -> tuple[str, dict | None]:
    """Returns ('new', None) | ('replay', cached_response) | raises IdempotencyConflict."""
    existing = store.get(key)
    if existing:
        if existing.request_hash != request_hash:
            raise IdempotencyConflict(key=key, expected_hash=existing.request_hash[:8], got_hash=request_hash[:8])
        if existing.status == "completed":
            return "replay", existing.response
        if existing.status == "in_progress":
            raise IdempotencyInFlight(retry_after_seconds=5)
    claimed = store.set(key, {"status": "in_progress", "request_hash": request_hash, "claimed_at": now()}, nx=True, ex=ttl)
    if not claimed:
        # Race lost — refetch and recurse once
        return acquire_or_replay(key, request_hash, ttl)
    return "new", None
```

### Q6: How do you thread idempotency through downstream side effects?

This is where most designs fail. The entrypoint dedup catches duplicate ENTRY, but the handler probably calls multiple downstream systems. Each needs its own idempotency story.

```python
async def handle_order(order_id: str, idempotency_key: str, payload: OrderPayload):
    # Entrypoint dedup catches duplicate requests
    status, cached = acquire_or_replay(idempotency_key, canonical_hash(payload.dict()))
    if status == "replay":
        return cached

    # Each downstream side effect needs its own key, derived from the parent
    charge_key = f"{idempotency_key}:charge"
    email_key = f"{idempotency_key}:email"
    notion_key = f"{idempotency_key}:notion"

    try:
        # Stripe: pass Idempotency-Key header
        charge = await stripe.charge_create(amount=payload.amount, idempotency_key=charge_key)
        # Internal DB: ON CONFLICT DO NOTHING
        await db.execute(
            "INSERT INTO orders (id, charge_id) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            order_id, charge.id,
        )
        # External API without idempotency: wrap in audit check
        if not await audit_log_exists(notion_key):
            notion_page = await notion.create_page(...)
            await audit_log_record(notion_key, {"page_id": notion_page.id})
        # Email: provider-specific dedup window (most do 24-48h)
        await sendgrid.send(message_id=email_key, ...)

        result = {"order_id": order_id, "charge_id": charge.id}
        store.set(idempotency_key, {"status": "completed", "request_hash": canonical_hash(payload.dict()), "response": result}, ex=86400)
        return result
    except Exception:
        store.delete(idempotency_key)  # release for retry; don't poison
        raise
```

**Critical**: `store.delete(idempotency_key)` on error. If the operation failed before completing, do NOT lock the key for 24 hours.

## Common patterns

### Pattern: Payment endpoints

- Key supplied by client (their UUID per "Submit Payment" button press)
- Stored in Redis with 24h TTL
- Pass `idempotency_key` to Stripe/Adyen/etc. (they keep their own dedup for 24h+)
- Audit log row in Postgres for compliance

### Pattern: Webhook receivers

- Key = provider's event ID (verified after HMAC check)
- Stored in Postgres `webhook_inbox` (unique constraint)
- TTL = provider's retry window × 2
- On collision: return 200 silently (provider's retrying a delivered event)

### Pattern: Queue consumers (SQS/RabbitMQ/Kafka)

- Key = message ID or producer-supplied dedupe ID
- Stored in Redis with TTL = visibility timeout × 5
- On collision in_progress: return to queue with delay (don't ack yet)
- On collision completed: ack (delete from queue)

### Pattern: Workflow steps (Temporal, Inngest)

- Workflow ID is the natural idempotency key for the workflow as a whole.
- Each activity / step needs its OWN key: `{workflow_id}:{step}:{params_hash}`.
- The engine handles retries; your activity just needs to be idempotent against the key.

### Pattern: Cron jobs

- Key = `{job_name}:{scheduled_time_floored}`
- Floor the scheduled time to the slot (e.g. truncate to hour for hourly jobs)
- Catches "two cron daemons started, both fire the same slot"

## Anti-patterns

- **"We never retry"** — your users do, your LB does, the network does.
- **Generating a fresh key per attempt** — defeats deduplication.
- **No collision detection for mismatched payloads** — silent corruption.
- **Checking via `SELECT then INSERT`** — race condition; use unique constraints + `ON CONFLICT`.
- **Storing keys forever** — store grows unboundedly, lookups slow.
- **Not storing the response with the key** — second call must redo work to know what to return.
- **Tying idempotency to user_id alone** — same user can legitimately repeat the action; key must be per-LOGICAL-action.

## Output format

When designing, produce:

```markdown
# Idempotency Design: {operation}

## Operation
{what it does}

## Key derivation
- Source: {client | webhook event_id | message_id | workflow}
- Format: {pattern}
- Example: `{example value}`

## Canonical hash
- Included fields: {list}
- Excluded fields: {list with rationale}

## Storage
- Store: {Redis | Postgres | DynamoDB}
- TTL: {duration} (justification: {upstream retry window})

## Collision handling
| Collision type | Response |
|---|---|
| Same key, same hash, completed | 200 + cached response |
| Same key, same hash, in_progress | 409 + Retry-After: 5s |
| Same key, DIFFERENT hash | 422, log alert |
| Same key, TTL expired | Treat as new |

## Downstream side effects
| Step | Idempotency mechanism |
|---|---|
| {step} | {provider's key | DB ON CONFLICT | audit_log guard} |

## Tests
- Replay same key → returns cached
- Replay with different payload → 422
- Concurrent retries → only one succeeds, other gets 409
- TTL expiry → key released
- Handler crash mid-flight → key released, next retry succeeds
```

## Knowledge graph integration

Source of truth: `knowledge/patterns/idempotency-patterns.md`. This skill produces concrete designs that conform to those patterns.

After designing, write a project node `knowledge/projects/idempotency-{operation}.md` documenting the actual design decisions made.

## Knowledge Systems

> **Full reference**: [`~/.claude/shared/KNOWLEDGE_SYSTEMS.md`](~/.claude/shared/KNOWLEDGE_SYSTEMS.md)

**Decision tree**:
- Known terms → `kg-search` CLI
- Conceptual → `hybrid_search` MCP
- Relationships → `semantic_graph_search` MCP
- Code by purpose → `search_code_graph` MCP
- Literal strings → Grep

## Success metrics

- Key derivation rule is explicit and stable across retries of the same logical operation.
- Storage choice has TTL ≥ longest retry window of any source.
- Collision-handling table covers all three collision types.
- Each downstream side effect has its own idempotency mechanism (key threading).
- Tests cover replay, mismatch, concurrent, expiry, crash-mid-flight.
- No `Exception` path leaves a poisoned key (delete on error).
