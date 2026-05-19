---
title: GitOps Progressive Delivery — Argo CD vs Flux, Canary, Blue-Green
type: pattern
tags: [patterns, devops, SRE, infrastructure, kubernetes, gitops, argo-cd, flux, deployment, mid-level-architecture]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# GitOps Progressive Delivery — Argo CD vs Flux, Canary, Blue-Green

## What "GitOps" Means

GitOps is **declarative continuous delivery driven by a git repository as the source of truth**. The cluster runs a reconciliation agent that continuously diffs the live state against the repo and converges them. Properties:

- Git is the single source of truth for desired cluster state
- Changes happen through git (PR, merge), not `kubectl apply` from a laptop
- The reconciler pulls; it isn't pushed to (no CI credentials in the cluster)
- Drift is detected and auto-corrected
- Rollback = `git revert`

**Progressive delivery** layers gradual traffic-shifting (canary, blue-green) on top of GitOps so a bad release is caught by metrics or smoke tests before it reaches all users.

## Argo CD vs Flux — Decision Matrix

Both are CNCF graduated projects, both implement GitOps for Kubernetes. They differ in style:

| Aspect | Argo CD | Flux |
|---|---|---|
| **Mental model** | App-centric: one `Application` resource per deployment unit | Source-centric: `GitRepository` + `Kustomization`/`HelmRelease` |
| **UI** | Strong built-in web UI showing diff and sync state | UI is via add-ons (Weave GitOps UI) |
| **Multi-tenancy** | Projects, RBAC built around teams | Namespaces + `Kustomization` scope |
| **App-of-apps** | First-class (`ApplicationSet`) | Composition via `Kustomization` referencing other manifests |
| **Helm support** | Renders Helm via templating; can lose drift detection for some chart patterns | First-class `HelmRelease` controller |
| **OCI artifacts as source** | Supported | Supported |
| **Sync strategies** | Auto-sync, manual sync, hooks (PreSync/Sync/PostSync) | Drift correction continuous; not hook-based |
| **Notification** | Add-on (argo-cd-notifications) | First-class controller |
| **Best when** | You want operators to drive deploys via UI; multi-team multi-cluster org | You want a code-first, controller-composed CD with strong Helm/Kustomize support |

Pick **Argo CD** if you want a UI that ops/dev share daily. Pick **Flux** if you want a small set of composable controllers and your team is comfortable reading YAML.

Either tool by itself does GitOps but **not** progressive delivery — for that, pair with:

- **Argo Rollouts** (sister project to Argo CD): adds `Rollout` CRD with canary/blue-green strategies and analysis templates
- **Flagger** (Flux ecosystem): adds canary/blue-green/A-B with metric analysis via Prometheus/Datadog/etc.

## Canary vs Blue-Green vs Rolling

| Strategy | Resource cost | Rollback speed | Risk surface | Best for |
|---|---|---|---|---|
| **Rolling** (k8s default) | None (in-place) | Slow (must re-roll back) | Mixed-version traffic during rollout | Stateless services with strict back-compat; least risky changes |
| **Blue-Green** | 2× during cutover | Instant (flip selector/route) | All-or-nothing (full traffic shifts at once) | Stateful migrations, schema changes, when you need an instant rollback button |
| **Canary** | ~1.1× during rollout | Fast (1m to stop progression) | Per-step % of traffic | Default for high-traffic stateless services where you can measure impact |
| **A/B** (header/cookie routed) | ~1.1× | Instant routing change | Targeted user cohorts only | Feature experimentation, not deployment safety |

## Canary with Argo Rollouts (Example)

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: api
  namespace: prod
spec:
  replicas: 10
  selector:
    matchLabels: {app: api}
  template:
    metadata:
      labels: {app: api}
    spec:
      containers:
        - name: api
          image: ghcr.io/example/api:v1.2.3@sha256:abc...
          ports: [{containerPort: 8080}]
          # ... (readiness, resources etc.)
  strategy:
    canary:
      canaryService: api-canary       # secondary Service for canary pods
      stableService: api-stable       # primary Service for stable pods
      trafficRouting:
        istio:
          virtualService:
            name: api
            routes: [primary]
      steps:
        - setWeight: 5
        - pause: {duration: 5m}
        - analysis:
            templates:
              - templateName: success-rate
              - templateName: latency-p99
            args:
              - name: service-name
                value: api-canary
        - setWeight: 25
        - pause: {duration: 10m}
        - analysis:
            templates: [{templateName: success-rate}, {templateName: latency-p99}]
        - setWeight: 50
        - pause: {duration: 10m}
        - setWeight: 100
