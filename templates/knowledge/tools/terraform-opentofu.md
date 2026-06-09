---
title: Terraform and OpenTofu (Infrastructure-as-Code)
type: tool
tags:
- tool
- infrastructure
- iac
- terraform
- opentofu
- devops
- low-level-implementation
- best-practices
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Terraform and OpenTofu

Declarative Infrastructure-as-Code (IaC) tools that take a desired-state description of cloud resources (HCL files) and reconcile real infrastructure against it.

## What they are

**Terraform** (HashiCorp, since 2014) and **OpenTofu** (Linux Foundation fork, since Jan 2024) share the same configuration language (HCL — HashiCorp Configuration Language), the same provider ecosystem (1000+ providers for AWS, GCP, Azure, Kubernetes, GitHub, Datadog, etc.), and produce the same plan/apply output format. OpenTofu forked from Terraform v1.5 after HashiCorp re-licensed Terraform from MPL 2.0 to the source-available Business Source License (BUSL). For ~90% of workloads they are drop-in compatible; OpenTofu adds state encryption at rest, early-evaluation variables in `init`, and other features Terraform lacks.

Choose Terraform if you depend on HashiCorp's commercial features (HCP Terraform, Sentinel policies, Vault tight integration). Choose OpenTofu if you want OSS-only, want state encryption, or are uncomfortable with BUSL.

[[relatedTo::SRE Incident Responder]]
[[relatedTo::Terraform Plan Reviewer]]
[[relatedTo::GitOps Progressive Delivery]]

## The core loop

```
write HCL  →  terraform init  →  terraform plan  →  human review  →  terraform apply
   ↑                                                                          ↓
   └──────────────────  state file records reality  ←──────────────────────  ┘
```

1. **HCL files** (`*.tf`) declare resources: `resource "aws_s3_bucket" "logs" { bucket = "acme-logs" }`.
2. **`init`** downloads providers + modules and configures the backend (where state lives — usually S3, GCS, or HCP).
3. **`plan`** compares declared state → real state → outputs a diff.
4. **`apply`** executes the diff against the cloud APIs and updates state.
5. **State file** is the source of truth for "what we manage" — must be stored remotely with locking (DynamoDB / Postgres / GCS object versioning).

## Plan-output anatomy

The plan diff uses single-character symbols:

| Symbol | Meaning | Risk |
|---|---|---|
| `+` | Create new resource | Low — usually safe |
| `~` | Update in place | Low–Medium — read which attributes change |
| `-/+` | **Destroy + recreate (replacement)** | **HIGH** — stateful resources lose data |
| `-` | Destroy | HIGH if stateful, low if compute/stateless |
| `<=` | Refresh data source (read-only) | None |

A `(forces replacement)` annotation on an attribute means changing it triggers `-/+`. The summary line at the bottom (`Plan: 3 to add, 5 to change, 27 to destroy`) is the first thing to scan — large destroy counts in a small intentional change indicate a cascade.

For machine-readable analysis: `terraform show -json planfile.bin | jq` produces structured JSON of the plan.

## Best practices (2026)

### State management

- **Remote state with locking**: never use local state for anything shared. S3 + DynamoDB (Terraform) or S3 + native locking (OpenTofu 1.10+) is the standard.
- **One state file per environment per service**: don't put prod + staging + dev in one state. Failure blast radius = entire state.
- **State encryption**: OpenTofu 1.7+ supports native state encryption; for Terraform use S3 SSE + KMS.
- **Never edit state by hand**: use `terraform state mv` / `import` / `rm`. `moved` and `removed` blocks (HCL-native, since TF 1.1 / 1.7) are preferred over CLI surgery.

### Module discipline

- **Pin module versions exactly**: `version = "5.2.1"`, not `>= 5.0`. Major bumps can silently change resource behaviour.
- **Pin provider versions**: in `required_providers` block, use `~> 5.62` (allow patch) or `= 5.62.0` (strict).
- **Prefer registry modules** (Terraform Registry / OpenTofu Registry) over copy-pasting from blogs.
- **Keep modules small**: one logical unit per module (a "VPC", a "Postgres cluster"), not one mega-module per environment.

