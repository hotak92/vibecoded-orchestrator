---
name: api-designer
description: Expert guidance on API design including REST vs GraphQL vs gRPC selection, endpoint patterns, authentication strategies, and versioning
short_desc: REST vs GraphQL vs gRPC, endpoints, versioning
keywords: [REST vs GraphQL, gRPC, endpoint design, API versioning, API authentication, "REST API", OpenAPI, "design an API", "API design", "REST API design", "endpoint structure", "version my API", "API patterns"]
model: sonnet
---

# API Designer

Expert guidance on API design: protocol selection (REST vs GraphQL vs gRPC vs WebSocket), endpoint patterns, authentication strategies, versioning, and performance.

## Capabilities

### 1. Protocol Selection

Compares protocols:
- **REST**: Resource-oriented HTTP (simple, cacheable, browser-friendly)
- **GraphQL**: Query language (flexible queries, single request, type safety)
- **gRPC**: Binary RPC (fast, streaming, microservices)
- **WebSocket**: Persistent connection (real-time, bidirectional)

Provides comparison table with criteria: Simplicity, Performance, Flexibility, Browser Support, Real-time, Caching.

### 2. REST API Design Best Practices

Guidance on:
- **Resource Naming**: Plural nouns, hierarchical structure
- **HTTP Methods**: GET, POST, PUT, PATCH, DELETE (proper semantics)
- **Status Codes**: 200, 201, 204, 400, 401, 403, 404, 409, 500
- **Query Parameters**: Pagination, filtering, sorting, searching
- **Response Format**: Consistent structure (data, meta, errors)
- **Error Format**: RFC 7807 compliance

### 3. GraphQL API Design

Covers:
- Schema definition (types, queries, mutations)
- Pagination patterns (connections, edges, pageInfo)
- Query examples with nested data
- Pros over REST: Single request, no over/under-fetching
- Challenges: Caching complexity, query cost attacks

### 4. Authentication Strategies

Compares approaches:
- **JWT**: Stateless, scalable, microservices (access + refresh tokens)
- **OAuth 2.0**: Third-party access, delegated authorization
- **API Keys**: Simple, service-to-service, rate limiting
- **Session-Based**: Traditional web apps, server control

### 5. API Versioning Strategies

Methods:
- **URL Versioning**: `/v1/users`, `/v2/users` (most common)
- **Header Versioning**: `Accept: application/vnd.myapi.v1+json`
- **Query Parameter**: `/users?version=1`

Best practices: Semantic versioning, support N-1 versions, document deprecation timeline.

### 6. API Performance Optimization

Techniques:
- **Pagination**: Offset-based (simple) vs cursor-based (large datasets)
- **Field Selection**: Reduce payload with `?fields=id,name`
- **Batch Requests**: Multiple operations in single request
- **Caching**: HTTP caching with ETag, Cache-Control
- **Compression**: gzip, Brotli
- **Rate Limiting**: Protect API with limits, retry-after headers

## Output Format

See [template.md](template.md) for complete API design documentation structure.

## Quick Workflow Reference

**Before implementing**: Search for proven patterns
```bash
.claude/scripts/kg-search search "api-design" --type concept
```

**For deep research**: `hybrid_search("[API pattern]")` (Weaviate MCP)

## Knowledge Graph Integration

Document reusable API patterns in `knowledge/concepts/`, tag appropriately, sync via `.claude/scripts/kg-sync`.

## Supporting Files

- **Template**: Use [template.md](template.md) for complete API design documentation
