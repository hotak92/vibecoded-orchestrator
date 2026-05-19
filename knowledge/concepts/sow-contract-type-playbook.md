---
title: SOW Contract-Type Playbook
type: concept
tags:
- concept
- consulting
- contracts
- SOW
- mid-level-architecture
- best-practices
- client-management
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# SOW Contract-Type Playbook

The four primary commercial structures a consulting Statement of Work can take, the conditions under which each is appropriate, and the clauses that fail predictably when omitted. Used when drafting a new SOW, evaluating a client-proposed structure, or deciding whether to convert a phase of an engagement to a different pricing model.

## Why this matters

The contract type determines who absorbs scope-change risk. Matching the type to the actual nature of the work is the single largest controllable factor in engagement margin and client satisfaction. The wrong contract type produces predictable disputes — fixed-price on exploratory work guarantees overrun and blame; T&M without a cap guarantees runaway and client outrage; outcome-based on metrics the firm can't move guarantees zero payment. The right type makes the rest of the engagement manageable.

## The four primary types

### Time & Materials (T&M)

**When right**: Discovery-heavy work, evolving scope, client wants flexibility, established trust between client and firm.
**Risk direction**: Low to firm (gets paid for hours), high to client (no cap on spend).
**Required clauses**: rate card, billing increment, monthly soft cap, change-control for cap revision, weekly burn report cadence, explicit ramp-up assumption.
**Common pitfall**: No cap at all. Even with a contractually-clean T&M agreement, the client experiences runaway and blames the firm. Default to a monthly soft cap with explicit change-control to revise it.

### Fixed-price

**When right**: Well-defined scope, repeatable work, mature deliverable definition with objective acceptance criteria.
**Risk direction**: High to firm (absorbs overrun), low to client.
**Required clauses**: scope statement, out-of-scope list, assumptions list, acceptance criteria as objective tests, change-control process, milestone-tied payment schedule, definition of "done".
**Common pitfall**: Vague acceptance criteria → client withholds final milestone payment over disagreements. Acceptance criteria must be objective tests, not subjective satisfaction. "Reviewed and approved by stakeholder" is not an acceptance criterion; "passes the specified end-to-end test suite" is.

### Retainer

