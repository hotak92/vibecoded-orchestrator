---
title: SRE Incident Response Playbook
type: concept
tags: [devops, sre, infrastructure, monitoring, operations, mid-level-architecture, incident-response]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# SRE Incident Response Playbook

A live incident is not the time to invent a process. This node is the four-phase playbook used during an active incident. The post-mortem comes *after*; for how to write that, see [[relatedTo::Postmortem Authoring Discipline]]. For the cultural framing of blamelessness, see [[relatedTo::Blameless Postmortem Methodology]].

The four phases — and the *order* is non-negotiable:

1. **Triage** — confirm, scope, page the right people, declare severity.
2. **Hypothesis** — form theories, prioritize by reversibility, gather evidence.
3. **Mitigate** — restore service before fully understanding the cause.
4. **Verify** — confirm mitigation held, exit incident, hand off to follow-up.

## Phase 1 — Triage (first 5–10 minutes)

Goal: answer four questions in this exact order.

1. **Is this real?** — Check the alert source, the dashboard, and a synthetic probe. False alarms (monitoring outage masquerading as service outage) cost 30+ minutes if you skip this step.
2. **What is the scope?** — One customer / one region / one tenant / global? Scope drives severity.
3. **Severity?** — Most orgs use Sev1 (full outage, paying customers affected, revenue/safety impact) / Sev2 (degraded, some customers) / Sev3 (internal-only impact) / Sev4 (cosmetic). Err high; downgrading later is cheap, upgrading later costs trust.
4. **Who is the Incident Commander?** — One person, named, on a call. Their job is *coordination*, not debugging. If you have only one engineer, they are both — but they must consciously switch hats.

**Communication starts immediately, before debugging starts**:

- Status page update (or internal channel) within 5 minutes of declaration. "Investigating — we have observed elevated error rates beginning at HH:MM UTC, investigating now." Don't speculate on cause.
- Page the on-call for adjacent services if cascade is possible.
- Open a dedicated incident channel; pin the IC, the scribe, and the latest status post. Side conversations go elsewhere.

**The scribe role** — one person whose only job is to type a timestamped log of every action ("16:42 — rolled back deploy abc123 in us-east-1"). This is the raw material for the post-mortem. Without it, post-mortems become fiction-writing exercises 48 hours later. On small teams, the IC scribes themselves but should announce that they are doing so.

## Phase 2 — Hypothesis (parallel with evidence gathering)

Don't fixate on the first theory. Generate 3–5 hypotheses *before* committing to investigation order:

| Hypothesis class | First-look evidence |
|---|---|
| Recent deploy | Deploy timeline vs alert timeline; `git log --since="2 hours ago"` |
| Recent config change | Feature-flag dashboard, infrastructure git log, kubectl rollout history |
| Capacity / scaling | CPU/memory/disk/network on dashboards, autoscaler events |
| Dependency failure | Downstream health checks, cloud provider status pages |
| Data event | Recent migrations, large query timing changes, hot keys |
| External (DDoS, BGP, certificate expiry) | Edge logs, traffic graphs, cert-monitor alerts |

**Prioritize hypotheses by reversibility, not by likelihood**:

1. Cheap, reversible mitigations first (rollback, flag flip, kill-switch) — try these even on weak evidence.
2. Expensive or partially-reversible mitigations next (scale up, failover) — need stronger evidence.
3. Irreversible actions last (drop a table, force-restart with data loss) — need overwhelming evidence and explicit IC approval.

