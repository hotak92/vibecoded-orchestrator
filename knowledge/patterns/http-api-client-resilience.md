---
title: HTTP API Client Resilience Patterns
type: pattern
tags:
  - patterns
  - integration
  - REST-API
  - reliability
  - automation
  - mid-level-architecture
  - best-practices
  - python
  - typescript
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# HTTP API Client Resilience Patterns

Wrapping a third-party HTTP API as a typed client is the most repeated task in automation engineering and the most consistently under-engineered. A naive client (httpx + json.loads) works in the demo and corrupts state when the provider has a bad five minutes. This is the structural pattern for clients that survive real conditions.

Composes with `[[relatedTo::OAuth2 Flow Decision Tree]]` (auth), `[[relatedTo::Retry Policy Design for Distributed Operations]]` (retry semantics), `[[relatedTo::Idempotency Patterns for Automation Workflows]]` (dedup).

## The eight required features

1. **Typed config** — credentials/URLs/timeouts at construction, not call sites.
2. **Auth handling** — header injection + token refresh.
3. **Retry policy** — transient-aware, bounded, backoff+jitter.
4. **Rate-limit handling** — reactive on response AND proactive on request.
5. **Idempotency-key threading** — where the API supports it.
6. **Structured errors** — typed exception hierarchy + provider request IDs.
7. **Pagination** — async iterators over cursor/offset/link-header.
8. **Observability hooks** — before/after request, on retry, on failure.

Skipping any of these moves the failure mode from "client handles it" to "your downstream service handles it" — usually badly.

## Typed config at construction

```python
class AcmeConfig(BaseModel):
    api_key: SecretStr                          # never logged in clear
    base_url: HttpUrl = "https://api.acme.com/v1"
    timeout_seconds: float = 30.0
    max_retries: int = 4
    rate_limit_per_minute: int = 100
```

Construction takes credentials; call sites take operation parameters only. Secrets in `SecretStr` (or equivalent) so they don't leak via tracebacks or logs.

## Auth + token refresh

The client owns token lifecycle. Callers never see token expiry:

```python
async def _ensure_token(self):
    if not self._token or self._token.expires_at <= time.time() + 60:
        self._token = await self._refresh_token()
```

Surface refresh failures via observability hooks (alert on them). See `[[relatedTo::OAuth2 Flow Decision Tree]]` for flow choice.

## Retry policy — universal decision table

| Condition | Retry? | Strategy |
|---|---|---|
| Network error (DNS, conn refused, read timeout) | Yes | Exp backoff + jitter, bounded |
| 429 Too Many Requests | Yes | Respect `Retry-After`; cap at budget |
| 502, 503, 504 | Yes | Exp backoff |
| 500 | Maybe | Configurable; default yes, log |
| 400, 404, 409, 422 | No | Surface immediately |
| 401, 403 | No (initially) | Refresh token; one retry after refresh |
| Mid-stream connection reset | Yes | Treat as network error |

Use `tenacity` (Python) with `retry_if_exception_type((NetworkError, TransientApiError))`. Full design in `[[relatedTo::Retry Policy Design for Distributed Operations]]`.

## Rate-limit handling — reactive AND proactive

Reactive: respect `Retry-After`, `X-RateLimit-Reset`, `X-RateLimit-Remaining` headers. This is the minimum.

Proactive: client-side limiter that throttles BEFORE 429s. Prevents the failure instead of recovering from it:

```python
from aiolimiter import AsyncLimiter
# Published 100/min → limit to 95/min for safety margin
self._limiter = AsyncLimiter(max_rate=95, time_period=60)
async with self._limiter:
    return await self._do_request(...)
```

Multi-process workloads: per-process limiter doesn't share budget. Use Redis-backed limiter (token bucket via Redis Lua script) so all instances share the global budget. Safety margin: limit to 90-95% of published cap; the remainder absorbs clock skew, retries, and other consumers of the same key.

## Idempotency-key threading

For POSTs accepting `Idempotency-Key` (Stripe, Adyen, modern APIs), expose the key as a caller-supplied parameter — don't auto-generate:

```python
async def create_order(
    self, payload: CreateOrderRequest, idempotency_key: str | None = None
) -> Order:
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    response = await self._request("POST", "/orders", json=payload.model_dump(), headers=headers)
    return Order.model_validate(response.json())
```

