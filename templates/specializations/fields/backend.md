# Backend field guide

Specialisation depth for server-side implementation work. Read this before
starting backend-heavy tasks (API endpoints, database operations, auth,
background jobs, external integrations). Referenced by the `expert-coder`
agent; usable by any agent or directly by Claude.

## Scope

- **API endpoints** — REST/GraphQL/gRPC: request validation, response
  formatting, correct status codes, error handling.
- **Database operations** — CRUD, complex queries, transactions, migrations.
- **Authentication & authorization** — JWT, OAuth, API keys, role-based
  access control, permission checking.
- **Background jobs** — async task queues, scheduled jobs, job monitoring.
- **External integrations** — third-party APIs, webhooks, message queues.

## Production-readiness standards

Code must work for real production workloads, not just tests:

- Never use placeholders ("... rest of endpoints", "// handle other cases").
- Implement general solutions that handle ALL edge cases (malformed input,
  network failures, concurrent requests).
- Do not hard-code values to pass tests — write logic that handles real data.
- Priority: production reliability per spec > test passing > speed.

Backend-specific completeness:

- **Database queries**: handle connection failures, timeouts, transaction
  rollbacks; use connection pooling with retry/backoff.
- **API endpoints**: validate ALL inputs; return specific status codes
  (400 validation, 401 auth, 403 permissions, 409 conflict, 429 rate limit,
  500 server) — not just 200/500; log full context on errors.
- **Authentication**: handle expired tokens, malformed credentials, missing
  claims, wrong signatures; rate-limit auth endpoints; never log secrets or
  password hashes; never leak stack traces in responses.
- **Background jobs**: retry logic with backoff, dead-letter handling, job
  failure alerting, idempotent processing.
- **External APIs**: timeout handling, fallback behavior, circuit breakers,
  retries with exponential backoff then a typed upstream error.
- **Webhooks**: validate signatures, resist replay attacks, process
  asynchronously, retry failed deliveries.
- **Concurrency**: row-level locks or optimistic locking (version column)
  for contended writes; atomic operations for counters; idempotency keys for
  double-submit prevention.

## Good simplification vs lazy shortcuts

- Good: remove unnecessary complexity, prefer the standard library,
  consolidate duplicate logic.
- Lazy (forbidden): skipping error handling to "simplify", removing
  validation to make tests pass, workarounds instead of proper solutions,
  `// TODO: handle properly` comments standing in for implementation.

## When requirements are unclear, ask

- Expected scale (10 req/s vs 1000 req/s)?
- Failure modes (database down → 503, cached data, or queue)?
- Idempotency (can users retry safely)?
- Data retention (soft vs hard delete)?
- Partial failures (commit all, rollback all, or partial)?

Document every assumption explicitly in the code or the report.

## Completion report shape

Files touched; endpoints working; tests passing + coverage; error handling
coverage; security checks done (auth, input validation); docs/OpenAPI
updated; next steps if any.

## Related specialisations

- API surface design decisions: `fields/api-design.md`
- Schema design, indexing, query performance: `fields/database.md`
- Deployment, CI/CD, secrets, monitoring: `fields/deployment.md`
- Reviewing backend work: `review-kinds/code.md`,
  `review-topics/database-migrations.md`, `review-topics/api-design.md`
