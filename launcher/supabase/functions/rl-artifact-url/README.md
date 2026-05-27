# rl-artifact-url

Supabase edge function that issues short-lived GHCR pull tokens for the
`vct-rl-reranker` paid module. Called by the launcher's
`installer_engine::container_pull` (in `launcher/src-tauri/src/installer_engine.rs`)
before each `podman pull` / `docker pull` of the private image.

## ⚠️ Supabase has TWO independent "secrets" systems — get this right or nothing works

Before configuring this function, internalize the following. **Confusing
these two systems cost ~3 hours of production debugging on 2026-05-27**
and the symptom (edge function returns `service_misconfigured` while the
dashboard shows the secret is "set") is genuinely opaque.

Supabase stores secrets in two unrelated places:

| System | Where it lives | Who reads it | What belongs here |
|---|---|---|---|
| **Vault** | Project → Integrations → Vault → Secrets | SQL functions, via `supabase.vault.decrypted_secrets` | The `vct_admin_tokens` JSON map (read by `validate-tier`'s SQL `lookupVaultAdminToken` constant-time compare). **Nothing else from this edge function.** |
| **Edge Function Secrets** | Project → Edge Functions → Secrets, or `supabase secrets set ...` CLI | Edge functions, via `Deno.env.get()` | `GHCR_USERNAME`, `GHCR_PAID_IMAGE_REPO`, `GHCR_PAID_TAG_DEFAULT`, `GHCR_SERVICE_PAT` — every variable this README documents in the "Required" / "Optional" tables below. |

These two systems do **NOT** read from each other. A value placed in
Vault is invisible to `Deno.env.get()`. A value placed in Edge Function
Secrets is invisible to `supabase.vault.decrypted_secrets`.

The Supabase dashboard does not warn you when you set a secret in the
wrong system — both systems happily accept and persist your value, and
the dashboard cheerfully shows it as "set". The edge function logs will
report `service_misconfigured` (env var missing) even though, to you,
the secret is right there in the dashboard.

### Verifying that secrets are in the right system

After configuring (or when debugging a `service_misconfigured` error),
run:

```bash
# Lists Edge Function Secrets only — the things `Deno.env.get()` can see.
supabase secrets list --project-ref <your-project-ref> | grep -E 'GHCR_|SERVICE_PAT'
```

**Every GHCR-related variable this README requires MUST appear in this
list.** If a variable appears in the dashboard Vault UI but not in
`supabase secrets list`, it is in the wrong system — re-set it via
`supabase secrets set` (or via the Edge Functions → Secrets dashboard
pane, which is a distinct UI from the Vault pane).

Vault values are not listable via the CLI; to verify the
`vct_admin_tokens` Vault entry is present, query
`select name from vault.secrets where name = 'vct_admin_tokens';`
from the Supabase SQL editor (the function that reads it lives in
[`launcher/supabase/functions/validate-tier/index.ts`](../validate-tier/index.ts) —
search for `lookupVaultAdminToken`).

## Architecture (locked decisions 2026-05-16)

```
launcher                      Supabase edge function          GHCR
────────                      ──────────────────────          ────
1. POST /rl-artifact-url
   { license_key, machine_id_hash }
                          ──> 2. Re-validate via /validate-tier
                              (server-to-server, service role)

                              3. POST ghcr.io/token
                                 (PAT in Bearer header,
                                  scope=repository:hotak92/vct-rl-reranker:pull)
                                                            ──> 4. {token, expires_in}

                          <── 5. {image, tag, pull_token, expires_at}

6. podman login ghcr.io -u vct-paid-module --password-stdin
   <feed pull_token via stdin>

7. podman pull ghcr.io/hotak92/vct-rl-reranker:0.1.0
   (registry honors the scoped pull token)

8. podman logout ghcr.io
   (discard token from auth.json)
```

The launcher never sees the long-lived service PAT — only short-lived
(typically 5-30 min, capped at 15 min by us) repository-scoped tokens.

## Required env vars (set via `supabase secrets set ...`)

| Variable | Purpose |
|---|---|
| `SUPABASE_URL` | Auto-set by Supabase runtime; used to call `/validate-tier` server-to-server |
| `SUPABASE_SERVICE_ROLE_KEY` | Auto-set by Supabase runtime; authorizes the inter-function call |
| `GHCR_SERVICE_PAT` | **Manually set.** A GitHub fine-grained PAT scoped to `read:packages` on the paid-image repo. Long-lived (90 days typical); rotate quarterly. NEVER the launcher's pull token — that's what this function GENERATES from this PAT. |

## Optional env vars (v0.2.36+)

| Variable | Default | Purpose |
|---|---|---|
| `GHCR_PAID_IMAGE_REPO` | `hotak92/vct-rl-reranker` | Paid-image repo in `<owner>/<image>` form. Set this when migrating from the personal-account image to the org image (see "Org migration" below). Malformed values (no slash, whitespace-only, etc.) are logged via `console.warn` and the default applies. |
| `GHCR_PAID_TAG_DEFAULT` | `0.1.0` | Fallback tag for the `tag` field in the response. The launcher's `resolve_variant_tag` is the actual source of truth — this default is advisory / used by callers that bypass the launcher logic. |
| `GHCR_USERNAME` | owner-half of `GHCR_PAID_IMAGE_REPO` | GitHub login the launcher uses for `podman login -u <user>`. Must match the owner of `GHCR_SERVICE_PAT` (the credential owner), NOT necessarily the owner of the package. For the v0.2.36 per-module bot-user architecture, set to the bot user's login (e.g. `vct-bot-rl`) while `GHCR_PAID_IMAGE_REPO` stays at the package path (e.g. `hotak92/vct-rl-reranker`). When unset, falls back to the owner-half of `GHCR_PAID_IMAGE_REPO` (Agent W's original auto-derivation; correct only when package owner IS credential owner). |

