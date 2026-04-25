# VCT Secrets Primitive — Phase 1 (Bash CLI)

A small command-line tool for managing secrets per project, with strict file
permissions and a sandbox-friendly `exec` wrapper that never exposes secret
values to parent shells.

This is **Phase 1** — a Bash implementation that is:
- Easy to audit (single-file `vct` script, ~600 lines)
- Zero-dependency (POSIX `bash` 4+, GNU coreutils)
- Cross-shell (works under bash, zsh, fish via shebang)

Phase 2 will replace `vct` with a Rust binary distributed inside the VCT
Launcher installer, but the on-disk layout (`~/.vct-secrets/`) and the CLI
contract stay identical, so all consumers (git credential helper, MCP
wrappers, orchestrator hooks) keep working.

## Layout (canonical)

```
~/.vct-secrets/
├── vct                       — the CLI (this script)
├── git-credential-vct        — git credential helper for github.com
├── shared/
│   └── <key>                 — secrets accessible to any project
└── projects/
    └── <PROJECT_NAME>/
        └── <key>             — project-scoped secrets (override shared)
```

All secret files are mode 600. The directory is mode 700.

## Subcommands

```
vct list                              — list available secrets (names only, no values)
vct get   --project P --key K         — print the secret value (requires --trusted in TTY)
vct set   --project P --key K         — read value from stdin, write atomically
vct exec  --project P --secret K=ENV -- <cmd> [args...]
                                      — run cmd with K's value in $ENV; never echoes
vct revoke --project P --key K        — secure-delete a secret
vct copy  --from-project P1 --to-project P2 --key K
                                      — copy a secret across projects
vct migrate-from-env <path/to/.env>   — import .env vars into project secrets
vct doctor                            — fix file permissions, report issues
vct detect-project                    — print the project name from cwd
```

## Resolution order

When `vct exec --project foo --secret github_pat=GITHUB_TOKEN` is invoked:

1. `~/.vct-secrets/projects/foo/github_pat` (project-scoped)
2. `~/.vct-secrets/shared/github_pat` (shared fallback)
3. fail (no leak to env)

This means projects can override the shared secret without touching it, and
shared secrets are the default for tools that aren't tied to a single project.

## Why `exec` instead of `get`?

`exec` runs the child command with the secret in its environment, then exits.
The secret is never:
- written to a file the child can `cat`
- visible in `ps` output (it's an env var)
- echoed by `set -x` (the literal value isn't on the command line)
- captured by shell history (no `$(cat ...)` substitution in the parent)

This makes `exec` safe to use under sandboxes that block `$(...)` heuristics.
`get` is provided for explicit script use but requires `--trusted` in TTY to
discourage accidental leaks via terminal scrollback / clipboard.

## Installation (manual, pre-launcher)

```bash
git clone https://github.com/hotak92/vibecoded-orchestrator
cd vibecoded-orchestrator
mkdir -p ~/.vct-secrets/{shared,projects}
chmod 700 ~/.vct-secrets
cp tools/vct-secrets/vct                 ~/.vct-secrets/vct
cp tools/vct-secrets/git-credential-vct  ~/.vct-secrets/git-credential-vct
chmod 755 ~/.vct-secrets/vct ~/.vct-secrets/git-credential-vct
```

Add to your shell rc:

```bash
export PATH="$HOME/.vct-secrets:$PATH"
```

The VCT Launcher will do all of the above plus migrate existing secrets and
register the git credential helper. Manual steps are for users who don't
want the launcher.

## Tests

```bash
bash tools/vct-secrets/tests/test_vct.sh
```

18 tests covering set/get/exec/revoke/copy/migrate-from-env/doctor and
permission enforcement.

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
in the orchestrator repo. Phases 2–4 (Rust binary, launcher GUI, daemon
auto-start) are described there.
