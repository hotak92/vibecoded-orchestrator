# Supabase CI deploy — edge functions + DB migrations on release tag

**Audience**: orchestrator maintainers and private-fork operators. End users don't need any of this.

The release workflow (`.github/workflows/release.yml`, `supabase-deploy` job) auto-deploys every edge function under `launcher/supabase/functions/<name>/` and applies every migration under `launcher/supabase/migrations/` to the linked Supabase project. This avoids the v0.2.36 incident where the `rebind-admin-token` function code shipped in the launcher binary (which CALLS the function) but the function itself was never deployed to Supabase — every admin recovery request hit a 404.

Both setup steps (GitHub secrets + one-time migration-history reconciliation) must be done ONCE per Supabase project before the CI job will succeed. After that, every `v*.*.*` tag push triggers a fresh deploy automatically.

---

## What CI does and doesn't do

The `supabase-deploy` job is gated `needs: [build, commit-dist-binaries]` so it only runs after the binary build + auto-commit have succeeded. Layout:

| Step | Behaviour |
|---|---|
| `setup-supabase-cli` | Installs the Supabase CLI on the Ubuntu runner via the upstream install script. Fails the job fast (without touching the project) if the CLI install fails. |
| `link-project` | `supabase link --project-ref ${{ vars.SUPABASE_PROJECT_REF }}` using `SUPABASE_ACCESS_TOKEN` from secrets. Fails the job fast on bad-token / bad-ref. |
| `deploy-functions` | Iterates every directory under `launcher/supabase/functions/` (excluding `_shared/`) and runs `supabase functions deploy <name>`. Per-function failures are collected; the step finishes the full list before exiting non-zero, so one broken function doesn't mask drift in the others. The `--no-verify-jwt` flag is NOT passed — each function's `verify_jwt` posture is committed in `launcher/supabase/config.toml`, which the CLI honours automatically. |
| `apply-migrations` | `supabase db push --include-all` — applies any migration files in `launcher/supabase/migrations/` that aren't in the remote `schema_migrations` ledger yet. Fails the job if the push fails, including drift errors that mean the one-time reconciliation (below) hasn't been done. |

What CI does NOT do (intentional):

* It does NOT run `supabase migration repair`. That command mutates the `schema_migrations` ledger and is destructive — keep it out of CI. See the migration-history reconciliation runbook (linked below) for the one-time human-driven repair.
* It does NOT `supabase db pull`. The baseline schema gets pulled exactly once during reconciliation, then committed by the operator. CI never writes migration files back to the repo.
* It does NOT block the release. The job runs AFTER `build` + `commit-dist-binaries`, so a release where the binaries shipped fine but Supabase deploy fails is still a partial success — the GitHub Release is already cut, the binaries are downloadable, and the Supabase failure surfaces as a red job in the run summary. This is the intentional trade-off: a Supabase outage shouldn't block a launcher release.

---

## Required GitHub Actions secrets + variables

Configure these on the repo (once per project — both the canonical orchestrator repo and any private fork that ships to its own Supabase project).

### Secret: `SUPABASE_ACCESS_TOKEN`

* **What it is**: a personal access token for the Supabase CLI. Equivalent to logging in via `supabase login` interactively, but for CI.
* **How to mint**: https://supabase.com/dashboard/account/tokens → "Generate new token". Name it `vco-ci-deploy` or similar. There's no permission scope to pick — Supabase tokens are tied to the user account that mints them, and the CLI inherits whatever projects that account can access.
* **How to set**:
  ```bash
  gh secret set SUPABASE_ACCESS_TOKEN --repo <owner>/<repo>
  # paste the token when prompted
  ```
* **Rotation**: Supabase access tokens don't auto-expire. Rotate when a maintainer leaves the project; revoke from the same dashboard.

### Variable: `SUPABASE_PROJECT_REF`

* **What it is**: the project reference (the 20-char slug in the project URL, e.g. `ovpdtijpdchzlxbojhsg`).
* **Why it's a variable and not a secret**: the project ref is NOT sensitive — it's visible in the function URLs the launcher already embeds in its source (see `launcher/supabase/config.toml` and `VCThelpers/license/validator.py`). Using a variable instead of a secret makes the CI log readable when something goes wrong (the secret-masking would otherwise replace the ref with `***` in error output).
* **How to set**:
  ```bash
  gh variable set SUPABASE_PROJECT_REF \
    --repo <owner>/<repo> \
    --body 'ovpdtijpdchzlxbojhsg'
  ```
