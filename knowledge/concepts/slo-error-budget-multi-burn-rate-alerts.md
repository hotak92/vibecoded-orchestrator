---
title: SLO Error Budgets and Multi-Window Multi-Burn-Rate Alerts
type: concept
tags: [SRE, observability, monitoring, SLO, alerting, prometheus, infrastructure, devops, mid-level-architecture]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# SLO Error Budgets and Multi-Window Multi-Burn-Rate Alerts

## Definition

A **Service Level Objective (SLO)** is a target reliability level for a Service Level Indicator (SLI) — e.g. "99.9% of requests succeed over a rolling 30 days". The **error budget** is the inverse: the amount of failure the SLO permits before it is violated (0.1% of requests over 30 days for the example above). **Multi-window multi-burn-rate alerts** fire when the budget is being consumed faster than sustainable, weighted across short and long observation windows so the alert is both fast (catches outages within minutes) and precise (doesn't flap on transient blips).

Canonical reference: Google SRE Workbook, *Alerting on SLOs* — https://sre.google/workbook/alerting-on-slos/

## Why Not Threshold Alerts

Static thresholds ("alert if error rate > 1%") are simultaneously too sensitive and too insensitive:
- They fire on benign 30-second spikes that never threaten the SLO
- They miss slow leaks where the rate sits just under the threshold for hours and burns the entire month's budget by Tuesday
- They don't tell you how urgent the situation is

Burn-rate alerting fixes both. A **burn rate** of `1` means the budget is being consumed at exactly the rate that would deplete it over the SLO window. A burn rate of `14.4` over 1h means at the current rate, you'd exhaust a 30-day budget in 2 days. The alert severity follows the burn rate.

## The Math

For SLO window `W` (e.g. 30 days = 720h) and target `T` (e.g. 99.9% → error budget `B = 1 - T = 0.001`):

```
burn_rate = (current_error_rate) / B
budget_consumed_per_hour = burn_rate / W_hours
```

A burn rate of `36` exhausts a 30-day budget in `720 / 36 = 20h`. A burn rate of `6` exhausts it in 5 days.

## Recommended Alert Tiers (Google SRE)

| Severity | Long window | Short window | Burn rate threshold | Budget consumed if sustained for long window |
|---|---|---|---|---|
| Page (urgent) | 1h | 5m | 14.4 | 2% |
| Page (slower) | 6h | 30m | 6 | 5% |
| Ticket | 3 days | 6h | 1 | 10% |

The **long window** prevents flapping (must hold for the long window to fire). The **short window** ensures resolution detection (alert clears within minutes of recovery, not hours).

## Prometheus Implementation

Recording rules pre-compute burn rates per window:

```yaml
groups:
  - name: slo_burn_rates
    interval: 30s
    rules:
      - record: job:slo_errors_per_request:ratio_rate5m
        expr: |
          sum(rate(http_requests_total{job="api",code=~"5.."}[5m]))
            /
          sum(rate(http_requests_total{job="api"}[5m]))

      - record: job:slo_errors_per_request:ratio_rate1h
        expr: |
          sum(rate(http_requests_total{job="api",code=~"5.."}[1h]))
            /
          sum(rate(http_requests_total{job="api"}[1h]))

      - record: job:slo_errors_per_request:ratio_rate6h
        expr: |
          sum(rate(http_requests_total{job="api",code=~"5.."}[6h]))
            /
          sum(rate(http_requests_total{job="api"}[6h]))
```

Alerting rules combine windows (both must exceed threshold):

```yaml
  - name: slo_alerts
    rules:
      - alert: APIErrorBudgetBurnFast
        expr: |
          (
            job:slo_errors_per_request:ratio_rate1h > (14.4 * 0.001)
            and
            job:slo_errors_per_request:ratio_rate5m > (14.4 * 0.001)
          )
        for: 2m
        labels:
          severity: page
          slo: api-availability-99.9
        annotations:
          summary: "API burning 30d error budget in <2d (burn_rate >= 14.4)"
          runbook: "https://runbooks.example.com/api-error-budget-burn"

      - alert: APIErrorBudgetBurnSlow
        expr: |
          (
            job:slo_errors_per_request:ratio_rate6h > (6 * 0.001)
            and
            job:slo_errors_per_request:ratio_rate30m > (6 * 0.001)
          )
        for: 15m
        labels:
          severity: page
          slo: api-availability-99.9
        annotations:
          summary: "API burning 30d error budget in <5d (burn_rate >= 6)"
```

## Latency SLOs (Histogram-Based)

Same pattern, different SLI. For "99% of requests < 250ms":

```yaml
- record: job:slo_latency_violations:ratio_rate5m
  expr: |
    1 - (
      sum(rate(http_request_duration_seconds_bucket{job="api",le="0.25"}[5m]))
        /
      sum(rate(http_request_duration_seconds_count{job="api"}[5m]))
    )
```

Then the same burn-rate tiers apply against `error_budget = 1 - 0.99 = 0.01`.

## Common Pitfalls

- **Single-window threshold**: `error_rate > 1%` for 5m flaps and misses slow burns. Always pair windows.
- **Counting all 5xx as errors**: Some 5xx are expected (e.g., 503 during a rolling restart drained by the load balancer). Filter or whitelist.
- **SLI from the wrong vantage point**: Measuring success rate inside the app misses ingress failures, DNS, TLS. Measure as close to the user as possible (CDN logs, synthetic checks, or load-balancer logs).
- **Including non-product traffic**: Health-check endpoints in the SLI inflate success rate. Exclude `/healthz`, `/readyz`, scrape endpoints.
- **30-day rolling vs calendar**: Rolling windows smooth across month boundaries; calendar windows align with billing/reporting cycles. Pick one and document it.

## Related Concepts

- [[implements::Four Golden Signals (Latency, Traffic, Errors, Saturation)]]
- [[relatedTo::Blameless Post-Mortem Methodology]]
- [[buildsOn::USE and RED Observability Methods]]

## References

- Google SRE Workbook, *Implementing SLOs*: https://sre.google/workbook/implementing-slos/
- Google SRE Workbook, *Alerting on SLOs*: https://sre.google/workbook/alerting-on-slos/
- Google SRE Book, *Service Level Objectives*: https://sre.google/sre-book/service-level-objectives/
- Prometheus recording-rule docs: https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/
- Sloth (SLO YAML → Prometheus rule generator): https://sloth.dev/
