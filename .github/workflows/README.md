# GitHub Actions workflows — discipline + cost notes

This directory holds the CI/release workflows. The notes below capture
conventions that aren't enforced by tooling — losing them costs minutes
of runner time per push and silent regressions in coverage.

## Workflows in this directory

| File | Trigger | Purpose | Approx wall time |
|---|---|---|---|
| `ci.yml` | push to main, PR | Rust + Python + Frontend tests + leak-check + managed-paths | ~9 min |
| `codeql.yml` | push to main, PR, weekly Mon 06:00 UTC | Security analysis (Python + JS/TS) | ~3 min |
| `hook-parity.yml` | push to main, PR | `.sh` ↔ `.ps1` hook parity gate + `set -e/-eo pipefail` discipline | ~25 s |
| `release.yml` | tag `v*.*.*`, manual dispatch | 3-OS matrix build, GitHub release, dist-binary auto-commit | ~20 min |
| `step22-multi-project-access-matrix.yml` | path-scoped PR | 2×3 matrix (Podman/Docker × OS) regression for v0.2.21 access-matrix | ~10 min |
| `dependabot-auto-merge.yml` | dependabot PRs | Auto-merge low-risk dependency bumps | ~30 s |

## Editing a workflow YAML — always `act --dryrun` first

GitHub doesn't validate workflow YAML until it actually runs, so a typo
in `on:`, a misplaced `jobs:` key, or an invalid `runs-on:` produces a
"push → 30s wait → see syntax error → fix → repush" loop. Each iteration
burns one full run setup (~15-20s) and clutters the Actions tab.

**Before pushing any `.github/workflows/*.yml` edit**, run `act --dryrun`
locally:

```bash
# Install `act` once (Linux/macOS): https://github.com/nektos/act
# Linux: curl https://raw.githubusercontent.com/nektos/act/master/install.sh | sudo bash
# macOS: brew install act

# Dry-run the workflow that the just-edited file defines:
act --dryrun -W .github/workflows/<file>.yml

# Or dry-run all workflows the most recent commit would trigger:
act --dryrun push
```

`act --dryrun` parses the YAML, resolves matrix entries, and prints the
job graph — without actually running anything. Catches:

- YAML syntax errors
- Invalid `runs-on:` values
- Referenced secrets / vars not defined
- Bad action references (`uses:` typos)
- Job dependency cycles (`needs:` mistakes)

It does NOT validate action-script semantics — that needs an actual run.
But ~90% of "trivial workflow-edit oops" pushes are caught by `--dryrun`.

For workflows that themselves run a build step (`ci.yml`, `release.yml`),
you can also run the actual Linux job locally:

```bash
act push -j rust       # Run the Rust matrix entry from ci.yml
act push -j python     # Run the Python matrix entry
act -W .github/workflows/hook-parity.yml  # Whole workflow
```

Caveats:
- `act` runs Linux jobs only. macOS + Windows jobs can't be simulated.
- `act` uses a Docker container with the same Ubuntu image GitHub uses,
  but network / secret access differs slightly. Treat as "high
  confidence" not "100% green-on-GitHub guaranteed".
- The Release workflow's full 3-OS matrix can't be validated this way.

## Cost discipline

### paths-ignore on docs-only changes (added v0.2.30)

`ci.yml`, `codeql.yml`, and `hook-parity.yml` all skip when the only
changed paths match the **pure-docs** allowlist below. This list is
deliberately narrow — DO NOT broaden it to `**.md` or `templates/**`
without checking what's actually under there.

```
knowledge/**
docs/**
.claude/context/**
README.md
CHANGELOG.md
LICENSE / LICENSE.*
CODE_OF_CONDUCT.md / CONTRIBUTING.md / SECURITY.md / CLA.md
BOOTSTRAP.md / KNOWN_ISSUES.md
.github/ISSUE_TEMPLATE/** / .github/PULL_REQUEST_TEMPLATE.md
.github/FUNDING.yml / .github/CODEOWNERS
```

**What's intentionally NOT ignored** (so a change to these DOES trigger
CI):

