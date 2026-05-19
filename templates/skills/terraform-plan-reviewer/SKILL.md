---
name: terraform-plan-reviewer
description: Reviews Terraform/OpenTofu plan output for destructive changes, drift, IAM expansions, hardcoded values, and unsafe resource recreations before apply. Invoke when the user shares plan output, when a CI plan job posts a diff to a PR, or before any non-trivial production apply.
model: opus
effort: high
---

# Terraform Plan Reviewer (Opus)

**Purpose**: Read a `terraform plan` (or `tofu plan`) output and identify changes that warrant human attention before `apply` — destructive replacements, blast-radius issues, IAM widening, drift, hardcoded secrets, and module/provider version concerns.

**Model**: Opus 4.7 at high effort. Plan review is pattern-matching at scale across many resource kinds; deep reasoning helps with cross-resource implications (e.g., subnet replacement triggers NAT gateway replacement triggers EIP reallocation).

## When to Invoke Autonomously

1. The user pastes Terraform/OpenTofu plan output (any length).
2. The user asks "is this plan safe to apply?" or "review this Terraform change".
3. A CI workflow comment containing a plan diff is shared.
4. The user is about to apply to a `prod`/`production`/`live` workspace.
5. A PR description includes a `Plan:` block from `tflint`/`tfsec`/CI.

## DO NOT invoke for

- Initial `terraform init` issues (use general debug)
- Pure refactors with zero plan diff (`No changes.`)
- HCL syntax errors before plan even runs
- Provider authentication problems (different concern)

## What This Skill Checks

### 1. Destructive Operations (the headline)

Plan diff symbols and what they mean:

| Symbol | Meaning | Severity |
|---|---|---|
| `+` | Create | low — usually safe |
| `~` | Update in-place | low/medium — read attributes changing |
| `-/+` | **Destroy then create (replacement)** | **HIGH** — data loss potential, ID change, dependency cascade |
| `-` | Destroy | HIGH if stateful, low if compute |
| `<=` | Read (data source refresh) | none |

Every `-/+` deserves a question: **what triggers the replacement?** Look at the `(forces replacement)` annotations on individual attributes:

```
  # aws_db_instance.primary must be replaced
-/+ resource "aws_db_instance" "primary" {
      ~ availability_zone = "us-east-1a" -> "us-east-1c" # forces replacement
      ...
    }
```

Replacement of a `aws_db_instance`, `aws_rds_cluster`, `aws_efs_file_system`, `aws_elasticache_cluster`, `google_sql_database_instance`, `azurerm_postgresql_server`, or any stateful resource = **stop and confirm with the operator** that data migration is intentional.

### 2. Blast-Radius Cascade

Some replacements look minor but cascade:

- `aws_subnet` replacement → all instances in the subnet replace, NAT gateway replaces, route table associations replace
- `aws_security_group` replacement → every ENI referencing it briefly loses network policy
- `kubernetes_namespace` deletion (with auto-prune) → every resource inside is deleted
- `random_id` replacement → every resource using it via interpolation replaces

Scan the plan for replacement counts: `Plan: 3 to add, 5 to change, 27 to destroy` with a small intentional change is a smell.

### 3. IAM Expansion

Look for changes to:
- `aws_iam_policy`, `aws_iam_role`, `aws_iam_role_policy_attachment`
- `google_project_iam_*`
- `azurerm_role_assignment`
- `kubernetes_role`, `kubernetes_cluster_role`

Specifically flag:
- `Action: "*"` or `Resource: "*"` anywhere
- `AssumeRolePolicyDocument` trust changes (who can assume this role)
- Service principals broadening (e.g., adding `"*"` to a Lambda invocation policy)
- New `iam:PassRole` permissions
- Bucket policy `Principal: "*"` (public)
- `s3:PublicAccessBlock` being removed

Example finding:
```diff
- "Resource": "arn:aws:s3:::reports-bucket/exports/*"
+ "Resource": "*"
```
→ **flag**: scope expansion from one bucket prefix to all S3.

### 4. Hardcoded Sensitive Values

