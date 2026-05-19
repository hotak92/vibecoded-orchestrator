---
title: SLI Selection Methodology
type: concept
tags: [devops, sre, monitoring, observability, mid-level-architecture, slo, sli]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# SLI Selection Methodology

Choosing the right Service Level Indicator (SLI) is the hardest part of SLO design. The burn-rate alerting math (see [[relatedTo::SLO Error Budget, Multi-Burn-Rate Alerts]]) is mechanical once you have the SLI; getting the SLI right requires judgment. This node is that judgment process.

The bad outcomes from a wrong SLI:

- SLI is green during a customer-facing outage → you didn't measure the right thing.
- SLI is red during normal operation → you over-alerted; team disables the alert; SLO becomes decorative.
- SLI is measurable but doesn't move when customers complain → SLI does not represent user happiness.

## The Three Questions

Before writing any PromQL or Datadog query, answer three questions:

1. **Who is the user?** — End human? Another service? A batch job? Different answers produce different SLIs.
2. **What does "the service worked" mean to that user?** — "Got a 200 in <500ms" is the cliché answer, but for a video service it might be "no rebuffering in the first 30 seconds", for an async queue it might be "job completed within 5 minutes of submission".
3. **What's measurable from where?** — Server-side metrics miss network/client problems. Client-side metrics include client problems you can't fix. Synthetic probes are reliable but don't reflect real users. Almost always you need a *layered* SLI set.

## Categories of SLI

Use this table to pick the *shape* of SLI before picking the metric:

| Service shape | Primary SLI | Secondary SLI |
|---|---|---|
| **Request-driven** (REST API, GraphQL, RPC) | Availability (success ratio) + Latency (percentile) | Throughput (saturation indicator) |
| **Storage** (DB, object store, queue) | Availability (read/write success) + Durability + Latency | Throughput |
| **Pipeline / batch** | Coverage (% of expected jobs ran) + Freshness (data age at output) | End-to-end latency (submit → output) |
| **Streaming** (Kafka, Kinesis, video) | Throughput + Consumer lag + Loss-rate | End-to-end latency |
| **Frontend (browser)** | Page-load success + LCP / INP (Core Web Vitals) | JS error rate, route-change duration |

Most services need 2–3 SLIs, not one. A pure availability SLI on a REST API misses latency degradations; pure latency misses outright failures.

## The "Good Event / Valid Event" Formulation

Express every SLI as a ratio of *good events to valid events*. This is the form that maps directly to the SLO/burn-rate math:

```
SLI = good_events / valid_events
```

Worked example — REST API availability:

```promql
# good_events: HTTP responses with non-5xx status
sum(rate(http_requests_total{job="api", status!~"5.."}[5m]))
  /
# valid_events: HTTP responses with any status (except those we don't count)
sum(rate(http_requests_total{job="api"}[5m]))
```

The *valid events* denominator is where most SLIs go wrong. Common decisions:

- **Exclude 4xx?** Usually yes for availability (a client error is not a server failure), but you may want to track 4xx separately because a sudden surge can indicate a client SDK bug or an auth-system regression.
- **Exclude `/health` and `/metrics` endpoints?** Yes — they're not user traffic. Their failure or success doesn't reflect product health.
- **Exclude requests during deploys?** No. The user experience during a bad deploy is part of the user experience. If your deploys cause errors, that's an SLO violation; fix the deploys.
- **Exclude one specific noisy endpoint?** Almost always wrong. If it's noisy, it's burning your error budget for a reason.

## Latency SLI Formulation

Latency SLIs don't divide neatly into good/valid without a threshold:

```promql
# good_events: requests completing under 500ms
sum(rate(http_request_duration_seconds_bucket{job="api", le="0.5"}[5m]))
  /
# valid_events: all requests
sum(rate(http_request_duration_seconds_count{job="api"}[5m]))
```

Pick the threshold from user research, not from your current p99. The classic trap: setting the latency threshold *at* current p99 means your SLO is 99% by construction, regardless of user happiness.

Threshold selection heuristics:

- **Direct user-facing API**: 200–500ms for the threshold; map to "user perceives fast".
- **Backend service**: derive from upstream's budget. If the upstream needs to complete in 500ms and your service is one of three hops, your threshold is ~150ms.
- **Search / recommendations**: often two thresholds (fast path + slow path) tracked as separate SLIs.

Avoid the *percentile-of-a-percentile* trap. "p99 latency under 500ms" is meaningful as a metric but harder to compose into the good/valid form than "fraction of requests under 500ms". The latter composes; the former doesn't.

## Where to Measure

For a typical REST API, you have ~4 measurement points, each with different trade-offs:

| Vantage point | Captures | Misses |
|---|---|---|
| Application code (instrumented) | Application logic errors, time spent in handler | TCP/TLS issues, request never reached the app |
| Load balancer / API gateway | Network-to-server problems, full request including TLS handshake | Client-side problems, DNS issues |
| Synthetic probe (Pingdom, Datadog Synthetics) | Repeatable, alerting-friendly | Doesn't reflect real-user diversity, low volume |
| Real User Monitoring (RUM, in-browser) | What the user actually experienced | High cardinality, sampling needed, can't alert on it cleanly |

The mature posture is *both* server-side (LB or app metrics) for the canonical SLO + RUM for diagnostic backup + synthetic probes as alert backstop when traffic volume is low. The SLO is computed from one canonical source; the others are corroborating.

## Multi-Window Considerations

The SLI must produce a stable signal across the windows you alert on (1h, 6h, 1d, 30d). Things that destabilize:

- **Low traffic** — a service with 10 requests/second has wild fractions on the 1-minute window. Either lengthen the shortest window, or use a different SLI (synthetic probes scale-independent).
- **Bursty traffic** — a batch service that runs every 30 minutes has zero traffic between runs; ratio is undefined. Use a count-based SLI ("% of batch runs completing") instead of a ratio-of-events.
- **Endpoint diversity** — one expensive endpoint (e.g., `/export`) can dominate the latency distribution. Either separate it into its own SLI, or use latency-bucket SLI rather than mean/percentile.

## Common Mistakes

- **CPU / memory / disk as SLIs**. These are *causes*, not *symptoms*. Customers don't experience CPU; they experience errors and slowness. Resource utilization belongs in capacity alerts, not SLOs.
- **One SLI per microservice**. The user doesn't care about your microservice; they care about the workflow. SLO on the workflow ("order placement succeeds in <2s"), then decompose into per-service SLOs for ownership.
- **SLI = monitoring tool's default**. Datadog's "errors" out-of-the-box may include exceptions in background threads, retries that ultimately succeeded, or expected 4xx. Always inspect the query.
- **No SLI for the dependency you require**. If your service fails when its dependency fails, you need an SLI that distinguishes "we failed" from "they failed and we propagated correctly". Otherwise every dependency outage burns your budget for no actionable reason.

## A Worked SLI Set for a Generic REST API

A defensible starting set:

```yaml
slos:
  - name: api_availability
    sli: |
      sum(rate(http_requests_total{job="api",path!="/health",status!~"5.."}[5m]))
      /
      sum(rate(http_requests_total{job="api",path!="/health"}[5m]))
    target: 0.999            # 99.9% — ~43m 12s downtime budget per 30d window
    window: 30d

  - name: api_latency
    sli: |
      sum(rate(http_request_duration_seconds_bucket{job="api",path!="/health",le="0.5"}[5m]))
      /
      sum(rate(http_request_duration_seconds_count{job="api",path!="/health"}[5m]))
    target: 0.99             # 99% of requests under 500ms
    window: 30d
```

Two SLIs. Each is good_events / valid_events. Each has a target. Each has a 30-day window. From here, the burn-rate alerts in [[relatedTo::SLO Error Budget, Multi-Burn-Rate Alerts]] are mechanical: 14.4×, 6×, 1× burn rates on 1h+5m, 6h+30m, 3d+6h windows.

[[relatedTo::SLO Error Budget, Multi-Burn-Rate Alerts]] [[relatedTo::USE, RED and Four Golden Signals]] [[relatedTo::SRE Incident Response Playbook]]
