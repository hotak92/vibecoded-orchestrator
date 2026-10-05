# API design review topic

Lens for reviewing an API surface change (REST/GraphQL/gRPC/webhook) as a
contract, on top of the ordinary code review. Findings carry severity,
`file:line`, and evidence. Design depth: `fields/api-design.md`.

## Contract completeness

- Every endpoint names: auth requirement, request validation rules, success
  shape, every error shape + status code, idempotency semantics, timeout.
- The machine-readable spec (OpenAPI class) is updated in the SAME change as
  the implementation — spec drift is a finding.
- Response envelope consistent with sibling endpoints (data/meta/errors);
  errors follow one documented shape across the API.

## Compatibility

- Breaking changes (removed/renamed fields, tightened validation, changed
  status codes, changed semantics of an existing field) are versioned or
  explicitly waved through by the owner — never silently deployed.
- Additive changes: new optional fields, new endpoints — old clients keep
  working; check that serialization of the new field cannot break strict
  client parsers.
- Deprecations carry a published timeline; a deprecated surface still works
  until the stated date.

## Semantics

- HTTP method semantics respected (GET safe/idempotent; PUT full replace;
  PATCH partial; DELETE idempotent).
- Status codes honest: 401 vs 403 distinguished; 409 for conflict; 422 vs
  400 consistent; 429 with Retry-After; 5xx only for server faults.
- Pagination on every collection endpoint that can grow; cursor preferred
  over offset for large or write-heavy data.
- Rate limits declared and enforced consistently; limit headers present.

## Security surface (cross-check review-kinds/security.md)

- AuthZ enforced per resource owner (IDOR), not only per route.
- Input validation server-side; client-side validation is UX only.
- No internal fields (hashes, internal IDs, stack traces) in responses.
- Webhooks: signature verification + replay resistance required.

## Consumer experience

- Error messages actionable (what failed, what to change) without leaking
  internals.
- Examples in the spec for request and response of every endpoint.
- Field naming consistent (case convention, pluralization, date formats —
  ISO 8601).
