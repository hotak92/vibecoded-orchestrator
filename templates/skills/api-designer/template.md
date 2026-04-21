# API Design Template

## API Design: [Application Name]

**Date**: [YYYY-MM-DD]
**Project**: [Project Name]
**Version**: v[1.0]
**Designed by**: [Name]

---

## Requirements

### Use Cases
1. [Primary use case 1]
2. [Primary use case 2]
3. [Primary use case 3]

### Clients
- [ ] Web application (browser)
- [ ] Mobile app (iOS/Android)
- [ ] Internal services (microservices)
- [ ] Third-party integrations
- [ ] Other: [specify]

### Scale
- **Expected load**: [X] requests per day
- **Concurrent users**: [Y]
- **Peak load**: [Z] requests per second
- **Data size**: [W] GB

### Security
- [ ] Public API (anyone can access)
- [ ] Authenticated API (registered users)
- [ ] Internal API (service-to-service only)
- [ ] Mixed (some public, some authenticated)

---

## Protocol Selection

### Recommended Protocol: [REST / GraphQL / gRPC / WebSocket]

**Rationale**:
- [Reason 1 based on requirements]
- [Reason 2 based on clients]
- [Reason 3 based on use cases]

**Alternatives Considered**:
- **[Protocol]**: Ruled out because [reason]
- **[Protocol]**: Ruled out because [reason]

**Tradeoffs Accepted**:
- [Tradeoff 1]: [Why acceptable]
- [Tradeoff 2]: [Why acceptable]

---

## Endpoint Design (REST)

### Base URL
```
https://api.[domain].com/v1
```

### Resources

#### Resource 1: [Resource Name] (e.g., Users)

**Endpoints**:

```http
# List resources (with pagination)
GET /users?page=1&limit=20&status=active&sort=-created_at
Response: 200 OK
{
  "data": [
    {
      "id": "uuid",
      "email": "user@example.com",
      "name": "John Doe",
      "created_at": "2025-01-15T10:30:00Z"
    }
  ],
  "meta": {
    "page": 1,
    "limit": 20,
    "total": 150,
    "pages": 8
  },
  "links": {
    "self": "/users?page=1&limit=20",
    "next": "/users?page=2&limit=20",
    "last": "/users?page=8&limit=20"
  }
}
```

```http
# Get single resource
GET /users/{id}
Response: 200 OK
{
  "data": {
    "id": "uuid",
    "email": "user@example.com",
    "name": "John Doe",
    "created_at": "2025-01-15T10:30:00Z"
  }
}

Response: 404 Not Found (if doesn't exist)
```

```http
# Create resource
POST /users
Content-Type: application/json
{
  "email": "new@example.com",
  "name": "Jane Doe",
  "password": "secure_password"
}

Response: 201 Created
Location: /users/new-uuid
{
  "data": {
    "id": "new-uuid",
    "email": "new@example.com",
    "name": "Jane Doe",
    "created_at": "2025-01-28T14:00:00Z"
  }
}
```

```http
# Update resource (full)
PUT /users/{id}
{
  "email": "updated@example.com",
  "name": "Jane Smith"
}

Response: 200 OK
```

```http
# Update resource (partial)
PATCH /users/{id}
{
  "name": "Jane Smith"
}

Response: 200 OK
```

```http
# Delete resource
DELETE /users/{id}

Response: 204 No Content
```

#### Nested Resources

```http
# List nested resources
GET /users/{userId}/orders
Response: 200 OK

# Create nested resource
POST /users/{userId}/orders
Response: 201 Created

# Get nested resource
GET /users/{userId}/orders/{orderId}
Response: 200 OK
```

---

## Error Handling (RFC 7807)

### Standard Error Format

```json
{
  "type": "/errors/[error-type]",
  "title": "Human-readable title",
  "status": 400,
  "detail": "Detailed explanation of the error",
  "instance": "/endpoint/that/failed",
  "errors": [
    {
      "field": "email",
      "code": "required",
      "message": "Email is required"
    },
    {
      "field": "password",
      "code": "min_length",
      "message": "Password must be at least 8 characters"
    }
  ]
}
```

### Common Error Types

**400 Bad Request** (Validation Failed):
```json
{
  "type": "/errors/validation-error",
  "title": "Validation Failed",
  "status": 400,
  "detail": "One or more fields are invalid",
  "errors": [...]
}
```

**401 Unauthorized** (Missing/Invalid Token):
```json
{
  "type": "/errors/unauthorized",
  "title": "Authentication Required",
  "status": 401,
  "detail": "Valid authentication token required"
}
```

**403 Forbidden** (Insufficient Permissions):
```json
{
  "type": "/errors/forbidden",
  "title": "Forbidden",
  "status": 403,
  "detail": "You don't have permission to access this resource"
}
```

