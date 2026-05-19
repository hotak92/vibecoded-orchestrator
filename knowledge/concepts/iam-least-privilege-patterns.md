---
title: IAM Least-Privilege Patterns
type: concept
tags: [devops, security, infrastructure, IAM, mid-level-architecture, cloud, access-control]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# IAM Least-Privilege Patterns

Least-privilege is the principle that every identity (human, service, workload) is granted only the permissions strictly required for its declared job — and no more. The practice is hard because (a) declaring the job exhaustively is laborious, (b) tooling defaults are usually permissive, and (c) over-time accretion ("we added one more permission so X would work") is invisible to most workflows. This node is the *patterns* for writing IAM policies that hold up over time.

The five anti-patterns to refuse, and the corresponding patterns to apply:

## 1. Wildcard Actions

Anti-pattern:
```json
{ "Effect": "Allow", "Action": "s3:*", "Resource": "*" }
```

Two wildcards: every action, every resource. This is almost never necessary; the only routine justifications are administrative roles for break-glass access (separate, MFA-gated) and AWS service-linked roles.

Pattern — enumerate actions; group by lifecycle:

```json
{
  "Statement": [
    {
      "Sid": "ReadOwnObjects",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:HeadObject"
      ],
      "Resource": "arn:aws:s3:::my-app-uploads/users/${aws:userid}/*"
    },
    {
      "Sid": "WriteOwnObjects",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::my-app-uploads/users/${aws:userid}/*",
      "Condition": {
        "StringEquals": {
          "s3:x-amz-server-side-encryption": "AES256"
        }
      }
    }
  ]
}
```

GCP and Azure have analogous patterns: GCP custom roles enumerate `includedPermissions` (e.g. `storage.objects.get`); Azure RBAC custom roles enumerate `actions` / `dataActions`. Cloud-specific syntax differs; the *principle* is identical — enumerate, don't wildcard.

Reviewer check: every `*` in `Action` is a defect to be argued for, not a default. The only defensible wildcards are within an action family that genuinely shares lifecycle (e.g., `logs:CreateLogStream` + `logs:PutLogEvents` for a log-writing role — and even then list explicitly when feasible).

## 2. Wildcard Resources

Anti-pattern: `"Resource": "*"` for a non-administrative action. The wildcard hides scope drift: today the workload accesses one bucket, tomorrow it accesses every bucket in the account because no one revisited the resource clause.

Pattern — pin to ARN with path prefix:

```json
{
  "Resource": [
    "arn:aws:s3:::my-app-uploads",
    "arn:aws:s3:::my-app-uploads/*"
  ]
}
```

The two-ARN form is the canonical S3 pattern: bucket-level actions (`s3:ListBucket`) need the bucket ARN; object-level actions (`s3:GetObject`) need the object ARN. Mixing them in one `Resource` array under one statement only works if the actions accept both shapes — usually they don't. Split into two statements when in doubt.

For multi-region or multi-account resources, use ARN variables:

```json
{
  "Resource": "arn:aws:dynamodb:${aws:RequestRegion}:${aws:PrincipalAccount}:table/AppData"
}
```

## 3. Missing Conditions

A policy without `Condition` blocks is doing only half the work. The condition restricts *when* / *from where* / *under what circumstances* the action is allowed.

Core condition patterns (AWS examples; analogous keys exist in GCP IAM Conditions and Azure ABAC):

**Require MFA for sensitive actions**:
```json
{
  "Effect": "Allow",
  "Action": "iam:DeleteUser",
  "Resource": "*",
  "Condition": {
    "Bool": { "aws:MultiFactorAuthPresent": "true" },
    "NumericLessThan": { "aws:MultiFactorAuthAge": "3600" }
  }
}
```

**Lock cross-account assume-role to a specific external ID** (prevents the confused-deputy attack):
```json
{
  "Effect": "Allow",
  "Principal": { "AWS": "arn:aws:iam::THIRDPARTY:role/their-role" },
  "Action": "sts:AssumeRole",
  "Condition": {
    "StringEquals": { "sts:ExternalId": "unique-string-shared-with-third-party" }
  }
}
```

**Restrict by tag** (attribute-based access control, ABAC):
```json
{
  "Effect": "Allow",
  "Action": "ec2:StartInstances",
  "Resource": "arn:aws:ec2:*:*:instance/*",
  "Condition": {
    "StringEquals": {
      "aws:ResourceTag/Environment": "${aws:PrincipalTag/Environment}"
    }
  }
}
```

The principal tag matches the resource tag — a user tagged `Environment=dev` can only touch resources tagged `Environment=dev`. ABAC scales with new resources; no policy update needed when a new instance is launched with proper tags. Other useful condition keys: `aws:SourceVpc`, `aws:SourceIp` (network), `aws:ViaAWSService` (whether call came through another AWS service), `aws:CurrentTime` (time-of-day restrictions).

