---
name: webhook-receiver
description: Hardens an inbound webhook endpoint with HMAC signature verification, timestamp replay protection, idempotency, schema validation, async processing, and dead-letter handling. Use when designing a new webhook receiver or auditing an existing one
keywords: [webhook, HMAC, signature verification, replay protection, dead-letter, X-Hub-Signature]
model: opus
effort: high
---

# Webhook Receiver (Opus)

**Purpose**: Produce a production-grade webhook receiver — not a `flask.route("/webhook", methods=["POST"])` that returns 200 and hopes for the best. Covers HMAC verification, replay protection, idempotency, payload validation, async dispatch, dead-letter queue, observability.

**Model**: Opus 4.7

## When to invoke autonomously

Invoke this skill when:
1. **New webhook endpoint** — "Build a receiver for Stripe / GitHub / [provider] webhooks."
2. **Webhook audit** — "Review our existing webhook handler for security and reliability."
3. **Migration** — "Move our Zapier webhook receiver in-house."
4. **Incident postmortem** — "We processed a duplicate webhook in production, what's the fix?"

**Don't invoke for**:
- Outbound webhook publishing (sending webhooks to others — that's a different problem).
- Internal RPC (use signed JWTs or mTLS, not webhook patterns).
- Polling-based integrations (no webhook = no need).

## Usage

```
/webhook-receiver design for [provider] events on [endpoint path]
/webhook-receiver audit [path to existing handler]
/webhook-receiver harden [provider] - convert from sync to async + DLQ
```

## What this skill produces

A concrete receiver implementation (Python FastAPI default; can target Node/Express, Go, etc.) with:

### 1. HMAC signature verification (provider-specific)

Look up the provider's signature format. Common ones:

| Provider | Signature header | Algo | Signed content |
|---|---|---|---|
| Stripe | `Stripe-Signature` (multi-key: `t=…,v1=…`) | HMAC-SHA256 | `{timestamp}.{raw_body}` |
| GitHub | `X-Hub-Signature-256` (prefix `sha256=`) | HMAC-SHA256 | raw body |
| Slack | `X-Slack-Signature` (prefix `v0=`) + `X-Slack-Request-Timestamp` | HMAC-SHA256 | `v0:{timestamp}:{raw_body}` |
| Twilio | `X-Twilio-Signature` (base64) | HMAC-SHA1 | full URL + sorted form params concatenated |
| Shopify | `X-Shopify-Hmac-SHA256` (base64) | HMAC-SHA256 | raw body |

**Implementation requirements** (non-negotiable):
- Constant-time comparison (`hmac.compare_digest` / `crypto.timingSafeEqual`)
- Verify against **raw bytes** of the body, never the parsed JSON
- Support **two active secrets** for rotation windows
- 401 on missing or invalid signature; do NOT process

```python
def verify_stripe(raw_body: bytes, sig_header: str, secrets: list[bytes], tolerance_seconds: int = 300) -> dict:
    parts = dict(item.split("=", 1) for item in sig_header.split(","))
    timestamp = int(parts["t"])
    if abs(time.time() - timestamp) > tolerance_seconds:
        raise WebhookReplayError("timestamp outside tolerance")
    signed = f"{timestamp}.".encode() + raw_body
    candidates = [parts.get(f"v{i}") for i in (1, 0) if parts.get(f"v{i}")]
    for secret in secrets:
        expected = hmac.new(secret, signed, hashlib.sha256).hexdigest()
        if any(hmac.compare_digest(expected, cand) for cand in candidates):
            return {"verified": True, "timestamp": timestamp}
    raise WebhookSignatureError("no signature matches")
```

### 2. Idempotency layer

Every verified event has a unique ID provided by the source (Stripe: `event.id`, GitHub: `X-GitHub-Delivery`, etc.). Persist on first-seen, reject reprocessing.

```python
async def acquire_event(event_id: str, ttl_days: int = 7) -> bool:
    """Atomic 'have I seen this?'. Returns True if this is the first sighting."""
    return await redis.set(f"webhook:{provider}:event:{event_id}", "1", nx=True, ex=ttl_days * 86400)

@app.post("/webhook/{provider}")
async def receive(request: Request):
    raw = await request.body()
    event_meta = verify(raw, request.headers, secrets=current_secrets())
    event = parse_and_validate(raw)  # pydantic-strict
    first_sight = await acquire_event(event.id, ttl_days=7)
    if not first_sight:
        return Response(status_code=200)  # quietly idempotent
    await persist_to_inbox(event, raw, event_meta)  # durable storage
    await enqueue(event.id)  # async processing
    return Response(status_code=200)
```

See `knowledge/patterns/idempotency-patterns.md`.

### 3. Persistence-before-ack

The inbox table is what guarantees at-least-once-with-recovery: even if the worker crashes, the event survives.

```sql
CREATE TABLE webhook_inbox (
    event_id        text PRIMARY KEY,
    provider        text NOT NULL,
    event_type      text NOT NULL,
    raw_payload     jsonb NOT NULL,
    signed_headers  jsonb NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    processed_at    timestamptz,
    status          text NOT NULL DEFAULT 'received',  -- received|processing|completed|failed|dlq
    attempts        int NOT NULL DEFAULT 0,
    last_error      text
);
CREATE INDEX ON webhook_inbox (status, received_at) WHERE status IN ('received', 'failed');
```

**Order matters**: `INSERT INTO webhook_inbox` MUST commit before `return 200`. If you return 200 first and crash, the event is lost.

### 4. Async processing

A worker picks events off the inbox, processes them, marks completed.

```python
async def process_pending():
    while True:
        events = await db.fetch(
            "SELECT * FROM webhook_inbox WHERE status IN ('received', 'failed') "
            "AND attempts < 5 ORDER BY received_at LIMIT 100 FOR UPDATE SKIP LOCKED"
        )
        for event in events:
            try:
                await db.execute("UPDATE webhook_inbox SET status='processing', attempts=attempts+1 WHERE event_id=$1", event.event_id)
                await dispatch(event)
                await db.execute("UPDATE webhook_inbox SET status='completed', processed_at=now() WHERE event_id=$1", event.event_id)
            except RetryableError as e:
                await db.execute("UPDATE webhook_inbox SET status='failed', last_error=$2 WHERE event_id=$1", event.event_id, str(e))
            except PermanentError as e:
                await db.execute("UPDATE webhook_inbox SET status='dlq', last_error=$2 WHERE event_id=$1", event.event_id, str(e))
                await alert_dlq(event)
```

For workflow engines (Temporal, Inngest), trigger the workflow from the worker; the engine owns retry semantics.

### 5. Dead-letter queue

After N attempts, an event lands in DLQ. The DLQ is NOT a place where events go to die — it's a queue for human review with explicit replay tooling.

```
.claude/scripts/webhook-dlq list                       # show pending DLQ events
.claude/scripts/webhook-dlq inspect {event_id}         # show payload + error history
.claude/scripts/webhook-dlq replay {event_id}          # mark as 'received', reset attempts
.claude/scripts/webhook-dlq drop {event_id} "reason"   # archive with rationale
```

Always log the human action with rationale ("dropped — duplicate of {event_id}", "replayed after fixing {bug}").

### 6. Schema validation

Strict Pydantic / Zod model with `additionalProperties: false`. Reject events that don't match the model BEFORE persistence — the rejection itself goes into a separate `webhook_rejected` table for inspection.

```python
class StripeChargeSucceededPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^evt_")
    type: Literal["charge.succeeded"]
    created: int
    livemode: bool
    data: ChargeData
```

### 7. Observability

Structured logs + metrics:

| Metric | Type | Why |
|---|---|---|
| `webhook.received.total{provider, event_type}` | counter | Volume baseline |
| `webhook.signature_failure.total{provider}` | counter | Alert if > 0.1% |
| `webhook.replay_rejected.total{provider}` | counter | Investigation: who's replaying? |
| `webhook.ack_latency{provider}` (histogram) | histogram | p99 < provider's deadline |
| `webhook.processing_latency{provider, event_type}` | histogram | Backlog detector |
| `webhook.dlq_depth{provider}` (gauge) | gauge | Alert if > N |
| `webhook.inbox_pending{provider}` (gauge) | gauge | Worker health |

Alerts:
- Signature failure rate > 0.1% over 5 min → secret rotation issue or attack
- DLQ depth > 10 → investigate
- Inbox pending > 1000 for > 5 min → worker stuck
- No events in 1h (where 1h is normal traffic) → publisher down or our endpoint broken

## Output format

When designing a new receiver, produce:

1. **Endpoint file**: `src/webhooks/{provider}.py` (or equivalent) with the receive function.
2. **Signature verifier**: `src/webhooks/_verify.py` with one function per provider.
3. **Schema models**: `src/webhooks/schemas/{provider}_events.py`.
4. **Worker**: `src/webhooks/worker.py`.
5. **DLQ tooling**: `scripts/webhook-dlq` (CLI for ops).
6. **Migration**: SQL for `webhook_inbox`, `webhook_rejected` tables.
7. **Tests**:
   - Valid signature → 200, persisted, enqueued
   - Invalid signature → 401, not persisted
   - Replay (same event_id twice) → 200 both times, processed once
   - Stale timestamp → 401
   - Malformed payload → persisted in `webhook_rejected`, 400 to sender
   - Worker crash mid-process → recovered on restart
   - 5 attempts fail → lands in DLQ

When **auditing** an existing receiver, produce a structured report:

```markdown
# Webhook Audit: {provider} receiver

## Severity: high|medium|low

## Findings
1. [HIGH] Signature verified against parsed JSON, not raw body — breaks under any whitespace variation.
2. [HIGH] No idempotency — duplicate events double-process.
3. [MEDIUM] No replay window check — captured webhook can be replayed at any time.
4. [LOW] No structured logging.

## Recommended fixes (prioritised)
...
```

## Anti-patterns to call out

- Returning 200 BEFORE persisting the event — recovery impossible.
- Verifying signature on parsed JSON instead of raw body.
- No retry of failed events (silent loss).
- DLQ that nobody monitors (becomes a write-only graveyard).
- One secret in env file, no rotation plan.
- Synchronous handler doing real work — provider times out, retries, you get 3 copies.
- "It's an internal webhook so we skip the signature" — there's no such thing.

## Knowledge graph integration

Before designing:
- `hybrid_search("webhook security")` — `knowledge/concepts/webhook-security-checklist.md` is the source of truth.
- `hybrid_search("idempotency [provider]")` — past patterns.

After completing, document the receiver in `knowledge/projects/webhook-{provider}.md` with the chosen secret-rotation plan, the DLQ runbook, and the alert thresholds.

## Knowledge Systems

> **Full reference**: [`~/.claude/shared/KNOWLEDGE_SYSTEMS.md`](~/.claude/shared/KNOWLEDGE_SYSTEMS.md)

**Decision tree**:
- Known terms → `kg-search` CLI
- Conceptual → `hybrid_search` MCP
- Relationships → `semantic_graph_search` MCP
- Code by purpose → `search_code_graph` MCP
- Literal strings → Grep

## Success metrics

- Signature verification uses raw body and constant-time comparison.
- Every event has unique ID; duplicates return 200 without reprocessing.
- 200 returned within the provider's deadline (typically < 5s p99).
- Inbox + worker pattern means worker crashes don't lose events.
- DLQ has tooling for inspect / replay / drop with audit log.
- Alerts on signature failure rate, DLQ depth, and worker backlog.
