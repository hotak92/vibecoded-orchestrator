# VCT Secrets Primitive — Design

A unified primitive for per-project secrets, replacing ad-hoc mixes of
per-service wrapper scripts, flat global token files, and `.env` files
scattered across an ecosystem of related projects.

This doc describes **phase 1** (the bash `vct` CLI shipped in this repo
under `tools/vct-secrets/`). Phases 2–4 (Rust port, GUI, daemon) are
sketched at the end and implemented in the closed-source VCT Launcher.

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

vct get --project NAME --key KEY [--trusted]
    Print a secret to stdout. Requires --trusted in a TTY (tripwire
    against accidental shell-history capture). Use sparingly; prefer exec.

vct exec --project NAME [--secret KEY[=VAR]]... [--preserve-env] -- <cmd>
    Inject + exec. Primary workflow. Never taints parent shell.
    Without --preserve-env, runs with a minimal scrubbed env.

vct revoke --project NAME --key KEY [--yes]
    Delete a secret file. Confirmation prompt unless --yes.

vct copy --from-project SRC --to-project DST --key KEY [--yes]
    Explicit cross-project copy. Audit log entry. Prompts unless --yes.

vct migrate-from-env FILE --project NAME [--dry-run]
    Read a `.env` file, import each KEY=VALUE into the project store,
    rename source to <FILE>.migrated on success.

vct doctor
    Audit dir/file permissions, fix where needed (700/600).

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

### Resolution order

For each requested `--secret KEY`:

1. `~/.vct-secrets/projects/<PROJECT>/<KEY>` — project-specific (first priority)
2. `~/.vct-secrets/shared/<KEY>` — cross-project fallback
3. Fail fast (exit 2, clear error message)

Project always wins, so e.g. one project's `openai_api_key` is its own,
period.

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
`$HOME/Desktop/PROGETTI`), and reads the per-project or shared
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

## Phase 2+ (out of scope for this doc)

- **Phase 2** — Rust binary with the same CLI surface, distributed
  inside the VCT Launcher installer. Drop-in replacement.
- **Phase 3** — Launcher GUI for add/rotate/revoke + audit-log viewer.
- **Phase 4** — Optional secrets daemon (`vct-secretsd`) that auto-starts
  on login or VS Code workspace open, exposing a Unix-domain socket for
  faster repeated lookups + centralised audit. Not required — phase 1
  works fine with direct file reads.

The on-disk layout (`~/.vct-secrets/`) and CLI contract are stable, so
all callers (git credential helper, MCP wrappers, project hooks) keep
working unchanged across phases.

---

## Open design points

1. **Shared secret fallback — on by default?** Yes. Projects can opt out
   by placing a `~/.vct-secrets/projects/<NAME>/.no-shared-fallback`
   marker file. (Phase 2 will honour this; phase 1 always falls back.)
2. **Cross-project copy — confirmation required?** Yes. `vct copy`
   prompts unless `--yes`. Prevents accidental token leaks.
3. **`vct get` `--trusted` requirement.** Required in a TTY (interactive
   shell, where scrollback / history capture are the risk). In scripts
   the choice is already explicit.
4. **Audit log rotation.** Phase 2 concern. For phase 1, unbounded
   growth is fine — hundreds of bytes per call, years before it matters.
