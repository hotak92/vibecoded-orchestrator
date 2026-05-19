---
title: Enterprise SaaS Automation Surfaces 2026
type: concept
tags:
- concept
- consulting
- automation
- enterprise
- API
- integration
- low-level-implementation
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Enterprise SaaS Automation Surfaces 2026

Reference map of the API + automation surface for the enterprise SaaS tools a consulting CTO most often needs to script against on behalf of clients. The aim is to skip the "which API does X have, and how do you auth?" first-day question and go straight to the right doc + the right gotcha.

**Note**: API surfaces evolve. Each section ends with a `verify-before-use` field listing what to confirm against current vendor docs before implementing.

## Why this matters

Clients run their work on Jira, ServiceNow, Salesforce, M365, Slack, GitHub, and a handful of others. A consulting CTO is asked, weekly, to either (a) build something on top of one of these or (b) integrate two of them. Knowing the auth model, the rate limit, and the most common gotcha per platform is the entry point.

## Atlassian (Jira / Confluence)

**Primary APIs**: REST API v3 (cloud), REST API v2 (server / data center), GraphQL (limited).
**Auth**:
- Cloud: API token (basic auth with email + token) for user-scoped; OAuth 2.0 for app-scoped; Forge platform for in-product apps.
- Server / DC: personal access tokens or OAuth 1.0a (legacy).
**Rate limits**: Per-tenant; varies by plan. Plan for HTTP 429 with `Retry-After` header.
**Webhooks**: yes, configured per project or globally; payload includes issue / comment / project events.
**Common automation**: status sync, ticket creation from external systems, SLA reporting, custom dashboards.
**Gotchas**:
- Cloud vs Server API URLs and shapes differ; one client may have a hybrid
- ADF (Atlassian Document Format) for rich-text fields, not Markdown
- Jira "issue" has custom fields named by ID (`customfield_10123`), not by display name — query the field metadata first
- Project lead transitions and workflow changes are admin operations, not REST
**verify-before-use**: current REST API version (v3 cloud as of 2026), Forge runtime restrictions if shipping an app, OAuth scopes required for the operations you need.

## ServiceNow

**Primary APIs**: REST Table API (generic CRUD over any table), Scripted REST API (custom endpoints), Import Set API (bulk loads), Aggregate API (counts and sums).
**Auth**: OAuth 2.0, basic auth (deprecated but still common in legacy integrations), mutual TLS for trusted partners.
**Rate limits**: Per-instance, configurable by admin; transactions-per-hour quota.
**Webhooks**: Outbound REST messages, MID Server-mediated when integrating with on-prem systems.
**Common automation**: incident creation from monitoring, CMDB sync, change-approval orchestration, knowledge-article publishing.
**Gotchas**:
- ACLs determine what each user can see — an API call returns only the rows the authenticating user has rights to. Test with a service account that mirrors prod.
- Table inheritance — `incident` extends `task`; querying `task` returns incidents too unless filtered
- Reference fields hold `sys_id`, not human-readable values; display values require `?sysparm_display_value=true`
- Domain separation (multi-tenant ServiceNow) — calls execute in the caller's domain only
- Discovery / orchestration APIs require licensed modules; not always available
**verify-before-use**: instance version (Now / Vancouver / Washington-DC etc.), licensed modules, ACL impact for the service account.

## Salesforce

**Primary APIs**: REST API, SOAP API (legacy but still widely used for metadata), Bulk API 2.0 (for large data loads), Streaming API (CometD, push events), GraphQL API (newer, scoping rapidly).
**Auth**: OAuth 2.0 with multiple flows (web server, JWT bearer for server-to-server, refresh token, device flow); Connected App must be configured first.
**Rate limits**: Daily API request limits per org based on edition + licenses; concurrent request limits; per-transaction Apex limits.
**Common automation**: lead/opportunity sync, custom object CRUD, batch updates, custom UI on top of Salesforce data.
**Gotchas**:
- SOQL is similar to SQL but not SQL — no joins in the SQL sense; relationship queries with dot notation
- Governor limits — every Apex transaction has hard limits (SOQL queries, DML statements, CPU time)
- Field-level security and sharing rules — API respects them; profile of the integration user matters
- Bulk API 2.0 vs REST — bulk is the right choice above ~2000 records
- Sandbox vs production differ in IDs (refresh sandbox after schema change)
- Lightning vs Classic — most REST works in both; some UI-specific APIs differ
**verify-before-use**: edition (Enterprise/Unlimited/etc.), API version (latest is moving target), governor limits for the operation, Connected App scopes.

## Microsoft 365 (Graph)

**Primary API**: Microsoft Graph (single endpoint for Outlook / Teams / OneDrive / SharePoint / Entra / Intune / Planner / etc.).
**Auth**: OAuth 2.0 via Entra ID (formerly Azure AD); app-only or delegated; required permissions declared in app manifest and consented by admin (app-only) or user.
**Rate limits**: Per-service throttling; Graph returns 429 with `Retry-After`.
**Webhooks**: Change notifications (subscriptions), expire and must be renewed; webhook endpoint must be HTTPS-reachable and validate subscription handshake.
**Common automation**: mailbox automation, Teams message posting, SharePoint document ops, calendar booking, user lifecycle.
**Gotchas**:
- Permissions are granular and many — app registration and admin consent are the recurring friction point
- Delegated vs application permissions are NOT interchangeable
- Tenant-specific (each client tenant = separate app registration in their tenant, or multi-tenant app with admin consent per tenant)
- Sharing / external collaboration permissions are tenant-governance settings, not API
- Beta vs v1.0 endpoints — beta is unstable; production should use v1.0
- Throttling is per-service (Outlook, SharePoint, Teams have separate budgets)
**verify-before-use**: Graph endpoint version, current required permissions for your scenarios, conditional access policies in the target tenant, throttling guidance per service.

