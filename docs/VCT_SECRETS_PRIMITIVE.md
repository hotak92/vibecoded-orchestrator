# VCT Secrets Primitive — Design

A unified primitive for per-project secrets, replacing ad-hoc mixes of
per-service wrapper scripts, flat global token files, and `.env` files
scattered across multiple projects on one machine.

This doc covers the whole primitive: the **canonical three-tier
resolution chain** (hub → file store → project `.env`, below), the bash
`vct` CLI shipped under `tools/vct-secrets/` (the file-store tier's
management surface), and the integration patterns around them.

---

## Motivation

Secret handling in a multi-project setup tends to fragment:

| Pattern | Example | Problem |
|---|---|---|
| Per-service wrapper script | `~/.secrets/search-mcp-wrapper.sh`, `git-credential-vct` | One script per service, hand-rolled. Doesn't scale. |
| Flat global secret file | `~/.secrets/openai_api_key`, `~/.secrets/github_pat` | No project isolation — one OpenAI key for every project on the box. |
| Per-project `.env` files | `proj/.env`, `proj/.env.secrets` | Risk of accidental git commit. Duplicated across projects. No rotation workflow. |
| Shell-substituted inline secret | `curl -H "Authorization: Bearer $(cat ~/.secrets/token)"` | Blocked by some sandboxes (e.g. Claude Code) that treat `$(...)` substitution as exfiltration. |

Project-scoped isolation is the goal: each project sees only the secrets
it owns; shared secrets (e.g. one GitHub PAT for all of your repos) are
explicitly opt-in via a `shared/` namespace.

---

## Canonical resolution chain — three tiers

Every sanctioned resolver walks the SAME chain, in this order:

