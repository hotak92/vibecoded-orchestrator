# VCT Secrets Primitive — file-store CLI (`vct`)

A small command-line tool for managing secrets per project, with strict file
permissions and a sandbox-friendly `exec` wrapper that never exposes secret
values to parent shells.

## For Claude Code agents — read this first

If you are a Claude agent that needs a credential (GitHub PAT, API key, …):

1. **Never grep the environment for secrets.** `env | grep TOKEN` is blocked
   by the `bash_security.py` PreToolUse hook; the block message points back
   here.
2. **Discover what exists**: `vct list` (shared scope) or
   `vct list --project <name>`.
3. **Probe before reading**: `vct can-read --key <KEY>` (exit 0/1, prints
   nothing). `vct resolve --key <KEY>` prints which file would be read.
4. **Prefer injection over printing**:
   `vct exec --secret KEY=ENV_VAR -- cmd args` runs `cmd` with the value in
   `$ENV_VAR` only — never on argv, never in shell history.
5. **Key purposes** are documented in `~/.vct-secrets/shared/_README.md`
   (materialized by `install.py`). Read it before guessing which GitHub
   token shape a tool needs.
6. **Launcher-installed projects** also have an OS-keychain-backed store
   resolved through `vct-hub` — see "Two stores, one mental model" below.

## Two stores, one mental model

There are two complementary secret stores in a VCO install:

| Store | Backed by | Written by | Read via |
|---|---|---|---|
| **Launcher keychain** (canonical for launcher-managed slots like `github_pat`, `openai_api_key`) | OS keychain (macOS Keychain / Linux Secret Service / Windows Credential Manager) | Launcher GUI: OnboardingWizard step 4, or Preferences → Special Secrets → SecretsPanel | vct-hub `GET /api/v1/projects/{id}/env?key=…` — clients: `templates/scripts/vct_secrets_resolve.sh` / `.ps1`, `vco_lib/agent_secrets.py` |
| **File store** (`~/.vct-secrets/`) | chmod-600 files | This CLI (`vct set`), manually | This CLI (`vct get` / `vct exec`), `git-credential-vct` |

The launcher does **not** create or migrate the file store, and does **not**
register the git credential helper — those are manual steps documented below.
(Earlier versions of this README claimed otherwise; that was never true.)
The file store is the right tool when no launcher is installed, or for
secrets you manage yourself outside the GUI.

## Layout (file store)

```
~/.vct-secrets/
├── vct                       — the CLI (this script), if you copied it here
├── git-credential-vct        — git credential helper for github.com
├── shared/
│   ├── _README.md            — per-key purpose/schema doc (agent-facing)
│   └── <key>                 — secrets accessible to any project
└── projects/
    └── <PROJECT_NAME>/
        └── <key>             — project-scoped secrets (override shared)
```

All secret files are mode 600. Directories are mode 700.

## Subcommands

```
vct list [--project P]                — list secret keys (names only, no values)
vct get   --key K [--project P]       — print the value (requires --trusted in TTY);
                                        no --project → shared scope
vct set   --project P --key K         — read value from stdin, write atomically
                                        (--project required: writes are deliberate)
vct exec  [--project P] --secret K=ENV -- <cmd> [args...]
                                      — run cmd with K's value in $ENV; never echoes;
                                        no --project → shared scope
vct can-read --key K [--project P]    — exit 0 if K resolves, 1 if not (silent)
vct resolve  --key K [--project P]    — print the file path the resolver would read
vct revoke --project P --key K        — delete a secret
vct copy  --from-project P1 --to-project P2 --key K
vct migrate-from-env <path/to/.env> --project P
vct doctor                            — fix file permissions; shape-check github_pat
                                        entries (warns on corrupted tokens, never
                                        prints values)
vct detect-project                    — print the project name from cwd
```

## Resolution order

When `vct exec --project foo --secret github_pat=GITHUB_TOKEN` is invoked:

1. `~/.vct-secrets/projects/foo/github_pat` (project-scoped)
2. `~/.vct-secrets/shared/github_pat` (shared fallback)
3. fail (exit 2, child not run — no leak to env)

`vct resolve --project foo --key github_pat` shows which of the two would win.

## Why `exec` instead of `get`?

`exec` runs the child command with the secret in its environment, then exits.
The secret is never:
- written to a file the child can `cat`
- visible in `ps` output (it's an env var)
- echoed by `set -x` (the literal value isn't on the command line)
- captured by shell history (no `$(cat ...)` substitution in the parent)

`get` is provided for explicit script use but requires `--trusted` in TTY to
discourage accidental leaks via terminal scrollback / clipboard.

## Installation (manual — the launcher does NOT do this for you)

```bash
git clone https://github.com/hotak92/vibecoded-orchestrator
cd vibecoded-orchestrator
mkdir -p ~/.vct-secrets/{shared,projects}
chmod 700 ~/.vct-secrets
cp tools/vct-secrets/vct                 ~/.vct-secrets/vct
cp tools/vct-secrets/git-credential-vct  ~/.vct-secrets/git-credential-vct
chmod 755 ~/.vct-secrets/vct ~/.vct-secrets/git-credential-vct
export PATH="$HOME/.vct-secrets:$PATH"   # add to your shell rc
```

To use the git credential helper for HTTPS pushes (also manual):

```bash
git config --global credential.https://github.com.helper \
    "!$HOME/.vct-secrets/git-credential-vct"
```

It reads `projects/<name>/github_pat` (when a `.vct-project` marker file or
`$VCT_PROJECT_ROOT_PATTERN`, default `$HOME/dev`, identifies the project)
and falls back to `shared/github_pat`.

## GitHub token shapes (avoid the 401 rabbit hole)

Different GitHub consumers need different token kinds. Store them under
distinct keys and check `~/.vct-secrets/shared/_README.md` for what each key
on your machine is for. `vct doctor` shape-checks any key named
`github_pat`/`github_pat.*`:

| Shape | Pattern | Typical use |
|---|---|---|
| Classic PAT | `ghp_` + 36 chars (40 total) | `git push` HTTPS, `gh` CLI |
| Fine-grained PAT | `github_pat_` + ~82 chars (~93 total) | `gh` REST API, scoped access |
| App installation | `ghs_` + 36 chars | GitHub App flows — NOT valid for `gh` |
| OAuth | `gho_` + 36 chars | OAuth apps |

Trailing newlines in stored values are stripped by `vct exec`; if you read a
token any other way, strip whitespace first
(`printf %s "$TOK" | tr -d '\r\n '`) — `gh` rejects tokens with stray
whitespace in the HTTP header.

## Tests

```bash
bash tools/vct-secrets/tests/test_vct.sh
```

22 tests covering set/get/exec/can-read/resolve/revoke/copy/
migrate-from-env/doctor and permission enforcement.

## Migration from flat `~/.vct-secrets/<key>`

If you have secrets at the legacy flat path (`~/.vct-secrets/github_pat`
instead of `~/.vct-secrets/shared/github_pat`):

```bash
bash tools/vct-secrets/migrate-shared.sh --dry-run
bash tools/vct-secrets/migrate-shared.sh
```

See `MIGRATION.md` for the full rationale and step-by-step.

## Design doc

Full design at [docs/VCT_SECRETS_PRIMITIVE.md](../../docs/VCT_SECRETS_PRIMITIVE.md)
in the orchestrator repo.
