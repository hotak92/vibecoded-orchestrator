# Secrets rotation runbook

The canonical rotation procedure for every secret the launcher and
orchestrator depend on. Walk through the relevant section end-to-end
whenever a key is rotated, exposed, or suspected to have leaked.

The companion doc `docs/VCT_SECRETS_PRIMITIVE.md` describes the
write-scoped storage layout under `~/.vct-secrets/` (file mode `0600`,
read scope per-app, write scope shared per-team).

## Master rule

Every consumer of a rotated secret reads it from a file under
`~/.vct-secrets/...`. Nothing reads `~/.claude.json` directly anymore
(it was scrubbed 2026-04-21 — see Memory). With one exception, rotation
is a single-file write plus a service restart.

The exception is **Vercel and Supabase server-side env**, which need a
dashboard or CLI update because those processes don't see the local
filesystem.

---

## Supabase key rotation

When rotating Supabase service-role + anon keys:

1. Supabase dashboard → Project Settings → API.
2. Click **Roll** next to `service_role` (and `anon` if rotating that
   too). Copy the NEW key immediately — the dashboard does not show it
   a second time.
3. Write to local file (mode `0600`):
   ```bash
   mkdir -p ~/.vct-secrets/shared
   echo "<NEW_KEY>" > ~/.vct-secrets/shared/supabase_token
   chmod 600 ~/.vct-secrets/shared/supabase_token
   ```
   Do the same for the anon key, target file
   `~/.vct-secrets/shared/supabase_publishable_key`.
4. Update Supabase **edge function env** so the validate-tier and
   send-waitlist-confirmation functions see the new key:
   ```bash
   supabase secrets set \
     SUPABASE_SERVICE_ROLE_KEY="$(cat ~/.vct-secrets/shared/supabase_token)" \
     --project-ref <ref>
   ```
5. Update **Vercel project env** (otherwise the marketing site's
   server-side waitlist insert breaks):
   - Vercel dashboard → Project → Settings → Environment Variables
   - Edit `SUPABASE_SERVICE_ROLE_KEY` (and `SUPABASE_ANON_KEY` if rotated)
   - Save → **Redeploy** (env changes do not auto-redeploy).
6. Update any local `~/.claude.json` MCP server entries that pin
   `SUPABASE_KEY`. Find them with:
   ```bash
   grep -n "SUPABASE_KEY\|SUPABASE_SERVICE_ROLE_KEY" ~/.claude.json
   ```
   Then point them at `~/.vct-secrets/shared/supabase_token` via a
   wrapper script (the same pattern the GitHub PAT uses with
   `~/.vct-secrets/search-mcp-wrapper.sh` — see CLAUDE.md > GitHub).
7. Restart any local services that read these keys:
   - MCP servers (VS Code: reload window).
   - Local orchestrator processes (`scripts/launch_api.py` etc.).
8. Verify against the live endpoint:
   ```bash
   curl -X POST https://api.vibecodedtools.it/validate-tier \
     -H 'Content-Type: application/json' \
     -d '{"license_key":"00000000-0000-0000-0000-000000000000",
          "machine_id_hash":"'"$(echo -n test | sha256sum | awk '{print $1}')"'"}'
   ```
   Expect HTTP 401 with `{"valid": false, "tier": "free", ...}` — that
   confirms the function is up and answering with the new key.

Old key remains valid for ~24 hours after rotation per Supabase's docs,
so you have a window to swap consumers without an outage.

---

## Lemon Squeezy API key rotation

Used by `validate-tier` to call `/licenses/activate`.

1. LS dashboard → Settings → API → revoke the old key + create new.
2. `supabase secrets set LEMON_SQUEEZY_API_KEY="<NEW>" --project-ref <ref>`
3. No client-side rotation needed — the key never leaves the edge
   function.
4. Verify by triggering one license validation from any teammate's
   launcher; check the Supabase function log line
   `[validate-tier] OK key=...` for the new key tag.

If the LS API key is exposed publicly, rotate immediately. Anyone with
the key can issue or revoke licenses.

