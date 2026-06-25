---
title: Kubernetes Manifest Review Discipline
type: concept
tags: [devops, infrastructure, kubernetes, code-review, mid-level-architecture, security, containerization]
created: 2026-05-19T00:00:00Z
updated: 2026-06-25T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Kubernetes Manifest Review Discipline

Reviewing a Kubernetes manifest is not the same as understanding the k8s resource model. The model tells you what objects exist; the review discipline tells you what to *check for* when one of those objects lands in a PR. This node is the per-kind review checklist. For the underlying resource model (requests / limits / QoS / eviction), see [[relatedTo::Kubernetes Resource Model, QoS and Eviction]].

The review pyramid (apply in order):

1. **Security context** — does this pod run with the least privilege required?
2. **Resource correctness** — requests/limits set, sane, and matched to QoS class.
3. **Liveness / readiness / startup** — does the probe distinction match the app's actual semantics?
4. **Disruption tolerance** — PDBs, anti-affinity, replicas, rollout strategy.
5. **Network exposure** — Service type, NetworkPolicy, Ingress annotations.

## Pod & Container Security Context

Default deny. Every container in a manifest should have a `securityContext`. Mandatory checks:

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1000        # explicit, not 0
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
    add: []              # add back specific caps only if proven necessary
  seccompProfile:
    type: RuntimeDefault
```

Pod-level `securityContext` also matters:

```yaml
spec:
  securityContext:
    runAsNonRoot: true
    fsGroup: 2000        # group ownership of mounted volumes
    seccompProfile:
      type: RuntimeDefault
```

Reviewer red flags:

- `privileged: true` — almost never justified outside CNI/CSI/host-monitoring DaemonSets. Force a written justification.
- `hostNetwork: true`, `hostPID: true`, `hostIPC: true` — breaks the pod isolation boundary; same justification bar as `privileged`.
- `hostPath` volume — escape hatch onto the node filesystem; should be `readOnly: true` whenever used.
- Capabilities added beyond `NET_BIND_SERVICE` (the only one routinely needed, for binding to ports <1024 — and even that is avoidable by binding to ≥1024).
- Missing `readOnlyRootFilesystem` — defense in depth against runtime tampering. If the app writes, mount `emptyDir` at the write path.
- `imagePullPolicy: Always` on a mutable tag (`:latest`, `:main`) — non-reproducible. Pin to digest (`image@sha256:...`) or immutable semver tag.

## Resource Requests & Limits

Every container needs `resources.requests.cpu`, `resources.requests.memory`, and `resources.limits.memory`. The CPU *limit* is the most contested setting — see below.

```yaml
resources:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    memory: 256Mi        # equal to request → Guaranteed QoS class
```

**Memory limit must equal request** for the Guaranteed QoS class — the only class safe from OOM-kill during node pressure. Burstable is acceptable for non-critical workloads; BestEffort (no requests/limits) only for genuinely transient pods.

**CPU limit: contested**. *Always set* prevents thread-leak starvation of neighbors but introduces CFS-throttling latency spikes on bursty workloads (100ms quota windows). *Never set, only requests* relies on scheduler request-packing but allows runaway pods to saturate the node. Defensible middle: set CPU limits on untrusted/multi-tenant workloads; omit on latency-sensitive first-party workloads where noisy-neighbor risk is mitigated by separate node pools. Pick one approach; document it as team convention.

## Probes

Three probe types, three different jobs:

| Probe | When it fires | Failure action | Use for |
|---|---|---|---|
| `startupProbe` | Container start until first success | Restart container | Slow-starting apps (JVM warmup, DB migrations) so the liveness probe doesn't kill them mid-init |
| `livenessProbe` | Continuously after startup | Restart container | Detect deadlock / wedged state |
| `readinessProbe` | Continuously after startup | Remove pod from Service endpoints | Detect "not ready to serve" (warming cache, backpressure, dependency down) |

Common mistakes: pointing liveness and readiness at the same endpoint (causes restart loops when a downstream dep is slow); liveness probes that call downstream dependencies (a slow DB triggers pod restart, amplifying outage scope — liveness should check only pod-local health); using `initialDelaySeconds` for slow startup instead of `startupProbe` (the modern, sized-correctly option). Tune `failureThreshold` deliberately — default 3 is sometimes too forgiving (genuinely fatal) or too strict (flaky check).

Reasonable defaults for an HTTP service:

```yaml
startupProbe:
  httpGet: {path: /healthz, port: 8080}
  failureThreshold: 30           # 30 * 10s = 5 min to come up
  periodSeconds: 10
livenessProbe:
  httpGet: {path: /healthz, port: 8080}
  periodSeconds: 10
  failureThreshold: 3
  timeoutSeconds: 1
readinessProbe:
  httpGet: {path: /ready, port: 8080}
  periodSeconds: 5
  failureThreshold: 2
  timeoutSeconds: 1
