# Security Review Checklists by Layer

## Frontend Security Checklist

**XSS Prevention**:
- [ ] User input sanitized before rendering (DOMPurify, textContent)
- [ ] No innerHTML/dangerouslySetInnerHTML with user data
- [ ] CSP headers configured (script-src, style-src, object-src)
- [ ] Template engine auto-escapes by default

**CSRF Protection**:
- [ ] CSRF tokens on all state-changing requests
- [ ] SameSite cookies (Strict or Lax)
- [ ] Custom headers on AJAX requests
- [ ] Origin/Referer validation on server

**Client-Side Data**:
- [ ] No sensitive data in LocalStorage/SessionStorage
- [ ] No credentials in URL parameters
- [ ] No console.log of sensitive data in production
- [ ] API keys/secrets not embedded in client code

**Third-Party Scripts**:
- [ ] Subresource Integrity (SRI) on CDN resources
- [ ] Minimal permissions for third-party scripts
- [ ] Content Security Policy whitelists sources
- [ ] Regular audit of dependencies for vulnerabilities

---

## Backend Security Checklist

**Injection Prevention**:
- [ ] Parameterized queries (no string concatenation)
- [ ] Input validation on all endpoints
- [ ] ORM escapes inputs automatically
- [ ] No eval(), exec(), or dynamic code execution
- [ ] Path traversal prevented (validate file paths)

**Authentication**:
- [ ] Password hashing with bcrypt/argon2 (not MD5/SHA1)
- [ ] Account lockout after failed attempts
- [ ] Session expiration and rotation
- [ ] Secure password reset flow (time-limited tokens)

**Authorization**:
- [ ] Principle of least privilege enforced
- [ ] IDOR prevention (authorize object access)
- [ ] Horizontal privilege escalation prevented
- [ ] Vertical privilege escalation prevented
- [ ] Role-based access control (RBAC) implemented

**Session Management**:
- [ ] HttpOnly cookies (prevent XSS access)
- [ ] Secure flag on HTTPS
- [ ] Session fixation prevention (regenerate on login)
- [ ] Timeout for inactive sessions
- [ ] Session invalidation on logout

---

## AI Security Checklist

**Prompt Injection**:
- [ ] User input separated from system prompts
- [ ] No direct user control of system prompts
- [ ] Input validation and filtering
- [ ] Context window boundaries enforced

**Data Exfiltration**:
- [ ] Output filtering (remove sensitive data)
- [ ] Audit logs for sensitive queries
- [ ] Rate limiting on API access
- [ ] Context isolation between users

**Model Output Safety**:
- [ ] Content filtering (profanity, hate speech)
- [ ] Hallucination detection for critical facts
- [ ] User warnings for generated content
- [ ] Human review for sensitive use cases

**Multi-Agent Security**:
- [ ] Agent permissions isolated (least privilege)
- [ ] Inter-agent communication authenticated
- [ ] No agent can escalate privileges
- [ ] Audit trail for all agent actions

---

## Infrastructure Security Checklist

**Secrets Management**:
- [ ] No secrets in code or version control
- [ ] Environment variables for configuration
- [ ] Secrets rotation policy in place
- [ ] Vault/secrets manager for production

**Dependencies**:
- [ ] Regular security audits (npm audit, pip audit)
- [ ] Vulnerable dependencies patched promptly
- [ ] Pin versions to prevent supply chain attacks
- [ ] Minimal dependencies (reduce attack surface)

**Security Headers**:
- [ ] Content-Security-Policy configured
- [ ] X-Frame-Options: DENY or SAMEORIGIN
- [ ] X-Content-Type-Options: nosniff
- [ ] Strict-Transport-Security (HSTS)
- [ ] X-XSS-Protection (deprecated but harmless)

**Cryptography**:
- [ ] Strong algorithms (AES-256, RSA-2048+, SHA-256+)
- [ ] No MD5, SHA1, DES, or RC4
- [ ] Secure random number generation
- [ ] No hardcoded keys or IVs
- [ ] HTTPS enforced (no mixed content)