Auto-generation defeats the purpose (each retry gets a fresh key). See `[[relatedTo::Idempotency Patterns for Automation Workflows]]`.

## Structured error hierarchy

Map provider errors to typed exceptions; never let raw HTTP errors escape:

```python
class AcmeError(Exception):
    request_id: str | None   # always captured

class AcmeAuthenticationError(AcmeError): ...    # 401
class AcmeNotFoundError(AcmeError): ...           # 404
class AcmeValidationError(AcmeError): ...         # 400/422 with field_errors
class AcmeRateLimitError(AcmeError): ...          # 429 with retry_after
class AcmeServerError(AcmeError): ...             # 5xx after retries
```

**Always capture the provider's request ID** (`X-Request-Id`, `request-id`, `x-amzn-RequestId` — varies). When something goes wrong, users quote it to provider support. A `request_id` field on the base class makes this consistent.

## Pagination — async iterators

```python
async def list_orders(self, **filters) -> AsyncIterator[Order]:
    cursor = None
    while True:
        page = await self._get("/orders", params={"cursor": cursor, **filters})
        for order in page["data"]:
            yield Order.model_validate(order)
        cursor = page.get("next_cursor")
        if not cursor: return
```

Common styles: **cursor** (`?cursor=...` → `next_cursor`; best for write-heavy), **page-number** (`?page=2`; can skip/repeat on concurrent inserts), **offset** (`?offset=100`; expensive on large tables), **link-header** (GitHub `Link: <...>; rel="next"`), **token** (Google `?page_token=...`).

Expose async iterators; provide a `list_all_X()` helper when callers need everything.

## Observability hooks

Four standard hooks:
- `before_request(method, path, headers, body)` — tracing span start.
- `after_response(status, headers, latency_ms)` — metrics.
- `on_retry(attempt, exception, next_wait)` — retry-rate alerting.
- `on_failure(exception)` — log enrichment.

Default implementation logs structured JSON; production wires to OTel via a `ClientHooks` adapter.

## Codegen vs. hand-written

Prefer established tooling when OpenAPI is available:

| Language | Tool | Notes |
|---|---|---|
| TypeScript | `@hey-api/openapi-ts` | Current best; Zod schemas + fetch/axios |
| Python | `openapi-python-client` | Pydantic v2 + httpx |
| Python | `datamodel-code-generator` | Models only; pair with hand-written httpx |

Pattern: generate models + low-level client, then **wrap with a handcrafted facade** that adds auth, retry, rate-limit, structured errors, observability. Generators handle these poorly; the facade is where production behaviour lives.

## Testing — three layers

- **Contract tests** (mocked transport, fast, in CI) — `respx` / `nock`-style mocks; cover happy path, 429-retries-then-succeeds, validation error, pagination.
- **Sandbox integration tests** — live, opt-in, marked `@pytest.mark.integration`; skip without sandbox creds.
- **Property tests for retry/error logic** — hypothesis over status codes.

Chaos scenarios (random 429s, 5xx, timeouts) catch bugs hidden in retry logic, error mapping, pagination edges.

## Anti-patterns

- API key in URL (leaks via logs, referrers, history). Headers only.
- Sync and async mixed.
- `dict[str, Any]` return types.
- Bare `try/except` masking specific failures.
- Caller-managed retries (duplicates effort; stacks layers).
- Hidden config (env var lookups inside the client).
- No request-id capture (provider-support debugging is painful).
- "We won't hit the rate limit" — you will, at the worst time.
- Skipping pagination because "we only need page 1".

## Links

- [[relatedTo::OAuth2 Flow Decision Tree]]
- [[relatedTo::Retry Policy Design for Distributed Operations]]
- [[relatedTo::Idempotency Patterns for Automation Workflows]]
- [[relatedTo::Webhook Security Checklist]]
- [[relatedTo::Automation Workflow Design Framework]]

## References

- `httpx`: https://www.python-httpx.org/
- `tenacity`: https://tenacity.readthedocs.io/
- `aiolimiter`: https://aiolimiter.readthedocs.io/
- `@hey-api/openapi-ts`: https://heyapi.dev/
- `openapi-python-client`: https://github.com/openapi-generators/openapi-python-client
