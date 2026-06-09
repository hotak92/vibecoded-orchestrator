---
title: Kubernetes Resource Model — Requests, Limits, QoS Classes, and Eviction
type: concept
tags: [kubernetes, infrastructure, devops, SRE, containerization, mid-level-architecture, resource-management]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: 2026-05-22T00:00:00Z
status: archived
---

# Kubernetes Resource Model — Requests, Limits, QoS Classes, and Eviction

## Definition

Kubernetes schedules and evicts pods based on declared CPU and memory **requests** (what the pod is guaranteed) and **limits** (the ceiling it can use). The scheduler uses requests to find a node with capacity; the kubelet uses limits to constrain runtime usage. The combination of requests vs limits across all containers in a pod determines its **Quality of Service (QoS) class** — `Guaranteed`, `Burstable`, or `BestEffort` — which is the primary signal the kubelet uses to decide which pods to evict under node pressure.

Canonical references:
- https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
- https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/
- https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/

## Requests vs Limits

| | What it does | Set on | Failure mode if wrong |
|---|---|---|---|
| `requests.cpu` | Scheduler reserves this much CPU on the node | Container | Underset: node oversubscribed → throttling under load. Overset: scheduling pressure, low cluster utilization |
| `limits.cpu` | Kernel CFS throttles the container above this | Container | Set too low: tail-latency spikes during legitimate bursts (CFS throttling) |
| `requests.memory` | Scheduler reserves this much memory on the node | Container | Underset: OOM kills under pressure. Overset: low cluster utilization |
| `limits.memory` | Kernel OOM-kills the container above this | Container | Set too low: the container is OOM-killed mid-request |

**CPU is compressible; memory is not.** Hitting CPU limit slows the container (CFS throttle). Hitting memory limit kills it. This drives the asymmetric guidance below.

## QoS Classes (Determined by the Scheduler)

```
Guaranteed:
  Every container has memory + cpu limits, AND
  Every container's request == limit for both resources

Burstable:
  At least one container has a request or limit set, AND
  Not Guaranteed

BestEffort:
  No container has any requests or limits
```

## Eviction Order Under Node Pressure

When a node hits a resource pressure signal (`memory.available`, `nodefs.available`, `imagefs.available`, etc.), the kubelet evicts pods in this order:

1. **BestEffort** pods first (no protection)
2. **Burstable** pods using *more than their request* (priority within tier follows pod priority class, then resource-usage-above-request)
3. **Guaranteed** pods last (only if system processes still need memory)

This is why production-critical workloads should be **Guaranteed**: they survive node pressure caused by neighbour pods.

## Recommended Defaults by Workload Type

| Workload | CPU request | CPU limit | Memory request | Memory limit |
|---|---|---|---|---|
| Critical stateful (DB, queue) | Measured P95 | **unset** or 4× request | Measured P99 + 20% | Same as request (= Guaranteed) |
| Critical stateless API | Measured P95 | unset or 2× request | Measured P99 + 20% | Same as request |
| Batch/CI worker | Measured average | 2× request | Measured P99 | 1.5× request (Burstable) |
| Side-car (log shipper) | 10m | 100m | 32Mi | 64Mi |

**CPU limits are controversial.** Setting CPU limits causes CFS throttling, which degrades tail latency dramatically even when the node has spare CPU. Many SRE teams (Google, Buoyant/Linkerd) advise **omitting CPU limits** for latency-sensitive workloads while keeping CPU requests. Memory limits should always be set (uncapped memory leak = node killed).

## Production-Grade Pod Spec

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: prod
spec:
  replicas: 3
  selector:
    matchLabels: {app: api}
  template:
    metadata:
      labels: {app: api}
    spec:
      # Spread across zones to survive AZ failure
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels: {app: api}
      # Prefer one pod per node, but don't refuse to schedule
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                topologyKey: kubernetes.io/hostname
                labelSelector:
                  matchLabels: {app: api}
      # Hard security defaults
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        fsGroup: 65532
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: api
          image: registry.example.com/api:v1.42.0@sha256:abc...  # pinned by digest
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8080
              name: http
          # Guaranteed QoS for critical service
          resources:
            requests:
              cpu: "500m"
              memory: "512Mi"
            limits:
              # CPU limit omitted intentionally — avoid CFS throttling
              memory: "512Mi"
          # Probes
          startupProbe:    # protects against slow boot
            httpGet: {path: /healthz, port: http}
            failureThreshold: 30
            periodSeconds: 5
          livenessProbe:   # restart if frozen
            httpGet: {path: /healthz, port: http}
            periodSeconds: 10
            failureThreshold: 3
          readinessProbe:  # remove from service if not ready
            httpGet: {path: /readyz, port: http}
            periodSeconds: 5
            failureThreshold: 2
          # Container-level hardening
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          # Graceful shutdown
          lifecycle:
            preStop:
              exec:
                command: ["sh", "-c", "sleep 10"]  # let LB drain
      terminationGracePeriodSeconds: 30
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: api
  namespace: prod
spec:
  minAvailable: 2          # never drop below 2 during voluntary disruptions
  selector:
    matchLabels: {app: api}
```

## Three Probes, Three Jobs

- **startupProbe**: protects slow-booting containers from liveness-probe murder. Pair with high `failureThreshold` to give JVMs, Rails, etc. time to warm.
- **livenessProbe**: kubelet restarts the container if this fails. Use sparingly — a flaky liveness probe can cascade and take down a healthy service.
- **readinessProbe**: kubelet removes the pod from the Service's endpoint list if this fails. Use this aggressively — it's safe (no restart) and protects users.

**Common bug**: using the same path for liveness and readiness. The liveness probe should test "is the process alive and the event loop unblocked", not "can I reach the database". If the database is down, you don't want every replica to restart — you want them to fail readiness and drain.

## Common Pitfalls

- **No PodDisruptionBudget** → voluntary disruptions (node drain, cluster autoscaler) can evict all replicas at once.
- **Resource limits set, requests unset** → kubernetes defaults requests to limits, creating a Guaranteed pod that may be wildly over-provisioned.
- **`imagePullPolicy: Always` on `latest` tag** → non-reproducible deploys, slow rollouts. Pin by digest.
- **No `topologySpreadConstraints` or anti-affinity** → all replicas land on one node, one node failure = full outage.
- **CPU limits on latency-sensitive workloads** → CFS throttling spikes tail latency under load.
- **No `runAsNonRoot` / `readOnlyRootFilesystem`** → easy escalation surface for compromised containers.

## Related Concepts

- [[implements::Four Golden Signals (Latency, Traffic, Errors, Saturation)]]
- [[relatedTo::SLO Error Budgets and Multi-Window Multi-Burn-Rate Alerts]]
- [[buildsOn::GitOps Progressive Delivery — Argo CD vs Flux, Canary, Blue-Green]]

## References

- Resource management for pods: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
- Pod QoS: https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/
- Node-pressure eviction: https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/
- Probes: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
- PodDisruptionBudget: https://kubernetes.io/docs/tasks/run-application/configure-pdb/
- Pod security standards: https://kubernetes.io/docs/concepts/security/pod-security-standards/