## 4. Trust Policy Sprawl

The trust policy (in AWS: `AssumeRolePolicyDocument`) is the *gate* for "who can assume this role". A widened trust policy is silent privilege expansion. Anti-patterns:

```json
// Trust the whole account — anything in the account can assume this role
{ "Principal": { "AWS": "arn:aws:iam::123456789012:root" } }

// Trust everyone everywhere
{ "Principal": "*" }

// Trust a service without conditions — any usage of that service can assume
{ "Principal": { "Service": "lambda.amazonaws.com" } }
```

Pattern — specific principal + condition:

```json
{
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "lambda.amazonaws.com" },
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {
        "aws:SourceAccount": "123456789012"
      },
      "ArnLike": {
        "aws:SourceArn": "arn:aws:lambda:us-east-1:123456789012:function:specific-function-name"
      }
    }
  }]
}
```

`aws:SourceArn` pins the trust to a specific calling resource; `aws:SourceAccount` prevents cross-account impersonation. Both are necessary for the "confused deputy" mitigation.

## 5. Service Account Sprawl (Kubernetes)

Kubernetes inherits the same problem. The Pod-level pattern:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: api-server
  namespace: api
  annotations:
    # AWS IRSA — map this SA to a specific IAM role
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/api-server-role
automountServiceAccountToken: false   # default-deny token mount
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    spec:
      serviceAccountName: api-server
      automountServiceAccountToken: true   # mount only on the pod that needs it
```

Two patterns visible here:

1. **Per-workload SA, never the namespace's `default`**. The `default` SA accumulates permissions over time as ad-hoc bindings; tracking ownership becomes impossible.
2. **`automountServiceAccountToken: false`** on the SA, override to `true` on the specific Pod that needs the API server. Most pods don't need to call the k8s API.

The RBAC binding then targets just that SA:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  namespace: api
  name: read-own-configmaps
rules:
- apiGroups: [""]
  resources: ["configmaps"]
  resourceNames: ["api-config"]    # specific configmap, not "*"
  verbs: ["get", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  namespace: api
  name: api-server-read-config
subjects:
- kind: ServiceAccount
  name: api-server
  namespace: api
roleRef:
  kind: Role
  name: read-own-configmaps
  apiGroup: rbac.authorization.k8s.io
```

Note the `resourceNames` — most k8s RBAC examples omit it, which silently allows the verbs on *every* ConfigMap in the namespace. `resourceNames` pins to specific named objects.

## Boundary Pattern (Permissions Boundary)

A *permissions boundary* in AWS is a meta-policy: the user / role can have any policy, but the *intersection* of their policies and the boundary is the effective permission set. Pattern for delegated admin:

```json
// PermissionsBoundary for "developer" — devs can grant themselves anything within this
{
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:*", "dynamodb:*", "lambda:*", "logs:*"],
      "Resource": "*"
    },
    {
      "Effect": "Deny",
      "Action": ["iam:*Boundary*", "iam:DeleteRole"],
      "Resource": "*"
    }
  ]
}
```

This lets developers self-serve IAM roles for their workloads but prevents them from removing or modifying their own boundary. Boundary + ABAC tags is the scaling pattern for "many teams, one cloud account" — without it, central security becomes a ticket queue.

## Auditing Patterns

Permissive policies decay over time. Required hygiene:

- **CloudTrail / GCP Audit Logs / Azure Activity Logs enabled** on all account / project / subscription resources. Without an audit trail, "least privilege" is unverifiable.
- **Access Analyzer (AWS) / Policy Intelligence (GCP) periodically reviewed**. These tools flag policies granting access to unintended principals.
- **Quarterly "permissions used vs granted" review** for production roles. AWS IAM provides "last accessed" data per service; remove services not touched in 90 days.
- **Unused-role cleanup**. A role with no recent assumption events is either dead code or a backdoor; either way it should not stay.

## Pre-Merge Checklist (Minimal)

For any PR touching IAM:

- [ ] No `Action: "*"` or `Resource: "*"` introduced without explicit justification in PR description.
- [ ] No `Principal: "*"` or `Principal: { AWS: "...:root" }` in trust policies.
- [ ] Conditions present on sensitive actions (delete, write, IAM, KMS, STS).
- [ ] No `Condition` block *removed* without explicit "why".
- [ ] Service accounts / managed identities are workload-specific, not shared.
- [ ] CloudTrail / audit logging not disabled or scoped down by the change.

[[relatedTo::Terraform Plan Review Discipline]] [[relatedTo::Kubernetes Manifest Review Discipline]] [[relatedTo::Supply-Chain Security (SLSA, SBOM, Sigstore)]] [[relatedTo::SRE Incident Response Playbook]]