---
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: success-rate
spec:
  args:
    - name: service-name
  metrics:
    - name: success-rate
      interval: 1m
      successCondition: result[0] >= 0.99
      failureLimit: 3
      provider:
        prometheus:
          address: http://prometheus:9090
          query: |
            sum(rate(http_requests_total{service="{{args.service-name}}",code!~"5.."}[2m]))
              /
            sum(rate(http_requests_total{service="{{args.service-name}}"}[2m]))
```

The pause-then-analysis pattern is the safety mechanism: each step ramps traffic, holds, queries Prometheus, and aborts the rollout if the success rate drops below threshold.

## Blue-Green with Argo Rollouts

```yaml
spec:
  strategy:
    blueGreen:
      activeService: api-active        # serving production traffic
      previewService: api-preview      # green; receives preview traffic for smoke tests
      autoPromotionEnabled: false      # require manual promotion (or automated gate)
      scaleDownDelaySeconds: 600       # keep blue alive for 10m after cutover for fast rollback
      prePromotionAnalysis:
        templates: [{templateName: smoke-tests}]
        args: [{name: service-name, value: api-preview}]
```

After deploy, the new version (`green`) runs alongside the old (`blue`). A smoke-test analysis runs against `api-preview`. On pass, traffic flips. On fail, green is scaled down and nothing reaches users.

## Connecting GitOps + Progressive Delivery

The full picture:

```
Developer
   │ (PR + merge)
   ▼
git repo (cluster manifests, Rollout CRDs)
   │
   ├─ Argo CD reconciles → applies manifests to cluster
   │
   ▼
cluster
   │
   ├─ Argo Rollouts controller takes over the Deployment-like resource
   │
   ▼
progressive rollout
   │
   ├─ Each step pauses, queries Prometheus
   ├─ On metric-based abort, rolls back to previous version automatically
   │
   ▼
fully promoted to 100%
```

The CD tool (Argo CD / Flux) is responsible for *what* should be running; the progressive-delivery tool (Argo Rollouts / Flagger) is responsible for *how* it gets there safely.

## Pitfalls

- **Auto-sync with auto-prune in dev = good; in prod = scary.** Manual sync gate for prod environments; auto-sync only after passing pre-prod environments.
- **Helm chart upgrade hooks bypass Rollout strategy.** A `helm.sh/hook` annotation that runs a Job can leak into production while the canary is at 5%.
- **Mixed-version traffic during rolling deploys breaks contracts.** If v1.3.0 removes a field v1.2.0 reads, rolling deploys produce 500s for the duration. Use canary + back-compat windows.
- **Database migrations are not GitOps-native.** Schema changes require careful expand-contract: add column → backfill → switch reads → drop old column, each step a separate release.
- **`ApplicationSet` clusters list from a generator at controller startup**: adding a cluster to the generator doesn't roll out until reconcile.

## Related Concepts

- [[implements::Kubernetes Resource Model — Requests, Limits, QoS Classes, and Eviction]]
- [[implements::SLO Error Budgets and Multi-Window Multi-Burn-Rate Alerts]]
- [[buildsOn::Supply-Chain Security — SLSA Levels, SBOM, and Sigstore]]

## References

- Argo CD: https://argo-cd.readthedocs.io/
- Argo Rollouts: https://argoproj.github.io/argo-rollouts/
- Flux: https://fluxcd.io/docs/
- Flagger: https://flagger.app/
- CNCF GitOps Working Group, *GitOps Principles v1.0*: https://opengitops.dev/
- OpenGitOps: https://github.com/open-gitops/documents
- *Deployment Strategies* (Container Solutions): https://www.container-solutions.com/blog/deployment-strategies