* **Fork override**: if you fork the orchestrator and ship to your own Supabase project, change ONLY this variable. Nothing else in the workflow knows the canonical ref — it's deliberately not hardcoded.

---

## Verifying a CI deploy succeeded

After a tag push, navigate to the run summary:

```
https://github.com/<owner>/<repo>/actions/workflows/release.yml
```

Look at the `supabase-deploy` job:

* **Green check** → all edge functions deployed + all migrations applied. Spot-check by hitting an endpoint:
  ```bash
  curl -sX POST "https://${SUPABASE_PROJECT_REF}.supabase.co/functions/v1/validate-tier" \
    -H 'Content-Type: application/json' \
    -d '{"license_key":"invalid"}' | jq .
  # Expect: { "valid": false, "reason": "invalid_license_key" }
  ```
* **Red X** → step output names which function or migration failed. See the manual-fallback section below.

You can also confirm function deploy timestamps from the Supabase dashboard: Project → Edge Functions → click each function → "Deployments" tab.

---

## Manual fallback — when CI deploy fails

If CI failed but the binaries were already published to the GitHub Release, you can deploy the Supabase side manually without re-cutting the tag. From a maintainer workstation with the Supabase CLI logged in (`supabase login`):

```bash
cd launcher/supabase
supabase link --project-ref <SUPABASE_PROJECT_REF>

# Deploy a single function:
supabase functions deploy <function-name>

# Or all of them in one shot (mirrors what CI does):
for fn_dir in functions/*/; do
  fn_name="$(basename "$fn_dir")"
  [ "$fn_name" = "_shared" ] && continue
  echo "Deploying $fn_name..."
  supabase functions deploy "$fn_name" || echo "FAILED: $fn_name"
done

# Apply any net-new migrations:
supabase db push
```

The function's `verify_jwt` posture comes from `config.toml` — no `--no-verify-jwt` flag needed.

---

## Forking — deploying to a different Supabase project

If you're maintaining a private fork and want to ship to your own Supabase project (rather than the canonical orchestrator project):

1. Create your own Supabase project from https://supabase.com/dashboard.
2. Override the two CI-config knobs:
   * `SUPABASE_PROJECT_REF` repository variable → your project's ref.
   * `SUPABASE_ACCESS_TOKEN` repository secret → an access token for an account that owns / has admin on your project.
3. Run the one-time migration-history reconciliation against YOUR project (see the next section). Forks that branched AT OR AFTER v0.2.21 likely have the same 8-ID drift as the canonical project; earlier forks have less drift and the operator must inspect their own `schema_migrations` ledger first.
4. Push a tag — your fork's CI deploys to YOUR project.

Nothing in the workflow file knows the canonical Supabase project ref — it's loaded entirely from `${{ vars.SUPABASE_PROJECT_REF }}`. The only place the canonical ref appears in source is documentation / read-side validator code (`VCThelpers/license/validator.py`, the README), and those callers are launcher-build-time concerns, not CI concerns.

---

## Known limitations / open questions

* **Supabase CLI version**: the workflow uses whatever the upstream `supabase/setup-cli@v1` action ships at run time (`version: latest`). If a future feature in this workflow needs a specific CLI version (e.g. v2.101+ for a new flag), pin via the `version:` input on the setup action and document the bump here. Today (2026-05-27) we're CLI-version-agnostic.
* **No rollback step**: if the `apply-migrations` step half-succeeds (3 of 5 migrations applied, then the 4th errors), the partial state stays on the remote. Supabase doesn't currently offer a transactional `db push`; the operator must resolve the failed migration manually before the next CI run. Symptom is a clear error message in the CI log naming the failing file.
* **Function deploy ordering**: edge functions are deployed in directory iteration order (alphabetical on Linux). If function A calls function B via the Supabase HTTP path and B isn't deployed yet, A will 404 between deploy(A) and deploy(B). In practice no function in the orchestrator calls another orchestrator function over HTTP (they all use Postgres or the GHCR token endpoint), so this hasn't bitten us — but if a future cross-function call appears, document the deploy ordering constraint in `config.toml` or a function-specific README.

---

## Reference

* `.github/workflows/release.yml` — workflow source (`supabase-deploy` job).
* `launcher/supabase/config.toml` — per-function `verify_jwt` overrides (committed-to-source-control auth posture).
* `docs/MAINTAINER_GUIDE.md` — broader release-pipeline secrets (DIST_COMMIT_TOKEN, etc.).
