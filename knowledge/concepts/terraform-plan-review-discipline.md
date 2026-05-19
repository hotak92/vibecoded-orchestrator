---
title: Terraform Plan Review Discipline
type: concept
tags: [devops, infrastructure, terraform, opentofu, code-review, mid-level-architecture, security, IaC]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Terraform Plan Review Discipline

Reviewing a `terraform plan` (or `tofu plan`) is a distinct discipline from writing HCL. A plan is a proposed state transition; the reviewer's job is to catch the *blast-radius surprises* and *invisible coupling* that the diff alone does not surface. This node is the review checklist — for the basics of the tool itself, see [[uses::Terraform / OpenTofu]].

Three review modes — apply each in order:

1. **Action audit** — what is being created, modified, destroyed, replaced.
2. **Blast-radius audit** — which dependents follow the changed resource.
3. **Drift / state-surgery audit** — does the plan reveal evidence that state and reality are out of sync.

## Action Audit — read the plan symbols

Plans render five action symbols. Each has a different risk profile:

| Symbol | Action | Default risk |
|---|---|---|
| `+` | create | Low if name doesn't collide; verify no name-squat |
| `~` | update in place | Low–medium; check whether the in-place attribute is actually in-place per the provider docs (some "updates" silently force replace) |
| `-/+` | destroy + create (replacement) | **HIGH** — this is destructive even when the resource type sounds inert |
| `-` | destroy only | **HIGHEST** — verify the resource is truly orphan |
| `<=` | read (data source refresh) | Informational |

The replacement symbol `-/+` is the single most common cause of production outages from "routine" applies. Any RDS instance, EBS volume, S3 bucket, persistent disk, Cloud SQL instance, EFS filesystem, or stateful k8s resource showing `-/+` is a stop-the-line event until you've verified the data-loss path (snapshot exists? deletion protection? cross-region replica?).

## Blast-Radius Cascades

Terraform's `depends_on` graph is implicit through references. A change to one resource cascades silently:

- **Security group rule change** → forces revalidation on every ENI attached → can drop active connections.
- **IAM role policy change** → propagates to every EC2/Lambda/Fargate task assuming the role; not eventually-consistent, but propagation can take 60–90 seconds during which active calls may 403.
- **Launch template version bump** → if attached to an ASG with `instance_refresh.strategy = "Rolling"`, triggers a fleet replacement.
- **VPC route table change** → can sever cross-AZ traffic mid-apply; routes are evaluated per-packet.
- **DNS record change** with low TTL → propagates fast; *with high TTL* → can leave clients pinned to a deleted target for hours.

Reviewer obligation: for every `-/+` and every non-trivial `~`, identify the set of dependent resources and ask "what happens to in-flight traffic during this change?". Plan output does not show this — you have to grep for the resource name in the broader module.

## IAM Expansion (Privilege Creep)

IAM diffs are the second-most-common review failure. The `~` (in-place update) symbol hides scope expansion when the change is a policy document. Mandatory checks:

- **New `Action`**: was a wildcard introduced (`s3:*`, `iam:*`, `kms:*`)? Wildcards on `iam:*` and `sts:*` enable privilege escalation.
- **New `Resource`**: did `Resource: "arn:aws:s3:::specific-bucket/*"` become `Resource: "*"`?
- **New `Principal`**: did a trust policy widen from a specific role to `AWS: "*"` or a cross-account principal?
- **Removed `Condition`**: condition blocks (`aws:SourceIp`, `aws:MultiFactorAuthPresent`, `aws:SourceArn`) being deleted is silent privilege expansion.
- **`NotAction` / `NotResource`**: deny logic inversions — read carefully; the negation flips intent.

When unsure, run `terraform plan -out=plan.tfplan && terraform show -json plan.tfplan | jq '.resource_changes[] | select(.type | startswith("aws_iam"))'` and inspect each IAM resource individually.

## Drift Indicators

Plan output can betray drift between Terraform state and real-world state. Smells:

- **A `~` change you didn't ask for** — the configuration is unchanged but Terraform proposes a "fix". Someone touched the resource out-of-band.
- **`update in-place` on `tags` where you didn't change tags** — often a sign of cloud-provider auto-tagging (AWS Backup, GuardDuty, Cost Explorer) that the config doesn't model.
- **`force replacement because: "id"` or similar opaque cause** — sometimes a provider upgrade reads attributes differently; treat as drift-adjacent.
- **Plan succeeds but `terraform refresh` shows different output than the last apply** — confirmed drift.

When drift is detected, do NOT just apply to "fix" it. First import the drift back into config (or reconcile the out-of-band change with the team), then apply. Otherwise you reintroduce the drift on the next manual change cycle.

## State Surgery Smells

State surgery (`terraform state rm`, `terraform state mv`, `terraform import`) is a sharp tool — necessary occasionally, dangerous always. Smells in a PR or change ticket:

- **"We just need to `terraform state rm` this and re-import"** — usually means someone deleted a resource in the console and wants to skip the cleanup. Force a real conversation first.
- **`moved {}` block in HCL** — legitimate use is refactoring (renaming a resource without destroying it). Verify the `from` and `to` resolve to the same logical resource. A wrong `moved` block silently destroys + creates.
- **`removed {}` block** — Terraform 1.7+ feature; removes from state without destroying. Confirm the resource will be managed elsewhere or is genuinely abandoned.
- **`terraform import` in a runbook** — every import is an admission that something exists outside IaC. Track these; each one is technical debt.

## Provider Version Bumps

A provider bump (`hashicorp/aws` 5.x → 5.y) can change plan output without any HCL change:

- New deprecation warnings → fix before they become errors.
- New default arguments → check whether the new default differs from what your environment has.
- Schema changes that force `~` updates on every existing resource → batch into a dedicated PR; never mix with feature work.

Provider upgrades belong in their own apply window with a fresh plan diff'd against the previous state.

## Workspace / Backend Sanity

Before clicking apply:

- `terraform workspace show` matches the environment you intend to change (prod vs staging vs dev).
- The backend (`s3`, `gcs`, `azurerm`, `tfc`) is the right one — wrong-backend applies write state to the wrong place, splitting state across two locations.
- `terraform plan` was generated against the same backend you're about to apply to. Stored plans (`-out=plan.tfplan`) are bound to the state version they were generated against — if state moved, the plan is stale.

## Pre-Apply Checklist (Minimal)

For every reviewed plan:

- [ ] No `-/+` on stateful resources without an explicit data-preservation plan.
- [ ] No IAM scope expansion without an explicit "why".
- [ ] No drift indicators in the plan output, OR drift is acknowledged and the apply will reconcile it.
- [ ] No state-surgery operation snuck into the plan.
- [ ] Workspace, backend, and target environment match the intent.
- [ ] Provider versions in `.terraform.lock.hcl` are committed and match what reviewers see.

[[uses::Terraform / OpenTofu]] [[relatedTo::Kubernetes Manifest Review Discipline]] [[relatedTo::IAM Least-Privilege Patterns]] [[relatedTo::Supply-Chain Security (SLSA, SBOM, Sigstore)]]
