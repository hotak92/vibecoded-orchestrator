---
name: k8s-manifest-reviewer
description: Reviews Kubernetes manifests (Deployment, StatefulSet, Service, Ingress, NetworkPolicy, etc.) for resource limits, probes, security context, PDBs, anti-affinity, image pinning, and network policies before they merge. Invoke when the user shares a manifest YAML, when a chart/kustomization PR appears, or before applying to prod.
short_desc: review k8s manifests: limits, probes, security
keywords: [Kubernetes, kubernetes, k8s, Deployment, StatefulSet, NetworkPolicy, Helm, kustomize, kubectl]
model: opus
effort: high
---

# Kubernetes Manifest Reviewer (Opus)

**Purpose**: Read Kubernetes manifests (raw YAML, Helm charts post-render, Kustomize overlays) and identify production-readiness gaps. Distinct from `deployment-advisor` (platform strategy) and `security-reviewer` (general code security): this skill checks *the YAML itself* against a known list of production patterns.

**Model**: Opus 4.7 at high effort. Manifest review benefits from cross-resource reasoning (PDB needs Deployment, ServiceMonitor needs Service+Prometheus Operator, NetworkPolicy needs labelled pods).

## When to Invoke Autonomously

1. The user shares one or more Kubernetes YAML files for review
2. A PR contains `.yaml`/`.yml` files under `k8s/`, `manifests/`, `helm/templates/`, `kustomize/`
3. The user says "deploy this to the cluster" / "ready for prod?"
4. A `helm template` or `kustomize build` output is shared
5. The user is creating a new chart or Kustomize base

## DO NOT invoke for

- Non-Kubernetes YAML (Docker Compose, GitHub Actions, Ansible)
- Cluster-level operators/CRDs install (different concern — operator security review)
- Pure label/annotation-only changes (no behavioural impact)

## Checklist (Apply Per Resource Kind)

### Deployment / StatefulSet / DaemonSet

**Image pinning**
- ✅ Image tagged by digest: `image: registry/app:v1.2.3@sha256:abc...`
- ❌ `latest` tag
- ❌ Missing tag (resolves to `latest`)
- ❌ `imagePullPolicy: Always` with floating tag (slow rollouts)
- ⚠️ Tag like `v1.2.3` without digest (mutable, but at least named)

**Resources**
- ✅ Both `requests` and `limits` set
- ✅ `memory.request == memory.limit` for critical workloads (Guaranteed QoS)
- ⚠️ CPU `limit` set on latency-sensitive workloads (CFS throttling risk — see [[KG: Kubernetes Resource Model]])
- ❌ No resources block (BestEffort QoS, first to evict)
- ❌ `limits.memory < requests.memory` (invalid)
- ❌ Resources copy-pasted from example (e.g., 100m/128Mi for a real service)

**Probes**
- ✅ `readinessProbe` present (always)
- ✅ `livenessProbe` distinct from readiness (or intentionally omitted)
- ✅ `startupProbe` for slow-booting apps (JVMs, Rails, Django+migrations)
- ❌ Same path/port for liveness and readiness when they should differ
- ❌ Liveness probe touches a dependency (database, downstream service) → cascading failure
- ❌ `initialDelaySeconds` so low the app never gets to start
- ❌ No probe at all (k8s will route to a not-yet-ready pod)

**Security context**
- ✅ `runAsNonRoot: true`
- ✅ `runAsUser: <non-zero>` and `runAsGroup: <non-zero>` (or use restricted PSS)
- ✅ `allowPrivilegeEscalation: false`
- ✅ `readOnlyRootFilesystem: true` (mount `emptyDir` for writable areas)
- ✅ `capabilities.drop: ["ALL"]`
- ✅ `seccompProfile.type: RuntimeDefault`
- ❌ `privileged: true` (only for known node-agents)
- ❌ `hostNetwork: true` / `hostPID: true` / `hostIPC: true` without justification
- ❌ Mounting `/var/run/docker.sock` or host paths without rationale

**Topology**
- ✅ `topologySpreadConstraints` across `topology.kubernetes.io/zone` for replicas ≥ 3
- ✅ Pod anti-affinity (at least preferred) across `kubernetes.io/hostname`
- ❌ All replicas can land on one node (single-node failure = full outage)

**Graceful shutdown**
- ✅ `terminationGracePeriodSeconds` reasonable (default 30s, increase for slow drains)
- ✅ `lifecycle.preStop` exec sleep matches LB deregistration time (typical: 10–15s)
- ❌ No preStop, fast termination = in-flight requests dropped

**Environment / config**
- ❌ Secrets in `env` value (vs `valueFrom.secretKeyRef`)
- ❌ ConfigMap mounted as env when file would be appropriate (no live reload)
- ⚠️ `envFrom` of a ConfigMap with no projection list (every key becomes env, hard to audit)
- ❌ Hardcoded URLs to other namespaces/clusters

### PodDisruptionBudget

For every Deployment/StatefulSet with `replicas >= 2`, expect a matching PDB.

- ✅ `minAvailable` set (not `maxUnavailable`) for production workloads — clearer semantics
- ✅ `minAvailable: replicas - 1` typical (allow one disruption at a time)
- ❌ `minAvailable: 100%` (blocks all voluntary disruptions including node drains)
- ❌ `maxUnavailable: 1` when replicas = 1 (PDB does nothing)
- ❌ No PDB at all

