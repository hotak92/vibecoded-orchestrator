# vct-cli

Command-line interface for the VCT Launcher (`vct-cli --version` prints its version).
Mirrors most GUI capabilities so power users can script project lifecycle,
audit pulls, and license operations from a terminal or CI job.

> **Renamed in v0.2.97: `vco` → `vct-cli`.** Until v0.2.96 this binary was
> called `vco` — the same name as the orchestrator's Python CLI (`vco doctor`,
> `vco verify-pins`, `vco project move`, …), which `install.py` puts in the
> install's `.venv`. Both answered `vco project`, with different
> subcommands, so whichever came first on `PATH` hid the other. Every
> command kept its name and flags (`telemetry pending` is new). Re-running
> `install.sh` removes the old `~/.local/bin/vco` it installed (only when that
> file identifies itself as this CLI) and installs `vct-cli`. If you do not,
> `vco doctor` — run by every install and update — reports any old copy still
> on `PATH` (`former_launcher_cli_on_path`) with the command to remove it; it
> never deletes the file itself.

## Install

```bash
cd launcher/tools/vct-cli
./install.sh
# → copies ~/.local/bin/vct-cli
```

`install.sh` runs `cargo build --release` and copies the binary to
`~/.local/bin/`. Make sure `~/.local/bin` is on your `PATH`. Build
target output (uninstalled) is at `target/release/vct-cli`.

## How it works

`vct-cli` is a small Rust binary that talks to the launcher's local hub
server (default `http://127.0.0.1:7700`). The hub exposes a parallel
REST surface for every Tauri command the GUI uses; the CLI just calls
those routes.

The launcher GUI MUST be running for `vct-cli` to work. If it is not, the
CLI prints:

```
vct-cli: Cannot reach launcher hub: ... Is the launcher running?
```

## Hub port discovery

In order: `--port <N>` (CLI flag) > `VCT_HUB_PORT` env > `~/.vct/hub.port`
(file written by the launcher on startup) > 7700 (default).

## Hub authentication (0.2.0+)

Every `/api/v1/*` route (except `/api/v1/health`) requires
`Authorization: Bearer <token>`. The launcher writes a fresh 32-byte
CSPRNG token to `~/.vct/hub.token` (mode `0o600`) on every startup. The
`vct-cli` CLI reads it transparently — no extra flag needed. If the launcher
has not been started since boot, the file is missing and `vct-cli` exits with
"Cannot reach launcher hub". See the Hub section of
[docs/features/01-launcher.md](../../docs/features/01-launcher.md)
(port discovery, auth token, project-scoped tokens) for the threat model.

## Commands

```text
vct-cli project list
vct-cli project show <id_or_slug>
vct-cli project create --name <name> --path <dir> [--host base|mao]
vct-cli project rename <id_or_slug> <new_name>
vct-cli project delete <id_or_slug> [--keep-collections] [--leave-unremovable] [--yes]

vct-cli module list
vct-cli module installed <project_id_or_slug>

vct-cli audit list [--project <id|slug>] [--since <epoch_ms>] [--limit <N>]

vct-cli license status
vct-cli license activate <key>
vct-cli license deactivate

vct-cli hooks list <project_id_or_slug>
vct-cli hooks enable <hook_id> --project <id|slug>
vct-cli hooks disable <hook_id> --project <id|slug>

vct-cli telemetry status
vct-cli telemetry on
vct-cli telemetry off
vct-cli telemetry pending

vct-cli hub health
vct-cli hub url

vct-cli kg collections
vct-cli kg search <query> --project <id|slug> [--collections <c1,c2>] [--limit <N>]

vct-cli codegraph collections
vct-cli codegraph search <query> --project <id|slug> [--collections <c1,c2>] [--scope all|code|interaction] [--limit <N>]
```

Every command outputs JSON for machine consumption. Pipe through `jq`
for human-readable formatting.

`project delete` is **Remove from VCO**, the same removal as the launcher's
button — not a row delete. What VCO put in the folder goes (the hooks,
scripts, agents and skills it installed that you have not changed, its entries
in `.claude/settings.json`, `.vscode/settings.json` and `.git/info/exclude`,
the lines it wrote to your env files, its runtime folders), and **the
project's own Weaviate collections are dropped** — knowledge graph, docs,
diagrams and code graph; shared collections and collections another project
uses are never dropped. The folder, your source code, `.env`, secrets,
`knowledge/` and anything you changed stay.

