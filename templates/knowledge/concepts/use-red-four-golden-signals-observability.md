---
title: USE, RED, and Four Golden Signals — Observability Method Selection
type: concept
tags: [SRE, observability, monitoring, prometheus, metrics, devops, infrastructure, mid-level-architecture]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# USE, RED, and Four Golden Signals — Observability Method Selection

## Definition

Three complementary methods exist for choosing *what* to measure on a system. They are not competing — they cover different layers of the stack and answer different questions:

| Method | What | For | Layer |
|---|---|---|---|
| **USE** (Brendan Gregg) | Utilization, Saturation, Errors | Resources — CPU, RAM, disk, network, IOPS | Host / kernel |
| **RED** (Tom Wilkie) | Rate, Errors, Duration | Request-driven services | App / RPC |
| **Four Golden Signals** (Google SRE) | Latency, Traffic, Errors, Saturation | User-facing services | App / system |

A complete observability story applies USE *and* RED to every service. The Four Golden Signals are roughly RED + saturation, suited specifically to user-facing systems.

## USE Method — for every resource

For every resource on every host, ask:

- **Utilization**: % time the resource was busy servicing work
- **Saturation**: amount of queued/blocked work the resource couldn't service immediately
- **Errors**: count of error events from the resource

Worked example for CPU:

```
Utilization: avg(rate(node_cpu_seconds_total{mode!="idle"}[5m])) by (instance)
Saturation : avg(node_load5) / avg(count without(cpu, mode) (node_cpu_seconds_total{mode="idle"}))
Errors     : (rare for CPU; for disks: rate(node_disk_io_now[5m]) or SMART error count)
```

For memory:
- Utilization: `(MemTotal - MemAvailable) / MemTotal`
- Saturation: page-out rate (`rate(node_vmstat_pswpout[5m])`), or PSI memory pressure
- Errors: OOM-kill count, ECC errors

For network:
- Utilization: `rate(node_network_receive_bytes_total[5m]) / interface_bandwidth`
- Saturation: TX/RX queue drops (`rate(node_network_receive_drop_total[5m])`)
- Errors: `rate(node_network_receive_errs_total[5m])`

USE is the **floor**: if a resource is saturated, no application-level fix matters until that's resolved.

## RED Method — for every request-driven service

For every service that handles requests, instrument:

- **Rate**: requests per second
- **Errors**: failed requests per second (and as a percentage)
- **Duration**: request latency distribution (P50, P95, P99 — not average)

Prometheus convention (using `http_requests_total` counter + `http_request_duration_seconds` histogram):

```
Rate:     sum(rate(http_requests_total{service="api"}[5m])) by (method, route)
Errors:   sum(rate(http_requests_total{service="api",code=~"5.."}[5m])) by (route)
            / sum(rate(http_requests_total{service="api"}[5m])) by (route)
Duration: histogram_quantile(0.99,
            sum(rate(http_request_duration_seconds_bucket{service="api"}[5m])) by (le, route)
          )
```

**Always use histograms, never gauges** for latency. Gauges lose tail information.

## Four Golden Signals — for user-facing services

Google SRE's framing combines RED with saturation:

- **Latency**: time to serve a request (separate success-latency from error-latency)
- **Traffic**: demand on the system (RPS, concurrent users, queue depth)
- **Errors**: explicit (5xx) and implicit (200 with wrong content) failures
- **Saturation**: how full the service is (utilization toward its capacity ceiling)

Saturation is the early-warning signal. Latency and errors are lagging — by the time they degrade, users are already affected. Saturation tells you a *capacity* problem is coming. Examples:
- Connection pool: `pool_in_use / pool_max`
- Goroutine count vs target
- JVM heap usage vs max
- Kafka consumer lag

## How They Compose

For a typical microservice on Kubernetes:

```
Host (node-exporter)
  └── USE: CPU, memory, disk, network for every node
Container (cAdvisor / kubelet)
  └── USE: CPU throttle %, memory working set, fs IO
Service (app instrumentation)
  ├── RED: request rate / error rate / latency histogram per route
  └── Saturation: pool usage, queue depth, in-flight requests
External dependencies (db client, cache client)
  ├── RED: db.queries.rate / .errors / .duration
  └── USE: connection pool utilization, saturation (waiting connections)
```

If you skip any layer, you'll have outages you can't diagnose. USE-only misses bad releases. RED-only misses noisy-neighbour CPU starvation. Four-signals-only misses internal queue saturation that hasn't yet leaked into user-visible latency.

## Cardinality Discipline

Every label you add multiplies the time-series count. Specifically:

- **DON'T** label by `user_id`, `request_id`, `email`, `customer_name` — unbounded cardinality kills Prometheus.
- **DO** label by `route`, `method`, `status_code`, `tenant_tier`, `region` — bounded sets.
- For high-cardinality investigation use **traces** (Jaeger, Tempo) or **logs** (Loki), not metrics.

A good ceiling: < 10 labels per metric, < 100 distinct values per label.

## What to Alert On

- **USE saturation** → page when saturation > 80% for > 5m on a critical resource
- **RED errors + duration** → use [[implements::SLO Error Budgets and Multi-Window Multi-Burn-Rate Alerts]], not static thresholds
- **Four-signal traffic** → alert on *unexpected* traffic (sudden drop = outage upstream; sudden spike = potential DoS or runaway client)
- **Don't alert on utilization alone** — high utilization is not a problem; saturation is.

## Common Pitfalls

- **Averaging latency** → P99 is often 10–100× the average. Average hides everything.
- **CPU utilization alarms at 80%** → modern multicore boxes can be at 95% utilization with zero saturation. Use load (saturation), not utilization.
- **No internal saturation metric** → you only learn the connection pool is exhausted by the resulting latency spike. Export `pool.in_use` and alarm on it.
- **Mixing success and error latency** → an endpoint returning 500 in 5ms looks "fast", dragging P99 down. Always separate.
- **No label for `route`** → the slow endpoint is invisible because it's averaged with all the fast ones.

## Related Concepts

- [[implements::SLO Error Budgets and Multi-Window Multi-Burn-Rate Alerts]]
- [[relatedTo::Kubernetes Resource Model — Requests, Limits, QoS Classes, and Eviction]]
- [[buildsOn::Blameless Post-Mortem Methodology]]

## References

- Brendan Gregg, *The USE Method*: https://www.brendangregg.com/usemethod.html
- Tom Wilkie, *The RED Method*: https://grafana.com/blog/2018/08/02/the-red-method-how-to-instrument-your-services/
- Google SRE Book, *Monitoring Distributed Systems* (Four Golden Signals): https://sre.google/sre-book/monitoring-distributed-systems/
- Prometheus histogram best practices: https://prometheus.io/docs/practices/histograms/
- OpenTelemetry semantic conventions: https://opentelemetry.io/docs/specs/semconv/
