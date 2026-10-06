# Performance review topic

Cross-domain performance lens for implementation and review work: frontend
render, backend queries, AI/ML inference. Read when something is slow, when
reviewing a performance-sensitive change, or before production deploy of a
critical path.

## Method: measure before optimizing

1. Reproduce the slowness and measure a baseline (response time, throughput,
   frame time, VRAM).
2. Profile to find the actual hotspot (CPU, memory, database, network) —
   never guess.
3. Prioritize by impact (80/20): fix the hotspot that dominates the baseline.
4. Change one thing, re-measure, keep it only if the measurement improved.
5. Validate under realistic load, not a single request.

Skip entirely when: the fix is already obvious (N+1 query, missing index —
just fix it), or performance is not a concern (prototypes, one-off scripts).

## Frontend

- Bundle size: route-based code splitting; audit heavy dependencies.
- Render performance: wasted re-renders (find the changed state and its
  subscribers); memoization only where profiling shows need.
- Long lists: virtualization.
- Targets: FCP < 1.5s, no layout shifts on the critical path.

## Backend

- Queries: N+1 patterns, missing indexes (EXPLAIN ANALYZE), SELECT *,
  unbounded result sets, filtering in app instead of DB.
- Caching: hot-read cache layer with explicit TTL and invalidation; HTTP
  caching (ETag/Cache-Control) at the edge.
- Connection pooling; async processing for slow work; queue + retry for
  external calls.
- Targets: API p95 < 200–500ms depending on workload; document the target.

## AI/ML inference

- Model quantization and batch processing for throughput.
- VRAM: release idle allocations; size context windows deliberately.
- Context caching / prompt caching where the provider supports it.
- Cost per call and latency per call are both first-class metrics.

## Review checklist

- Any change to a hot path carries a before/after measurement.
- No unbounded loops over external data without pagination/limits.
- No per-item remote calls inside a loop (batch them).
- Cache additions state their TTL and invalidation trigger.
- Performance "fixes" without a measurement are findings, not improvements.

## Related

- Query/schema depth: `fields/database.md`
- Frontend depth: `fields/frontend.md`
