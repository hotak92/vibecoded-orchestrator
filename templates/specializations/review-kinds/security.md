# Security review kind

What to hunt when reviewing a diff for security defects. Every finding
carries severity, `file:line`, evidence, and a suggested fix. Severity here
is about exploitability, not style.

## Injection

- SQL: parameterized queries everywhere; string-built SQL is a finding even
  when "the input looks trusted".
- Command: shell invocation with interpolated input (`shell=True`, backticks,
  `os.system`) — require list-form/escaped invocation and validated paths.
- Path traversal: user-controlled paths checked for `..`, symlinks, and
  absolute-path escape before any file operation.
- XSS: untrusted content rendered without escaping; error messages echoing
  request data.
- Template/SSRF: user-controlled URLs fetched server-side without an
  allowlist or private-range blocking.

## Authentication & authorization

- Every endpoint/action names its auth requirement; missing check on an
  owner-scoped resource (IDOR) is a high-severity finding.
- Tokens: expiry enforced, signature verified, malformed/missing claims
  rejected; refresh flow doesn't leak the access token.
- Password/credential handling: strong hashing (bcrypt/argon2 class), never
  logged, never in error responses, never in URLs.
- Permission escalation: role checks at the operation, not only the route.

## Secrets & data exposure

- Secret values in code, logs, error messages, test fixtures, or committed
  files — any occurrence is a finding, even a "fake" one that trains the
  habit.
- API responses leaking internal fields (password hashes, internal IDs,
  stack traces).
- CORS/CSRF: wildcard origins with credentials; state-changing GETs.

## Dependencies & crypto

- New dependency: license compatible, maintained, pinned; a transitive GPL
  dependency entering an AGPL repo with non-AGPL paid modules is a finding.
- Home-grown crypto or encoding used for security purposes — always a
  finding.
- Randomness for tokens/ids: CSPRNG required.

## Race conditions & state

- Check-then-act on balances, inventory, quotas without locking or atomic
  operations.
- Replay: webhooks/callbacks without signature+nonce or timestamp checks.

## Verification discipline

Verify claims in source; where exploitability cannot be confirmed, say
UNVERIFIED and name what would confirm it. Never include a working exploit
payload against a live system in the report — describe, don't demonstrate.