```

`/healthz` and `/ready` must be different handlers in the app, not aliases.

## Disruption Tolerance

A workload that needs to survive node drains, rolling updates, and AZ failures must answer four questions in its manifest set:

**Replicas + rollout strategy** (in the Deployment):
```yaml
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0      # zero-downtime
```

**PodDisruptionBudget** (separate object — without this, voluntary disruption can take the whole Deployment down):
```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: api-pdb
spec:
  minAvailable: 2            # OR maxUnavailable: 1 — pick one
  selector:
    matchLabels:
      app: api
```

**Anti-affinity** (so replicas don't co-locate on one node):
```yaml
spec:
  template:
    spec:
      topologySpreadConstraints:
      - maxSkew: 1
        topologyKey: kubernetes.io/hostname
        whenUnsatisfiable: ScheduleAnyway   # DoNotSchedule for hard requirement
        labelSelector:
          matchLabels:
            app: api
```

`topologySpreadConstraints` has largely replaced `podAntiAffinity` for spread purposes — clearer semantics, supports multi-topology spread.

**Termination grace** — the default 30 seconds is often too short for connection draining. Set `terminationGracePeriodSeconds` to comfortably exceed your longest-lived request + readiness-probe-failure detection time. Implement `preStop` hook to flip readiness to false and sleep, so the Service removes the pod from endpoints before SIGTERM lands.

## NetworkPolicy (Default Deny)

Without a NetworkPolicy, pods can talk to anything. The mature posture is *default-deny ingress + explicit allows*:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
  namespace: api
spec:
  podSelector: {}            # all pods in namespace
  policyTypes: ["Ingress"]
  # no ingress rules → deny all
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-from-ingress-controller
  namespace: api
spec:
  podSelector:
    matchLabels: {app: api}
  policyTypes: ["Ingress"]
  ingress:
  - from:
    - namespaceSelector:
        matchLabels: {name: ingress-nginx}
    ports:
    - {protocol: TCP, port: 8080}
```

For egress, default-deny is harder (DNS, metrics, identity provider calls all need allowlisting) but worth the production investment. Verify the cluster's CNI supports NetworkPolicy (Calico, Cilium yes; flat overlays without policy enforcement: no).

## Service & Ingress

- `Service.spec.type: LoadBalancer` provisions a cloud LB — expensive and externally exposed. Prefer `ClusterIP` + an Ingress controller for HTTP.
- `externalTrafficPolicy: Local` preserves source IP but skips kube-proxy load-balancing across nodes — only useful when the LB does its own per-pod balancing.
- Ingress annotations are controller-specific; an `nginx.ingress.kubernetes.io/*` annotation on an AWS LBC ingress is silently ignored. Match annotations to the installed controller.

## Per-Kind Review Quick-Reference

| Kind | Key checks |
|---|---|
| `Deployment` | replicas ≥ 2 (prod), strategy, securityContext, probes, resources |
| `StatefulSet` | volumeClaimTemplates sized correctly, `podManagementPolicy`, headless Service for stable DNS |
| `DaemonSet` | tolerations for taints, host-path mounts read-only where possible, no rolling-update surge |
| `Job` / `CronJob` | `backoffLimit`, `activeDeadlineSeconds`, `successfulJobsHistoryLimit`, idempotency assumption |
| `Service` | type, selector matches pod labels, port name (for named-port probes) |
| `Ingress` | TLS configured, annotations match controller, host overlaps with other Ingresses |
| `ConfigMap` / `Secret` | not committed to git unencrypted; mounted vs envFrom (env leaks via `/proc/<pid>/environ`) |
| `HorizontalPodAutoscaler` | min/max replicas, target metric realistic, stabilization windows |
| `NetworkPolicy` | rules don't accidentally over-permit via `podSelector: {}` + permissive ingress |
| `RBAC (Role/ClusterRole)` | verbs not wildcarded, resources not `"*"`, no `nonResourceURLs` unless intentional |

## Common Anti-Patterns

- `emptyDir` for "persistence" — wiped on pod restart; use PVC.
- `hostPort` for service exposure — bypasses Service abstraction; use Service.
- Pod inheriting the namespace's `default` ServiceAccount — create a per-app SA for meaningful RBAC + audit.
- `imagePullSecrets` on the pod — usually belongs on the ServiceAccount.
- No `priorityClassName` — under node pressure, the scheduler evicts arbitrary pods. Create custom classes for app tiers (reserve `system-cluster-critical` for system pods).
- `namespace: default` — defeats namespace-level NetworkPolicy / RBAC / ResourceQuota. Never deploy production into `default`.

[[relatedTo::Kubernetes Resource Model, QoS and Eviction]] [[relatedTo::Terraform Plan Review Discipline]] [[relatedTo::IAM Least-Privilege Patterns]] [[relatedTo::SRE Incident Response Playbook]]
