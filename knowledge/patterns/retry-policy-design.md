---
title: Retry Policy Design for Distributed Operations
type: pattern
tags:
  - patterns
  - reliability
  - integration
  - automation
  - workflow
  - REST-API
  - mid-level-architecture
  - best-practices
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Retry Policy Design for Distributed Operations

Retries are the cheapest reliability mechanism in distributed systems and the most commonly misconfigured. A naive `try/catch → retry` loop creates more outages than it solves: retries amplify load on a struggling downstream, retries on non-idempotent operations double-spend, retries without backoff thundering-herd a service that just recovered.

## The four questions every policy must answer

1. **WHAT to retry** — transient vs. permanent classification.
2. **HOW MANY times** — bounded attempts AND wall-clock budget.
3. **WHEN to retry** — backoff schedule with jitter.
4. **AT WHAT LAYER** — caller vs. transport vs. workflow engine.

Each is independent; getting one wrong wastes the others.

## Transient vs. permanent error taxonomy

| Class | Examples | Retry? |
|---|---|---|
| Network transient | DNS, TCP reset, read timeout, connection refused | Yes |
| Server overload | HTTP 429, 503 with `Retry-After` | Yes (respect header) |
| Server transient | HTTP 502, 504 | Yes |
| Server internal | HTTP 500 | Maybe — configurable, log loudly |
| Client error | 400, 404, 409, 422 | No — wastes quota |
| Auth | 401, 403 | No — refresh token first |
| Validation (Pydantic/Zod) | Schema rejection | No — payload is bad |
| Deadline exceeded | Wall-clock budget hit | No — give up |

Rule: if retrying the SAME request might succeed because the world changed (network healed, server recovered), it's transient. If the request itself is wrong, it's permanent.

## Exponential backoff with jitter (mandatory)

Naive `sleep(2^attempt)` synchronises every retrying client → thundering herd on the just-recovered service. Use jitter:

```python
def backoff(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Full-jitter exponential (AWS-recommended)."""
    return random.uniform(0, min(cap, base * (2 ** attempt)))
```

Variants (use one consistently): **full jitter** (`random.uniform(0, exp)`), **equal jitter** (`exp/2 + random(0, exp/2)`), **decorrelated jitter** (`min(cap, random(base, prev*3))`). AWS measured full jitter completing work in ~1/3 the wall-clock of no-jitter at high contention.

## Two bounds: attempts AND wall-clock

```python
@retry(
    stop=(stop_after_attempt(5) | stop_after_delay(timedelta(seconds=120))),
    wait=wait_random_exponential(min=1, max=30),
    retry=retry_if_exception_type(TransientError),
    reraise=True,
)
```

- Attempts cap (4-5): a service down 60s isn't coming back at 90.
- Wall-clock cap: should be the CALLER's SLO, not the downstream's recovery time. Server-respected `Retry-After: 60` can silently blow your SLO.

## Respect `Retry-After`

Cap it at your wall-clock budget. A server saying "come back in 1 hour" doesn't override your 30s deadline; for you, that's a permanent failure.

## Idempotency is a precondition

Retrying non-idempotent operations is double-spending in slow motion. Either the operation accepts an `Idempotency-Key` header, OR it's naturally idempotent (`PUT /user/{id}`, `INSERT ON CONFLICT DO NOTHING`). See `[[relatedTo::Idempotency Patterns for Automation Workflows]]`.

## Pick ONE retry layer

Stacked layers compound (retry × retry × retry):

| Layer | When | Disable elsewhere? |
|---|---|---|
| Transport (urllib3 `Retry`, httpx-retry) | Connection-level only (DNS, TCP reset) | Disable retry on 4xx/5xx here |
| Caller (tenacity, app-level) | Classified transient errors | Primary layer |
| Workflow engine (Temporal `RetryPolicy`, Inngest, n8n) | When step is an engine activity | Engine owns retry; HTTP client retries transport only |
| DLQ + re-queue | After in-flight retries exhausted | Durable retry over hours/days |

Anti-pattern: tenacity wraps httpx (which retries 5xx) inside a Temporal activity (4 retries) → 4×3×3 = 36 attempts per logical operation. Pick the layer that owns the retry; disable retry elsewhere.

## Non-retryable error annotations

```python
# Temporal
RetryPolicy(maximum_attempts=4, non_retryable_error_types=[
    "ValidationError", "AuthenticationError", "InsufficientFundsError",
])

# Tenacity — anything NOT in this list is not retried
@retry(retry=retry_if_exception_type((NetworkError, TransientApiError)))
```

Raise specific exception types from steps so the engine/decorator can classify automatically.

## Retry budgets at scale

For workflows with many calls, per-operation bounds aren't enough:
- **Per-workflow budget** — cap total retries across all steps; runaway DLQ when exceeded.
- **Per-target budget** — circuit-breaker pattern; if a downstream is failing globally, stop hammering. See `[[relatedTo::Circuit Breaker Pattern]]`.

## Observability

Per attempt emit: `attempt`, `next_wait_seconds`, `error_class`, `status_code`, `cumulative_elapsed`, `outcome` on final (success | exhausted_attempts | exhausted_wall_clock | non_retryable). Alert on retry rate > 10% sustained — usually a downstream issue.

## Default policies

| Use case | Attempts | Wait | Notes |
|---|---|---|---|
| Synchronous user-facing | 2-3 | 100-500ms | Tight; user is waiting |
| Async background job | 4-5 | exp 1-30s | Standard worker |
| Workflow activity | 4-5 | exp 1-60s, cap 5min | Engine handles persistence |
| Webhook dispatch | 3 + DLQ + replay | exp 1-30s | Don't loop after exhaustion |
| Critical (payments) | 3 + alert | exp 1-10s | Fast fail to human |

## Anti-patterns

- Retrying on every exception (masks bugs like KeyError).
- No jitter (synchronised thundering herd).
- No wall-clock cap (retry storm during outage).
- Stacked retry layers (compound multiplier).
- Retry on non-idempotent operations without an idempotency key.
- Hard-coded `time.sleep(N)` (no growth, no jitter).
- Logging only failures, not retries (invisible retry storms inflate cost).

## Links

- [[relatedTo::Idempotency Patterns for Automation Workflows]]
- [[relatedTo::Circuit Breaker Pattern]]
- [[relatedTo::HTTP API Client Resilience Patterns]]
- [[relatedTo::Dead-Letter Queue Operations]]
- [[relatedTo::Workflow Engine Tradeoffs 2026]]

## References

- AWS: [Exponential Backoff and Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)
- Tenacity: https://tenacity.readthedocs.io/
- Temporal Retry Policies: https://docs.temporal.io/encyclopedia/retry-policies
