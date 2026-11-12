# rl-latest-version

Supabase edge function that tells the launcher whether a newer RL
Reranker weights snapshot exists, and if so, issues a 15-minute
signed Storage URL to the `.pt` file. Called by the launcher's
Stream B Rust poller; complements `rl-artifact-url` (which handles
container-image pulls) by handling the **weights-only refresh** path.

## Architecture (locked decisions 2026-05-19)

```
launcher                       Supabase edge function          Supabase Storage
────────                       ──────────────────────          ──────────────
1. POST /rl-latest-version
   { license_key,
     machine_id_hash,
     current_weights_version,
     embedding_source?,
     module_id? }
                           ──> 2. Re-validate via /validate-tier
                               (server-to-server, service role)

                               3. SELECT version, storage_path, sha256, ...
                                  FROM paid_module_releases
                                  WHERE module_id = ?
                                    AND embedding_source = ?
                                    AND is_latest = true

                               4. If version matches client's
                                  current_weights_version:
                                      return has_update=false (no URL).
                                  Else:
                                      createSignedUrl(storage_path, 15min)
                                                            ──> 5. signed URL
                           <── 6. { has_update, latest_version,
                                    download_url, sha256, ... }

7. GET <download_url>                                       ──> 8. Stream .pt
   (curl/reqwest, 15-min window)
```

The launcher never sees the long-lived service-role key or the private
Storage bucket directly — only short-lived per-pull signed URLs.

## Required env vars (set via `supabase secrets set ...`)

| Variable | Purpose |
|---|---|
| `SUPABASE_URL` | Auto-set by Supabase runtime; used to call `/validate-tier` server-to-server and to construct the Storage client. |
| `SUPABASE_SERVICE_ROLE_KEY` | Auto-set by Supabase runtime; authorizes the inter-function call and the `createSignedUrl` operation on the private bucket. |
| `WEIGHTS_BUCKET` | Storage bucket name. **Defaults to `paid-module-weights`.** Override only when running against a staging bucket. |

To deploy:
```bash
cd launcher/supabase
# (No manual secrets to set for this function — only the platform-provided
# SUPABASE_* envs and the optional WEIGHTS_BUCKET override.)
supabase functions deploy rl-latest-version
```

## Wire contract

### Request

```http
POST /functions/v1/rl-latest-version
Content-Type: application/json

{
  "license_key": "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
  "machine_id_hash": "<sha256 hex, ≥16 chars>",
  "current_weights_version": "",
  "embedding_source": "arctic",
  "module_id": "vct-rl-reranker"
}
```

`embedding_source` and `module_id` are optional. Defaults:

- `embedding_source` → `"qwen3"`
- `module_id` → `"vct-rl-reranker"`

`current_weights_version` is required but may be the empty string (signals
"never-fetched"; always triggers `has_update=true` when a row exists).

### Success response (200)

```json
{
  "has_update": true,
  "latest_version": "arctic-2026-05-19",
  "embedding_source": "arctic",
  "download_url": "https://<project>.supabase.co/storage/v1/object/sign/...",
  "download_url_expires_at": "2026-05-19T20:15:00.000Z",
  "sha256": "<hex64 or empty>",
  "released_at": "2026-05-19T12:34:56.000Z",
  "notes": "Initial arctic v0.1.0 trained 2026-05-19. F1 +0.39pp..."
}
```

When `has_update` is `false`, `download_url` and `download_url_expires_at`
are both empty strings (`sha256`, `released_at`, `notes` still describe
the head version so the client can update its local metadata if needed).

### Error responses

| HTTP | error code | Meaning |
|---|---|---|
| 400 | `invalid_request_body` | Body is not JSON or fails the validation rules (the `detail` field carries the specific validation code: `license_key_invalid_format`, `machine_id_hash_invalid_format`, `current_weights_version_invalid_type`, `embedding_source_invalid_type`, `module_id_invalid_type`) |
| 400 | `unsupported_embedding_source` | No `paid_module_releases` row matches `(module_id, embedding_source)`. Response includes `supported_embedding_sources: string[]` — the discovered list for that `module_id` (server-side discovery; see below). |
| 401 | `license_invalid` | `/validate-tier` rejected the key (`detail` carries the sub-reason) |
| 401 | `tier_insufficient` | License valid but tier < `pro` (response includes `required_tier`, `got`) |
| 405 | `method_not_allowed` | Non-POST |
| 500 | `service_misconfigured` | Required env vars missing |
| 500 | `release_lookup_failed` | DB error from the `paid_module_releases` lookup |
| 500 | `signed_url_generation_failed` | Storage API rejected `createSignedUrl` |

## Embedding-source discovery (no client-side enum)

The function does **not** hardcode the list of allowed embedding sources.
Instead, it queries the `paid_module_releases` table on every request:

- If `(module_id, embedding_source)` matches a `is_latest = true` row → success.
- If no match → 400 `unsupported_embedding_source`, response includes
  `supported_embedding_sources: string[]` (the discovered list of all
  `embedding_source` values shipped under that `module_id`).

This means **a client that doesn't know the current vocabulary can
discover it from a single error response**. New embedding sources
(e.g. `matryoshka`) become live the moment a row is inserted; no
function redeploy required.

