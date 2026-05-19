---
title: Technical Due Diligence Framework
type: pattern
tags:
- pattern
- consulting
- due-diligence
- M&A
- vendor-evaluation
- mid-level-architecture
- best-practices
- risk-assessment
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Technical Due Diligence Framework

A structured methodology for evaluating a third party (vendor, acquisition target, strategic partner) on technical and operational dimensions, producing a defensible recommendation that holds up in a board meeting and that counsel + finance can consume as input to their parallel streams.

## Why this matters

Technical DD findings drive 6-7 figure decisions. The frequent failure modes are not "missed a bug" but "produced a confident PROCEED recommendation that the evidence didn't support" or "produced a 40-page report that the buyer could not use because it was a checklist disconnected from this target's specifics". A reusable framework prevents both failures: it forces the eight standard dimensions to be considered, and it enforces a recommendation that is traceable to specific findings.

## Scope modes

DD is not one activity. Three distinct shapes share the framework but differ in emphasis:

- **Vendor evaluation** — evaluating a SaaS / platform vendor before signing. Focus: SOC 2 / ISO 27001 posture, data residency, sub-processors, API stability, exit/portability, financial viability, support model.
- **Acquisition target** — focus: code quality, architecture, team retention, IP cleanliness, tech-debt valuation, hidden licensing risk, integration feasibility.
- **Strategic partnership** — focus: API compatibility, security alignment, joint operational model, client-fit, exit/divorce mechanics.

The scope shapes which dimensions get weight, not which dimensions get omitted.

## Depth tiers (and the honesty discipline)

Depth determines time investment AND the credibility of the conclusion. The framework defines three tiers:

- **Quick** — half-day, desk research + 1-2 interviews, ~5-page report.
- **Standard** — 3-5 days, data-room review + 5-10 interviews, ~15-page report.
- **Deep** — 2-4 weeks, full data room, code review, security testing, ~40-page report.

**Critical discipline**: never produce a "deep" report from "quick" inputs. If access is restricted to public information, the report is a quick public-sources assessment regardless of how much time was spent on it. Calibrated depth = trustworthy depth. A confident recommendation off thin evidence is the dangerous outcome the framework exists to prevent.

## The eight dimensions

Every DD assesses all of these unless scope explicitly excludes one. The 8 are the minimum complete surface — gaps in any of them are themselves a finding.

### 1. Architecture
High-level system diagram (or its absence — itself a finding); stack age (frameworks >2 major versions behind = risk); coupling and modularity; scalability proof points (actual production load, not marketing claims); multi-tenancy model where applicable.

### 2. Security
Compliance certifications and currency (SOC 2 Type II, ISO 27001, HIPAA, PCI-DSS as relevant); recent pen test (date, scope, findings status); vulnerability management (CVE response time, patch cadence); data classification and encryption (at rest, in transit, in use); access controls (RBAC, SSO/SAML, audit logging); incident history (number, severity, response timeline); supply chain (sub-processor list, SBOM availability).

### 3. Reliability & operations
Uptime track record (last 12 months SLA evidence, not just SLA promise); on-call rotation and incident response process; observability (logging, metrics, tracing in place); DR / BCP plan with last successful test date; deployment frequency and rollback capability.

### 4. Code quality (acquisition primarily)
Test coverage (line and branch); CI/CD maturity; open issue / bug backlog age distribution; tech-debt areas (specific files / modules / services); dependency hygiene (outdated, abandoned, security-flagged); license cleanliness (no GPL contamination in proprietary code, no SaaS-incompatible licenses).

### 5. Team & retention
Engineering org size and shape (IC / lead / manager ratio); tenure distribution (red flag: founder-only knowledge holders); recent attrition (volume + reasons); hiring pipeline and velocity; key-person dependency (single points of failure).

### 6. Product & roadmap
Customer concentration (top 10 customers as % of revenue); roadmap public vs internal (alignment or divergence); feature velocity trend; customer satisfaction signals (NPS, churn, expansion rate if available).

