# Migration: VCO Launcher 0.1.7 — secrets architecture cleanup

**TL;DR.** 0.1.7 removes the "Fix #3 auto-bridge" introduced earlier in
the 0.1.7 cycle (PR #171). That bridge mirrored a hard-coded allowlist
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

No file-side mirror anywhere in `secrets::set` / `secrets::delete`.

## Who is affected?

You are affected if **all** of the following are true:

- You upgraded from a pre-0.1.7 launcher to a 0.1.7 build that
  shipped the Fix #3 bridge (any pre-release of 0.1.7 from
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

| Component | Pre-0.1.7 (Fix #3) | 0.1.7 |
| --- | --- | --- |
| `launcher::secrets::set` / `::delete` | Wrote to keychain AND to `~/.vct-secrets/<key>` for an allowlist | Keychain only |
| `BRIDGE_SHARED_KEYS` allowlist | 7 keys hard-coded in Rust | Removed |
| `VCT_SECRETS_DIR` env var | Honoured by `secrets::set/delete` | Still honoured by the user-facing `vct` CLI under `tools/vct-secrets/` (out of scope for this change) |
| Hub `GET /projects/{id}/env` | Returned secrets (no filter) | Returns secrets, supports `?key=NAME` filter, structured error envelope |
| New: `GET /projects/by-path?path=...` | Did not exist | Resolves a folder → registered project_id |
| New: `templates/scripts/vct_secrets_resolve.sh|.ps1` | Did not exist | Shared helper for bundled wrappers |

The hub's response contract is documented in
`launcher/src-tauri/src/hub/modules_api.rs::project_env`.

## Migration steps

### Step 1 — restart the launcher

The launcher must be running to resolve secrets. The 0.1.7 hub speaks
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

If you set a secret in the GUI between when Fix #3 landed and 0.1.7
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

These were updated as part of 0.1.7 and need no manual action; they're
listed here so you can audit your own clone if you've forked or pinned
the source:

- `claude_mcp_servers/search_mcp/wrapper.sh` — now resolves
  `github_pat` via `vct_secrets_resolve.sh`. Has a one-release-cycle
  legacy file fallback gated behind `VCT_LEGACY_FILE_FALLBACK=1`,
  documented inline. Will be removed in 0.1.8.

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

#### `~/.vct-secrets/git-credential-vct`

This is a **git credential-helper protocol** consumer. Git invokes it
once per `git fetch/push` against `https://github.com/...`, expecting a
`username=...\npassword=...\n` reply on stdout. The protocol is
synchronous, single-shot, and oblivious to the VCT project model: git
has no notion of "current VCT project", so the helper can't ask the
hub for a per-project secret.

**Migration path: NONE for now.** The right long-term fix is to switch
to a keychain-native git credential helper:

- Linux: `git-credential-libsecret` (ships with most distros)
- macOS: `git-credential-osxkeychain` (built into Apple Git)
- Windows: `git-credential-manager` (built into Git for Windows)

Each of those reads the OS keychain directly without going through
`~/.vct-secrets/` — the same store the launcher writes to. Until you
switch helpers, leave `~/.vct-secrets/git-credential-vct` and
`~/.vct-secrets/shared/github_pat` in place; they keep working as
file-based reads.

This is documented as an **intentional exception** to the keychain-only
contract: not all consumers fit the per-project active-flag model, and
breaking git's credential flow during the 0.1.7 release would block
every clone/push for affected users.

### Step 5 — custom scripts

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

- **OnboardingWizard's `register_github_pat`** still writes
  `~/.vct-secrets/shared/github_pat` directly (see
  `commands/installer.rs::register_github_pat`). It's an
  out-of-keychain primary store, not a keychain mirror. Migrating
  the OnboardingWizard to the keychain is tracked as Open Question #1
  in the GUI audit and will land in 0.1.8.
- **`tools/vct-secrets/vct` CLI** is the user-facing `vct set/get`
  tool. It writes to `~/.vct-secrets/projects/<project>/<key>`. It
  is not a runtime consumer — it's the primitive humans use to manage
  arbitrary file-based secrets outside the launcher. It stays
  unchanged. (We may add a `vct sync` subcommand later that ingests
  flat-file secrets into the launcher keychain.)

---

If you hit a regression not covered above, file an issue with the
prefix `[secrets-0.1.7]` and include:

- the launcher version you upgraded from / to
- the wrapper or hook that broke
- the exit code from `vct_secrets_resolve.sh <project> <key>`
- the contents of `~/.vct/hub.port` (the port the launcher claims it's
  listening on)