1. **vct-hub (tier 1, keychain-backed)** — `GET
   /api/v1/projects/{id}/env?key=NAME` against the launcher's hub.
   Values live in the OS keychain; reads are gated by the launcher's
   per-`(secret × requester)` active-flag matrix (a paused or ungranted
   key answers `key_not_active` — respect it, don't route around).
   Hub discovery: `$VCT_HUB_PORT` / `$VCT_HUB_TOKEN` env →
   `<vct_root>/hub.port` + `hub.token` → defaults. Canonical for
   launcher-managed slots (`github_pat`, `openai_api_key`, module
   secrets, and every GUI-saved user secret).
2. **File store (tier 2)** — when the hub is unreachable, the project
   isn't registered, or the key isn't active there: `$VCT_SECRETS_DIR`
   (default `~/.vct-secrets`), `projects/<NAME>/<key>` first, then
   `shared/<key>` (see [Storage layout](#storage-layout) below — the
   `vct` CLI manages this tier).
3. **Project `.env` (tier 3, READ-ONLY, lowest priority)** — the
   requesting project's own root `.env`. Line-oriented parse: `KEY=VALUE`
   and `export KEY=VALUE`, one matching pair of quotes stripped, NO
   variable expansion, NO command substitution, first match wins. VCO
   only ever READS this file for resolution — see the invariant below.

**Must-match triplet**: the chain is implemented three times — the bash
resolver `templates/scripts/vct_secrets_resolve.sh`, its PowerShell
sibling `templates/scripts/vct_secrets_resolve.ps1`, and the Python
helper `vco_lib/agent_secrets.py` (`get` / `exec_with_secrets`). Tier
order, fall-through rules, and the tier-3 parsing rule MUST stay
identical across all three; each carries a must-match comment naming
the other two. Change one → change all three.

**Write invariant**: VCO never writes secret values into the project tree.
Resolution is read-at-need only — no resolver, projection writer, or
bundle step persists a secret VALUE into `.claude/settings.json`,
`.claude/env`, `.vscode/settings.json`, `.env`, or any other file under
the project folder (the tree-wide invariant tests in
`launcher/src-tauri/src/commands/projects_v2.rs` and
`tests/test_config_projection_byte_identical.py` enforce this).

### Hub network posture (tier 1)

The hub binds `127.0.0.1` (loopback-only) by default, so a leaked
`hub.token` is useless from the LAN. The bind widens to `0.0.0.0`
only (a) while a hub-consuming module (a global container module such
as the RL reranker) is installed — its container reaches the hub via
`host.containers.internal`, which never maps to the host's own loopback
on any OS — or (b) when the user sets `VCT_HUB_BIND_ALL=1` explicitly.
An explicit `VCT_HUB_BIND_ALL` value wins in both directions. Every
`/api/v1/*` route stays bearer-token gated while widened. Full runbook:
`docs/TROUBLESHOOTING.md` § "Hub bind posture".

### Per-project resolver tokens (tier 1, v0.2.76)

The two per-project routes — `GET /api/v1/projects/{id}/env` and
`GET /api/v1/projects/{id}/config` — accept a **project-scoped** bearer
in addition to the global `hub.token`. On startup the hub mints one
`hub.token.<project_id>` (mode `0o600` on Unix — POSIX; on Windows the
default same-user ACL) per registered project, rotating them each start
like `hub.token` and removing files for deleted projects. A resolver
that knows its project id (it is the lookup key) presents the scoped
token, so a credential that leaks to a different local user/process reads
only that ONE project's env + config, not every project's.

The bundled resolvers already prefer the scoped token: each reads
`hub.token.<id>` first and falls back to `hub.token` when the scoped file
is absent (a project added while the hub is running, or a pre-v0.2.76
hub). `VCT_HUB_TOKEN` (env) still overrides both, so a test/dev harness
that pins it keeps a single token across every route.

As of **v0.2.77 the global `hub.token` is REFUSED by default** on these two
routes (`/env` + `/config`) — the one-release compatibility window that
v0.2.76 opened is now closed by default. A per-project scoped token is
required; a wrong-project token is always a hard `403`. If a bespoke caller
still presents the global token and cannot migrate yet, set
`VCT_HUB_LEGACY_GLOBAL_ENV=1` (or `true` / `TRUE` / `yes`) on the **hub
process** to re-open the compat window for one more release, then restart the
hub. Any other value — including **unset**, `0`, `false`, `no`, or a typo —
**denies** (fail-closed); note that unset now DENIES (the opposite of the
v0.2.76 default). This escape hatch will be removed in a future release. The
hub lazy-mints a scoped token on the first request for a project added while
it was running, so standard installs need nothing. POSIX and PowerShell
resolvers behave identically — see the must-match triplet above.

---

## Architecture — secret-injecting `exec` wrapper

### Core primitive

```
vct exec --project <NAME> [--secret KEY[=VAR_NAME]]... -- <command...>
```

Behaviour:

1. Look up each requested secret in the project scope, fall back to shared scope.
2. Inject resolved values into the child process environment.
3. `exec` the child command — the secret never appears in the parent shell's
   command line, history, or environment.
4. If any requested secret is missing, fail fast with exit code 2 before
   running the child.

Example:

```bash
vct exec --project myproj --secret VERCEL_TOKEN -- \
  curl -sS -H "Authorization: Bearer $VERCEL_TOKEN" \
  https://api.vercel.com/v6/deployments?limit=10
```

The `curl` subprocess sees `$VERCEL_TOKEN`. The parent shell does not.
The file `~/.vct-secrets/projects/myproj/vercel_token` is opened only by
`vct exec`, which appends a single audit-log line.

### Rename on inject

```bash
vct exec --project myproj --secret github_pat=GH_TOKEN -- gh pr list
```

Reads `~/.vct-secrets/projects/myproj/github_pat`, exposes it to the child
as `$GH_TOKEN`. Different tools expect different env var names for the
same concept (`GITHUB_TOKEN` vs `GH_TOKEN` vs `GITHUB_PERSONAL_ACCESS_TOKEN`).

### Multiple secrets

```bash
vct exec --project myproj \
  --secret VERCEL_TOKEN \
  --secret SUPABASE_SERVICE_ROLE_KEY=SUPABASE_KEY \
  -- ./deploy.sh
```

Declare each needed secret once. Missing ones fail fast before the script runs.

---

## CLI surface (complete)

```
vct list [--project NAME]
    List secret keys for a project (names only, never values).
    Omit --project to list shared secrets.

vct set --project NAME --key KEY  (reads value from stdin)
    Store a secret. Never takes the value on the command line.
    Writes to ~/.vct-secrets/projects/<NAME>/<KEY> with chmod 600.

vct get --key KEY [--project NAME] [--trusted]
    Print a secret to stdout. Requires --trusted in a TTY (tripwire
    against accidental shell-history capture). Use sparingly; prefer exec.
    Omit --project to resolve from the shared scope (v0.2.54).

vct exec [--project NAME] [--secret KEY[=VAR]]... [--preserve-env] -- <cmd>
    Inject + exec. Primary workflow. Never taints parent shell.
    Without --preserve-env, runs with a minimal scrubbed env.
    Omit --project to resolve from the shared scope (v0.2.54).

vct can-read --key KEY [--project NAME]                       (v0.2.54)
    Exit 0 if KEY resolves (project scope first, then shared), 1 if
    not. Prints nothing — for scripts/agents probing availability.

vct resolve --key KEY [--project NAME]                        (v0.2.54)
    Print the file path the resolver would read (project wins over
    shared). Never prints the value. Exit 2 when unresolvable.

vct revoke --project NAME --key KEY [--yes]
    Delete a secret file. Confirmation prompt unless --yes.

vct copy --from-project SRC --to-project DST --key KEY [--yes]
    Explicit cross-project copy. Audit log entry. Prompts unless --yes.

vct migrate-from-env FILE --project NAME [--dry-run]
    Read a `.env` file, import each KEY=VALUE into the project store,
    rename source to <FILE>.migrated on success.

vct doctor
    Audit dir/file permissions, fix where needed (700/600). Also
    shape-checks github_pat/github_pat.* keys (classic ghp_ 40 chars /
    fine-grained github_pat_ / App ghs_; warns on corrupted blobs and
    embedded whitespace — never prints values) (v0.2.54).

vct detect-project
    Walk up from PWD looking for a `.vct-project` marker file;
    print its content (the project name).

vct help | version
```

---

## Storage layout

```
~/.vct-secrets/
├── vct                                 # The CLI (phase 1: bash)
├── git-credential-vct                  # Git credential helper
├── projects/
│   ├── <project-name>/
│   │   ├── <key>                       (chmod 600)
│   │   └── …
│   └── …
├── shared/                             # Secrets that span projects
│   ├── github_pat
│   └── …
└── audit.log                           # Append-only JSON-lines log
```

### Resolution order (within tier 2)

This is the order INSIDE the file store — the second tier of the
[canonical three-tier chain](#canonical-resolution-chain--three-tiers)
above. For each requested `--secret KEY`, the `vct` CLI resolves:

1. `~/.vct-secrets/projects/<PROJECT>/<KEY>` — project-specific (first priority)
2. `~/.vct-secrets/shared/<KEY>` — cross-project fallback
3. Fail fast (exit 2, clear error message)

Project always wins, so e.g. one project's `openai_api_key` is its own,
period. (The hub-first resolvers — `vct_secrets_resolve.sh` / `.ps1` /
`agent_secrets.py` — consult the hub before this tier and the project
`.env` after it.)

### Permissions

- `~/.vct-secrets/` → `chmod 700`
- All files under it → `chmod 600`
- `vct set` enforces these on write
- `vct doctor` audits and auto-fixes

### Audit log

Every `exec` / `get` / `set` / `revoke` writes one JSON line to `audit.log`:

```json
{"ts":"2026-04-24T14:32:01Z","op":"exec","project":"myproj","secrets":["VERCEL_TOKEN"],"caller_pid":12345,"caller_cmd":"…"}
```

Never logs the value. Purpose: forensics — if a secret is suspected
leaked, who requested it recently?

---

## Integration patterns

### For humans using a shell

Direct CLI calls as shown above.

### For MCP servers and other long-lived processes

A thin wrapper script that `exec`s into `vct exec`:

```bash
#!/usr/bin/env bash
exec vct exec --project <name> \
  --secret github_pat=GITHUB_TOKEN \
  -- "$0.real" "$@"
```

Or, if you control the launcher config, point it directly at
`vct exec --project NAME --secret KEY -- python /path/to/server.py`.

### For `git push` / `gh` CLI

Use the `git-credential-vct` helper shipped in this repo:

```bash
git config --global credential.https://github.com.helper '/path/to/git-credential-vct'
```

It walks up from `$PWD` looking for a `.vct-project` marker, falls back
to matching against `$VCT_PROJECT_ROOT_PATTERN` (default
`$HOME/dev`), and reads the per-project or shared
`github_pat`. No PAT in `~/.git-credentials`.

### For CI / deploy scripts

Scripts can declare required secrets at the top:

```bash
#!/usr/bin/env bash
# Required secrets: VERCEL_TOKEN, SUPABASE_SERVICE_ROLE_KEY
set -euo pipefail
: "${VERCEL_TOKEN:?Run via: vct exec --project NAME --secret VERCEL_TOKEN -- $0}"
# …
```

And get invoked as:

```bash
vct exec --project myproj \
  --secret VERCEL_TOKEN \
  --secret SUPABASE_SERVICE_ROLE_KEY=SUPABASE_KEY \
  -- ./scripts/deploy.sh
```

---

## Migration

If you already have a flat `~/.vct-secrets/<key>` layout (no `shared/`,
no `projects/`), use the bundled migration helper:

```bash
bash tools/vct-secrets/migrate-shared.sh --dry-run
bash tools/vct-secrets/migrate-shared.sh
```

To pull a project's `.env` file into the per-project store:

```bash
vct migrate-from-env path/to/.env --project myproj --dry-run
vct migrate-from-env path/to/.env --project myproj
```

The source file is renamed to `<file>.migrated` on success — review and
delete it once you've confirmed nothing references the old path.

---

## Security properties

| Property | Guarantee |
|---|---|
| Secrets never appear in shell history | `vct set` reads from stdin; `exec` injects into child env not parent |
| Secrets never appear in parent process env | Child process env is constructed fresh, parent unchanged |
| Project isolation | Resolution order + file path separation; one project cannot read another's directory by default |
| File perms enforced | `vct set` forces chmod 600; `vct doctor` audits |
| Audit log append-only | File mode 600, owned by user; written with `O_APPEND` |
| No network | Everything is local disk. No phone-home. |

---

## What this primitive does NOT do

- **Not a password manager for humans.** No master-password unlock flow.
- **Not a secret scanner.** Detecting leaked secrets in source code is
  a separate concern (see `.claude/hooks/`).
- **Not a team sync layer.** Secrets stay local. If teammates need the
  same key, they each `vct set` it locally.
- **Not a KMS.** Values at rest are plaintext files (chmod 600). Enough
  for a developer workstation trust boundary; not enough for production
  server secrets.

---

## Design choices

1. **Shared secret fallback is on by default.** Projects can opt out
   by placing a `~/.vct-secrets/projects/<NAME>/.no-shared-fallback`
   marker file.
2. **Cross-project copy requires confirmation.** `vct copy`
   prompts unless `--yes`. Prevents accidental token leaks.
3. **`vct get --trusted` is required in a TTY** (interactive
   shell, where scrollback / history capture are the risk). In scripts
   the choice is already explicit.
4. **Audit log is unbounded.** Hundreds of bytes per call, years before
   rotation matters.

The on-disk layout (`~/.vct-secrets/`) and CLI contract are stable, so
all callers (git credential helper, MCP wrappers, project hooks) keep
working across implementations.
