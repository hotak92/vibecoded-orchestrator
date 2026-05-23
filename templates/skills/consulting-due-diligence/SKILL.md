---
name: consulting-due-diligence
description: Runs structured technical due diligence on a vendor, acquisition target, or strategic partner; produces a risk-ranked report with findings, evidence, and a recommendation
keywords: [technical due diligence, vendor DD, acquisition target, strategic partner, risk-ranked report, M&A evaluation]
model: opus
effort: high
argument-hint: "[target-name] [--scope vendor|acquisition|partnership] [--depth quick|standard|deep]"
---

# Consulting Technical Due Diligence

Structured technical assessment producing a defensible recommendation, used for vendor selection, M&A target evaluation, and strategic partnership decisions. The deliverable holds up in a board meeting and gives counsel/finance the technical inputs they need for their parallel diligence streams.

**Model**: Opus — DD findings drive 6-7 figure decisions and require tradeoff reasoning across security, architecture, team, and commercials.

## When this skill auto-invokes

- "Due diligence on `<vendor>`"
- "Evaluate `<company>` as acquisition target"
- "Assess `<partner>`'s tech stack before signing"
- "Vendor selection between `<A>` and `<B>`"
- "Technical risk review of `<target>`"

## When NOT to use

- Buying a $200/mo SaaS tool (a checklist is enough)
- Routine engineering interviews (different process)
- After the deal is signed (use post-mortem instead)
- When you don't have access to the target (skill needs evidence, not speculation)

## Scope modes

### `--scope vendor`
Evaluating a SaaS or platform vendor before signing.
**Focus**: SOC 2 / ISO 27001 posture, data residency, sub-processors, API surface stability, exit/portability, financial viability, support model.

### `--scope acquisition`
Evaluating an acquisition target.
**Focus**: code quality, architecture, team retention, IP cleanliness, tech debt valuation, hidden licensing risk, ability to integrate.

### `--scope partnership`
Evaluating a strategic technology or co-delivery partner.
**Focus**: API compatibility, security alignment, joint operational model, client-fit, exit/divorce mechanics.

## Depth modes

- `--depth quick` — half-day, desk research + 1-2 interviews, ~5 page report
- `--depth standard` — 3-5 days, data room review + 5-10 interviews, ~15 page report (default)
- `--depth deep` — 2-4 weeks, full data room, code review, security testing, ~40 page report

## Required inputs

NEEDS:
- Target identity (legal name, primary product, geography)
- Scope (vendor / acquisition / partnership)
- The commercial question being answered (sign / not sign, buy / not buy, partner / not partner)
- Access mode (data room access, NDA in place, direct interviews available, etc.)

SHOULD have:
- Prior assessments (auditor reports, pen test results, customer references)
- Comparator data (if benchmark to alternatives is needed)
- Internal red lines (deal-killer criteria the firm has decided in advance)

If access is "public information only", make that explicit in the report and DOWNGRADE depth accordingly. A "deep" report from public sources alone is misleading.

## Diligence dimensions

The skill assesses ALL of these (unless scope explicitly excludes):

### 1. Architecture
- High-level system diagram (or absence thereof — itself a finding)
- Stack age (frameworks > 2 major versions behind = risk)
- Coupling and modularity
- Scalability proof points (actual production load, not marketing claims)
- Multi-tenancy model (if applicable) — see `consulting-multi-tenancy-isolation` KG node

### 2. Security
- Compliance certifications and currency (SOC 2 Type II, ISO 27001, HIPAA, PCI-DSS as applicable)
- Recent pen test (date, scope, findings status)
- Vulnerability management (CVE response time, patch cadence)
- Data classification and encryption (at rest, in transit, in use)
- Access controls (RBAC, SSO/SAML, audit logging)
- Incident history (number, severity, response timeline)
- Supply chain — sub-processor list, SBOM availability

### 3. Reliability & operations
- Uptime track record (last 12 months SLA evidence, not just SLA promise)
- On-call rotation and incident response process
- Observability (logging, metrics, tracing in place)
- DR / BCP plan with last successful test date
- Deployment frequency and rollback capability

### 4. Code quality (acquisition only)
- Test coverage (line and branch)
- CI/CD maturity
- Open issue / bug backlog age distribution
- Tech debt areas (specific files / modules / services)
- Dependency hygiene (outdated, abandoned, security-flagged)
- License cleanliness (no GPL contamination in proprietary code, no SaaS-incompatible licenses)