**404 Not Found**:
```json
{
  "type": "/errors/not-found",
  "title": "Resource Not Found",
  "status": 404,
  "detail": "User with id 'abc123' not found"
}
```

**409 Conflict** (Duplicate/State Conflict):
```json
{
  "type": "/errors/conflict",
  "title": "Conflict",
  "status": 409,
  "detail": "User with email 'user@example.com' already exists"
}
```

**429 Too Many Requests** (Rate Limit):
```json
{
  "type": "/errors/rate-limit-exceeded",
  "title": "Rate Limit Exceeded",
  "status": 429,
  "detail": "Too many requests. Please try again later.",
  "retry_after": 60
}
```

**500 Internal Server Error**:
```json
{
  "type": "/errors/internal-server-error",
  "title": "Internal Server Error",
  "status": 500,
  "detail": "An unexpected error occurred. Please contact support.",
  "request_id": "req-uuid"
}
```

---

## Authentication

### Strategy: [JWT / OAuth 2.0 / API Keys / Session-Based]

**Flow**:
1. Login: `POST /auth/login` → `access_token` + `refresh_token`
2. Use token: `Authorization: Bearer <access_token>`
3. Refresh: `POST /auth/refresh` → new `access_token`

**Token Expiry**:
- Access token: [1] hour
- Refresh token: [30] days

**Example**:
```http
# Login
POST /auth/login
{
  "email": "user@example.com",
  "password": "password"
}

Response: 200 OK
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "...",
  "token_type": "Bearer",
  "expires_in": 3600
}

# Use token
GET /users/me
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

# Refresh token
POST /auth/refresh
{
  "refresh_token": "..."
}

Response: 200 OK
{
  "access_token": "new_token...",
  "expires_in": 3600
}
```

---

## Versioning

### Strategy: URL Versioning (`/v1`, `/v2`)

**Breaking Changes** (Trigger new version):
- Removing endpoints or fields
- Changing data types
- Renaming fields
- Changing response structure

**Non-Breaking Changes** (No new version):
- Adding new endpoints
- Adding new optional fields
- Adding new enum values

**Deprecation Policy**:
- Support current + previous version
- Deprecation notice: [6] months before removal
- Sunset header: `Sunset: Sat, 01 Jan 2027 00:00:00 GMT`

---

## Performance Optimizations

### Pagination
```http
# Offset-based (simple, works for most)
GET /users?offset=0&limit=20

# Cursor-based (better for large datasets)
GET /users?cursor=abc123&limit=20
```

### Field Selection (Reduce Payload)
```http
GET /users?fields=id,name,email
```

### Batch Requests (Reduce Round Trips)
```http
POST /batch
{
  "requests": [
    {"method": "GET", "url": "/users/1"},
    {"method": "GET", "url": "/users/2"},
    {"method": "GET", "url": "/orders/123"}
  ]
}
```

### Caching
```http
# Response headers
Cache-Control: public, max-age=3600
ETag: "abc123"

# Conditional request
GET /users/1
If-None-Match: "abc123"

# Response if not modified
304 Not Modified
```

### Compression
```http
# Request
Accept-Encoding: gzip, br

# Response
Content-Encoding: gzip
```

### Rate Limiting
```http
# Response headers
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 950
X-RateLimit-Reset: 1621234567

# If exceeded
429 Too Many Requests
Retry-After: 60
```

---

## Monitoring

### Metrics to Track
- Request rate (per endpoint)
- Response time (p50, p95, p99)
- Error rate (4xx, 5xx)
- Cache hit rate
- Authentication failures

### Alerts
- Error rate > [5]%
- p99 latency > [1]s
- Rate limit hits > [10]%
- Authentication failures > [20]%

---

## Documentation

### Tools
- [ ] OpenAPI 3.0 spec
- [ ] Postman collection
- [ ] Interactive docs (Swagger UI / Redoc)
- [ ] Code examples (curl, Python, JavaScript)

### Include
- [ ] Authentication flow
- [ ] All endpoints with examples
- [ ] Error codes and meanings
- [ ] Rate limits
- [ ] Pagination details

---

## Testing Strategy

### Unit Tests
- [ ] Endpoint handlers
- [ ] Validation logic
- [ ] Authentication middleware

### Integration Tests
- [ ] Full request/response cycles
- [ ] Database interactions
- [ ] External service calls

### API Contract Tests
- [ ] OpenAPI spec validation
- [ ] Response schema validation
- [ ] Backward compatibility

---

## Deployment Checklist

- [ ] API versioning implemented
- [ ] Authentication working
- [ ] Rate limiting configured
- [ ] Caching headers set
- [ ] Error handling consistent
- [ ] Logging and monitoring active
- [ ] Documentation published
- [ ] Load testing passed

---

## Sign-Off

**Designed by**: [Name]
**Reviewed by**: [Name]
**Approved**: [Yes/No]
**Date**: [YYYY-MM-DD]