To deploy:
```bash
cd launcher/supabase
supabase secrets set GHCR_SERVICE_PAT='<your-fine-grained-PAT>'
supabase functions deploy rl-artifact-url
```

## Org migration (v0.2.36 architectural follow-up)

The v0.2.35-shipped default points at a **personal-account** image
(`hotak92/vct-rl-reranker`). GHCR's `/token` endpoint has a known quirk for
personal-account packages: it returns the original PAT base64-encoded
rather than issuing a separate scoped credential (see
`exchangeForRegistryToken` doc in `index.ts`). This means the decoded
pull_token returned to the client IS the underlying PAT — breaking the
original design property that "the PAT never leaves the server".

Moving the image to a **GitHub Organization** (e.g., `vibecodedtools/`)
changes the `/token` behaviour to issue proper repository-scoped tokens.
When that migration happens, it should be a one-line Supabase secret
update — not a code redeploy:

```bash
supabase secrets set \
  GHCR_PAID_IMAGE_REPO=vibecodedtools/vct-rl-reranker \
  GHCR_USERNAME=vibecodedtools \
  --project-ref ovpdtijpdchzlxbojhsg
```

That's all. No `index.ts` edit, no version bump, no launcher change. The
launcher already reads `image` + `username` from the response and uses
the resolved username for `podman login -u <user>`.

## Per-module bot-user architecture (v0.2.36)

A middle-ground alternative to org migration: keep the image under the
personal account, but mint the `GHCR_SERVICE_PAT` from a **dedicated bot
GitHub user** (e.g. `vct-bot-rl`) whose only access is collaborator-read
on the paid-module package. This bounds the PAT-echo leak surface to
exactly one package without requiring a GitHub Organization.

Setup:
1. Create `<your-bot-user>` GitHub user (regular sign-up + 2FA).
2. Invite `<your-bot-user>` as a **Read** collaborator on the paid-module
   repo — AND on the package itself (see "Package ACL vs Repo ACL"
   below; doing only the repo half is a common footgun that returns
   `denied` from `podman pull` even though every other check passes).
3. Mint a `read:packages`-only PAT from `<your-bot-user>`'s account.
4. Update Supabase secrets (replace `<your-supabase-project-ref>` with
   the ref from your Supabase project dashboard URL):

```bash
supabase secrets set \
  GHCR_SERVICE_PAT='ghp_xxxxxxxxxx' \
  GHCR_PAID_IMAGE_REPO=<your-package-owner>/vct-rl-reranker \
  GHCR_USERNAME=<your-bot-user> \
  --project-ref <your-supabase-project-ref>
```

Key property: `GHCR_USERNAME` (`<your-bot-user>`) differs from the
owner-half of `GHCR_PAID_IMAGE_REPO` (`<your-package-owner>`). The
launcher's `podman login` uses `<your-bot-user>` — the credential owner
— even though the image lives at a path owned by a different account.
Without `GHCR_USERNAME` the auto-derivation would send
`<your-package-owner>` and get 403 from ghcr.io.

For each new paid module, repeat with a new bot user (e.g.
`<your-bot-user>-coord`, `<your-bot-user>-telegram`) on a sibling edge
function. See the KG node `multi-module-paid-distribution-architecture`
for the full rationale and constraint analysis, plus the scaling note
in "Scaling beyond one paid module" below.

