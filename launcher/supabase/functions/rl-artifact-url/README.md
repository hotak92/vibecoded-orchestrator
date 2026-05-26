# rl-artifact-url

Supabase edge function that issues short-lived GHCR pull tokens for the
`vct-rl-reranker` paid module. Called by the launcher's
`installer_engine::container_pull` (in `launcher/src-tauri/src/installer_engine.rs`)
before each `podman pull` / `docker pull` of the private image.

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
  --project-ref ovpdtijpdchzlxbojhsg
```

That's all. No `index.ts` edit, no version bump, no launcher change. The
launcher already reads `image` + `username` from the response and uses
the org owner for `podman login -u <user>`.

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