### Code organisation

```
infra/
├── modules/                       # reusable building blocks
│   ├── network/
│   └── postgres/
└── environments/
    ├── prod/
    │   ├── main.tf               # module instantiations
    │   ├── backend.tf            # remote state config
    │   ├── providers.tf
    │   └── variables.tf
    ├── staging/
    └── dev/
```

### Workflow

- **Plan in CI, apply manually** (or via a CD trigger with approvals). Never auto-apply to prod from a PR merge.
- **PR-driven**: every change goes through a PR; CI posts the plan as a comment; review the plan, not just the HCL.
- **Separate plans by concern**: provider upgrades, IAM changes, and feature work get separate PRs/applies. Bundling makes rollback harder.
- **Use workspaces or directories for envs** — workspaces share state backend config but isolate state; directories give full separation. Directories are more common for prod-grade setups.

### Security

- **Static analysis in CI**: `tfsec` / `checkov` / `tflint` catch common misconfigurations (public S3 buckets, wide-open security groups, unencrypted volumes, IAM `*` policies). Run them on every PR.
- **Cost preview**: `infracost` posts cost diff to PRs — flags accidental £10K/mo additions.
- **Policy-as-code**: Open Policy Agent (OPA/Conftest), Sentinel (HCP Terraform), or `terraform-compliance` enforce organisation rules (e.g. "no public S3 buckets", "all resources tagged with `owner`").
- **No secrets in HCL**: use Vault / AWS Secrets Manager / SSM Parameter Store (SecureString) data sources at plan time. Mark variables `sensitive = true` so they don't appear in plan output.

### Common foot-guns

- **Forgetting `lifecycle.prevent_destroy`** on irreplaceable resources (production databases, DNS zones, KMS keys).
- **Replacing a `random_id` / `random_password`** when not intended — every downstream resource using it replaces too.
- **Subnet/VPC replacement cascade**: changing a subnet's AZ replaces it, which replaces every ENI, which replaces every instance in it.
- **`tags` drift**: someone clicks in the AWS console, then the next plan tries to revert their tags. Use ignore-changes for tag keys managed elsewhere.
- **Provider major-version bumps** silently change defaults — read the upgrade guide and apply the bump on a quiet day.

## Tooling ecosystem

| Tool | Purpose |
|---|---|
| `tflint` | Lint for HCL style + provider-specific best-practices |
| `tfsec` / `trivy config` | Security misconfiguration scanning |
| `checkov` | Policy-as-code (broader than tfsec, covers more frameworks) |
| `terraform-docs` | Auto-generate module README from HCL |
| `infracost` | Cost diff on PR plans |
| `terragrunt` | Wrapper that adds DRY config across envs (popular but adds complexity) |
| `atlantis` / `env0` / `spacelift` | PR-driven apply automation |
| `pre-commit-terraform` | Local pre-commit hooks: `fmt`, `validate`, `tflint`, `tfsec` |

## When NOT to use Terraform/OpenTofu

- **Imperative one-off operations**: snapshotting a database for a migration, draining a node before maintenance — use the cloud CLI directly.
- **Application-level config**: don't use TF to deploy app code or run DB migrations. Use proper CD (Argo CD, Flux, GitHub Actions) for application layer; keep TF for infrastructure.
- **Tiny single-account/single-region setups**: a 50-line `aws-cdk` or even raw CloudFormation might be simpler. TF shines when you have multiple environments / accounts / regions.

## Sources

- HashiCorp Terraform docs: https://developer.hashicorp.com/terraform
- OpenTofu docs: https://opentofu.org/docs/
- OpenTofu fork rationale: https://opentofu.org/manifesto/
- Plan JSON format: https://developer.hashicorp.com/terraform/internals/json-format
- Common foot-guns survey: https://www.hashicorp.com/blog/terraform-state-best-practices