This ordering matters because during an incident, false-negative cost (didn't try the cheap fix, customers stay down) typically exceeds false-positive cost (rolled back unnecessarily, learned something).

## Evidence Collection

Capture *as you go* — incidents destroy the evidence trail when you reach steady state. Minimum capture set:

- **Timeline screenshots**: error-rate dashboard, latency dashboard, the alert that fired. With timestamps visible.
- **Logs from the affected service**: tail and save (`kubectl logs ... --since=1h > incident-logs.txt`). Logs rotate.
- **kubectl describe / cloud-console state**: pod status, recent events, autoscaling events.
- **Recent deploy diff**: the actual code or config that shipped before the incident.
- **A copy of the metric/log query** that surfaced the problem — not just a screenshot.

If you have to choose between "save evidence" and "mitigate now", mitigate. But ask another responder to save evidence in parallel.

## Phase 3 — Mitigate (restore service)

The mitigation hierarchy — try in this order:

1. **Rollback** the last deploy or config change if the timeline correlates. Even if you're not sure it's the cause; cheap and reversible. The rule of thumb: if the deploy was within the last 60 minutes and the symptom started after, roll back first, investigate second.
2. **Feature-flag off** any recently-enabled flag. Same logic.
3. **Failover** to a healthy replica / region / cluster if the affected scope is regional.
4. **Scale up** if metrics show saturation. Note: scaling up does not fix a bug, it only buys time.
5. **Kill-switch** any non-critical-path feature (search, recommendations, exports) to reduce load on the broken component.
6. **Restart** stuck pods / processes — last resort because it destroys the evidence trail. If you must, capture a heap dump / thread dump first.

After mitigation, **wait** before declaring resolved. The alert clearing is necessary but not sufficient — give it at least 5 minutes (or one full sample window of the slowest metric) to confirm the recovery is stable. Premature "all clear" announcements are how single incidents become two-act dramas.

## Phase 4 — Verify and Exit

Before closing the incident:

- [ ] Primary alert has been cleared for ≥ 5 minutes (or one slow-metric window).
- [ ] Synthetic / user-facing probes show normal behavior.
- [ ] Adjacent dashboards (downstream services, queue depth, error budget consumption) are normal.
- [ ] No new alerts firing.
- [ ] Status page / customer comms updated to "monitoring" → "resolved".
- [ ] Scribe log is saved somewhere durable (not the chat scrollback).
- [ ] Post-mortem owner assigned and scheduled (within 24–48 hours, while memory is fresh).

The incident **is not over** until follow-up tickets exist for:

- The root cause fix (if not already deployed during the incident).
- Any monitoring gap the incident revealed ("we should have alerted on X 20 minutes earlier").
- Any documentation gap ("the runbook for failover was wrong").

## Communication Patterns

Three audiences, three different messages:

| Audience | Cadence | Tone | Content |
|---|---|---|---|
| Customers (status page) | Every 30 min during active incident | Calm, factual, no jargon | Impact, what we're doing, next update time |
| Internal stakeholders | Every 15 min in incident channel | Detailed, technical | Current hypothesis, next action, blockers |
| Leadership | Once per phase change, or hourly | Concise, decision-oriented | Severity, ETA, decisions needed |

Always commit to a *next update time* in customer comms, even if it's "next update by 18:30 UTC even if no change". Silence between updates is what generates support tickets.

## Incident Commander Discipline

The IC's job is the hardest in an incident because it requires *not debugging*. Key behaviors:

- **Delegate, don't execute**. The IC names a debugger, a comms lead, a scribe. The IC asks "who is doing X?" not "I'll do X".
- **Time-box hypotheses**. "We'll spend 10 minutes on the dependency-failure theory; if no progress, we move to capacity."
- **Run a stand-up every 15 minutes**. "Where are we? What did we learn? What are we trying next?"
- **Make the call on irreversible actions**. Only the IC approves "drop the table" / "fail over to DR" / "drain the queue".
- **Hand off cleanly**. If the incident exceeds 4 hours or crosses shifts, the outgoing IC briefs the incoming IC with: current state, hypotheses tried, hypotheses pending, evidence saved, comms cadence. Hand-off is in writing, not just verbal.

[[relatedTo::Postmortem Authoring Discipline]] [[relatedTo::Blameless Postmortem Methodology]] [[relatedTo::Incident Communication Tempo]] [[relatedTo::USE, RED and Four Golden Signals]] [[relatedTo::SLI Selection Methodology]] [[relatedTo::Kubernetes Manifest Review Discipline]]