- `**.md` globally — many `.md` files are functional code, not docs:
  - `templates/agents/**/*.md` + `templates/skills/**/SKILL.md` — their
    YAML frontmatter is parsed by the keyword-suggest hook and
    validated by `tests/test_keyword_match.py`. A keyword rollout (e.g.
    PR #259) MUST trigger CI.
  - `CLAUDE.md` — affects Claude Code behavior in every install.
  - `launcher/**/*.md`, `internal/**/*.md` — sometimes contain
    subtree migration notes or release packaging details still under
    test.
- `templates/**` globally — templates are SHIPPED CODE (hooks, scripts,
  settings.json, agents, skills). Every change needs CI.
- `.claude/agents/`, `.claude/skills/`, `.claude/hooks/` — same as
  templates.

Pre-v0.2.30 a docs touch ran the full ~9-min CI matrix (zero signal
value). If a future test exercises any path currently in the ignore
list, REMOVE that specific entry — don't remove `paths-ignore`
wholesale. See the long comment at the top of `ci.yml` for the policy
rationale.

`step22-multi-project-access-matrix.yml` uses the inverse pattern
(`paths:` allowlist) — only fires when relevant hub/launcher-core code
changes. Already optimal; no `paths-ignore` needed.

`release.yml` runs on tag push — no paths optimization possible (tags
are atomic).

### concurrency / cancel-in-progress

All non-Release workflows set `concurrency.cancel-in-progress: true` on
PR triggers — pushing a fix commit to a PR cancels the prior run. Saves
minutes on iteration-heavy PRs. Don't remove without good reason.

## Deferred for v0.2.30+

These were considered for v0.2.29 but deferred for focused testing:

### 1. Cargo + pip cache via `actions/cache@v4`

Pre-v0.2.30 every Rust job re-downloads + recompiles all 200+ crate
dependencies (~30-45s of the 3-min `cargo test --lib` wall time).
Adding:

```yaml
- uses: actions/cache@v4
  with:
    path: |
      ~/.cargo/registry
      ~/.cargo/git
      launcher/src-tauri/target
    key: ${{ runner.os }}-cargo-${{ hashFiles('**/Cargo.lock') }}
    restore-keys: |
      ${{ runner.os }}-cargo-
```

…cuts ~25-30s per Rust job. Same pattern for Python:

```yaml
- uses: actions/cache@v4
  with:
    path: ~/.cache/pip
    key: ${{ runner.os }}-pip-${{ hashFiles('requirements*.txt') }}
```

**Why deferred**: cache key design needs care — too tight a key (e.g.
hash on every Cargo.toml) misses on every commit; too loose (e.g. just
`{{ runner.os }}`) poisons across branches with conflicting versions.
Worth a focused PR with restore-keys laddering, not bundled into a
release commit.

### 2. Local Linux build for Release workflow

The Release workflow's 3-OS matrix burns ~12 min on the Linux job alone.
If the local build environment is set up (cargo + tauri toolchain), we
can build the Linux dist binary locally before tagging, commit it to
`launcher/dist/linux-x64/`, and skip the GitHub Linux matrix entry via:

```yaml
build:
  strategy:
    matrix:
      include:
        - os: ubuntu-latest
          # ...
  steps:
    - name: Skip Linux build if locally-built binary present
      if: matrix.os == 'ubuntu-latest' && hashFiles('launcher/dist/linux-x64/vct-launcher') != ''
      run: echo "Skipping Linux build — binary already committed"
      # ... rest of steps gated on the same condition
```

Pairs with a new `scripts/build-locally.sh` that wraps the cargo + tauri
commands the Release workflow runs, producing identical output.

**Why deferred**: requires verifying the binary path + metadata.json
shape matches exactly what Release expects (signed sha256, metadata
fields, file permissions on Linux). Easy to get wrong silently — needs
a focused PR with a manual `release.yml` dry-run + binary-diff check
before merging.

### 3. Per-project `.gitignore` management for `.claude/state/`

`vco_lib/project_init.py` / `install.py` don't currently add patterns to
user-project `.gitignore` files. As of v0.2.29, hooks write per-session
dedup state to `<project>/.claude/state/` — gitignored at the orchestrator
level but not necessarily at per-project install sites. Users either
already have `.claude/` patterns or VCO docs them to add manually.

A v0.2.30 enhancement: detect missing `.claude/state/` in project
`.gitignore` on `install-bundle --update` and emit a `gitignore_advice`
deferral entry recommending the addition. Non-blocking, advisory only.

## Workflow editing checklist

Before pushing any workflow edit:

- [ ] `act --dryrun -W .github/workflows/<file>.yml` — passes
- [ ] If changing triggers: `paths` / `paths-ignore` covers all relevant paths
- [ ] If changing matrix: `act push -j <job>` runs Linux locally (where applicable)
- [ ] `concurrency.cancel-in-progress` preserved on PR triggers
- [ ] If the edit changes runner minutes substantially, update this README