### Package ACL vs Repo ACL — a critical gotcha

GHCR packages have **two independent access lists**:

1. **Repo collaborators** — `https://github.com/<owner>/<repo>/settings/access`
2. **Package's own Manage Access** —
   `https://github.com/users/<owner>/packages/container/<package>/settings`

When a package is first published from a repo, GitHub sets it to
**Inherit access from source repository** — the package ACL mirrors
repo collaborators automatically, and adding a user to the repo is
enough.

However, if that "Inherit access" toggle is ever turned OFF (which
typically happens while debugging a different access issue, then never
turned back on), the package falls back to its **own curated ACL**.
That curated list is initially empty. From that moment on, **adding a
user as a repo collaborator is no longer sufficient** — the user must
also be added to the package's own Manage Access list.

Symptom when this is wrong:

- `gh api /repos/<your-package-owner>/<repo>/collaborators` shows the bot
  with `pull: true` ✓
- `curl -u "<your-bot-user>:$PAT" \
  https://api.github.com/users/<your-package-owner>/packages/container/<package>`
  returns **"Package not found"** (404) ✗
- `podman login ghcr.io -u <your-bot-user> -p $PAT` succeeds (the PAT is
  valid; auth works) ✓
- `podman pull ghcr.io/<your-package-owner>/<package>:<tag>` returns
  **`denied`** ✗

The `podman pull` failure is the production symptom — `denied` here
specifically means "you authenticated, but the package's ACL does not
list you", which is distinct from "PAT invalid" or "image not found".

#### Diagnostic snippet

