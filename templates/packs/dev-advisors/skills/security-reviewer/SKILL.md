---
name: security-reviewer
description: Cross-layer security review covering frontend (XSS, CSRF, CSP), backend (SQL/NoSQL/command injection, path traversal, auth/authz, session management), AI (prompt injection, data exfiltration, output filtering), and infrastructure (exposed secrets, weak crypto, missing headers). Maps attack surface, threat-models entry points, and gives concrete remediation. Use when reviewing auth or input-handling code, before a production deploy, or after a vulnerability is discovered. Not for internal utilities with no external input or documentation-only changes.
short_desc: XSS/CSRF/SQLi/prompt-injection cross-layer audit
keywords: [XSS, CSRF, "SQL injection", "prompt injection", "security review", OWASP, "code review", "security audit", "pre-release review", "API authentication", "security check", "is this secure", "secure my app", "authentication review"]
model: opus
---

# Security Reviewer (Opus)

**Purpose**: Cross-layer security analysis (frontend XSS/CSRF, backend injection, AI prompt injection, infrastructure).

**Model**: Opus (expert security reasoning, attack surface analysis)

## When to Invoke Autonomously

Use this skill when:
1. **Auth/Security Code**: Authentication, authorization, session management, crypto
2. **Input Handling**: User input, API requests, file uploads, query parameters
3. **Pre-Production**: Security review before deploying to production
4. **Data Handling**: Sensitive data (PII, credentials, payment info)
5. **External Integration**: Third-party APIs, webhooks, OAuth flows
6. **After Security Incident**: Review related code after vulnerability discovered

## DO NOT invoke for

- Internal utilities with no external input
- Documentation updates
- Simple UI text changes
- Configuration files without sensitive data

## Decision Tree

```
Code involves:
├─ Authentication/authorization? → Use this skill
├─ User input (forms, APIs, uploads)? → Use this skill
├─ Sensitive data (PII, passwords, tokens)? → Use this skill
├─ Pre-production security check? → Use this skill
├─ Third-party integration? → Use this skill
├─ Internal-only utility? → Skip security review
└─ Just documentation? → Skip security review
```

## Usage

```
/security-reviewer audit [component/endpoint]
/security-reviewer xss-check [frontend-code]
/security-reviewer injection-check [backend-code]
/security-reviewer prompt-injection-check [ai-code]
```

## What This Skill Does

**Layer-Specific Security Analysis**:
- Frontend: XSS, CSRF, CSP, client-side data exposure, third-party scripts
- Backend: SQL/NoSQL injection, command injection, path traversal, auth/authz flaws, session management
- AI: Prompt injection, data exfiltration, model output filtering, context poisoning
- Infrastructure: Exposed secrets, vulnerable dependencies, missing security headers, weak crypto

**Attack Surface Mapping**:
- Identify all entry points (inputs, APIs, uploads, integrations)
- Map sensitive operations and data access
- Calculate blast radius of potential exploits

**Threat Modeling**:
- What can attacker control?
- What sensitive operations are possible?
- What's the impact of successful exploit?

**Remediation Guidance**:
- Specific code fixes (validation, sanitization)
- Framework features to leverage (CSRF middleware, CSP)
- Architectural improvements (least privilege, defense in depth)
- Security test recommendations

**See**: `examples/security-checklist.md` for layer-specific checklists, `examples/common-vulnerabilities.md` for OWASP Top 10 patterns

## Quick Workflow Reference

**Before reviewing**: Search for security patterns and vulnerabilities
```bash
.claude/scripts/kg-search search "security" --type concept
```

**For deep research**: run `hybrid_search("<vulnerability type topic>")` (Weaviate MCP)

**Development env**: Python 3.12, Weaviate:8081, Ollama:11435. KG/code-graph scripts run through the `.claude/scripts/kg-*` and `.claude/scripts/code-graph-*` wrappers, which resolve the correct venv internally.

## Success Metrics

- ✅ Identifies vulnerabilities before production
- ✅ No false positives (real issues, not theoretical)
- ✅ Fixes are actionable and correct
- ✅ Security test coverage improves
- ✅ Reduced vulnerability reports post-deployment
