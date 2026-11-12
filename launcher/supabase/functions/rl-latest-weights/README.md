# rl-latest-weights

Supabase edge function that issues a short-lived (15-min) signed Storage
URL to the **latest default RL weights bundle** for a given
`(module_id, embedding_source)` pair. Called by the launcher's "Download
default weights" manifest button (v0.2.32 #D) via
`launcher/src-tauri/src/commands/module_default_weights.rs`.

## Why this exists separately from `rl-latest-version`

The two endpoints query the SAME `paid_module_releases` table and reuse
the SAME private storage bucket — but serve different intents:

| Endpoint               | Caller                          | Intent                                                        |
|------------------------|---------------------------------|---------------------------------------------------------------|
| `rl-latest-version`    | Stream B daily poller (Rust)    | "Is the head NEWER than my current version?" → maybe-update.  |
| `rl-latest-weights`    | Manifest-button `tauri_command` | "Just give me the head — I've never had a `.pt` before."      |

The "first install" path doesn't have a `current_weights_version` to
compare against, so the always-return-the-head contract is cleaner than
overloading `rl-latest-version` with a sentinel value. A dedicated
function was chosen over redirecting the caller to `rl-latest-version`
because the contracts are different enough that a shared endpoint
would silently degrade either the poller or the manifest button.

## Architecture

```
launcher                                Supabase edge function          Supabase Storage
────────                                ──────────────────────          ──────────────
1. User clicks "Download default weights"
   → module_download_default_weights
     (commands/module_default_weights.rs)

2. POST /rl-latest-weights
   Authorization: Bearer <license_key>
   { license_key, machine_id_hash,
     embedding_source, module_id }
                                    ──> 3. validateRequestBody()

                                        4. Re-validate via /validate-tier
                                           (server-to-server, service role).
                                           Refuse if tier < "pro".

                                        5. SELECT version, storage_path, sha256
                                           FROM paid_module_releases
                                           WHERE module_id = ?
                                             AND embedding_source = ?
                                             AND is_latest = true

                                        6. If no row matches:
                                            discoverSupportedSources()
                                            → 400 unsupported_embedding_source
                                              { supported_embedding_sources: [...] }

                                        7. createSignedUrl(storage_path, 15min)
                                                                  ──> 8. signed URL
                                    <── 9. { download_url, version,
                                              sha256, expires_at }

10. GET <download_url>                                            ──> 11. Stream .pt
    (curl/reqwest, 15-min window)
```

## Wire contract

### Request

```http
POST /functions/v1/rl-latest-weights
Authorization: Bearer <license_key>
Content-Type: application/json

{
  "license_key": "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
  "machine_id_hash": "<sha256 hex, ≥16 chars>",
  "embedding_source": "qwen3",
  "module_id": "vct-rl-reranker"
}
```

The `Authorization` header is redundant with the body's `license_key`
(the caller sets both for compatibility with both the rl-latest-version
endpoint, which only reads the body, and a potential future variant
that prefers the header). The function reads the body.

`embedding_source` and `module_id` are optional. Defaults:

- `embedding_source` → `"qwen3"`
- `module_id` → `"vct-rl-reranker"`

### Success response (200)

```json
{
  "download_url": "https://<project>.supabase.co/storage/v1/object/sign/...",
  "version": "arctic-2026-05-19",
  "sha256": "<hex64 or empty>",
  "expires_at": "2026-05-19T20:15:00.000Z"
}
```

The caller verifies `sha256` if non-empty (see
`download_to_module_dir`); empty string means the release row didn't
record one.

### Error responses

