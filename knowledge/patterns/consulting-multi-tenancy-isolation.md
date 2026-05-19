---
title: Consulting Multi-Tenancy Isolation
type: pattern
tags:
- pattern
- consulting
- security
- isolation
- multi-tenancy
- mid-level-architecture
- best-practices
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Consulting Multi-Tenancy Isolation

The set of isolation patterns a consulting firm uses to serve N client engagements concurrently without accidentally leaking one client's data, identity, or work product into another. Unlike SaaS multi-tenancy (one product, many customers), consulting multi-tenancy is N separate products / repos / clouds, with the firm's team and infrastructure spanning across.

## Why this matters

Cross-client leakage is the worst-case failure for a consulting firm: it's an NDA breach, it's a reputational hit, it's often a contractual termination event, and it's frequently a regulatory event. Multi-tenancy isolation is not an abstract security concern — it's the discipline that lets the firm sustain growth past 3-5 concurrent engagements without an incident.

## Dimensions of isolation

A consulting engagement spans multiple isolation dimensions. Each dimension needs a deliberate decision.

### 1. Identity isolation

**Concern**: Which firm employees can access which client's systems / data.

**Patterns**:
- **Per-engagement access grant** — access added on kickoff, REMOVED on handover. Default-deny.
- **Named-person allow-list** — client's IAM only allows specific named individuals from the firm
- **Just-in-time access** — temporary elevated access for specific tasks, expires automatically
- **Audit trail** — every access event logged on both client and firm sides

**Anti-pattern**: Standing access on the firm's side that outlives the engagement. After-the-fact "we still had a token" findings are how cross-client incidents start.

### 2. Credential isolation

**Concern**: Where the client's secrets (API tokens, cloud keys, SSH keys, service-account passwords) live.

**Patterns**:
- **Per-client secret namespace** — Vault / AWS Secrets Manager / GCP Secret Manager with one path/namespace per client
- **No mixing in env files** — `.env.client-a` and `.env.client-b` are separate files in separate directories, never combined
- **No copy-paste between repos** — secrets retrieved by short-lived token, not embedded
- **Rotation on offboarding** — when a team member leaves the engagement, their credentials are revoked, not just disabled

**Anti-pattern**: A single `~/.aws/credentials` file with N client profiles, one of which is the default. The default is the wrong one half the time.

### 3. Repository isolation

**Concern**: Code for client A and code for client B do not share a repo, a branch, or a CI runner.

**Patterns**:
- **Separate organisations / namespaces** per client (e.g. GitHub org per client) OR a single firm org with strict per-repo access lists
- **Per-client CI runners** — self-hosted runners separated by client; cloud runners with per-job credentials
- **No shared monorepo across clients** — even shared libraries belong to ONE owning entity (usually the firm), and client repos import them
- **Branch protection mirrors client policies** — if the client requires PR review, the firm's repo for that client enforces it

**Anti-pattern**: A "consultants" monorepo with subfolders per client. One leaked clone exposes every engagement.

### 4. Infrastructure / cloud isolation

**Concern**: Cloud accounts, Kubernetes clusters, networks belonging to one client do not share with another's.

**Patterns**:
- **Client owns the cloud account** — firm's IAM users are guests; firm cannot consolidate billing across clients
- **Per-client AWS / GCP / Azure profile** in the team's workstation tooling
- **No shared bastion** across clients (each client provides their own VPN / bastion)
- **Per-client kubeconfig context** with explicit context name (`client-a-prod`, never `prod`)

**Anti-pattern**: A firm-owned AWS account that runs shared dev infra alongside client-specific environments. A misconfigured security group exposes the wrong thing.

### 5. Communication isolation

**Concern**: Messages about client A do not appear in channels client B sees, and vice versa.

**Patterns**:
- **Per-engagement Slack workspace / channel** with explicit member list
- **Email aliases per engagement** (e.g. `client-a@firm.com` routes to the team currently on it)
- **No cross-client backchannel** — if internal discussion is needed, it's in firm-internal channels, never in shared channels
- **Status updates audited** — multi-client digest documents (see [[uses::consulting-portfolio-status]]) are INTERNAL ONLY and labelled

**Anti-pattern**: A firm-wide #engineering channel where consultants debug client problems with code snippets that include the client's internals.

### 6. Knowledge / IP isolation

**Concern**: What the firm learns at client A's expense cannot be repackaged into client B's deliverable without permission.

**Patterns**:
- **Internal patterns vs client work** — generic patterns (e.g. "how to set up CI/CD") flow into the firm's knowledge base; client-specific architectures stay in the client repo
- **Sanitisation discipline** — case studies require client written permission; reference architectures are abstracted before reuse
- **NDA per engagement** with clarity on whether the firm retains "residual knowledge" rights

**Anti-pattern**: Lifting a client's domain model and reusing it as a template for the next engagement. Even if anonymised, the client's competitors will recognise it.

### 7. Workstation isolation (often overlooked)

**Concern**: The consultant's laptop has files / credentials / clones from multiple clients simultaneously.

**Patterns**:
- **Per-client directory structure** — `~/clients/client-a/`, `~/clients/client-b/` with no shared parent containing tools that span both
- **Per-client shell session** — `direnv` or equivalent per directory, switching `AWS_PROFILE`, `KUBECONFIG`, `GOOGLE_APPLICATION_CREDENTIALS` etc. on `cd`
- **Per-client browser profile** — Chrome / Firefox profiles per client; SSO sessions can't accidentally cross
- **Encryption at rest** — laptop FDE is non-negotiable; recovery key escrowed by IT
- **Remote-wipe capability** — for laptops left at airports

**Anti-pattern**: A single AWS profile, a single KUBECONFIG, manual `unset` / `export` rituals when switching clients. Mistakes accumulate; one misdirected `kubectl delete` is enough.

## Decision framework

For each new engagement, the firm should explicitly decide:

| Dimension | Default | Client may require stricter | Notes |
|---|---|---|---|
| Identity | Named-person allow-list | Per-task JIT access | Most clients accept allow-list |
| Credentials | Per-client secret namespace | Client-side HSM | Default works for most |
| Repos | Per-client org / namespace | On-client-side infra | If client requires their own GitHub org, accept |
| Infra | Client-owned accounts | Air-gapped | Air-gapped engagements have unique cost model |
| Comms | Per-engagement channel | Client-tenant Teams / Slack | Client-tenant for regulated industries |
| Knowledge | Sanitisation discipline | No residual knowledge clause | Affects reusability of firm IP |
| Workstation | direnv + per-client dirs | Client-provided VDI | VDI common in finance / pharma |

## Detection: how to know isolation is failing

- Same person has standing access to >2 clients with no rolloff scheduled → contractor sprawl
- Secret rotation event for client A reveals a token that's still in client B's CI → cross-pollination
- Status digest accidentally sent to a client recipient → audit your distribution list
- Workstation contains live clones from clients no longer engaged → cleanup discipline gap
- Single Slack channel has discussion about >1 client → channel hygiene gap

## Links

- [[implements::Client Engagement Lifecycle]] — isolation decisions are made in phase 3 (kickoff) and reversed in phase 5 (handover)
- [[uses::Enterprise SaaS Automation Surfaces 2026]] — each SaaS surface has its own isolation discipline
- [[relatedTo::Contractor vs Employee Management]] — contractors complicate identity isolation
- [[relatedTo::Consulting CTO Portfolio Coordinator]] — the portfolio digest is a multi-client document and must respect these boundaries
