# API design field guide

Specialisation depth for designing API surfaces: protocol selection,
endpoint patterns, authentication, versioning, performance. Read this before
designing or reviewing an API contract.

## Protocol selection

| Protocol | Strengths | Choose when |
|---|---|---|
| REST | Simple, cacheable, browser-friendly | Resource-oriented CRUD, public APIs |
| GraphQL | Flexible queries, single request, type safety | Clients need varying shapes; over/under-fetching hurts |
| gRPC | Fast binary RPC, streaming | Internal microservice-to-microservice |
| WebSocket | Persistent, bidirectional | Real-time feeds, chat, live updates |

Criteria: simplicity, performance, flexibility, browser support, real-time
needs, caching. GraphQL trades caching complexity and query-cost attack
surface for fetch flexibility.

## REST design basics

- **Resources**: plural nouns, hierarchical (`/orders/{id}/items`).
- **Methods**: GET (safe/idempotent), POST (create), PUT (full replace),
  PATCH (partial), DELETE (idempotent).
- **Status codes**: 200/201/204 success; 400 validation; 401 unauthenticated;
  403 forbidden; 404 missing; 409 conflict; 422 semantic error; 429 rate
  limited; 500 server.
- **Query parameters**: pagination, filtering, sorting, search — consistent
  names across endpoints.
- **Response envelope**: consistent `data` / `meta` / `errors` structure;
  errors follow RFC 7807 (problem+json) or an equally documented shape.

## Authentication strategies

| Approach | Fit |
|---|---|
| JWT (access + refresh) | Stateless, scalable, microservices |
| OAuth 2.0 | Third-party/delegated authorization |
| API keys | Simple service-to-service, easy rate limiting |
| Session cookies | Traditional server-rendered web apps |

## Versioning

- URL versioning (`/v1/users`) — most common, most visible.
- Header versioning (`Accept: application/vnd.myapi.v1+json`).
- Query parameter (`?version=1`) — least preferred.

Best practices: semantic versioning; support N-1; publish a deprecation
timeline before removing a version.

## Performance

- Pagination: offset (simple) vs cursor (large datasets, stable under writes).
- Field selection (`?fields=id,name`) to cut payload.
- Batch endpoints for multi-resource operations.
- HTTP caching: ETag, Cache-Control.
- Compression: gzip/Brotli.
- Rate limiting with `Retry-After` headers.
- Observability: clients and endpoints propagate a request/correlation id and
  log per-call latency + outcome, so a failing integration is traceable
  end-to-end without tcpdump.

## Design review checklist

- Every endpoint names its auth requirement, validation rules, error
  responses, and idempotency semantics.
- Contracts are documented (OpenAPI spec or equivalent) and the spec is
  updated in the same change as the code.
- Request-id propagation and latency/error logging are part of the contract,
  not an afterthought.
- Breaking changes are versioned, not silently deployed.

## Related

- Implementation standards: `fields/backend.md`
- Review lens: `review-topics/api-design.md`
