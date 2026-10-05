# Deployment field guide

Specialisation depth for deployment strategy: platform selection, CI/CD
pipeline design, environment configuration, monitoring. Read this before
first production deployment, platform migration, or pipeline redesign.

## Platform selection

Analyze the app first: frontend/backend split, serverless vs long-running,
container needs, GPU needs, expected load, budget, team skills.

| Option | Fit |
|---|---|
| PaaS (Vercel/Netlify/Fly.io/Railway/Render) | Fast iteration, small teams, standard web apps |
| Cloud (AWS/GCP/Azure) | Variable load, global distribution, managed services |
| Self-hosted / on-prem | Regulatory requirements, predictable load, cost control, data locality |
| Hybrid | Migration in progress, compliance + cloud burst |

Weigh vendor lock-in against convenience; note the exit path before signing up.

## CI/CD pipeline design

- Stages: lint → typecheck → unit tests → build → integration tests → deploy.
- Automated gates: a red stage blocks the next; no manual override in CI.
- Environment-specific deployments (dev → staging → prod) with promotion,
  not rebuild.
- Zero-downtime: feature flags for risky changes; gradual rollout (10% →
  50% → 100%) with error-rate monitoring between steps; rollback = flip the
  flag, not a redeploy.

## Environment configuration

- Secrets: platform secret manager or Vault; never in code, logs, or committed
  `.env`. Rotation plan; least privilege per environment.
- Env-var organization: one documented list per environment; type-checked at
  startup so a missing var fails fast, not at 3 a.m.
- Configuration that changes behavior between environments should be minimal
  — same artifact, different config.

## Monitoring & observability

- Uptime monitoring on critical endpoints.
- Error tracking (Sentry-class) wired from day one.
- Metrics: latency histograms, error counters by type, saturation.
- Log aggregation with correlation/request IDs; JSON logs, ISO timestamps.
- Alerts: thresholds tied to user-visible symptoms, with escalation paths;
  every alert actionable.

## Pre-production review checklist

- Deploy is automated end-to-end (no manual steps).
- Secrets verified absent from repo history and logs.
- Rollback tested at least once.
- Monitoring catches a synthetic failure before users would.
- Cost estimate within budget at expected load.

## Related

- Review lens: `review-topics/infra-ci.md`
- Kubernetes manifest review lives in the `devops-reliability` pack
  (`k8s-manifest-reviewer` skill).