Run this as the operator (the human who owns `<your-bot-user>`'s PAT) to
verify both ACLs in one go:

```bash
OWNER=<your-package-owner>
REPO=<your-paid-module-repo>           # e.g. vct-rl-reranker
PACKAGE=<your-package-name>            # usually same as REPO
BOT=<your-bot-user>
PAT=ghp_xxxxxxxxxx                     # bot's read:packages PAT

# 1. Is the bot a repo collaborator? (Inherits-access path)
gh api "/repos/$OWNER/$REPO/collaborators/$BOT" \
  2>/dev/null && echo "✓ repo collaborator" || echo "✗ NOT a repo collaborator"

# 2. Can the bot see the package via its OWN ACL? (Curated path)
curl -s -o /dev/null -w "%{http_code}\n" \
  -u "$BOT:$PAT" \
  "https://api.github.com/users/$OWNER/packages/container/$PACKAGE"
# 200 = bot can see the package. 404 = bot is not on the package ACL
# (regardless of repo collaboration).
```

If step 1 passes but step 2 returns 404, you are hitting the
package-ACL gotcha. Fix:

1. Sign in to GitHub as `<your-package-owner>`.
2. Navigate to
   `https://github.com/users/<your-package-owner>/packages/container/<package>/settings`.
3. Scroll to **Manage Access**.
4. Click **Add user**, search for `<your-bot-user>`, set role to
   **Read**, and confirm the invitation.
5. Sign in as `<your-bot-user>` (or open the invitation email) and
   accept.
6. Re-run the curl in step 2 — it should now return 200.
7. Retry `podman pull` — it should now succeed.

You can leave "Inherit access from source repository" off; explicitly
listing the bot on the package ACL is the more robust configuration
for the per-bot-user pattern (it makes the access surface auditable
package-by-package rather than tangled with the repo's collaborator
list, which tends to grow contributors over time).

### Scaling beyond one paid module — GitHub ToS constraint

The per-bot-user pattern works cleanly for **one** paid module today,
but it does not scale to many. GitHub's Terms of Service permit a human
to operate **one** machine account in addition to their Personal
Account. Running `<your-bot-user>-rl`, `<your-bot-user>-coord`,
`<your-bot-user>-telegram`, etc. — one bot per paid module — is
technically a ToS violation past the first bot, and GitHub may suspend
the accounts (which would simultaneously break every paid-module
install across every customer).

Long-term, the two viable paths are:

- **Migrate to a GitHub Organization** (e.g.
  `https://github.com/<your-org>/`). Organizations have no
  machine-account limit, and packages owned by an org get proper
  scoped tokens from GHCR's `/token` endpoint instead of the
  PAT-echo behaviour that personal-account packages have (see
  "Org migration" earlier in this README). One org-scoped service
  PAT, or one bot member of the org, covers all paid modules.
- **Move artifact distribution off GHCR entirely.** The Lemon Squeezy
  hosted file feature can serve signed artifact URLs directly,
  bypassing the GHCR ACL system; the long-term plan in the Lemon
  Squeezy spec already contemplates this path.

This README does not propose which path to take — that's a separate
architecture decision tracked in the orchestrator's roadmap. The point
of this section is to make sure operators reading the per-module
bot-user setup *know* the pattern has a ceiling at 1 module so they
plan accordingly.

## Wire contract

### Request

```http
POST /functions/v1/rl-artifact-url
Content-Type: application/json

{
  "license_key": "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
  "machine_id_hash": "<sha256 hex, ≥16 chars>"
}
```

### Success response (200)

```json
{
  "image": "ghcr.io/hotak92/vct-rl-reranker",
  "tag": "0.1.0",
  "registry": "ghcr.io",
  "pull_token": "<short-lived-registry-token>",
  "expires_in_s": 900,
  "expires_at": "2026-05-16T20:00:00.000Z"
}
```

### Error responses

| HTTP | error code | Meaning |
|---|---|---|
| 400 | `invalid_json_body` | Body is not JSON |
| 400 | `license_key_invalid_format` | Missing or not a UUID v4 |
| 400 | `machine_id_hash_invalid_format` | Missing or `<16` chars |
| 401 | `license_invalid` | `/validate-tier` rejected the key (see `detail` for sub-reason) |
| 401 | `tier_insufficient` | License valid but tier `<pro` (response includes `required_tier`, `got`) |
| 405 | `method_not_allowed` | Non-POST |
| 500 | `Service misconfigured` | Required env vars missing |
| 500 | `registry_token_exchange_failed` | GHCR `/token` API rejected our service-PAT or returned malformed JSON |

## Defense in depth — why server-to-server re-validation

The launcher already calls `/validate-tier` at startup and caches the
result for 3 days. We *could* trust the launcher's cache here. But:

1. **Cache forgery**: a malicious launcher could send us a forged
   "tier=pro" claim — we'd issue tokens to free-tier users.
2. **Lapsed licenses**: a user whose subscription expired mid-cache
   should be cut off from new pulls immediately, not after the 3-day
   grace window expires.

So this function re-calls `/validate-tier` from inside the edge
function (server-to-server, with the service-role key) for every
token request. The 3-day grace exists for offline operation of
*already-pulled* artifacts, not for new pulls.

## Anti-piracy posture

- **Image is private** (`ghcr.io/hotak92/vct-rl-reranker`). Anonymous
  pulls return 401. Without a valid pull token from this function,
  there is no way for a user to obtain the image.
- **Tokens are short-lived** (~15 min). A leaked token from a single
  pull is useless within 15 min.
- **Tokens are scoped to pull only** (no push, no other repos). Even
  if extracted from `podman auth.json` before the logout cleanup,
  attacker can't push tampered images.
- **Model weights rotate weekly** (separate `/rl-latest-version`
  endpoint, Phase 3C). A leaked snapshot's `RLModel.pt` from week N
  degrades vs free-tier hybrid_search within ~2 weeks of stopping
  refreshes. The container code is the moat-less part; the weights
  are the moat.

## Local tests

```bash
deno test --no-check \
  launcher/supabase/functions/rl-artifact-url/validation_test.ts

# Config layer (v0.2.36+) — requires --allow-env for Deno.env.set/delete:
deno test --no-check --allow-env \
  launcher/supabase/functions/_shared/config_test.ts
```

23 tests in `validation_test.ts` cover the validation helpers + tier
comparison + token preview deterministic hashing. 34 tests in
`_shared/config_test.ts` cover env-driven paid-image-repo / paid-tag
resolution + the malformed-value fallback + `console.warn` capture +
default-value tripwires.

The integration path (full request through `/validate-tier` round-trip
+ `/token` exchange) is exercised by manual `curl` against the deployed
function — automated integration tests would require a Supabase project
mock and are deferred.

## Related

- `launcher/src-tauri/src/installer_engine.rs::container_pull` —
  client-side counterpart that POSTs here, then `podman login` /
  `pull` / `logout`.
- `launcher/supabase/functions/validate-tier/index.ts` — upstream tier
  validation we re-call.
- `paid-modules/vct-rl-reranker/vct-module.json` — manifest that
  declares this endpoint's URL under
  `install.container.pull_token_endpoint`.
- `.claude/context/RL_RERANKER_RELEASE_PLAN_2026-05-16.md` — full
  release plan; this function is Phase 3A.
- `knowledge/concepts/launcher-packaging-paid-module-distribution.md` —
  architectural rationale for choosing container-pull + signed-URL
  gateway over the original IONOS-static-archive plan.
