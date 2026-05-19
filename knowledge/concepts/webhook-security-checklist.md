---
title: Webhook Security Checklist
type: concept
tags:
  - security
  - webhooks
  - integration
  - automation
  - mid-level-architecture
  - REST-API
  - best-practices
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
status: active
---

# Webhook Security Checklist

Webhooks are inbound HTTP requests from third parties that trigger side effects in your system. The attack surface is unusually wide: anyone on the internet can call your `/webhook/...` URL and pretend to be Stripe, GitHub, or any vendor. This checklist covers the controls every receiver should implement.

## The threat model in one paragraph

An attacker who discovers your webhook URL can: (1) forge events to trigger downstream state changes (fraudulent "payment received" notifications), (2) replay legitimate events to double-process work, (3) flood your endpoint to exhaust resources, (4) inject malicious payloads to exploit downstream parsers. Mitigations are layered; no single control is sufficient.

## 1. HMAC signature verification (mandatory)

Every reputable provider signs the request body with a shared secret. Verify it BEFORE any other processing.

```python
import hmac
import hashlib

def verify_signature(body: bytes, signature_header: str, secret: bytes) -> bool:
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

**Critical rules**:
- Use **constant-time comparison** (`hmac.compare_digest` in Python, `crypto.timingSafeEqual` in Node) — naive `==` leaks the secret via timing attack.
- Verify against the **raw request body**, not the parsed JSON — re-serialization changes whitespace/key order and breaks the signature.
- Some providers include a **timestamp** in the signed string (Stripe: `${timestamp}.${body}`) — include it in your verification.
- Reject requests missing the signature header. Don't fall back to "no signature = ok".

## 2. Timestamp / replay protection

Even valid signed requests can be replayed by an attacker who captured them. Reject any event older than a tolerance window (Stripe recommends 5 minutes).

```python
def check_timestamp(timestamp: int, tolerance_seconds: int = 300) -> None:
    now = int(time.time())
    if abs(now - timestamp) > tolerance_seconds:
        raise WebhookReplayError("timestamp outside tolerance")
```

Combine with **idempotency on the event ID** (see `[[relatedTo::Idempotency Patterns for Automation Workflows]]`): even within the tolerance window, the same event ID must not be processed twice.

## 3. IP allowlisting (where available)

Stripe, GitHub, Twilio, SendGrid, etc. publish their outbound IP ranges. Restrict your webhook endpoint at the L7 firewall / API gateway layer to those CIDRs. This is **defense-in-depth**, not a primary control — IP spoofing is hard in HTTPS but cloud egress IPs can rotate, so don't break your integration over a stale list.

| Provider | Published IP list |
|---|---|
| Stripe | `https://stripe.com/files/ips/ips_webhooks.json` |
| GitHub | `https://api.github.com/meta` (`.hooks`) |
| Twilio | https://www.twilio.com/docs/sip-trunking/ip-addresses |

## 4. mTLS for high-value integrations

For B2B integrations (banking, healthcare, supply chain), upgrade to mutual TLS: the sender presents a client certificate signed by a CA you trust. Combine with HMAC; don't replace it. Most enterprise iPaaS platforms (MuleSoft, Boomi) support outbound mTLS; configure inbound via your API gateway (Kong, Envoy, AWS API Gateway with truststore).

## 5. Payload schema validation

Validate the parsed payload against a strict schema BEFORE touching any business logic. Use Pydantic, Zod, JSON Schema, or your stack's equivalent. Reject unknown fields where the provider's spec allows it (avoid attackers smuggling extra fields).

```python
from pydantic import BaseModel, ConfigDict, Field

class StripePaymentSucceededEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")  # reject unknown keys
    id: str = Field(pattern=r"^evt_[A-Za-z0-9]+$")
    type: Literal["payment_intent.succeeded"]
    data: PaymentData
```

## 6. Size limits and parser hardening

- Reject payloads larger than the provider's documented max (Stripe: 256 KB; GitHub: 25 MB for `push`).
- Use a streaming JSON parser if the provider can send large arrays; avoid loading 25 MB into memory.
- Set strict per-request timeouts on downstream calls. A slow webhook handler is a DoS vector.

## 7. Async processing + DLQ

Acknowledge the webhook quickly (return 200 within the provider's deadline — typically 5-30 seconds), then process asynchronously. If async processing fails after N retries, push to a **dead-letter queue** with the full original payload, signature, and headers for human inspection. Never silently drop.

```
Provider → HTTPS POST → Receiver
                          │
                          ├─ Verify signature (sync, <50ms)
                          ├─ Persist event to "received" table (sync, atomic)
                          ├─ Return 200 (sync, <500ms total)
                          └─ Enqueue background job (async)
                                  │
                                  ├─ Idempotency check (event.id)
                                  ├─ Business logic
                                  └─ On final failure: DLQ + alert
```

Persisting before returning 200 is what makes "at-least-once" actually safe: even if the background worker crashes, you can replay from the persisted record.

## 8. Rate limiting per source

Even with signature verification, throttle by source IP / provider to absorb bursts and prevent resource exhaustion. Most API gateways have per-route rate limiting; otherwise use a token bucket in Redis. Set the limit well above the provider's documented burst rate to avoid dropping legitimate spikes (Stripe can send 1000+ events/sec during a campaign).

## 9. Secret rotation

Webhook secrets are credentials. Treat them like passwords:
- Store in a secret manager (Vault, AWS Secrets Manager, Doppler), not in env files committed to git.
- Plan for rotation: most providers support **two active secrets** simultaneously during a rotation window. Accept signatures matching EITHER, then retire the old one after the cutover.
- Log secret rotations to your audit trail.

## 10. Observability

Each verified webhook should emit structured logs / traces with:
- `event_id`, `event_type`, `provider`, `received_at`, `signature_valid`, `processed_at`, `outcome`
- Correlation ID linking the webhook to downstream actions
- Latency histogram (provider → receiver → ack)
- Alarm on: signature failure rate > 0.1%, processing failure rate > 1%, DLQ depth > N, ack latency p99 > 2s

## Quick reference: per-provider quirks

| Provider | Signature header | Algo | Replay window | Notes |
|---|---|---|---|---|
| Stripe | `Stripe-Signature` | HMAC-SHA256 over `t.payload` | 5 min recommended | Supports key rotation natively |
| GitHub | `X-Hub-Signature-256` | HMAC-SHA256 over raw body | None (use delivery_id for idempotency) | Verify also `X-GitHub-Event` |
| Slack | `X-Slack-Signature` | HMAC-SHA256 over `v0:timestamp:body` | 5 min (RFC: must be in last 5 min) | Includes `v0=` version prefix |
| Twilio | `X-Twilio-Signature` | HMAC-SHA1 of URL+params | None | URL must include the full path with port |
| Shopify | `X-Shopify-Hmac-SHA256` | HMAC-SHA256 (base64) | None | Body is JSON; verify raw |

## Anti-patterns

- Skipping signature check on "internal" webhooks (anything reachable over HTTP is external).
- Verifying the parsed JSON, not the raw bytes.
- Treating absence of signature header as "must be a legit retry without a header".
- Synchronous handlers that do real work before returning 200 — provider times out, retries, you get N copies.
- No DLQ; failures silently disappear in your logs.

## Links

- [[relatedTo::Idempotency Patterns for Automation Workflows]]
- [[relatedTo::OAuth2 Flow Decision Tree]]
- [[uses::HMAC]]