### Service / Ingress

- ✅ `Service` uses correct port-name reference (`targetPort: http` matching `containerPort.name`)
- ❌ `Service` of `type: LoadBalancer` per app (cost; use Ingress or Gateway API)
- ❌ `Ingress` without TLS section
- ❌ Wildcard `host:` matching with no tenancy boundary
- ⚠️ Annotations on Ingress that contradict the IngressClass (e.g., `nginx.ingress.kubernetes.io/...` on a Gateway API resource)
- ✅ Gateway API (`HTTPRoute`, `Gateway`) for new work, Ingress for legacy

### NetworkPolicy

- ✅ Default-deny ingress + egress per namespace, then explicit allow-from rules
- ❌ No NetworkPolicy at all in `prod`/`production` namespaces
- ❌ NetworkPolicy that allows from `podSelector: {}` cluster-wide
- ❌ Egress allowing 0.0.0.0/0 when only specific external APIs are needed

### HorizontalPodAutoscaler / VerticalPodAutoscaler

- ✅ `minReplicas` ≥ 2 for production
- ✅ `maxReplicas` reasonable (not unbounded; budget guardrail)
- ✅ Scale on more than CPU alone (custom metrics for queue-driven workloads)
- ❌ HPA + manual `replicas` field on the Deployment (fights each other)
- ❌ HPA target utilization < 50% (too eager, churns)

### ConfigMap / Secret

- ❌ Secret value in `data:` rather than `stringData:` mistakenly base64-encoded twice
- ❌ Secret committed to git unencrypted (use SOPS / sealed-secrets / external-secrets)
- ❌ Secret with no `type:` declared (defaults to Opaque, fine, but flag `kubernetes.io/dockerconfigjson` etc. that need specific types)
- ⚠️ Large ConfigMap (> 1 MiB) — etcd has 1 MiB key size limit

### ServiceMonitor / PodMonitor (Prometheus Operator)

- ✅ Matches the Service's port name
- ✅ `interval` not absurdly low (default 30s is usually fine)
- ❌ ServiceMonitor in a namespace Prometheus isn't watching

## Cross-Resource Checks

- For every Deployment with `replicas >= 2`: PDB exists, anti-affinity or spread constraint exists
- For every Service exposed via Ingress: TLS configured, NetworkPolicy permits ingress controller's namespace
- For every ServiceAccount used by Deployment: RBAC binding is least-privilege (no `cluster-admin` unless justified)
- For every PVC: storageClassName matches an available class, requested size reasonable

## Output Format

```markdown
# Kubernetes Manifest Review

**Files reviewed**: N
**Resources**: <list, e.g. "Deployment/api, Service/api, PDB/api, NetworkPolicy/api">
**Recommendation**: READY | NEEDS CHANGES (blocking) | NEEDS CHANGES (advisory)

## Blocking Issues (must fix before merge)

1. **Deployment/api — missing readinessProbe**
   File: `k8s/api/deployment.yaml:42`
   Impact: k8s routes traffic to pods that haven't finished initialization → 503s during rollout
   Fix:
   ```yaml
   readinessProbe:
     httpGet: {path: /readyz, port: http}
     periodSeconds: 5
   ```

2. **Deployment/api — image uses `latest` tag**
   File: `k8s/api/deployment.yaml:18`
   Impact: non-reproducible deploys, can't pin a known-good version, cosign signature verification can't function
   Fix: `image: ghcr.io/example/api:v1.2.3@sha256:abc...`

## Advisory Issues (recommend fix this sprint)

1. **Deployment/api — CPU limit set**
   ...

## What's Done Well

- ✅ Pod anti-affinity present
- ✅ NetworkPolicy default-denies and explicitly allows ingress namespace
- ✅ All containers run as non-root with read-only root filesystem

## Suggested Companion Resources (not present, would improve posture)

- PodDisruptionBudget for Deployment/api (replicas=3, no PDB)
- HorizontalPodAutoscaler if traffic is variable
- ServiceMonitor for Prometheus scraping (if cluster runs Prometheus Operator)
```

## Quick Workflow Reference

**Search KG**:
```bash
.claude/scripts/kg-search search "kubernetes" --type concepts
.claude/scripts/kg-search search "pod security" --tags security
```

**For deep research**: invoke `hybrid_search("kubernetes production checklist 2026")`.

**Adjacent tools** (the user runs; the skill interprets output):
- `kubeconform` / `kubeval` — schema validation
- `polaris audit` — opinionated production-readiness checks
- `kube-score score` — composite scoring
- `trivy config <dir>` — security misconfiguration scanner
- `kubectl explain <resource>.spec.x.y` — verify field names exist in the cluster's API server version

## Success Metrics

- ✅ Every Deployment with replicas ≥ 2 leaves with a PDB and topology spread
- ✅ Every container leaves with non-root securityContext and dropped capabilities
- ✅ Images pinned by digest, secrets via Secret references not env values
- ✅ Probes are configured and distinguishable (liveness ≠ readiness purpose)
- ✅ Review actionable: every finding has file:line and a concrete fix
