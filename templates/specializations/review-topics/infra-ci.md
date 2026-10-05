# Infrastructure & CI review topic

Lens for reviewing CI pipelines, build scripts, hooks, containers, and
infrastructure-as-code changes. Findings carry severity, `file:line`, and
evidence.

## Pipeline correctness

- Every gate actually gates: a red stage blocks what it claims to block.
  A non-blocking "informational" check presented as a gate is a finding.
- Exit statuses propagate: no `|| true`, no pipe that swallows a failure
  (pipefail or equivalent), no command whose failure leaves the step green.
- A tally printed at the end is not a piped exit status — the step must fail
  on the count, not display it.
- Caches: keyed on the inputs that actually affect the output; a stale cache
  that can pass old artifacts into a "new" run is a finding.
- Secrets: never echoed, never in logs, never in cache artifacts; masked
  where the CI supports masking; injected at the narrowest scope.

## Environment divergence

- Local vs CI differences named and minimized: same tool versions (pinned),
  same OS assumptions or explicit per-OS legs, same env-var contract.
- A new CI-only context (service, container, runner capability) gets a
  matching local check the same cycle, or the divergence is documented as
  accepted with rationale.
- Tests that depend on ambient state (default ports, running services, user
  config) must declare or provision it — green-on-my-machine is a finding.
- Cross-platform siblings: a `.sh` change has its `.ps1` sibling updated in
  the same change where one exists; hooks/scripts parity is enforced by the
  repo's own parity checks.

## Reproducibility & pinning

- Dependency versions pinned or lockfile-committed; floating `latest` tags in
  anything load-bearing (base images, actions, tools) are findings.
- Build hermeticity: no network fetch inside a step that claims offline
  operation; no hidden reliance on pre-installed runner software without a
  check.

## Infrastructure-as-code

- Declarative manifests reviewed for: resource limits, health probes,
  privilege level (no root/container-escape by default), secret references
  (names, never values), and destructive operations behind explicit flags.
- Rollout has a rollback: previous version retained; migration steps
  additive-first.
- Ports/endpoints match the documented service table; a changed default is
  reflected in every doc that names it in the same change.

## Release gates

- No tag/release with any known-red workflow on the branch (release, CI,
  smoke, lint, security scans — informational ones included per repo rule).
- Version pins consistent across manifests, lockfiles, and docs in the same
  commit.
