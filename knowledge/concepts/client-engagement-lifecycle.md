---
title: Client Engagement Lifecycle (Consulting)
type: concept
tags:
- concept
- consulting
- workflow
- mid-level-architecture
- client-management
- contracts
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Client Engagement Lifecycle (Consulting)

The end-to-end phases a consulting engagement moves through, and the artefacts / decisions / risks specific to each phase. Used as scaffolding by `[[uses::consulting-sow-drafter]]`, `[[uses::consulting-cto-portfolio-coordinator]]` and `[[uses::consulting-portfolio-status]]`.

## Why this matters

Engagements fail in characteristic ways at specific phases. Naming the phase the engagement is in makes the next-most-likely failure mode predictable, and lets the same status vocabulary be reused across the portfolio.

## Phases

### Phase 0: Lead / qualification
Pre-engagement. The buyer is exploring; the firm is qualifying whether to pursue. No commitment.

**Artefacts**: meeting notes, internal "go / no-go" memo.
**Typical duration**: 1-3 weeks.
**Failure mode**: pursuing every lead → discovery hours spent on opportunities that won't close.
**Exit criterion**: explicit decision to invest in discovery, OR explicit decision to pass.

### Phase 1: Discovery
Paid or unpaid scoping. The goal is to produce enough understanding to write a defensible SOW.

**Artefacts**: discovery notes, problem statement, stakeholder map, current-state architecture sketch, indicative cost+duration range.
**Typical duration**: 1-4 weeks.
**Failure mode**: skipping discovery → fixed-price SOW signed on a sales-deck-level understanding, overruns guaranteed.
**Exit criterion**: discovery brief signed off internally; SOW draftable.

### Phase 2: SOW negotiation
Drafting and negotiating the contract.

**Artefacts**: SOW draft, internal risk register, redlined client version, signed SOW.
**Typical duration**: 2-8 weeks (enterprise procurement is the long tail).
**Failure mode**: starting work before signature → no contractual basis if dispute arises; also commits team capacity that's not contractually paid for.
**Exit criterion**: SOW signed; PO issued; kickoff date set.

See [[uses::consulting-sow-drafter]] for the drafting playbook.

### Phase 3: Kickoff
First 1-2 weeks after signature.

**Artefacts**: kickoff deck, project plan, comms cadence agreement, escalation matrix, access provisioning checklist (VPN, repos, ticketing, calendars), team introduction.
**Typical duration**: 1-2 weeks.
**Failure mode**: access lag → team is paid, can't work; budget burns without output. Surface within the first 3 business days.
**Exit criterion**: all team members have full access; first sprint / first deliverable has started.

### Phase 4: Build / delivery
The bulk of the engagement.

**Artefacts**: weekly status notes, sprint reviews, deliverables per SOW, change requests as needed, risk register kept current.
**Typical duration**: weeks to months depending on contract.
**Failure modes**:
- Scope creep without change-control → fixed-price overrun
- Stakeholder turnover at client → priorities shift mid-flight
- Single-point-of-failure on engagement team → bus-factor 1
- Wishful status reporting → green-green-green-red-red

**Exit criterion**: deliverables accepted; pre-handover stable for N weeks.

### Phase 5: Handover
Transferring ownership to the client (or to a steady-state retainer phase).

**Artefacts**: runbooks, architecture docs (current state, not just initial design), credential rotation, ticket-system handover, key-person knowledge transfer sessions, sign-off memo.
**Typical duration**: 2-4 weeks. Compresses badly — don't.
**Failure mode**: skipped or rushed handover → client unable to operate; warranty-period firefights at firm's cost; relationship damage.
**Exit criterion**: handover sign-off from client; warranty / hypercare period agreed.

### Phase 6: Warranty / hypercare
Defined window (typically 30-90 days) during which the firm fixes defects against the delivered scope at no charge.

**Artefacts**: defect log, incident records, root-cause notes.
**Typical duration**: per SOW; 30 / 60 / 90 days are common.
**Failure mode**: ambiguous "defect vs change request" distinction → unpaid work creeps. Define defect in the SOW.
**Exit criterion**: warranty period elapses with no open defects; engagement closes.

### Phase 7: Retention / steady state
Optional. The engagement converts to a retainer, a follow-on SOW, or ends.

**Artefacts**: retention review, expansion proposal if applicable, case study / reference request, off-boarding if ending.
**Failure mode**: ending hard with no warm handoff to future-self → losing the relationship and the reference.
**Exit criterion**: either retainer signed, follow-on engagement started, or engagement closed with documented references and lessons.

## Portfolio implications

Each engagement on the firm's portfolio is in exactly ONE phase. The phase determines:

- Which risks dominate (discovery risk is different from delivery risk is different from handover risk)
- Which status questions matter (discovery: "do we understand the problem?"; delivery: "are we on plan?"; handover: "is the client ready to operate?")
- Which commercial pattern is normal (discovery: low burn; delivery: peak burn; warranty: zero or sunk-cost burn)

The `[[uses::consulting-portfolio-status]]` skill rolls phase into the portfolio digest so portfolio-level imbalances are visible (e.g. "70% of engagements are in delivery phase, no new discovery for 6 weeks → pipeline risk in Q+1").

## Anti-patterns

- ❌ Treating discovery as a free sales activity → firm absorbs cost; quality suffers
- ❌ Starting build before signed SOW → no contractual basis
- ❌ Skipping handover → warranty-period firefights at firm's cost
- ❌ Ending without retention conversation → losing case study and reference

## Links

- [[uses::consulting-sow-drafter]] — drafts SOWs in phase 2
- [[uses::consulting-portfolio-status]] — rolls up phases across portfolio
- [[uses::consulting-incident-coordinator]] — handles incidents during phase 4 / 6
- [[implements::Technical Debt Accounting]] — debt accrues in build, has to be priced in handover
- [[relatedTo::Consulting Multi-Tenancy Isolation]] — phase 3 / 4 access provisioning depends on the isolation model