---

## Lemon Squeezy admin variant ID rotation

If `LS_ADMIN_VARIANT_IDS` (the env that classifies a license as `admin`)
leaks, rotate it:

1. LS dashboard → Products → create a NEW admin / maintainer variant.
2. Note the new variant ID.
3. `supabase secrets set LS_ADMIN_VARIANT_IDS='["<NEW_ID>"]' --project-ref <ref>`.
4. Re-issue admin license keys against the new variant for each
   maintainer (the old keys remain LS-valid but no longer classify as
   admin server-side).
5. Optionally disable the old variant in LS so no new licenses can be
   issued under it.

See `docs/ADMIN_LICENSE.md` for the full activation flow.

---

## GitHub PAT rotation

The PAT is read from `~/.vct-secrets/github_pat` by every consumer (gh
CLI, search MCP, git credential helper). Rotation = single file write.

1. https://github.com/settings/tokens → regenerate the token.
2. `echo "<NEW_PAT>" > ~/.vct-secrets/github_pat && chmod 600 ~/.vct-secrets/github_pat`.
3. Verify:
   ```bash
   GITHUB_TOKEN=$(cat ~/.vct-secrets/github_pat) gh api user | jq .login
   ```
   Expect `hotak92`.
4. No other change needed. The credential helper, search MCP wrapper,
   and `gh` CLI all re-read the file on each invocation.

---

## Resend / IONOS SMTP credentials

Resend (transactional sender for the Supabase edge function):

1. Resend dashboard → API keys → revoke + create.
2. `supabase secrets set RESEND_API_KEY="<NEW>" --project-ref <ref>`.

IONOS SMTP fallback (used by the same edge function when Resend errors):

1. IONOS dashboard → Email → SMTP credentials → reset password.
2. `supabase secrets set IONOS_SMTP_PASSWORD="<NEW>" --project-ref <ref>`.

---

## Telegram bot token (if rotating MAO Telegram channel)

Lives at `~/.vct-secrets/shared/telegram_bot_token`. Rotation:

1. Telegram BotFather → `/revoke` the old token, then `/token` a new one.
2. `echo "<NEW>" > ~/.vct-secrets/shared/telegram_bot_token && chmod 600 ...`.
3. Restart any MAO maestro processes reading the token.

---

## After ANY rotation

- Tail the relevant logs for at least one full request to confirm the
  new key is in use:
  ```
  Supabase: Project → Functions → validate-tier → Logs
  Vercel:   Project → Deployments → latest → Function logs
  Local MCP: ~/.claude/logs/<date>_tool_usage.jsonl
  ```
- Update CONTEXT_STATE.md or the relevant audit log with the rotation
  date + reason. Future you needs to know which key was active when.
- If the rotation was triggered by a leak: search git history + chat
  history for any commit that may still expose the OLD key, and
  consider amending / force-rotating those entries.

---

## Schema and storage reference

Every secret lives in exactly ONE of these roots, mode `0600`, never
committed:

| Root | Scope | Examples |
|---|---|---|
| `~/.vct-secrets/<app>/<file>` | per-app reader, shared writer | `vct-cli/license_key`, `mao/telegram_session_id` |
| `~/.vct-secrets/shared/<file>` | all VCT apps | `supabase_token`, `telegram_bot_token`, `github_pat` |
| `~/.vct-secrets/legal/<file>` | controller-only | `loops-dpa-signed.pdf` (kept for audit even after Loops removed 2026-04-26) |
| Supabase edge env | function-only | `LEMON_SQUEEZY_API_KEY`, `LS_ADMIN_VARIANT_IDS`, `RESEND_API_KEY`, `IONOS_SMTP_PASSWORD` |
| Vercel project env | site-only | `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY`, `NEXT_PUBLIC_SUPABASE_URL` |

`docs/VCT_SECRETS_PRIMITIVE.md` is the design spec for the
`~/.vct-secrets/` layout (write-scoped explorers, file modes, race-free
writes); this doc is the operational rotation runbook on top.
