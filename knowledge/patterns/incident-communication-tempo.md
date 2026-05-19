---
title: Incident Communication Tempo
type: pattern
tags:
- pattern
- consulting
- incident-response
- communication
- post-mortem
- mid-level-architecture
- best-practices
- SRE
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Incident Communication Tempo

The cadence and artefact set that keeps an incident response coordinated across stakeholders without forcing engineers out of the technical work. Used during active client-facing incidents and during the post-incident review (PIR) that follows. Equally applicable to internal production incidents, but written with consulting engagements in mind because that adds the client-tenant boundary as an extra constraint.

**Scope vs sibling nodes**: this node covers the *communications* stream that runs in parallel with the technical response. The 4-phase engineering SOP (triage → hypothesis → mitigate → verify) lives in [[relatedTo::SRE Incident Response Playbook]]. How to write the post-mortem document itself lives in [[relatedTo::Postmortem Authoring Discipline]]. The cultural framing of blamelessness lives in [[relatedTo::Blameless Postmortem Methodology]].

## Why this matters

When production breaks, engineers must stay in the technical work. If they also have to draft client comms, status-page entries, internal pings, and the PIR, the incident lasts longer and the comms quality drops. The communications stream has its own tempo and structure; running it in parallel to the technical response is what keeps both effective.

## Three phases, three cadences

The communication tempo is phase-dependent. The phases are not arbitrary labels; each phase has a different audience expectation.

### Active phase
Incident in progress, engineers actively responding.

- **Internal updates**: every 30 minutes
- **Client-facing updates**: every 60 minutes
- **Status page**: updated when state changes (not on a clock — on each meaningful transition)

The asymmetry matters. Internal stakeholders need higher tempo because they're making staffing and escalation decisions in real time; clients need lower tempo because what they need is "still being worked, here's what we know now, next update at X".

### Stabilising phase
Workaround in place OR root cause identified, fix in flight. The crisis is bounded.

- Cadence relaxes to every 2-4 hours
- Communication shifts to "what we know, what we're doing, when we'll know more"
- Premature "resolved" framing is the failure mode

### Review phase
Resolved. Cadence becomes the PIR document itself — see below.

## The four parallel artefacts (active / stabilising phases)

Communication happens across four channels with different audiences and different content depth. Inconsistency between them is a credibility gap; the same state must be visible at each, just at different levels of detail.

### 1. Internal war-room update
For firm leadership and on-call coordinators. Full detail.

Fields: incident ID, client, phase, symptom (1-2 sentences, externally observable), affected systems/users/regions, current state, lead responder, cause hypothesis (with confidence level), actions in flight (with owners), next update time, last client comms sent, SLA exposure.

### 2. Client-facing update
For the client's named contacts. Tone: informative, honest, no speculation, no blame, no engineering jargon.

Fields: what's being worked on (verb), what's confirmed, what's being done, next update by time, urgent-questions contact.

Critical rule: **stick to confirmed facts and named actions**. "We believe it's a database issue" becomes "they confirmed a database issue" in the client's retelling.

### 3. Status page entry (if applicable)
Public-facing. Shorter than client comms. Tense matches state (Investigating / Identified / Monitoring / Resolved).

Brief non-technical description of the affected component plus what engineers are doing and the time of the next update. No jargon, no internal attribution.

### 4. Internal stakeholder ping
For the partner / CEO / commercial owner of the client account. Decision-oriented.

Fields: client, severity guess, phase, brief affected scope, lead responder, risk-to-relationship rating with reason, action needed from the recipient (most common: "nothing right now; will update").

## The cross-channel consistency principle

At any given moment, the four artefacts must say the same thing about state. Different levels of detail are fine; different facts are not. The most common failure mode is the status page lagging behind the war-room update — clients reading the public page see a different state than they hear from their account contact. The coordination role's job is keeping the four in sync.

Detail-level ordering (least to most detailed):
1. Status page (shortest, public, no internal jargon)
2. Client email update (more detail, named recipient, no NDA-protected technical detail)
3. Slack to client (same content as email but threaded)
4. Internal Slack war-room (full detail, attributed)

## Post-incident review (PIR) structure

When the incident resolves, the PIR is the artefact that determines whether the next incident with the same root cause is prevented. Required sections:

- **Summary** — 3-4 sentences for a non-technical reader
- **Customer impact** — users affected (count or %), functionality lost, duration of degradation, data loss assessment (none / bounded / unbounded with specifics)
- **Timeline** — UTC timestamped events
- **Root cause** — the cause, not the symptom; multiple causes if a chain
- **Contributing factors** — things that made it worse or made detection slower (not framed as blame)
- **What went well** — at least one; genuine retention requires acknowledging recovery wins
- **What didn't go well** — candid; includes the firm's actions, not just the client's environment
- **Action items** — owner / action / due / status
- **Lessons / pattern** — 2-4 sentences capturing the generalisable lesson (future-self optimisation)
- **Communication review** — was external comms good? Where did it lag? Template changes suggested?

## Blameless discipline

The PIR is BLAMELESS in language. Write "alert routing did not page the on-call" not "Maria didn't see the alert". The blameless framing is not just culture — it's the only way to get honest contribution to the next PIR. The moment one PIR names a human, every subsequent PIR is sanitised by the people closest to the facts.

**"Human error" is never a root cause.** The system that allowed the human action to cause the incident is the cause. If a junior engineer ran `DROP TABLE` in production, the cause is "production write access available to junior role with no second-pair check", not "the junior engineer".

## Refuse-and-redirect cases

- **Refuse to draft external comms without a confirmed symptom** — premature comms are worse than slightly delayed comms.
- **Refuse to declare "resolved" while root cause is unknown** — correct phrase is "stable on workaround, monitoring", not "resolved".
- **Refuse to mix credit / SLA math into client-facing updates** — credits are a separate commercial conversation post-resolution. Mentioning credits during the incident invites scope-shifting.
- **Refuse to assign blame in PIR** — see above.

## Contractual SLA awareness

If the engagement has notification SLAs (e.g. "notify the client within 1 hour of detection"), they must be tracked during the active phase. Missing a contractual notification SLA is itself a finding for the PIR. The coordination role surfaces SLA risk to the war room before it's breached, not after.

## Lessons-to-knowledge pipeline

Incidents that don't generate KG nodes recur. After every PIR, the "Lessons / pattern" section becomes a candidate KG node — captured before the incident's specifics fade from memory. This is the discipline that makes a portfolio safer year-over-year rather than relearning the same lessons.

## Anti-patterns

- ❌ Speculation in client comms ("we think it's the database")
- ❌ Blame-language in PIR ("Alice deployed without testing")
- ❌ Calling "resolved" when the workaround is fragile
- ❌ Mixing credit-and-comms in the same update
- ❌ Omitting "what went well" out of false humility (no one contributes to the next PIR if it's punishment)
- ❌ Letting status page lag behind internal updates
- ❌ Engineers drafting comms while also debugging

## Links

- [[implements::Client Engagement Lifecycle]] — incidents occur in phase 4 (build/delivery) and phase 6 (warranty)
- [[relatedTo::Consulting Multi-Tenancy Isolation]] — client comms during incidents must respect the same channel-isolation discipline
- [[relatedTo::Consulting Deliverable Skepticism]] — refuse-to-speculate is one instance of the cross-cutting skepticism posture
- [[relatedTo::Technical Debt Accounting]] — discovered-class debt frequently surfaces during incidents
