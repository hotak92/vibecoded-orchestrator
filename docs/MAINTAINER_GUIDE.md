# Maintainer Guide

Operations docs for the orchestrator's maintainer + admins. End users don't need any of this.

---

## Release-pipeline secrets

### DIST_COMMIT_TOKEN — auto-commit dist binaries back to main

**What it does**: the release workflow's `commit-dist-binaries` job (in `.github/workflows/release.yml`) downloads the freshly-built launcher binaries from the matrix-job artifacts and commits them into `launcher/dist/<os>-<arch>/` on `main`. This keeps `git clone` users on a binary that matches HEAD — avoids the v0.2.13 regression where a 5+ month old binary lingered in the committed dist tree.

**The problem this solves**: GitHub's branch-protection ruleset on `main` ("enforce PR + tests on main") requires PR-based changes. The auto-commit step needs to bypass that. Two ways:

#### Option A (org-owned repos): `bypass_actors`

For repos owned by a GitHub organization, the auto-commit job's default `GITHUB_TOKEN` belongs to the GitHub Actions integration (actor ID `41898282`). Add the Actions integration to the ruleset's `bypass_actors`:

```bash
gh api --method PUT repos/<owner>/<repo>/rulesets/<ruleset-id> --input - <<'JSON'
{
  "bypass_actors": [
    { "actor_id": 41898282, "actor_type": "Integration", "bypass_mode": "always" }
  ]
}
JSON
```

`bypass_mode: "always"` lets the integration bypass even without PR review. `pull_request` is the more restrictive alternative.

This path is **unavailable for user-owned repos** — GitHub's API rejects the PUT with "Actor integration must be part of the ruleset source or owner organization".

#### Option B (user-owned repos): `DIST_COMMIT_TOKEN` repository secret

For user-owned repos (e.g. `hotak92/vibecoded-orchestrator`), use a fine-grained Personal Access Token (PAT) as a repository secret. The auto-commit job's `actions/checkout` step picks up this token instead of the default `GITHUB_TOKEN`. PATs belonging to a repo admin bypass ruleset enforcement.

**One-time setup**:

1. **Create the fine-grained PAT** (https://github.com/settings/tokens?type=beta):
   - Token name: `vco-dist-commit-token`
   - Expiration: pick a renewable cadence (1 year is common).
   - Repository access: "Only select repositories" → pick the orchestrator repo only.
   - Permissions: **Contents: Read and write** (everything else: No access).
   - Click "Generate token" + copy the value once (GitHub doesn't show it again).

2. **Store as repository secret**:
   ```bash
   gh secret set DIST_COMMIT_TOKEN --repo <owner>/<repo>
   # paste the PAT value when prompted
   ```

3. **Verify**: trigger a workflow_dispatch on `release.yml` and confirm the `commit-dist-binaries` job's "Commit and push updated binaries" step succeeds (look for "main → main" in the push output rather than a 403).

4. **Rotation**: when the PAT expires, regenerate it + re-run `gh secret set DIST_COMMIT_TOKEN`. No workflow changes needed.

**Why this works**: the workflow uses `${{ secrets.DIST_COMMIT_TOKEN || secrets.GITHUB_TOKEN }}` — if the secret is set, that's the token; otherwise it falls back to `GITHUB_TOKEN` (which will 403 against a protected branch). The fallback means the workflow doesn't fail-loud on a fresh fork without the secret; it just doesn't auto-commit.

#### What the auto-commit DOES + DOESN'T do

DOES:
- Skip the commit entirely if all three OS binaries are byte-identical to what's at HEAD (no churn).
- Stage ONLY `launcher/dist/*/vct-launcher[.exe]` + `.metadata.json` files (other paths in `launcher/dist/` are guarded against).
- Use a `github-actions[bot]` author identity, not the tag-pusher's identity.
- Honor a `workflow_dispatch` input `skip_dist_commit: true` for dry-runs.

DOESN'T:
- Push the release tag itself (the original tag-push triggered the workflow; the auto-commit lands the binaries on `main` as a separate commit AFTER the tag).
- Touch the release artifacts on GitHub Releases (those are uploaded by the matrix build jobs).
- Re-build or re-test the binaries — the auto-commit just re-uses the matrix artifacts.

---

## Branch protection ruleset

The orchestrator's `main` branch is protected by a ruleset (default name: `enforce PR + tests on main`) with these rules:
- `pull_request`: requires PRs (no direct pushes).
- `required_status_checks`: `Python (pytest)`, `Rust (cargo test --lib)`, `Frontend (svelte-check)`.
- `deletion`, `non_fast_forward`: prevent destructive operations.

**For one-off direct pushes from an admin** (release-bump commits, etc.) where bypass actors aren't configured:

```bash
# Disable, push, re-enable. Atomic from the admin's perspective.
gh api -X PUT repos/<owner>/<repo>/rulesets/<ruleset-id> -f enforcement=disabled --jq '.enforcement'
git push origin main
gh api -X PUT repos/<owner>/<repo>/rulesets/<ruleset-id> -f enforcement=active --jq '.enforcement'
```

The ~30-second window where enforcement is `disabled` is the only protection gap. Mitigation: run the three commands in sequence in a single shell snippet so a context switch can't leave it disabled.

A `disabled` enforcement state shows up as "Inactive" in the GitHub UI ruleset panel. Verify post-push that it's back to `active`.

---

## Release workflow

The full release flow (per commit to a `v*.*.*` tag):

1. **Three matrix jobs** build OS-specific launcher binaries + archive zips. Each uploads `vct-launcher-<target>` + `vibecoded-orchestrator-<version>-<target>` artifacts.
2. **GitHub Release** is auto-created from the matrix outputs (`actions/release-action` or similar).
3. **`commit-dist-binaries` job** (post-v0.2.13 addition) downloads the matrix binaries + commits them to `main` IF either bypass option above is configured.

Build time: ~15-18 minutes for all 3 OSes. Auto-commit adds ~30 seconds.

If the auto-commit fails (no bypass configured), the release artifacts are still published to GitHub Releases — only the committed dist tree on `main` stays stale. Manual fix:

```bash
cd /tmp && mkdir vco-vX.Y.Z && cd vco-vX.Y.Z
for tgt in linux-x64 windows-x64 macos-arm64; do
  gh release download vX.Y.Z --repo <owner>/<repo> --pattern "*${tgt}*"
  sha256sum -c vibecoded-orchestrator-X.Y.Z-${tgt}.zip.sha256
done
# extract binaries; cp into <repo>/launcher/dist/<target>/; commit; push
```

---

## v0.2.14 maintainer-specific deferred items

- Set `DIST_COMMIT_TOKEN` per Option B above. Until set, every release-tag push needs the manual ruleset-disable trick.
- Verify Fix 2 (Windows zip layout) on next tag push: unzip a `vibecoded-orchestrator-X-windows-x64.zip` and confirm the binary is nested at `<archive_base>/vct-launcher.exe` (matching Linux/macOS layout), not flat at zip root.
- Verify Fix 3 (KG-sync content-aware timestamp skip) on next `install.py --update`: post-update `git status knowledge/` should produce ZERO `updated:`-only modifications (previously: 60+ such files).
