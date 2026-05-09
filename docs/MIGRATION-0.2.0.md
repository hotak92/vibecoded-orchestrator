# Migration: VCO Launcher 0.2.0 — secrets architecture overhaul

**TL;DR.** 0.2.0 removes the "Fix #3 auto-bridge" introduced earlier in
the 0.2.0 cycle (PR #171). That bridge mirrored a hard-coded allowlist
of well-known shared keys (`github_pat`, `OPENAI_API_KEY`, etc.) from
the launcher's keychain to flat files under `~/.vct-secrets/<key>` so
bundled MCP wrappers and hooks could read them with `cat`. The bridge
was rejected by the project owner: it bypassed the per-project
active-flag gate, materialised values to disk for any tool that
scanned `~/.vct-secrets/`, and scaled by a hard-coded allowlist
instead of the launcher's per-project access matrix.

The replacement is a single, principled path:

1. **Launcher keychain** is the authoritative store (unchanged).
2. **Launcher hub** (`http://127.0.0.1:7700/api/v1`) exposes
   `GET /projects/{id}/env[?key=NAME]` — returns the active set of
   (key, value) pairs the project is entitled to. Active-flag gating
   and cross-launcher pause checks happen here, in one place.
3. **Bundled wrappers** consume the hub via the shared resolver
   helper at `templates/scripts/vct_secrets_resolve.sh` (Bash) /
   `.ps1` (PowerShell). The helper handles hub-port discovery,
   project-id-or-folder resolution, and a documented exit-code
   contract (0=ok, 1=hub unreachable, 2=project not registered,
   3=key paused, 4=key not found).
4. **Hub auth-token gate** (H5, 2026-05-08) — the hub now generates a
   fresh 32-byte token on every launcher startup, persists it to
   `<vct_root_dir>/hub.token` (mode `0o600` on Unix), and requires
   `Authorization: Bearer <token>` on every `/api/v1/*` request except
   the unauthenticated `/health` liveness probe. Without this, any
   process running as the same OS user could `curl` the hub's secrets
   endpoint without going through the keychain at all. Same-user
   processes that legitimately talk to the hub (the launcher GUI, the
   resolver helper, the `vco` CLI) read `hub.token` fresh on every call
   and authenticate transparently. See [Hub authentication](#hub-authentication)
   below for the full threat model.

No file-side mirror anywhere in `secrets::set` / `secrets::delete`.

## Release artifacts

0.2.0 is the first VCO release shipped as proper per-OS archive bundles
(GitHub Release assets), in addition to the existing standalone launcher
binary attachments. Three archive variants are produced by the release
workflow:

- `vibecoded-orchestrator-0.2.0-linux-x64.tar.gz`
- `vibecoded-orchestrator-0.2.0-macos-arm64.tar.gz`
- `vibecoded-orchestrator-0.2.0-windows-x64.zip`

Each archive bundles the launcher binary, the `vco` CLI, the Python MCP
stack (`claude_mcp_servers/`), templates (agents / skills / hooks), the
`.claude/` project-scoped configuration, the compose files in
`infrastructure/`, the `tools/` helpers, the OS-appropriate
`first-install` / `start-launcher` / `install` entry points, plus the
`README.md` / `BOOTSTRAP.md` / `LICENSE` / `MIGRATION-0.2.0.md`
documentation. A `.sha256` sidecar is published alongside each archive.

**Path forward by user class**:

- **New users** — download the archive for your OS from
  [Releases](https://github.com/hotak92/vibecoded-orchestrator/releases),
  extract, double-click `first-install.{sh,command,desktop,bat}`. The
  installer detects/installs Python 3.11+ and Podman/Docker. No git
  clone required.
- **Existing dev clones** — `git pull && bash install.sh` (or the
  Windows / macOS equivalent). The clone-based path is unchanged; the
  archive is purely additive.
- **CI users** — pin to the archive's `.sha256` for reproducible
  downloads; `softprops/action-gh-release` ships every archive +
  checksum on tag push.

The archive workflow lives in `.github/workflows/release.yml` and runs
on tag push (`v*.*.*`). It builds per-OS in a 3-entry matrix
(`ubuntu-latest`, `macos-latest`, `windows-latest`) using the same
`build-bundled-launcher.sh` script maintainers run locally for
bit-identical builds. SLSA build provenance attestation (Sigstore-signed)
is generated for the launcher binary inside each archive.

## Who is affected?

You are affected if **all** of the following are true:

- You upgraded from a pre-0.2.0 launcher to a 0.2.0 build that
  shipped the Fix #3 bridge (any pre-release of 0.2.0 from
  ~2026-05-08 onwards).
- You set a secret through the launcher GUI in that pre-release
  window — the bridge wrote it to `~/.vct-secrets/<key>`.
- A wrapper or hook on your machine still reads
  `~/.vct-secrets/<key>` directly (e.g. the legacy
  `~/.vct-secrets/search-mcp-wrapper.sh` user-home artifact, or a
  custom shell script you wrote against that contract).

If you only use the in-tree wrappers shipped by the orchestrator
(e.g. `claude_mcp_servers/search_mcp/wrapper.sh`), they are migrated
to use the resolver helper and you're fine — restart the launcher
once after upgrading and your secrets keep working.

## What changed under the hood

| Component | Pre-0.2.0 (Fix #3) | 0.2.0 |
| --- | --- | --- |
| `launcher::secrets::set` / `::delete` | Wrote to keychain AND to `~/.vct-secrets/<key>` for an allowlist | Keychain only |
| `BRIDGE_SHARED_KEYS` allowlist | 7 keys hard-coded in Rust | Removed |
| `VCT_SECRETS_DIR` env var | Honoured by `secrets::set/delete` | Honoured by the user-facing `vct` CLI under `tools/vct-secrets/` AND by `commands::installer::vct_secrets_dir` for the `github_pat` legacy file-fallback path (test isolation + parity with the CLI) |
| Hub `GET /projects/{id}/env` | Returned secrets (no filter) | Returns secrets, supports `?key=NAME` filter, structured error envelope |
| New: `GET /projects/by-path?path=...` | Did not exist | Resolves a folder → registered project_id |
| New: `templates/scripts/vct_secrets_resolve.sh|.ps1` | Did not exist | Shared helper for bundled wrappers |
| `commands::installer::register_github_pat` | Wrote `~/.vct-secrets/shared/github_pat` (mode 0o600) directly | Writes to OS keychain `vct._user_shared_.shared.installer/github_pat`; one-shot file→keychain migration on first call |
| `~/.vct-secrets/git-credential-vct` | Helper protocol bypassed per-project active-flag gate | RETIRED — replaced by per-project `GITHUB_TOKEN` env propagation |
| `CANONICAL_INSTALL_ENV_KEYS` | 15 keys (no `GITHUB_TOKEN`) | 16 keys (adds `GITHUB_TOKEN`, conditionally emitted from keychain via active-flag gate) |
| Hub `/projects/{id}/env` keychain lookup for `scope='shared'` secrets | Used `&project.id` (real UUID) — guaranteed miss vs writer's SENTINEL_SHARED slot | Uses SENTINEL_SHARED (`_user_shared_`) for both lookup and active-flag gate (H1, 2026-05-08) |
| `OrchestratorManifest` (slim shape parsed from `vct-module.json`) | `version` + `description` + `components` only | Adds `bundled_secrets[]` so the hub's resolver can serve orchestrator-core secrets (`github_pat`, etc.) for every base-host project without requiring a module install (H1, 2026-05-08) |
| `resolve_user_secret_state` (env-pair builder for SecretsPanel writes) | Per-project user-bucket only — Shared / Global tab writes were silent to env surfaces | Enumerates all three SecretsPanel buckets (per-project + shared + global); shared / global writes fan out to every registered project (H2, 2026-05-08) |
| `is_secret_set` / `get_secret_preview` / `get_secret_status_v2.is_set` | Used `db.is_secret_active` (own DB only) — GUI could disagree with hub + env-file emit when sibling launcher had paused | Use `is_secret_active_cross_launcher` — GUI badge matches what subprocesses see (H3, 2026-05-08) |
| `claude_mcp_servers/search_mcp/wrapper.sh` legacy file fallback | Read `~/.vct-secrets/shared/github_pat` when hub unreachable AND `VCT_LEGACY_FILE_FALLBACK=1` set | REMOVED in 0.2.0 final (H4, 2026-05-08) — env-first AND resolver both work end-to-end after H1, the fallback is no longer needed |
| Hub auth (`/api/v1/*`) | Unauthenticated — any same-user process could `curl http://127.0.0.1:7700/api/v1/projects/<id>/env` and exfiltrate every active secret | `Authorization: Bearer <token>` required on every non-`/health` route. Token is a fresh 32-byte CSPRNG value per launcher startup, persisted to `<vct_root_dir>/hub.token` (mode `0o600` on Unix). Resolver helpers + `vco` CLI + `hub_proxy` read the file fresh on every call (H5, 2026-05-08) |

The hub's response contract is documented in
`launcher/src-tauri/src/hub/modules_api.rs::project_env`.

## Migration steps

### Step 1 — restart the launcher

The launcher must be running to resolve secrets. The 0.2.0 hub speaks
the new `?key=NAME` filter and the `by-path` route; older launcher
processes still running will return 4xx/5xx for those.

```bash
# Linux / macOS — quit the launcher fully, then restart.
# Windows — quit via the system tray, then relaunch.
```

After restart, verify the hub is up and on the new port:

```bash
cat ~/.vct/hub.port  # should be 7700, possibly 7701-7705 if 7700 was busy
curl -s http://127.0.0.1:$(cat ~/.vct/hub.port)/api/v1/projects | head
```

### Step 2 — re-set any secrets touched during the Fix #3 window

If you set a secret in the GUI between when Fix #3 landed and 0.2.0
final, the value is in the keychain (good) AND in `~/.vct-secrets/<key>`
(stale after upgrade). The keychain copy is still authoritative — no
action required for the launcher's own consumers.

For external tools that read `~/.vct-secrets/<key>` directly, you have
two choices:

- **Option A** (recommended): switch the tool to use the resolver helper.
  See "Step 4" below for the wrapper-migration recipe.
- **Option B** (transitional): re-run `vct set --project SHARED --key <name>`
  using the user-facing CLI under `tools/vct-secrets/`. That CLI continues
  to write `~/.vct-secrets/<key>` files; it operates independently of the
  launcher keychain. **You will then have two stores** (file + keychain)
  and must keep them in sync yourself. We recommend Option A.

### Step 3 — wrappers that ship in the orchestrator clone (already migrated)

These were updated as part of 0.2.0 and need no manual action; they're
listed here so you can audit your own clone if you've forked or pinned
the source:

- `claude_mcp_servers/search_mcp/wrapper.sh` — resolves `github_pat`
  via two paths: (1) `$GITHUB_TOKEN` env first (canonical 0.2.0 path,
  populated by the launcher's per-project env-file emission), (2)
  `vct_secrets_resolve.sh` against the launcher's hub if the env var
  is missing. The legacy `~/.vct-secrets/shared/github_pat` file
  fallback (gated behind `VCT_LEGACY_FILE_FALLBACK=1` in earlier
  pre-releases) was REMOVED in the 0.2.0 fork-readiness sweep
  (item H4, 2026-05-08): both canonical paths work end-to-end after
  H1, so the fallback is no longer needed.

### Step 4 — user-home wrappers (manual migration)

If your `~/.vct-secrets/` directory contains shell wrappers (artefacts
from the original Phase 1 layout migration around 2026-04-24 — see
`~/.vct-secrets/MIGRATION.md`), they still read flat files directly.
After Fix #3 removal those reads return stale data when the launcher
keychain has changed. Replace them or delete them.

The two known artefacts:

#### `~/.vct-secrets/search-mcp-wrapper.sh`

A locally-installed copy of the search-mcp wrapper. It was a
hand-written bridge before the orchestrator's in-tree wrapper at
`claude_mcp_servers/search_mcp/wrapper.sh` existed. **Action:** delete
it and point Claude Code at the orchestrator's wrapper instead.

```bash
# Backup, then remove.
mv ~/.vct-secrets/search-mcp-wrapper.sh ~/.vct-secrets/search-mcp-wrapper.sh.bak

# Update ~/.claude.json mcpServers.search.command to:
#   "/path/to/your/orchestrator/clone/claude_mcp_servers/search_mcp/wrapper.sh"
```

#### `~/.vct-secrets/git-credential-vct` (RETIRED in 0.2.0)

This was a **git credential-helper protocol** consumer. Git invoked it
once per `git fetch/push` against `https://github.com/...`, expecting a
`username=...\npassword=...\n` reply on stdout. The protocol is
synchronous, single-shot, and oblivious to the VCT project model: git
has no notion of "current VCT project", so the helper could not ask the
hub for a per-project secret. That made it incompatible with the
per-project active-flag gate (Lifecycle B): a paused PAT in the GUI
remained readable to any `git push` because the helper bypassed the
gate by design.

The 0.2.0 fork-readiness sweep retired this helper in favour of
**per-project `GITHUB_TOKEN` env propagation** — see "Replacing
`git-credential-vct`" below.

**Action:** delete the helper and reconfigure git to use a `GITHUB_TOKEN`-
backed credential flow.

```bash
# Backup, then remove the helper:
mv ~/.vct-secrets/git-credential-vct ~/.vct-secrets/git-credential-vct.bak

# Remove the credential-helper line from your global git config (find
# the entry referencing `git-credential-vct` and either delete the line
# or replace with one of the keychain-native helpers below):
git config --global --unset 'credential.https://github.com.helper'
# (or edit ~/.gitconfig directly)
```

Then pick ONE of:

- **Recommended (`gh`):** if you have GitHub CLI installed,
  `gh auth setup-git` configures git to call `gh` for credentials. `gh`
  reads `GITHUB_TOKEN` from the environment first (which the launcher
  now populates per-project — see below), then falls back to its own
  keychain-backed login.
- **Thin shell helper:** add a one-line custom helper to your global
  git config that reads `GITHUB_TOKEN` from the environment:
  ```bash
  git config --global credential.https://github.com.helper \
    '!f() { echo username=x-access-token; echo password=$GITHUB_TOKEN; }; f'
  ```
- **Keychain-native helper:** for clones/pushes from contexts where
  the launcher hasn't injected `GITHUB_TOKEN` (e.g. a fresh terminal
  before any project context loads), pick the OS-native helper:
  - Linux: `git-credential-libsecret` (ships with most distros)
  - macOS: `git-credential-osxkeychain` (built into Apple Git)
  - Windows: `git-credential-manager` (built into Git for Windows)

  These read the OS keychain directly — the same store the launcher
  writes to — but bypass the per-project active-flag gate.

### Step 5 — Replacing `git-credential-vct`

The 0.2.0 fork-readiness sweep retires `~/.vct-secrets/git-credential-vct`
in favour of **per-project `GITHUB_TOKEN` env propagation**. The change
has two halves:

#### Half 1 — OnboardingWizard PAT moved into the keychain

The `register_github_pat` flow used by the OnboardingWizard previously
wrote the token to `~/.vct-secrets/shared/github_pat` (mode 0o600). It
now writes to the OS keychain at the entry
`vct._user_shared_.shared.installer / github_pat` — same store every
other launcher-managed secret already used. On first call after
upgrade, a one-shot migration ingests any pre-existing file at
`~/.vct-secrets/shared/github_pat` (or the legacy flat layout
`~/.vct-secrets/github_pat`) into the keychain and deletes the file.
The migration is gated by an `app_state` flag (`github_pat.file_to_keychain.v1`)
so it runs at most once per launcher install. Soft-fail: if the
keychain backend is unreachable, the file is left in place for the
next attempt.

The `has_github_pat` / `get_github_pat_preview` / `clear_github_pat`
read APIs were updated symmetrically: they consult the keychain first
(honouring the cross-launcher active-flag gate) and only fall back to
the legacy file paths during the upgrade window.

#### Half 2 — Per-project `GITHUB_TOKEN` env propagation

The launcher's project-env writer
(`commands/projects_v2.rs::write_project_env_files`) now emits
`GITHUB_TOKEN=<value>` to all three install surfaces:

- `.claude/env` (POSIX export form, sourced by `tools/claude` wrapper)
- `.claude/settings.json` `env` block (read by Claude Code CLI)
- `.vscode/settings.json` `claude-code.env` block (read by Claude Code
  VS Code extension)

The value is resolved at write time from the same keychain entry the
OnboardingWizard wrote (`vct._user_shared_.shared.installer/github_pat`)
and gated through the active-flag check. The key is omitted entirely
(not emitted as `GITHUB_TOKEN=""`) when:

- the keychain has no entry, OR
- the entry is paused via Lifecycle B in the launcher GUI, OR
- the keychain backend is unreachable.

This omission semantic is critical: downstream consumers (`gh` CLI,
custom git credential helpers) distinguish "GITHUB_TOKEN unset" from
"GITHUB_TOKEN=''" and the latter would mask the user's other auth
flow (e.g. an existing `~/.config/gh/hosts.yml` token).

#### Per-project gating (conservative, 0.2.0)

Today, every registered project receives `GITHUB_TOKEN` whenever the
PAT is set and active in the keychain. This matches the pre-0.2.0
file-based behaviour: `~/.vct-secrets/shared/github_pat` was readable
by every process running as the user, so any project's hook /
subprocess could already read it. A finer-grained per-project access
matrix for `github_pat` (analogous to the KG access matrix in P1-D) is
out of scope for the 0.2.0 fork sweep and is the natural follow-up if
fork users ask for it.

#### What you need to do (one-time)

1. Delete `~/.vct-secrets/git-credential-vct` and remove the matching
   entry from your global git config (see the snippet under the
   "RETIRED in 0.2.0" section above).
2. Pick ONE of the three replacement helper options listed there
   (`gh auth setup-git`, thin shell helper, or keychain-native helper).
3. Verify your registered projects now have `GITHUB_TOKEN` populated:
   ```bash
   # In a registered project folder:
   grep '^export GITHUB_TOKEN=' .claude/env
   # Should print a single line if you've set a PAT in the OnboardingWizard.
   ```
   This verifies the env-file emission path (the canonical 0.2.0 path).
   On 0.2.0+, `register_github_pat` automatically re-runs
   `write_project_env_files` for every registered project, so existing
   projects pick up `GITHUB_TOKEN` without you needing to re-touch them.
4. (Optional) Re-run the OnboardingWizard PAT step if you skipped it
   originally — the launcher won't migrate a token you never set.

### What's working in 0.2.0

After the 2026-05-08 fork-readiness sweep (items H1-H5) every secrets
path that was previously gated on a 0.1.8 follow-up now works in 0.2.0,
and the localhost-reachable-without-auth gap (the "anyone running as
the same user can curl localhost and exfiltrate secrets" attack class)
is closed by the H5 token gate.

| Path | Status |
|---|---|
| `register_github_pat` writes to keychain (not file) | ✅ 0.2.0 |
| Existing registered projects auto-receive `GITHUB_TOKEN` env on PAT save | ✅ 0.2.0 |
| `clear_github_pat` strips `GITHUB_TOKEN` from all projects' env surfaces | ✅ 0.2.0 |
| `wrapper.sh` consumes `$GITHUB_TOKEN` env first (canonical path) | ✅ 0.2.0 |
| `wrapper.sh` resolver fallback (calls hub `/projects/{id}/env`) | ✅ 0.2.0 (H1: orchestrator's `vct-module.json::bundled_secrets[]` declares `github_pat`; hub uses SENTINEL_SHARED for keychain lookup) |
| GUI Shared/Global tabs auto-emit secrets to env surfaces | ✅ 0.2.0 (H2: `resolve_user_secret_state` now enumerates all three buckets; shared/global writes fan out to every registered project) |
| GUI Per-project tab auto-emits user-set secrets to env | ✅ 0.2.0 |
| Hub `/projects/{id}/env` resolver path for arbitrary modules | ✅ 0.2.0 (works for explicit module installs AND for orchestrator-bundled secrets) |
| `is_secret_set` / `get_secret_preview` use cross-launcher gate | ✅ 0.2.0 (H3: GUI badge agrees with hub + env-file emit; no prod-vs-dev launcher disagreement) |
| `wrapper.sh` legacy file fallback (`VCT_LEGACY_FILE_FALLBACK=1`) | ❌ REMOVED in 0.2.0 (H4: env-first AND resolver both work end-to-end; the file fallback was a one-release safety net and is no longer needed. Stale `~/.vct-secrets/shared/github_pat` files get auto-migrated into the keychain on the next `register_github_pat` call.) |
| Hub auth-token gate on `/api/v1/*` (except `/health`) | ✅ 0.2.0 (H5: 32-byte CSPRNG token regenerated per launcher startup, persisted to `<vct_root_dir>/hub.token` mode `0o600`; `Authorization: Bearer <token>` enforced in middleware with constant-time comparison; resolver helpers + `hub_proxy` + `vct-cli` all updated to send the header. See [Hub authentication](#hub-authentication).) |

**Mental model**: in 0.2.0, both the **env-file emission** path and the
**hub resolver** path are first-class. The env path is what feeds
Claude Code subprocesses on session start; the resolver path is what
bundled wrappers (and any future external tooling) call when they need
to read a secret directly. Both go through the same keychain slots,
the same active-flag gate, and (after H3) the same cross-launcher pause
check. They cannot disagree.

### Hub authentication

(Item H5, 2026-05-08.) Implemented in `launcher/src-tauri/src/hub/auth.rs`,
wired in `hub/server.rs` between the route nest and the CORS layer.

#### Why this exists

Before H5, the launcher's hub bound `127.0.0.1:7700` with no
authentication. Any process running as the same OS user could `curl`
`http://127.0.0.1:7700/api/v1/projects/<id>/env` and exfiltrate every
secret the launcher's keychain had marked active for the project — a
rogue `npm install` script, a malicious `pip install`, a browser
extension calling `fetch()` against localhost, an installer post-script,
or an injected build dependency in any toolchain dropping into the
user's home dir. The same attack class has hit other localhost-bound
daemons (Docker socket exposure, Bun's dev server, the typosquats that
periodically appear in `npm`/`pip`/`crates.io`).

The `~/.vct-secrets/` plaintext-file path that 0.2.0 retired (Fix #3)
had the same threat profile in a different shape: any process that
could read `$HOME` could also read the secrets. The hub-only architecture
fixed THAT path but introduced the localhost-reachable-without-auth gap.
H5 closes the new gap by requiring an unguessable bearer token that
only same-user processes can read.

#### How it works

| Phase | Action |
|---|---|
| Token generation | On every hub startup, `auth::generate_token()` pulls 32 bytes from `OsRng` (the OS CSPRNG) and hex-encodes them — 64 ASCII chars, 256 bits of entropy. Matches the length of GitHub PAT classic and Vercel access tokens. |
| Persistence | `auth::write_token_file()` writes the token to `<vct_root_dir>/hub.token`. On Unix the file is created with `O_CREAT\|O_TRUNC\|O_WRONLY` and mode `0o600` in a single syscall — closes the write-then-`chmod` TOCTOU window where the file briefly exists with the umask's default mode. On Windows we rely on the default profile-dir ACL (same-user-only by Windows defaults). |
| Server-side gate | The axum middleware `auth::require_auth` runs in front of every route. It bypasses `OPTIONS` (CORS preflight) and `/api/v1/health` (liveness probe), then for every other request reads the `Authorization` header, parses `Bearer <token>` (case-insensitive scheme, case-sensitive token), and compares against the in-memory `AuthState::token` using a constant-time XOR-accumulator pattern (avoids leaking prefix-match info through timing). |
| Exempt endpoints | Only `/api/v1/health`. It returns `{"status":"ok",...}` with no secrets — gating it would force every liveness-check caller to read `hub.token` first, and a 401-vs-503 response gives an attacker the same probe ability anyway. Everything else under `/api/v1/` requires auth. |
| Token rotation | Every launcher startup regenerates the token and overwrites `hub.token` (truncate-on-open, mode 0o600 reapplied). Long-lived tokens widen the window where a misconfigured tar/zip created with permissive perms could leak the value; rotating per-startup keeps the window vanishingly small. |

#### Client integration

Every in-tree client that talks to the hub has been updated to send the
token. Third-party tooling that talks to the hub directly needs the same
treatment — see [Step 6 — custom scripts](#step-6--custom-scripts) for
the recipe.

| Client | How it gets the token |
|---|---|
| Resolver helpers (`vct_secrets_resolve.{sh,ps1}`) | `$VCT_HUB_TOKEN` env var first (tests / dev harnesses), else `${VCT_STATE_DIR:-$HOME/.vct}/hub.token`. Fed to `curl` via `--header @-` (Bash) / `-Headers @{Authorization=...}` (PowerShell) so the token never lands on `argv` and isn't visible in `ps`/`/proc/<pid>/cmdline`. |
| Launcher GUI ↔ hub (`commands::hub_proxy`) | Reads `hub.token` directly from disk on each call. The launcher process is what wrote it — no rotation race. |
| `vco` / `vct-cli` (`launcher/tools/vct-cli`) | Reads `hub.token` once per invocation. CLI is short-lived; no caching needed. |
| Future browser-side clients | The hub's CORS layer explicitly allowlists `Authorization` in `Access-Control-Allow-Headers` so browser preflights don't strip it. |

#### Error semantics

| Scenario | HTTP status | Resolver exit code | Diagnostic |
|---|---|---|---|
| `Authorization` header missing | 401 | 1 (hub unreachable) | `unauthorized` envelope: `{"error":{"code":"unauthorized","message":"missing or invalid Authorization: Bearer <token>; read the launcher's hub.token file..."}}` |
| Wrong token (e.g. stale: launcher restarted between resolver calls) | 401 | 1 | Same envelope. The resolver maps 401 → exit 1 ("hub unreachable") because the user fix is the same: re-source env / restart the resolver / talk to the launcher. |
| `hub.token` file missing AND `VCT_HUB_TOKEN` unset | (no request sent) | 1 | Resolver short-circuits before the HTTP call: `[vct-secrets-resolve] hub.token missing; is the launcher running?` |
| Wrong scheme (`Authorization: Basic ...`) | 401 | 1 | Same envelope as missing-header. |

#### Threat-model boundary

What this DOES defend against:

- Rogue same-user package executing arbitrary code in a build/install
  hook and curling localhost without doing anything explicit about
  reading `~/.vct/hub.token`.
- Browser extensions (which run in the browser process, separate from
  the user's home dir read access by default sandboxing) attempting to
  `fetch()` against localhost.
- Most localhost-reachable-from-the-network attacks (the hub binds
  `127.0.0.1` only; H5 is defence in depth).

What this does NOT defend against:

- Same-user attacker with arbitrary code execution who actively reads
  `~/.vct/hub.token`. They can also dump the OS keychain directly
  (libsecret, Windows Credential Manager, macOS Keychain are all
  per-user). The auth gate raises the bar from "any package in the
  dependency tree can curl localhost" to "the package has to actively
  read `~/.vct/hub.token`" — same protection level Docker socket
  permissions provide for the docker daemon.
- Network adversary — out of scope, hub binds 127.0.0.1 only.
- A malicious launcher build that ships a known token. Out of scope
  (the user implicitly trusts the launcher binary they run).

#### Migration recipe for third-party tools

If you have a script or tool that talks to the launcher hub directly
(without going through the resolver helper), you need to add the
`Authorization: Bearer <token>` header. The token lives at
`~/.vct/hub.token` (or `$VCT_STATE_DIR/hub.token` if set). Read it fresh
each call — the launcher rewrites it on every startup.

```bash
# Before:
curl -s "http://127.0.0.1:7700/api/v1/projects/$PID/env?key=GITHUB_TOKEN"

# After:
TOKEN=$(tr -d '[:space:]' < "${VCT_STATE_DIR:-$HOME/.vct}/hub.token")
[ -z "$TOKEN" ] && { echo "launcher not running" >&2; exit 1; }
curl -s --header @- \
    "http://127.0.0.1:7700/api/v1/projects/$PID/env?key=GITHUB_TOKEN" \
    <<<"Authorization: Bearer ${TOKEN}"
```

`--header @-` reads the header from stdin so the token never appears
on `argv` (visible to any other process via `/proc/<pid>/cmdline`).

For PowerShell:

```powershell
# Before:
Invoke-RestMethod "http://127.0.0.1:7700/api/v1/projects/$pid/env?key=GITHUB_TOKEN"

# After:
$tokenPath = if ($env:VCT_STATE_DIR) { "$env:VCT_STATE_DIR\hub.token" } else { "$env:USERPROFILE\.vct\hub.token" }
$token = (Get-Content $tokenPath -Raw).Trim()
if (-not $token) { Write-Error "launcher not running"; exit 1 }
Invoke-RestMethod -Uri "http://127.0.0.1:7700/api/v1/projects/$pid/env?key=GITHUB_TOKEN" `
    -Headers @{ Authorization = "Bearer $token" }
```

Or — preferred — call `vct_secrets_resolve.sh` / `.ps1`. The helper
handles hub-port discovery, token-file reading, and the exit-code
contract (1=hub-down, 2=project-not-registered, 3=key-paused,
4=key-not-declared). See [Step 6 — custom scripts](#step-6--custom-scripts)
for the wrapper-migration recipe.

### Step 6 — custom scripts

If you wrote a script that does `cat ~/.vct-secrets/shared/<key>` and
expects the launcher to keep it up-to-date, switch to the resolver:

```bash
# Before:
TOKEN=$(cat ~/.vct-secrets/shared/MY_KEY)

# After:
TOKEN=$(/path/to/orchestrator/.claude/scripts/vct_secrets_resolve.sh \
        "$VCT_PROJECT_PATH" MY_KEY) || {
    echo "secret resolution failed (exit $?)" >&2
    exit 1
}
```

Or, for a project-agnostic script that previously read the SHARED
secret regardless of cwd: pass the path of any registered project the
secret is declared for. The hub will still apply the
active-flag gate.

## Rollback

If the migration breaks something on your machine and you need to roll
back, downgrade the launcher to 0.1.6 (last pre-Fix-#3 build):

```bash
# Linux / macOS:
sudo apt-get install vco-launcher=0.1.6  # or your distro's equivalent
# Windows: re-install from the 0.1.6 MSI in Releases
```

0.1.6's secrets layer is keychain-only without Fix #3 or the hub-side
secrets endpoint hardening; bundled MCP wrappers fall back to reading
`~/.vct-secrets/` directly (the original architecture).

## Why this isn't just "Fix #3 part 2"

The user's verdict on Fix #3:

> "Any secret added by the user through Launcher's GUI should be
> handled through the launcher's secure keychain with per-project
> allowlist/gating"

The two structural problems with Fix #3:

1. It hard-coded which keys were "blessed" enough to leave the keychain
   and land on disk. New secrets the user added via the GUI weren't on
   the list, so they were silently invisible to wrappers — exactly the
   bug Fix #3 was supposed to solve. Every "blessed" key required a
   security-sensitive change to the Rust source.
2. It bypassed the per-project active-flag gate (Lifecycle B). A user
   who paused `OPENAI_API_KEY` in the GUI still had the cleartext value
   readable at `~/.vct-secrets/shared/OPENAI_API_KEY` — the launcher's
   GUI lied "secret unset", the file said "here's the value".

Both problems are dissolved by routing every read through
`/projects/{id}/env`: the hub already owns the active-flag gate and
the per-project access decision, and a wrapper asking for a key that
isn't active for the project gets a clean 404 with `key_not_active`
rather than a stale cleartext value.

## Open questions deliberately deferred

- **OnboardingWizard's `register_github_pat`** ~~still writes
  `~/.vct-secrets/shared/github_pat` directly~~ — RESOLVED in 0.2.0
  fork-readiness sweep (2026-05-08). The PAT now lives in the OS
  keychain; the helper `git-credential-vct` is retired in favour of
  per-project `GITHUB_TOKEN` env propagation. See
  "Replacing `git-credential-vct`" above.
- **Finer-grained per-project access matrix for `github_pat`** —
  today every registered project sees `GITHUB_TOKEN` whenever the PAT
  is set and active. That matches the pre-0.2.0 file-based behaviour
  but is broader than necessary. A future release could add
  `github_pat` to the launcher's per-project access matrix (analogous
  to KG access in P1-D) so the user can grant the PAT to a subset of
  projects.
- **`tools/vct-secrets/vct` CLI** is the user-facing `vct set/get`
  tool. It writes to `~/.vct-secrets/projects/<project>/<key>`. It
  is not a runtime consumer — it's the primitive humans use to manage
  arbitrary file-based secrets outside the launcher. It stays
  unchanged. (We may add a `vct sync` subcommand later that ingests
  flat-file secrets into the launcher keychain.)

---

If you hit a regression not covered above, file an issue with the
prefix `[secrets-0.2.0]` and include:

- the launcher version you upgraded from / to
- the wrapper or hook that broke
- the exit code from `vct_secrets_resolve.sh <project> <key>`
- the contents of `~/.vct/hub.port` (the port the launcher claims it's
  listening on)