### 7. Commercial & legal
Recurring revenue / churn / margin; customer contracts (assignment clauses — M&A blocker risk); open litigation; IP ownership (founders' prior employer IP risk); open source compliance posture.

### 8. Cultural & alignment (partnership / acquisition)
Engineering practices alignment (PR review, testing, release process); communication / decision-making patterns; time zone and language overlap.

## Recommendation discipline

The framework requires exactly one of three recommendations, each with a defensible link to findings:

- **PROCEED** — findings support moving forward; no material unresolved risks.
- **PROCEED WITH CONDITIONS** — findings support moving forward IF named conditions are met (contractual asks, technical asks, or both). Each condition is tied to a specific finding.
- **DO NOT PROCEED** — findings are sufficiently adverse that conditions can't fix them within the deal's time / cost envelope.

If 8 findings are amber and 2 are red, "PROCEED" is not defensible. Match the recommendation to the evidence; if the buying side is enthusiastic and the findings disagree, the report's job is to surface the disagreement, not paper it over.

## The "methodology and limits" section (mandatory)

Every DD report ends with an explicit section listing:

- Time spent (hours / days)
- Sources accessed (data room sections, interviews, public sources)
- **What we COULDN'T see** — explicit list

This last item is the framework's main protection. A DD report that claims to know everything is more dangerous than one that lists what it couldn't see. When the deal goes badly, the methodology section is the artefact that distinguishes "the consultant missed it" from "the consultant flagged that this couldn't be assessed with the access provided".

## Refuse-and-redirect cases

The framework explicitly defines situations where the right output is NOT a full DD report:

- **Insufficient access for the requested depth** → produce a quick report explicitly, plus a list of access requests for moving to standard / deep.
- **Target is direct competitor of an existing client** → stop, flag conflict-of-interest to the partner, do not draft. CoI is structurally disqualifying.
- **Decision already made** ("we're signing tomorrow, just give me cover") → produce a risk register, not a recommendation. Naming the output correctly is the framework's protection against being used as deal cover.

## Two-file output

DD produces two files:

1. `{target-slug}-dd-report-v1.md` — the report itself, including the recommendation, findings, conditions, and methodology / limits sections.
2. `{target-slug}-dd-evidence-v1.md` — sourced evidence backing each finding (file references, paraphrased interview quotes, document IDs).

The evidence file is the audit trail. Counsel and finance use it to seed their parallel streams; if the deal goes badly later, the evidence file is what justifies the recommendation that was made on the information that was available.

## Critical-thinking checks

- **Distinguish signal from marketing** — vendors present polished decks. Anchor findings on artefacts (audit reports, code, dashboards, contracts), not on vendor claims.
- **Don't grade on a curve** — every dimension has its own bar; weak in one dimension shouldn't be averaged against strong in another. Surface each dimension independently; let the reader weight.
- **Surface conflicts of interest** — if the firm has a relationship with a competitor of the target, name it for the partner before producing the recommendation.
- **Time-box honestly** — calibrated depth is the protection against false confidence.

## Anti-patterns

- ❌ Recommending PROCEED with 3 red findings unaddressed
- ❌ Treating sales-deck claims as findings
- ❌ Producing a generic checklist disconnected from this target's specifics
- ❌ Hiding the "things we couldn't see" section
- ❌ Letting the commercial enthusiasm of the buying side bias the framing
- ❌ Producing a "deep" report from "quick" inputs

## Links

- [[relatedTo::Enterprise SaaS Automation Surfaces 2026]] — vendor DD often targets one of these platforms
- [[relatedTo::Consulting Multi-Tenancy Isolation]] — multi-tenancy model is a dimension-1 finding for SaaS targets
- [[relatedTo::SOW Contract-Type Playbook]] — DD conditions feed into SOW assumptions and out-of-scope items
- [[relatedTo::Consulting Deliverable Skepticism]] — the "what we couldn't see" discipline is one instance of the cross-cutting skepticism posture