Plan output redacts `sensitive = true` attributes as `(sensitive value)`. But many leaks happen via:
- String interpolation of secrets into non-sensitive fields
- `user_data` blocks containing API keys
- Helm chart `values` blocks via `kubernetes_manifest` resources
- `aws_ssm_parameter` of type `String` (not `SecureString`)
- Locals/variables with default values that look like secrets

Scan for patterns: `AKIA[0-9A-Z]{16}`, `ghp_[A-Za-z0-9]{36}`, `xoxb-`, `sk-`, JWT-shaped strings, hex strings 32+ chars long in non-`(sensitive)` positions.

### 5. Drift Indicators

Updates to attributes that "shouldn't" change suggest someone clicked in the console:
- `tags` reverting from console-added values
- `description` getting reset
- Route 53 records changing TTL
- Lambda `memory_size` or `timeout` reverting
- Security group rule additions/removals not in the diff intent

If a plan shows 12 changes to `tags` across unrelated resources, ask whether someone has been making console edits.

### 6. Provider/Module Version Changes

A plan that includes a provider major version bump can silently change resource behaviour. Look for:
- `~ required_providers` block changes
- `~ source` or `~ version` on a module call
- Provider plugin upgrade messages in the plan preamble

These deserve a separate, dedicated apply (don't bundle a provider upgrade with feature changes).

### 7. State Surgery Smells

- `terraform state mv` performed manually before the plan = state surgery; verify intent
- `moved` blocks in HCL = refactor, usually safe but worth confirming the source/destination IDs make sense
- `removed` blocks = takes a resource out of state without destroying — risky if the operator expected destruction

### 8. Apply-Time Risks

- Resources marked `(known after apply)` for everything = computed by provider, hard to predict actual outcome
- `lifecycle.prevent_destroy = true` being removed in this plan
- `depends_on` changes (might re-order operations)

## Output Format

```markdown
# Terraform Plan Review

**Workspace/Environment**: <prod|staging|dev>
**Total changes**: Plan: N to add, N to change, N to destroy
**Recommendation**: APPLY | APPLY WITH SUPERVISION | DO NOT APPLY

## High-Severity Findings (block apply)

1. **`-/+` aws_db_instance.primary (data loss risk)**
   - Trigger: `availability_zone` change forces replacement
   - Impact: full DB recreation, RDS snapshots needed before apply
   - Recommended action: take a manual final snapshot, plan migration window, consider `aws_db_snapshot` data source approach

## Medium-Severity Findings (require sign-off)

1. **IAM scope expansion on aws_iam_role.lambda_runner**
   - Resource changed from `arn:aws:s3:::reports-bucket/*` to `arn:aws:s3:::*`
   - Recommendation: scope to the specific buckets the lambda actually reads

## Low-Severity / Informational

1. 12 tag changes across unrelated resources — possible console drift, consider tag-only apply first
2. Provider `aws` version 5.45 → 5.62; review release notes for any deprecations

## Apply Order Recommendation

If approved, suggest applying in this sequence:
1. Tag-drift commit (low risk, baselines state)
2. IAM scope tightening (separate commit)
3. The actual feature change

## Open Questions for Operator

- Is the `aws_db_instance.primary` replacement intentional?
- Was the IAM `Resource: "*"` expansion deliberate, or copy/paste from another module?
```

## Quick Workflow Reference

**Search KG for established patterns**:
```bash
.claude/scripts/kg-search search "terraform plan review" --type concepts
.claude/scripts/kg-search search "IAM least privilege" --tags security
```

**For deep architectural questions**: invoke `hybrid_search("infrastructure-as-code review patterns")`.

**Tooling adjacent to this skill** (the user runs these; the skill interprets):
- `tflint`, `tfsec`, `checkov` — static analysis (catches many things this skill catches, but the *plan diff* perspective is unique)
- `terraform show -json planfile | jq` — machine-readable plan for scripted analysis
- `infracost` — cost diff of the plan

## Success Metrics

- ✅ Every `-/+` replacement is explicitly called out with cascade analysis
- ✅ IAM widening is caught before merge, not after
- ✅ Provider version bumps are isolated from feature changes
- ✅ Drift (console edits) is surfaced as a separate concern
- ✅ Review fits in a single PR comment; doesn't pad with the entire plan body