## Slack

**Primary APIs**: Web API (HTTP), Events API (push), Socket Mode (websocket for behind-firewall), Bolt SDKs (Python / JS / Java).
**Auth**: OAuth 2.0 with workspace install; bot token vs user token; granular scopes.
**Rate limits**: Tier-based per method; webhook posts limited to 1/sec per channel.
**Common automation**: incident notification, daily digest posting, command bots, approval workflows, channel-based ticketing.
**Gotchas**:
- Bot scopes are additive only via reinstall — design the scope list with future ops in mind
- Threading — most messages should be in threads to avoid channel noise; thread parent ID is required
- `chat.postMessage` vs `chat.postEphemeral` vs incoming webhooks — different auth surfaces, different audit visibility
- Slack Enterprise Grid changes scopes (org-level vs workspace-level)
- Modal interactions have a 3-second response budget — defer with a "thinking" response
**verify-before-use**: current scope catalog, Enterprise Grid implications, rate limit tier for your method.

## GitHub

**Primary APIs**: REST API v4 (the GraphQL v4 is the preferred modern surface), Apps + Installations, OAuth, fine-grained PATs.
**Auth**: OAuth (user-scoped), GitHub App (preferred for org-scoped automation with installation tokens), fine-grained PAT (per-repo or per-org).
**Rate limits**: 5000 / hour for authenticated user; 15000 / hour for GitHub Apps; secondary rate limits for search and abuse detection.
**Webhooks**: Org-level or repo-level, signed with shared secret.
**Common automation**: PR automation, code-scan integration, branch-protection enforcement, repo provisioning.
**Gotchas**:
- GitHub App vs OAuth App — Apps are the right choice for org-scoped automation; PATs are increasingly restricted
- Fine-grained PATs replaced classic PATs; classic PATs are deprecated for new use
- Search API has its own (lower) rate limit
- Secondary rate limits (anti-abuse) can trip on bursty traffic
- `pulls` API and `issues` API overlap — PRs are issues but not all issue endpoints work on PRs
**verify-before-use**: GitHub App permission model (READ/WRITE per resource), webhook signature verification, enterprise vs cloud differences if the client uses GitHub Enterprise.

## SAP

**Primary APIs**: OData via SAP Gateway, REST via SAP Cloud Platform Integration, RFC / BAPI via SAP NetWeaver RFC SDK, Cloud APIs via SAP BTP.
**Auth**: SAML SSO, OAuth 2.0 (BTP), basic auth (on-prem common), X.509 client certs.
**Rate limits**: System-dependent; on-prem instances usually unconstrained at API level (constrained at backend processing level).
**Common automation**: master-data sync, transaction posting, document creation, workflow triggers.
**Gotchas**:
- On-prem vs S/4HANA Cloud have radically different API surfaces — verify which one
- SAP Cloud Connector required for cloud-to-on-prem
- Custom Z-fields and Z-tables are ubiquitous; reference docs cover standard fields only
- Transport requests for any configuration change
- Each client's SAP is unique in extensions; expect significant per-client work
**verify-before-use**: SAP product variant (ECC / S/4HANA on-prem / S/4HANA Cloud), licensed APIs, Cloud Connector availability.

## Workday

**Primary APIs**: REST API (newer, growing), SOAP / Web Services API (mature, broader coverage), RaaS (Report as a Service for custom reports), PECI (continuous integration).
**Auth**: OAuth 2.0 (REST), WS-Security (SOAP), Integration System User.
**Rate limits**: Tenant-level; SOAP and REST have different governance.
**Common automation**: HR data sync, org-chart sync, time-tracking, expense workflows.
**Gotchas**:
- SOAP still covers operations REST doesn't
- Workday's data model is hierarchical and time-effective ("as of date X")
- Integration System Users vs human users — must use ISU for production integrations
- "Inbound" (data into Workday) vs "outbound" (data out) have different patterns; outbound often via RaaS
- Studio (Workday's iPaaS) is a separate licensed surface
**verify-before-use**: Workday release version (semi-annual), licensed integration surfaces.

## Generic patterns

When automating across N of these:

- **Per-client tenant isolation** — see [[relatedTo::Consulting Multi-Tenancy Isolation]]. Each client tenant = separate secret store, separate audit log, separate test fixture.
- **Auth secret hygiene** — never commit tokens / keys. Centralised secret store (Vault, AWS Secrets Manager, GCP Secret Manager) per client.
- **Rate-limit handling** — exponential backoff, respect `Retry-After`, surface persistent 429s as a capacity problem, not a code problem.
- **Webhook signature verification** — every platform that signs webhooks does it slightly differently; never skip verification.
- **Audit logging** — every automation that mutates client data must log who-when-what; the client's compliance team will ask.
- **Sandbox-first** — production integrations get tested against the vendor's sandbox / dev tenant first. Salesforce especially.

## Links

- [[relatedTo::Consulting Multi-Tenancy Isolation]] — managing secrets and isolation across N client tenants
- [[uses::Client Engagement Lifecycle]] — these APIs are typically wired up in phase 3 / 4
- [[relatedTo::Contractor vs Employee Management]] — who has access to which client's tenant is a contractor-access question