### 5. Team & retention
- Engineering org size and shape (IC / lead / manager ratio)
- Tenure distribution (red flag: founder-only knowledge holders)
- Recent attrition (volume + reasons)
- Hiring pipeline and velocity
- Key-person dependency (single points of failure)

### 6. Product & roadmap
- Customer concentration (top 10 customers as % of revenue)
- Roadmap public vs internal (alignment / divergence)
- Feature velocity trend
- Customer satisfaction signals (NPS, churn, expansion rate if available)

### 7. Commercial & legal
- Recurring revenue / churn / margin
- Customer contracts — assignment clauses (M&A blocker risk)
- Open litigation
- IP ownership (founders' prior employer IP risk)
- Open source compliance posture

### 8. Cultural & alignment (partnership / acquisition)
- Engineering practices alignment (PR review, testing, release process)
- Communication / decision-making patterns
- Time zone and language overlap

## Output format

Two files:

1. `{target-slug}-dd-report-v1.md` — the report itself
2. `{target-slug}-dd-evidence-v1.md` — sourced evidence backing each finding (file references, interview quotes paraphrased, document IDs)

### Report structure

```markdown
# Technical Due Diligence: {Target}
**Scope**: {vendor|acquisition|partnership}
**Depth**: {quick|standard|deep}
**Date**: {iso-date}
**Prepared for**: {who}
**Access mode**: {data room | public sources | hybrid}

## Recommendation
**{PROCEED | PROCEED WITH CONDITIONS | DO NOT PROCEED}**

{2-3 sentences justifying}

## Conditions (if any)
- {numbered conditions, each tied to a finding}

## Top 5 risks
1. **{Risk}** — Likelihood: {L/M/H}, Impact: {L/M/H}, Source: {finding ref}
   {2 sentences}

## Findings by dimension
### Architecture
- Finding A1: {fact} → {risk/strength interpretation}
...

(repeat for each dimension above)

## Open questions
- {what couldn't be answered with current access}

## Comparator (if applicable)
{brief table vs alternatives}

## Methodology and limits
- Time spent: {hours/days}
- Sources accessed: {data room sections | interviews | public sources}
- What we COULDN'T see: {explicit list — protects the recommendation if it turns out wrong}
```

## Critical thinking required

- **Recommendation must follow from findings** — if 8 findings are amber and 2 are red, "PROCEED" is not defensible. Match the recommendation to the evidence.
- **Surface the unknowns** — a DD report that claims to know everything is more dangerous than one that lists what it couldn't see. The "methodology and limits" section is mandatory.
- **Distinguish signal from marketing** — vendors will present polished decks. Anchor findings on artefacts (audit reports, code, dashboards, contracts), not on claims.
- **Time-box and label depth honestly** — if you spent half a day, don't produce a "deep" report. Calibrated depth = trustworthy depth.
- **Flag conflicts of interest** — if the firm has a relationship with a competitor of the target, surface it for the partner.

## Refuse-and-redirect cases

- **Insufficient access for the requested depth** → produce a quick report explicitly, and a list of access requests for moving to standard/deep.
- **Target is direct competitor of an existing client** → stop, flag CoI to partner, do not draft.
- **Decision already made** ("we're signing tomorrow, just give me cover") → produce a risk register, not a recommendation. Naming it correctly is the skill's protection.

## Knowledge graph integration

```bash
hybrid_search("technical due diligence framework")
hybrid_search("vendor risk assessment")
hybrid_search("M&A integration patterns")
hybrid_search("enterprise SaaS automation surfaces")
```

After producing the report, if a novel risk pattern was identified, write it to `knowledge/patterns/` so the next DD can reuse it.

## Anti-patterns

- ❌ Recommending PROCEED with 3 red findings unaddressed
- ❌ Treating sales-deck claims as findings
- ❌ Producing a generic checklist disconnected from this target's actual data
- ❌ Hiding the "things we couldn't see" section
- ❌ Letting the commercial enthusiasm of the buying side bias the framing

## Success criteria

- The recommendation is defensible if the deal turns out badly — the report shows what was known, what wasn't, and on what basis the recommendation was made
- Each finding has a source the reader can verify
- Counsel and finance can use the evidence file to seed their parallel streams
- Conditions (if any) are actionable contractual or technical asks, not vague aspirations