| HTTP | error code                       | Meaning                                                                                                                                                                                                                            |
|------|----------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 400  | `invalid_request_body`           | Body is not JSON or fails validation (`detail` carries: `license_key_invalid_format`, `machine_id_hash_invalid_format`, `embedding_source_invalid_type`, `module_id_invalid_type`)                                                  |
| 400  | `unsupported_embedding_source`   | No `paid_module_releases` row matches `(module_id, embedding_source)`. Response includes `module_id` + `supported_embedding_sources: string[]` for client recovery. *(This subsumes the conceptual "404 no bundle for this combo".)* |
| 401  | `license_invalid`                | `/validate-tier` rejected the key (`detail` carries the sub-reason)                                                                                                                                                                |
| 401  | `tier_insufficient`              | License valid but tier < `pro` (response includes `required_tier`, `got`)                                                                                                                                                          |
| 405  | `method_not_allowed`             | Non-POST                                                                                                                                                                                                                           |
| 500  | `service_misconfigured`          | Required env vars missing (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`)                                                                                                                                                            |
| 500  | `release_lookup_failed`          | DB error from the `paid_module_releases` lookup                                                                                                                                                                                    |
| 500  | `signed_url_generation_failed`   | Storage API rejected `createSignedUrl`                                                                                                                                                                                             |

### Storage bucket convention

Bundles live in the `paid-module-weights` private bucket (same bucket
as `rl-latest-version`) under the path pattern:

```
paid-module-weights/<module_id>/<embedding_source>/<version>/<filename>.pt
```

Example:

```
paid-module-weights/vct-rl-reranker/qwen3/2026-05-19/rl_model_qwen3_1024.pt
```

The actual `storage_path` value is stored on the
`paid_module_releases` row; the convention above is what the operator
runbook (in `rl-latest-version/README.md`) recommends for new uploads.

## Required env vars

Set via `supabase secrets set ...` (or rely on Supabase's auto-injected
runtime vars):

| Variable                       | Purpose                                                                                                                  |
|--------------------------------|--------------------------------------------------------------------------------------------------------------------------|
| `SUPABASE_URL`                 | Auto-set by Supabase runtime; used to call `/validate-tier` server-to-server and construct the Storage client.           |
| `SUPABASE_SERVICE_ROLE_KEY`    | Auto-set by Supabase runtime; authorizes the inter-function call and `createSignedUrl` on the private bucket.            |
| `WEIGHTS_BUCKET`               | Storage bucket name. **Defaults to `paid-module-weights`.** Override only when running against a staging bucket.         |

No additional secrets are required — the function reuses everything
`rl-latest-version` uses.

## Deploy

**DEPLOY IS GATED ON USER ACTION** — the source ships in this repo;
the operator owns the actual deploy.

### Recommended one-liner (from the repo root)

```bash
bash launcher/supabase/functions/rl-latest-weights/deploy.sh
```

`deploy.sh` does the necessary `cd launcher/` before calling
`supabase functions deploy rl-latest-weights`. This is required because
the Supabase CLI defaults to `./supabase/functions/<name>/index.ts`
and our function source lives at `launcher/supabase/functions/...` —
running the deploy command from the repo root without the `cd` fails
with `entrypoint path does not exist (supabase/functions/rl-latest-weights/index.ts)`.

### Manual equivalent (skip the script)

```bash
cd launcher
supabase functions deploy rl-latest-weights
```

After deploy, verify with the smoke test below.

### First-time CLI setup (only if you haven't run these before)

Skip this section if you've already deployed any function for this
project from this machine.

```bash
supabase login                                       # CLI auth (browser flow)
cd launcher && supabase link --project-ref ovpdtijpdchzlxbojhsg
```

These steps persist across deploys — no need to re-run them for
future function deploys.

## Local development

### Run tests

```bash
deno test --no-check \
  launcher/supabase/functions/rl-latest-weights/validation_test.ts
```

30 tests cover the request-body validator (happy + failure paths +
defaults + the brief's T1–T5 cases), the tier-check ladder, the
token-preview deterministic hasher, and the UUID regex. The
integration path (full request through `/validate-tier` round-trip +
Storage `createSignedUrl`) is exercised by manual `curl` against the
deployed function (see Smoke test below).

### Serve locally (Supabase CLI)

```bash
cd launcher
supabase functions serve rl-latest-weights --env-file .env.local

# In another shell:
curl -X POST http://localhost:54321/functions/v1/rl-latest-weights \
  -H "Authorization: Bearer 00000000-0000-4000-8000-000000000000" \
  -H "Content-Type: application/json" \
  -d '{
    "license_key": "00000000-0000-4000-8000-000000000000",
    "machine_id_hash": "'"$(echo -n test | sha256sum | head -c 64)"'",
    "embedding_source": "qwen3"
  }' | jq
```

Expect 401 `license_invalid` against a test license; that's the
validate-tier gate firing as designed. A real Pro-tier key produces a
200 with a signed URL.

## Smoke test (post-deploy)

```bash
SUPABASE_URL=https://ovpdtijpdchzlxbojhsg.supabase.co
LICENSE_KEY=<a valid Pro license>
MACHINE_HASH=$(echo -n test | sha256sum | head -c 64)

curl -s -X POST \
  "$SUPABASE_URL/functions/v1/rl-latest-weights" \
  -H "Authorization: Bearer $LICENSE_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "license_key": "'"$LICENSE_KEY"'",
    "machine_id_hash": "'"$MACHINE_HASH"'",
    "embedding_source": "qwen3"
  }' | jq
```

Expect:

```json
{
  "download_url": "https://ovpdtijpdchzlxbojhsg.supabase.co/storage/v1/object/sign/...",
  "version": "<latest qwen3 version on the row>",
  "sha256": "<hex64 or empty>",
  "expires_at": "<ISO-8601, ~15 min from now>"
}
```

If you get:

- `401 license_invalid` — the license isn't valid or hasn't activated.
- `401 tier_insufficient` — license is below Pro.
- `400 unsupported_embedding_source` — no row for that
  `(module_id, embedding_source)` combo yet; check the response's
  `supported_embedding_sources` array.
- `500 service_misconfigured` — Supabase env vars not injected;
  redeploy.

## Anti-piracy posture

Same as `rl-latest-version`:

- **Bucket is private.** Anonymous access returns 401. The only way to
  obtain the `.pt` is a signed URL from this function.
- **URLs are short-lived** (~15 min). A leaked URL is useless within
  the same coffee break.
- **URLs are object-scoped** (single file, read-only). A leaked URL
  reveals exactly ONE version of the weights — not the whole bucket.
- **Server-to-server tier re-validation.** The launcher's 3-day tier
  cache is bypassed; new pulls always go through fresh `/validate-tier`
  re-validation. A user whose subscription lapsed mid-cache is cut off
  from new weights pulls immediately.
- **Weights rotate.** Re-training cadence (target weekly) means a
  leaked snapshot's `.pt` from week N degrades vs. free-tier baseline
  within ~2 weeks of stopping refreshes.

## Related

- `launcher/supabase/functions/rl-latest-version/` — sibling function for
  the daily poller path; this function adopts its CORS / tier /
  logging / storage conventions exactly.
- `launcher/supabase/functions/rl-artifact-url/` — sibling for
  container-image pulls; another consumer of the same `/validate-tier`
  gate.
- `launcher/supabase/functions/validate-tier/` — upstream tier
  validation we re-call server-to-server.
- `launcher/src-tauri/src/commands/module_default_weights.rs` — the
  Rust caller. `DEFAULT_RL_LATEST_WEIGHTS_ENDPOINT` const at line 74
  pins this function's production URL.
- `launcher/supabase/migrations/20260516_paid_module_releases.sql` —
  the table this function reads.

## Note

The deploy step is gated on operator action (`bash deploy.sh`) — the
source ships in the repo; deployment to Supabase is a separate manual
step under operator control.