**When right**: Ongoing capacity, advisory work, fractional roles (CTO / ops / security), predictable monthly need.
**Risk direction**: Medium to firm (under-utilisation if client doesn't draw), medium to client (paying for capacity that may go unused).
**Required clauses**: monthly hour allocation, unused-hours treatment (forfeit / roll forward / refund), in-scope/out-of-scope work types, response-time SLAs (if any), term, notice period.
**Common pitfall**: Client treats retainer hours as 24/7 on-call. Define response-time SLAs explicitly or state "best-effort during business hours" — the silence on this clause is what becomes the dispute.

### Outcome-based / success fee

**When right**: Measurable business outcome (revenue, cost saved, deal closed), firm has the actual leverage to move the metric, both parties trust each other.
**Risk direction**: High to firm (no payment if outcome misses for reasons partly outside its control), low-to-medium to client.
**Required clauses**: outcome metric (objectively measurable), measurement window, base/floor fee, success-fee formula, mid-engagement scope-change handling, attribution method (how to know the outcome was caused by the firm's work, not other inputs).
**Common pitfall**: Outcome metric depends on client behaviour the firm can't control ("increase revenue 20%" when the client's sales team is the bottleneck). DECLINE outcome-based pricing if the metric isn't substantially within the firm's influence — the engagement will end with no payment and damaged trust.

### Hybrid (most common in practice)

Typical real-world pattern: fixed-price discovery phase → T&M build phase with monthly soft cap → retainer for steady-state support. Each phase is drafted separately with explicit transition criteria. The transition criterion (e.g. "discovery sign-off triggers move to build phase") is itself a contractual artefact — without it the phases bleed into one another and the commercial structure becomes ambiguous mid-engagement.

## SOW skeleton (default 15-section structure)

```
1. Parties and effective date
2. Background and objectives             ← from discovery notes
3. Scope of services                     ← what we're doing
4. Deliverables                          ← what we're producing (each objectively defined)
5. Out of scope                          ← what we're NOT doing (as important as scope)
6. Assumptions                           ← what must be true for the plan to hold
7. Client responsibilities               ← what the client provides / decides
8. Team and key personnel                ← who, named where appropriate
9. Timeline and milestones               ← phased with explicit dates or relative durations
10. Commercials                          ← rates, caps, milestone amounts, billing cadence
11. Acceptance criteria                  ← objective tests per deliverable
12. Change control                       ← how scope/budget changes happen
13. Term and termination                 ← duration, notice, for-cause vs convenience
14. IP, confidentiality, data protection ← reference MSA if it exists, default clauses if not
15. Signatures
```

For competitive proposals, prepend an executive summary (2-page max) and append credentials/case-studies.

## The dual-file output pattern

When producing an SOW draft, generate TWO files, not one:

1. `{client-slug}-sow-draft-v1.md` — the SOW itself, client-facing tone, ready (after counsel review) to send.
2. `{client-slug}-sow-internal-notes-v1.md` — internal-only: assumptions made, gaps in discovery, risk register, suggested negotiation positions, alternatives the partner might consider.

The split is structural, not stylistic. Mixing internal commentary into the client draft is how internal risk-register language ("we expect they will push back on milestone 3") leaks into a client's hands. Two files; never one.

## Critical-thinking discipline when drafting

- **Surface a wrong-type request explicitly** — when the client asks for fixed-price but discovery notes describe evolving exploratory scope, do not silently draft the fixed-price. Surface the mismatch; propose T&M with a fixed-price discovery phase.
- **Refuse to fabricate commercials** — `[CONFIRM: monthly cap]` placeholder is better than a guessed number. Numbers in SOW drafts tend to survive into signed contracts unchanged.
- **Mark every estimate** — `[ESTIMATE: ...]` so the partner adjusts before signing. Estimates that look like commitments become commitments.
- **Reference the MSA if it exists** — don't redraft clauses already covered by the master agreement. Pointer is sufficient; duplication creates inconsistency risk.
- **Flag unenforceable terms** — "client will provide data in a clean format" without defining "clean" is a fight waiting to happen. Replace with specifics or move to assumptions.

## Change-control weight

The change-control clause is the most-used clause in any active engagement and the most-skipped clause in drafts. Allocating it a single sentence ("scope changes require written approval") guarantees disputes. Required sub-clauses:

- Who can authorise changes (named role, not "the client")
- What constitutes a change (delta from scope, deliverables, or assumptions)
- How changes are quantified (incremental cost, schedule impact)
- How changes are documented (signed change order vs email confirmation)
- Default disposition of disputed changes (continue on prior scope until resolved)

Without these, every meaningful disagreement becomes a renegotiation, not a change order.

## Anti-patterns

- ❌ Drafting an SOW from a one-paragraph discovery summary — push back and ask for input
- ❌ Mixing internal commentary into the client-facing draft
- ❌ Boilerplate IP / confidentiality clauses without flagging that counsel must review
- ❌ Treating change-control as a one-sentence clause
- ❌ Estimating duration / cost when discovery notes don't justify the number
- ❌ Outcome-based pricing on metrics outside the firm's influence

## Links

- [[implements::Client Engagement Lifecycle]] — SOWs are drafted in phase 2 and reviewed at phase boundaries
- [[relatedTo::Consulting Deliverable Skepticism]] — refuse-to-fabricate is the cross-cutting discipline this playbook applies
- [[relatedTo::Technical Debt Accounting]] — debt produced during a fixed-price build is the firm's commercial problem
- [[relatedTo::Technical Due Diligence Framework]] — DD findings feed scope statements and assumptions