- It **asks first**: in a terminal it prints what would be removed — the files
  and the collections, computed by the same rules the removal uses — and
  removes only when you answer `y`.
- `--yes` removes without asking. Without a terminal (a script, CI) and
  without `--yes` it refuses and changes nothing, so a script can never drop a
  project's collections by accident. The hub enforces this too: the DELETE
  route refuses a removal that drops collections unless the request carries
  `confirm=true`, which `vct-cli` sends only after your `y` or `--yes`
  (`--keep-collections` needs no confirmation — it drops nothing).
- `--keep-collections` keeps the collections (the files still go).
- `--leave-unremovable`: if the removal stopped because a value VCO wrote
  could not be removed, rerun with it to finish anyway (the dialog's
  "Unregister anyway"); the report lists each value to clean by hand.
- A removal can take several minutes on a large project; `vct-cli` waits up
  to 32 minutes for the hub's answer, and a second delete of the same project
  while one runs is refused (HTTP 409, `busy: true`).
- To re-register a project, add it again (`project create` or Add Project): a
  delete followed by a create starts with EMPTY collections unless the delete
  had `--keep-collections`.

The launcher binary offers the same command without the hub:
`vct-launcher --remove-project <id|slug|name> [--keep-collections]
[--leave-unremovable] [--yes] [--preview]` (`--preview` prints what would be
removed as JSON and changes nothing). Its exit codes: 0 removed, 1 usage or
lookup error, 2 stopped on a value it could not remove, 3 failed for another
reason (the message says which), 4 not confirmed. On Windows the launcher is
a GUI binary: started from a console it inherits it and asks there; started
with no console at all (a shortcut, `start /b`) and without `--yes` it opens
a console window to ask in, so the question is never invisible.

`telemetry pending` is the one command that does not talk to the hub: it
reads `~/.vibecoded/telemetry_pending.jsonl` (where opted-in telemetry waits
while no upload endpoint is deployed — see `docs/TELEMETRY.md`) and prints
`{path, exists, count, unparsed_lines, events}`, so it works with the
launcher closed.

## Examples

```bash
# Quick health check
vct-cli hub health

# List all projects (slug + module count visible)
vct-cli project list | jq '.projects[] | {name, slug, module_count}'

# Create a project + see its slug
vct-cli project create --name "Acme Corp" --path ~/code/acme | jq '.slug'

# Pull audit log for a tenant in a CI job
vct-cli audit list --project acme-corp --limit 500 > acme-audit.json

# Toggle telemetry for headless CI runs
vct-cli telemetry off

# Discover orchestrator-shaped KG collections on the local Weaviate
vct-cli kg collections | jq '.collections[] | {name, node_count}'

# Search the KG with auto-detected collection list
vct-cli kg search "rerank pipeline" --project acme-corp --limit 10

# Search the code graph (only functions/classes/modules)
vct-cli codegraph search "auth middleware" --project acme-corp --scope code

# Search interaction-only (APIs + cross-service calls)
vct-cli codegraph search "/users" --project acme-corp --scope interaction
```

## Limitations

- License activation persists the key to `~/.vct/license.key` and
  audits the action, but the actual remote validation against the
  Supabase tier service is performed by the launcher GUI on next
  refresh. CLI-only headless validation is not yet supported.
- Module install/uninstall is GUI-only for now (the install path
  spawns subprocesses tied to Tauri's app handle). Use the launcher
  GUI for installs; the CLI can list catalog and installed modules.
- KG search enforces per-collection ACLs from the launcher DB
  (`kg_collection_access`). If the project doesn't have a `read` or
  `write` grant on a collection, the hub returns 403. Configure grants
  via the launcher GUI's KG dashboard or set them programmatically
  with `Db::kg_set_access`.
- Auto-detection is strict: a Weaviate class only counts as an
  "orchestrator KG collection" if its schema has all four markers
  (`title` text, `node_type` text, `tags` text[], `typed_links`
  object[]). Use `--collections c1,c2,...` to override.

## Subcommand reference

Run `vct-cli <subcommand> --help` (or `vct-cli help`) for full flag-level help
generated by clap. The on-disk help is the source of truth — the list
above may lag minor additions.
