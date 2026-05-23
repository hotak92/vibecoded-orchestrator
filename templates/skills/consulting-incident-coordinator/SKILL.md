---
name: consulting-incident-coordinator
description: Coordinates multi-channel incident response for a consulting engagement - drafts war-room updates, status-page entries, client comms, and the post-incident review; use when a client production issue is active or just resolved
keywords: [client incident, war-room update, client comms, post-incident review, consulting incident, blameless post-mortem, post-mortem, blameless]
model: opus
effort: high
argument-hint: "[incident-slug-or-description] [--phase active|stabilising|review]"
---

# Consulting Incident Coordinator

Runs the communication and documentation tempo of an incident on a client engagement so the responding engineers can stay in the technical work. Produces the artefacts that a consulting CTO needs to (a) keep the client informed without lying, (b) keep internal stakeholders informed without leaking client confidential detail, and (c) produce a post-incident review the client will accept and learn from.

## When this skill auto-invokes

- "Client production is down, what do we say?"
- "Draft incident comms"
- "Post-mortem for `<incident>`"
- "We're in a war room, help me run the cadence"

## When NOT to use

- The incident is in another firm's production and you have no role (don't draft for clients you don't serve)
- The cause is sensitive personnel matter (HR, not incident coordination)
- The "incident" is a feature complaint (use product feedback, not incident response)

## Phases

### `--phase active`
Incident is in progress. Engineers are responding. Communication tempo: every 30 min internal, every 60 min client-facing, status page update when state changes.

### `--phase stabilising`
Workaround in place or root cause identified, fix in flight. Communication shifts to "what we know, what we're doing, when we'll know more". Cadence relaxes to every 2-4 hours.

### `--phase review`
Resolved. Producing the post-incident review (PIR / post-mortem). Cadence is the document.

## Required inputs

NEEDS:
- Client name + engagement context
- Brief description of symptom (what users / systems are affected)
- Current understanding of cause (even if "unknown")
- Lead responder name (internal — for war-room attribution)
- Whether the client has a status page or public-facing comms expectation

SHOULD have:
- Timeline of detection and key events so far
- Prior similar incidents (for pattern recognition)
- Contractual SLA / SLO commitments (for credit calculations later)
- The client's named technical / business contact for this incident

If symptom isn't described, the skill REFUSES to draft external comms — premature comms are worse than slightly delayed comms.

## Communication artefacts produced

### 1. Internal war-room update
For the firm's leadership and on-call coordinators.

```markdown
## Incident {id} — {client} — {phase} — {iso-time}

**Symptom**: {1-2 sentences, externally observable}
**Affected**: {systems / users / regions}
**Current state**: {1 sentence}
**Lead responder**: {name}
**Cause hypothesis** (confidence: low/med/high): {if any}
**Actions in flight**: {bullet list with owner}
**Next update**: {iso-time}
**Client comms sent**: {timestamp of last, channel}
**SLA exposure**: {credit risk or "below threshold"}
```

### 2. Client-facing update
For the client's named contacts. Tone: informative, honest, no speculation, no blame.

```
Subject: [{client-id}] Production Incident — Update {n} — {iso-time}

We are continuing to {action} the {brief symptom} affecting {scope}.

Current status: {1-2 sentences, externally observable facts only}
What we know: {what's been confirmed}
What we're doing: {without engineering jargon}
Next update by: {iso-time}

For urgent questions: {contact + channel}
```

### 3. Status page entry (if applicable)
Public-facing. Shorter than client comms. Tense matches state.

```
[Investigating | Identified | Monitoring | Resolved] — {component}

We are aware of an issue affecting {brief, non-technical}. Engineers are
{verb}. We will update by {iso-time}.
```

### 4. Internal stakeholder ping
For the partner / CEO / commercial owner of the client account.

```
{client} incident — {severity guess} — {phase}.
Affected: {brief}. Lead: {name}. Risk to relationship: {low/med/high} because {reason}.
Action needed from you: {nothing | call client lead | escalate to MD}.
```

## Post-incident review (PIR)

When `--phase review`, produce a PIR document that includes:

```markdown
# Post-Incident Review: {brief}
**Client**: {name}
**Incident date**: {iso}
**Severity**: {SEV-N}
**Duration**: detection to resolution = {hh:mm}
**Authors**: {names}
**Status**: draft | reviewed-internal | reviewed-with-client | final

## Summary
{3-4 sentences for a non-technical reader}

## Customer impact
- Users affected: {count or %}
- Functionality lost: {list}
- Duration of degradation: {start - end}
- Data loss: {none | bounded | unbounded — with detail}

## Timeline
| Time (UTC) | Event |
|---|---|
| {t} | {what happened} |

## Root cause
{the cause, not the symptom; multiple causes if a chain}

## Contributing factors
{things that made it worse or made detection slower; explicitly not "blame" framing}

## What went well
{at least one. genuine retention requires acknowledging recovery wins.}

## What didn't go well
{candid; the firm's actions too, not just the client's environment}

## Action items
| Owner | Action | Due | Status |
|---|---|---|---|
| {name} | {specific verb} | {iso-date} | open |

## Lessons / pattern
{2-4 sentences capturing the generalisable lesson — what we want future-self to know}

## Communication review
{Was external communication good? Where did it lag? What template changes does this suggest?}
```

The post-mortem is BLAMELESS in language: write "alert routing did not page the on-call", not "Maria didn't see the alert". The blameless framing is the only way to get honest contribution to the next one.

## Critical thinking required

- **Refuse to speculate in client-facing comms** — "we believe it's a database issue" turns into "they confirmed a database issue" in the client's retelling. Stick to confirmed facts and named actions.
- **Refuse to assign blame in PIR** — "human error" is never a root cause. The system that allowed the human action to cause the incident is the cause.
- **Push back on premature "resolved" declarations** — if the workaround is in place but root cause is unknown, the right phrase is "stable on workaround, monitoring", not "resolved".
- **Separate SLA / credit math from comms** — the client-facing update is not the place to discuss credits. That's a separate commercial conversation post-resolution.
- **Surface contractual obligations** — if the engagement has notification SLAs (e.g. "notify within 1 hour of detection"), check whether they've been met and flag if not.

## Multi-channel orchestration

When the user is running comms across multiple channels (status page + Slack + email + phone), the skill produces consistent content for each. The principle:

- Status page: shortest, public, no internal jargon
- Email update: more detail, named recipient, no NDA-protected technical detail
- Slack (internal): full detail, attributed
- Slack (shared with client): same as email but threaded

Do not let inconsistency between channels create a credibility gap. The skill checks that all four artefacts say the same thing about state, even if at different levels of detail.

## Knowledge graph integration

```bash
hybrid_search("incident response patterns")
hybrid_search("blameless post-mortem")
hybrid_search("SLA SLO management")
```

After a PIR is finalised, suggest writing the lesson as a KG node — incidents that don't generate KG nodes will recur.

## Anti-patterns

- ❌ Speculation in client comms ("we think it's the database")
- ❌ Blame-language in PIR ("Alice deployed without testing")
- ❌ Calling resolved when workaround is fragile
- ❌ Mixing credit-and-comms in the same update
- ❌ Missing the "what went well" section out of false humility

## Success criteria

- Client receives accurate, timely updates and does not have to ask for them
- Internal stakeholders know what they need to do, and only what they need to do
- PIR is acceptable to the client and produces concrete action items with owners
- A future incident with the same root cause can find the previous PIR and not repeat the mistakes
