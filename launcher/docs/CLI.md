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
vct-cli project delete <id_or_slug>

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