For Stream B's poller this implies:

1. First call may post `embedding_source: "<whatever the launcher
   thinks is right>"` based on its config.
2. On 400 `unsupported_embedding_source`, the poller reads
   `supported_embedding_sources` from the response, picks the right
   one (or surfaces a UI prompt), and retries.

This avoids the "edge function redeploy on every new model variant"
trap that a hardcoded enum would create.

## Defense in depth — why server-to-server re-validation

Same posture as `rl-artifact-url`. The launcher already calls
`/validate-tier` at startup and caches the result for 3 days. We could
trust the launcher's cache here. But:

1. **Cache forgery**: a malicious launcher could send us a forged
   `tier=pro` claim — we'd issue a signed URL to free-tier users.
2. **Lapsed licenses**: a user whose subscription expired mid-cache
   should be cut off from new weights pulls immediately, not after
   the 3-day grace window expires.

So this function re-calls `/validate-tier` from inside the edge
function (server-to-server, with the service-role key) for every
request. The 3-day grace exists for offline operation of
**already-pulled** artifacts, not for new pulls.

## Operator runbook — shipping a new weights release

1. Train + export the `.pt` to a known path (e.g. `rl_model_arctic_1024.pt`).
2. Compute checksum: `shasum -a 256 rl_model_arctic_1024.pt` → save the
   hex digest.
3. Upload to the private bucket:
   ```bash
   supabase storage cp \
     rl_model_arctic_1024.pt \
     "storage://paid-module-weights/vct-rl-reranker/arctic/2026-05-19/rl_model_arctic_1024.pt"
   ```
4. Insert the release row (psql against the project):
   ```sql
   insert into public.paid_module_releases
     (module_id, embedding_source, version, storage_path, sha256, notes)
   values (
     'vct-rl-reranker',
     'arctic',
     'arctic-2026-05-19',
     'vct-rl-reranker/arctic/2026-05-19/rl_model_arctic_1024.pt',
     '<the sha256 hex>',
     'Release notes ≤500 chars markdown'
   );
   ```
   The `paid_module_releases_set_latest` trigger automatically demotes
   the previous head row for the same `(module_id, embedding_source)`
   pair to `is_latest=false`.

5. Verify with a smoke test:
   ```bash
   curl -s -X POST \
     "$SUPABASE_URL/functions/v1/rl-latest-version" \
     -H "Content-Type: application/json" \
     -d '{
       "license_key": "<a valid pro license>",
       "machine_id_hash": "'"$(echo -n test | sha256sum | head -c 64)"'",
       "current_weights_version": "",
       "embedding_source": "arctic"
     }' | jq
   ```
   Expect `has_update: true`, `latest_version: "arctic-2026-05-19"`,
   non-empty `download_url`.

6. To **rollback** to a previous version: flip the `is_latest` flag.
   ```sql
   update public.paid_module_releases
   set is_latest = true
   where module_id = 'vct-rl-reranker'
     and embedding_source = 'arctic'
     and version = '<previous version>';
   ```
   The trigger demotes the bad release; clients on the next poll get
   the rolled-back version.

## Anti-piracy posture

- **Bucket is private.** Anonymous access returns 401. The only way
  to obtain the `.pt` is a signed URL from this function.
- **URLs are short-lived** (~15 min). A leaked URL from a single
  pull is useless within 15 min.
- **URLs are object-scoped** (single file, read-only). A leaked URL
  reveals only ONE version of the weights — not the whole bucket.
- **Weights rotate** (re-training cadence configured separately,
  target weekly). A leaked snapshot's `.pt` from week N degrades vs
  free-tier baseline within ~2 weeks of stopping refreshes. The
  container code is the moat-less part; the weights are the moat.

## Local tests

```bash
deno test --no-check \
  launcher/supabase/functions/rl-latest-version/validation_test.ts
```

33 tests cover the request-body validator (happy + failure paths +
defaults), the version comparator (incl. empty-client and
lexically-newer edge cases), the tier-check ladder, the token-preview
deterministic hasher, and the UUID regex. The integration path
(full request through `/validate-tier` round-trip + Storage
`createSignedUrl`) is exercised by manual `curl` against the
deployed function (see Operator runbook step 5) — automated
integration tests would require a Supabase project mock and are
deferred.

## Related

- `launcher/src-tauri/src/self_update.rs` — Stream A's update flow
  (orchestrator launcher self-update; **does not** touch this
  function).
- Stream B's RL service commands (`launcher/src-tauri/src/rl_*.rs`,
  not yet landed at the time of writing) — the consumer of this
  function.
- `launcher/supabase/functions/rl-artifact-url/index.ts` — sibling
  function for container-image pulls; this function adopts its
  CORS / tier / logging posture.
- `launcher/supabase/functions/validate-tier/index.ts` — upstream
  tier validation we re-call.
- `launcher/supabase/migrations/20260516_paid_module_releases.sql` —
  the table this function reads.
- `paid-modules/vct-rl-reranker/vct-module.json` (when present) —
  manifest declaring this endpoint URL under
  `install.weights.latest_version_endpoint`.
