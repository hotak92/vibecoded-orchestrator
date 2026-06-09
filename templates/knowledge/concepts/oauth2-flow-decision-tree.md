---
title: OAuth2 Flow Decision Tree
type: concept
tags:
  - security
  - authentication
  - integration
  - automation
  - mid-level-architecture
  - REST-API
  - oauth
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
status: active
---

# OAuth2 Flow Decision Tree

When integrating with a third-party API, the auth flow you pick determines what your workflow can and cannot do. Picking the wrong flow forces architectural rewrites later. The choice is governed by two questions:

1. **Who is the resource owner?** (end user vs. your own organisation)
2. **What can hold a secret?** (server vs. browser vs. CLI vs. mobile)

## The tree

```
Are you accessing data on behalf of an end user (their Gmail, their Salesforce, their GitHub repo)?
│
├─ YES (user-delegated access)
│   │
│   ├─ Where does the client run?
│   │   ├─ Server-side web app (can keep a client secret)
│   │   │   → Authorization Code with PKCE
│   │   │
│   │   ├─ SPA / mobile / desktop (cannot keep a secret)
│   │   │   → Authorization Code with PKCE (no client secret, mandatory PKCE)
│   │   │
│   │   └─ CLI / IoT device with limited UI
│   │       → Device Authorization Grant (RFC 8628) — user types code on phone
│   │
│   └─ Need long-lived offline access?
│       → Request `offline_access` scope; store refresh token securely
│
└─ NO (you're accessing your own resources, or service-to-service)
    │
    ├─ Service-to-service, your service vs. their API
    │   → Client Credentials Grant — store client_id + client_secret in secret manager
    │
    ├─ Workload identity (AWS, GCP, Azure to managed service)
    │   → Use platform-native (AWS IAM, GCP Workload Identity, Azure Managed Identity)
    │      instead of static OAuth credentials
    │
    └─ "Service account" pattern (Google Workspace admin acting as a user)
        → JWT Bearer flow (RFC 7523) — sign assertion with service account key
```

## Flow details (the ones you'll actually use)

### Authorization Code + PKCE

Standard for any user-delegated flow in 2026. The legacy "Authorization Code without PKCE" and "Implicit" flows are deprecated (OAuth 2.1 mandates PKCE for ALL authorization-code clients, public or confidential).

```
1. Client generates code_verifier (random 43-128 chars) and code_challenge = SHA256(verifier).
2. Browser → authorize URL with code_challenge, code_challenge_method=S256.
3. User logs in, consents, redirected back with ?code=...
4. Client POSTs /token with code + code_verifier (+ client_secret if confidential).
5. Server returns access_token (short-lived) + refresh_token (long-lived if offline_access).
```

**Implementation notes**:
- Use a library (`authlib`, `oauthlib`, `passport-oauth2`, vendor SDKs). Hand-rolling is error-prone.
- `state` parameter is non-negotiable for CSRF protection.
- Store refresh tokens encrypted at rest (per-user encryption key, not a single global key).
- Rotate refresh tokens if the provider supports it (Google, Microsoft do; GitHub doesn't).

### Client Credentials

For machine-to-machine flows where you ARE the user.

```
POST /oauth/token
grant_type=client_credentials
client_id=...
client_secret=...
scope=...
```

**Implementation notes**:
- Treat `client_secret` like a database password: secret manager, not env vars committed anywhere.
- Cache the access token until ~80% of its TTL elapses; re-fetch on demand.
- If the provider supports it (Auth0, Okta, AWS Cognito), prefer **mTLS client authentication** over client_secret for higher-value integrations.
- No refresh token in this flow — when the access token expires, just request a new one.

### Device Authorization Grant (RFC 8628)

For CLIs, smart TVs, printers — anything that can't reasonably show a browser.

```
1. Device POSTs /device_authorization → gets device_code, user_code, verification_uri.
2. Device displays: "Go to https://provider/device and enter ABCD-EFGH".
3. Device polls /token every N seconds with device_code.
4. User completes auth on their phone/laptop.
5. Next poll returns access_token + refresh_token.
```

GitHub, Google, Microsoft, Anthropic Claude Code login all use this for CLI auth.

### JWT Bearer (RFC 7523) — service accounts impersonating users

Google Workspace admin SDK, Salesforce JWT bearer flow, etc. You sign an assertion with a private key whose public counterpart is registered with the provider.

```
1. Build JWT: iss=service_account, sub=user_to_impersonate, aud=token_endpoint, exp=...
2. Sign with RS256 + private key.
3. POST /token with grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer + assertion.
4. Get access_token in response.
```

Common in enterprise SaaS where one service account does work on behalf of many users.

## Anti-patterns to avoid in 2026

- **Storing client_secret in client-side code** — Authorization Code with PKCE was designed precisely so you don't have to.
- **Implicit flow** — deprecated by OAuth 2.1; use Authorization Code + PKCE for SPAs.
- **Resource Owner Password Credentials Grant** — collecting the user's password to your app to exchange for a token. Deprecated, never use, ever.
- **Hard-coding tokens with no refresh logic** — works in dev, breaks at 3am in prod when the access token expires.
- **Sharing a single refresh token across users** — multi-tenant systems must isolate per-user tokens.
- **Bearer tokens without TLS** — bearer means "anyone holding this token has access"; without TLS, anyone on the network has them.

## Token storage and rotation

| Token | Lifetime | Storage |
|---|---|---|
| Access token | 15 min – 1 hour typically | Memory or short-lived cache (Redis with TTL = token TTL) |
| Refresh token | Days – years | Encrypted at rest, per-user key; never in logs |
| Client secret | Indefinite (rotate quarterly) | Secret manager (Vault, AWS SM, Doppler), not env files committed to git |

Rotate refresh tokens on use where the provider supports it (Google: every refresh returns a new refresh token; GitHub: stable refresh tokens). Track rotation in audit logs.

## Scope hygiene

- **Request least privilege**: ask only for the scopes you actually use; users see scope screens and over-asking kills conversion.
- **Re-prompt for new scopes**: if you add a feature requiring new scopes, incremental authorization is better than a full re-consent.
- **Document each scope's purpose**: future-you will need to know why your app asks for `gmail.send` (and the security review will too).

## Links

- [[relatedTo::Webhook Security Checklist]]
- [[relatedTo::API Integration Scaffolding]]
- [[buildsOn::OAuth 2.1 (RFC drafts consolidating PKCE)]]

## References

- OAuth 2.1 draft: https://datatracker.ietf.org/doc/html/draft-ietf-oauth-v2-1
- RFC 8628 (Device Grant): https://datatracker.ietf.org/doc/html/rfc8628
- RFC 7523 (JWT Bearer): https://datatracker.ietf.org/doc/html/rfc7523
- Auth0 Flow Picker: https://auth0.com/docs/get-started/authentication-and-authorization-flow/which-oauth-2-0-flow-should-i-use
