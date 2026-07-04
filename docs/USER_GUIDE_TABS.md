# VCT Launcher — User Guide

This guide documents the per-project tabs in the VCT Launcher window. Each project has its own state stored in the launcher's SQLite DB (`projects.db`), and most tabs render rows from one of the `project_*` tables.

---

## Secrets, Permissions, and Rules tabs

> **Heads up on "Rules"**: there is no tab literally labeled `Rules` in the launcher today. What people usually mean by "rules" is split across two tabs:
> - **Permissions** — agent-scoped allow/deny rules (which tools an agent may call, which MCP servers it sees, where it may write).
> - **Hooks** — event-driven shell commands (the closest thing to "automation rules"; documented in the Hooks tab section, not here).
>
> The KG / code-graph access bindings are in the **KgCodegraph** tab (also out of scope here).
>
> If you came here looking for "Rules", read the **Permissions** section below.

---

### Secrets tab

#### What this tab is for

The Secrets tab shows **secret references** — _pointers_ telling the launcher where to look for each secret a project needs. The reference rows live in the `project_secret_refs` SQLite table. The actual secret values **never** live in this DB.

Where the values live depends on the `resolution` field on each row:

| `resolution` value         | Where the value is stored                                                                  |
|----------------------------|---------------------------------------------------------------------------------------------|
| `keychain-per-project`     | OS keychain, namespace `vct.{project_id}.{module_id}`                                       |
| `keychain-shared`          | OS keychain, namespace `vct.{project_id}.shared.{module_id}` (shared across modules in one project) |
| `keychain-global`          | OS keychain, namespace `vct.global.{module_id}` (machine-wide, all projects)                |
| `file`                     | A file on disk (e.g. `~/.vct-secrets/github_pat`, `chmod 600`)                              |
| `env`                      | A process env var read at launch (e.g. `$GITHUB_TOKEN`)                                     |

The OS keychain backends:
- **Linux**: libsecret (GNOME Keyring / KWallet)
- **macOS**: Keychain
- **Windows**: Credential Manager

This is implemented in `launcher/src-tauri/src/secrets.rs`. Every keychain entry uses `service_name = vct.{scope}.{module_id}` and `username = {key}`, which makes secrets discoverable in the OS credential manager UI (e.g. `seahorse` on GNOME).

#### Why secrets are referenced by `secret_ref`, not value

Three reasons, all enforced in code:

1. **The DB has no value column.** The `project_secret_refs` table schema deliberately omits any `value` / `secret_value` column — see the assertion in `db/project_state.rs::secret_ref_never_stores_value`. Project DB backups / `.sqlite` exports therefore can never leak credentials.
2. **The value lives in the OS-managed credential store**, which is encrypted at rest by the OS (libsecret unlocks with the user's session, macOS Keychain with the login keychain password, etc.).
3. **Values never leave the Rust process.** The Tauri commands return only `is_set: bool` and (for non-sensitive entries) a masked preview like `ghp_•••abc`. The frontend cannot read raw secret values via `invoke`. See `commands/secrets_cmd.rs::get_secret_preview` — it errors out with `"cannot preview sensitive secret"` if the manifest marks the entry sensitive.

The audit log (`db.audit("secret_set", …)`) records `{key, scope, sensitive}` but **explicitly never logs the value, not even truncated**.

#### Common operations

**See what secrets a project needs:**
- Open the project, go to the **Secrets** tab. The table lists `KEY`, `Resolution`, `Required for` (which agents/modules need it), and a `set` / `missing` status badge.

**Add or set a secret value:**
- The Secrets tab itself does **not** have an inline editor for values. Click the green **"Open secrets panel"** button at the top right, or click **"Set value"** on any row. Both dispatch a `vct-open-secrets` event that opens the dedicated SecretsPanel modal.
- In the panel: pick the scope (`per-project` / `shared` / `global`), pick the module, type the key (e.g. `GITHUB_TOKEN`), paste the value, click Save.
- Under the hood that calls `set_secret_v2(project_id, module_id, scope, key, value, validation_regex?, sensitive)`. If a `validation_regex` is set in the module manifest, the value is checked against it before being written to the keychain.
- The value lands in the OS keychain at `vct.{scope}.{module_id}` (username = the key). The Secrets tab then re-renders showing `set`.

**Delete a secret reference (without deleting the value):**
- Click **"Delete ref"** on a row. The confirmation box explicitly says: _"Delete the secret REFERENCE for X? The secret VALUE in keychain is untouched."_ This is intentional — clearing the ref removes the launcher's awareness that the project needs this secret, but does not touch the keychain entry. The next module that registers the same key will pick it up again.

**Delete the actual value from the keychain:**
- Use the secrets panel's "Clear" action (calls `clear_secret_v2`), or remove it from the OS credential manager (`seahorse` / Keychain Access / Credential Manager). The DB ref will then flip to `missing` on next reload.

#### Field reference (SecretsTab columns)

| Column        | Source                              | Meaning                                                                 |
|---------------|-------------------------------------|-------------------------------------------------------------------------|
| `KEY`         | `secret_key`                        | The env-var-style key the consuming module will look up (e.g. `GITHUB_TOKEN`). |
| `Resolution`  | `resolution`                        | Where the launcher looks for the value (see resolution table above).    |
| (sub-tag)     | `file_path` / `env_name`            | Concrete file path or env var name when `resolution` is `file` / `env`. |
| `Required for`| `required_for: Vec<String>`         | Agent/module names that need this secret to function.                   |
| `Set?`        | `is_set: bool`                      | Whether the value resolves successfully right now.                      |

#### Gotchas / failure modes

- **`is_set: missing` after you just set it.** The flag is recomputed lazily when the launcher rescans. Click another tab and back, or restart the launcher.
- **"keyring entry for vct.… : platform error"** on Linux usually means there is no running Secret Service. Ensure `gnome-keyring-daemon` (or KWallet) is running and the login keyring is unlocked.
- **Headless / SSH sessions on Linux**: libsecret requires a D-Bus session and an unlocked keyring. Either run the launcher inside a desktop session, or use `resolution: file` with `~/.vct-secrets/<name>` (chmod 600) instead.
- **"value does not match validation pattern"** comes from `set_secret_v2` validating against the module manifest's `validation_regex`. Check the module's secrets manifest (e.g. for `GITHUB_TOKEN` the regex usually requires `ghp_…` / `github_pat_…`).
- **Sensitive vs non-sensitive**: only non-sensitive secrets get a masked preview in the UI. For sensitive ones you'll only ever see `set` / `missing` and a `••••••••` placeholder — by design. Don't try to "fix" this; `get_secret_preview` will hard-error if you ask for a sensitive value.
- **Secret refs do not auto-populate**. They are written by the modules during install (via `set_project_secret_ref`). If you uninstall a module, its refs are deleted by the install logic, not by anything in this tab.
- **Deleting a project deletes all its refs but NOT the keychain entries.** The DB cascades on `projects.id`; the OS keychain does not. Clear stale entries manually if you care about that.

---

### Permissions tab

#### What this tab is for

The Permissions tab manages rows in `project_permissions` — these are the launcher's allow/deny lists telling Claude Code (and agents spawned from it) what they can and can't do **inside this specific project**. Each row is keyed on `(project_id, subject, kind, value)` and has a small JSON `config` blob for kind-specific extras.

When the launcher writes the project's `.claude/settings.json` (or the per-agent frontmatter), it folds the rows from this table into the right shape: `permissions.allow`, `permissions.deny`, `mcpServers` allowlist, and per-agent `tools` / `disallowedTools` / write-scope hooks.

#### How the launcher decides whether an agent can use a tool / MCP server

For each `(agent, tool)` or `(agent, mcp_server)` pair the launcher follows this resolution order:

1. **Deny wins**: if any row matches with `kind=denied_tool` for either `subject=<agent_name>` or `subject=project`, the tool is blocked. `denied_tool` always overrides `allowed_tool`.
2. **Subject specificity**: rows with `subject=<agent_name>` take precedence over `subject=project` for the same kind+value.
3. **Allowlist gating** (`allowed_tool`, `mcp_server`): if any allow row exists for the subject and that kind, the agent is restricted to those values. If no allow rows exist, the platform default applies.
4. **`permission_mode`** sets the global behavior (`default` | `acceptEdits` | `dontAsk` | `bypassPermissions` | `plan`) — this is what controls "ask each time vs auto-accept".
5. **`write_scope`** gets translated into an `Edit`/`Write` PreToolUse hook that rejects writes outside the listed globs. So a `write_scope=src/**` row on `subject=coder` means the `coder` agent's writes are limited to `src/**` regardless of any `Edit` allowlist.

The `subject` field is free-form text. Two conventions:
- **`project`** — applies to the whole project (every agent run inside it).
- **`<agent_name>`** — applies to one agent specifically (e.g. `coder`, `planner`, `tester`). The form's placeholder `agent:planner or @global` is suggestive — the literal stored value is whatever you type.

#### Common operations

**See all permissions for the project:**
- Open the **Permissions** tab. Rows are grouped by subject (sorted alphabetically) so you can see at a glance "what can `coder` do, what can `tester` do, what's project-wide".

**Add a permission:**
- Click **+ Add**.
- Fill `Subject` (e.g. `coder` or `project`), pick `Kind` from the dropdown, fill `Value`, click **Add permission**.
- The form calls `add_project_permission(projectId, {subject, kind, value, config: {}})`. Duplicate `(subject, kind, value)` upserts (replaces config), it does not create a second row.

**Delete a permission:**
- Click **Delete** on the row. Confirms, then calls `delete_project_permission(perm_id)`. The row is removed; on next agent run the launcher regenerates settings without it.

**Restrict an agent to read-only:**
- Add `subject=<agent>, kind=allowed_tool, value=Read` and `value=Grep`. Then add `subject=<agent>, kind=denied_tool, value=Edit`, `value=Write`, `value=Bash`. The deny rules are belt-and-braces in case the allow list is interpreted permissively somewhere upstream.

**Give an agent access to a single MCP server:**
- Add `subject=<agent>, kind=mcp_server, value=weaviate-kg`. The agent's spawned `claude` subprocess will only see that server in its MCP config.

**Make a project auto-accept edits without prompting:**
- Add `subject=project, kind=permission_mode, value=acceptEdits`.

#### Field reference (PermissionsTab columns)

| Column / field | DB column     | Allowed values / format                                                                                       |
|----------------|---------------|---------------------------------------------------------------------------------------------------------------|
| Subject        | `subject`     | Free-form. Use `project` for project-wide, or an agent name (e.g. `coder`, `planner`).                         |
| Kind           | `kind`        | One of: `write_scope`, `allowed_tool`, `denied_tool`, `mcp_server`, `permission_mode`. Validated server-side. |
| Value          | `value`       | Depends on kind — see the table below.                                                                         |
| (hidden) config| `config_json` | Reserved for kind-specific extras (e.g. timeout, scope hints). Today the UI sends `{}`.                       |
| (hidden) id    | `id`          | Auto-increment row id, used for delete.                                                                        |
| (hidden) granted_at | `granted_at` | ms-epoch when added/last upserted.                                                                       |

**`Value` semantics by kind** (also surfaced as inline help in the form):

| Kind              | Value example          | Meaning                                                                            |
|-------------------|------------------------|------------------------------------------------------------------------------------|
| `write_scope`     | `src/**`               | Glob the agent is allowed to write to. Multiple rows = union.                      |
| `allowed_tool`    | `Read`, `Edit`, `Bash` | Tool name on the allow list.                                                       |
| `denied_tool`     | `Bash`                 | Tool on the deny list. **Overrides `allowed_tool`.**                               |
| `mcp_server`      | `weaviate-kg`          | MCP server scoped to this subject.                                                  |
| `permission_mode` | `acceptEdits`          | One of `default` \| `acceptEdits` \| `dontAsk` \| `bypassPermissions` \| `plan`.   |

#### Gotchas / failure modes

- **"invalid permission.kind"** from the backend means you typed a kind not in the validated set. The dropdown only offers valid ones, so this only fires if you call the Tauri command directly.
- **Adding `allowed_tool=Edit` doesn't grant write access**: writes are also gated by `write_scope`. Add a `write_scope` row covering the paths the agent should touch, otherwise PreToolUse will block.
- **`denied_tool` always wins.** If you're surprised an agent can't use `Bash`, check both its own subject _and_ `subject=project` — a project-wide deny silently overrides any agent-level allow.
- **Duplicate detection is on `(project_id, subject, kind, value)` exact match.** `Read` and `read` are NOT the same; tool names are case-sensitive and must match Claude Code's tool registry.
- **MCP server allowlist is exclusive**: as soon as you add even one `mcp_server` row for a subject, all other servers are hidden from that subject. Don't add one `mcp_server` rule expecting it to mean "also allow this on top of the default".
- **`subject=project` is not the same as no rows.** No rows = platform defaults. `subject=project, kind=allowed_tool, value=Read` = ONLY Read is allowed for everything in this project.
- **Renaming an agent breaks its rules.** The subject is plain text — there's no FK to an agent record. If you rename `coder` → `developer` in AgentsTab, the old `subject=coder` rows still exist and apply to nothing. Delete them by hand.
- **No history**: deleting a permission does not soft-delete or audit-log under a dedicated label. The change-log captures it as a generic DB write. Don't expect a "who removed what" trail in this tab.

---

### "Rules" — what's actually here

To restate the heads-up at the top, in concrete terms:

| You probably want…                                                | Use this tab          |
|-------------------------------------------------------------------|------------------------|
| "Block this agent from running shell commands"                    | Permissions (`denied_tool=Bash`) |
| "This agent can only edit files under `src/`"                     | Permissions (`write_scope=src/**`) |
| "Run `ruff check` after every Python edit"                        | Hooks (`PostToolUse`, matcher `Edit(*.py)`) |
| "Auto-sync KG on edits to `knowledge/**`"                         | Hooks (`PostToolUse`, matcher `Edit(knowledge/**/*.md)`) |
| "Limit which Weaviate collections this project sees"              | KgCodegraph tab        |
| "Show this agent only `weaviate-kg`, hide the others"             | Permissions (`mcp_server=weaviate-kg`) |
| "Set `acceptEdits` so the user doesn't get prompted"              | Permissions (`permission_mode=acceptEdits`) |

If a future version adds a dedicated `Rules` tab (e.g. for higher-level policies that compile down to multiple hook+permission rows), this section will need updating. Until then, treat **Permissions + Hooks together** as the rules surface.

---

_End of Secrets / Permissions / Rules section. Other tabs (Agents, Skills, Hooks, KgCodegraph) are documented in their own sections of this file._

---

## Agents, Skills, Hooks, KG, and Codegraph tabs

These five tabs share a common contract:

- Rows are **auto-populated** at project onboarding by scanning
  `<project>/.claude/`. See `populate_project_state_from_filesystem`
  in `launcher/src-tauri/src/commands/project_state_populate.rs`.
  Subsequent edits to filesystem files do not retroactively appear;
  populate must be re-triggered.
- The `enabled` column is **owned by the user via the GUI**. Re-running
  populate (or re-onboarding the project) preserves user toggles —
  the `register_*` upsert helpers in `db/project_state.rs` intentionally
  omit the `enabled` column from `ON CONFLICT … DO UPDATE SET …`.
- Deleting a row from a tab removes the **registry row only**. The
  source file on disk is not touched. To fully remove an agent/skill,
  delete the file and unregister the row.
- All mutations are written to the audit log (`db.audit(...)`) without
  recording values.

Schema source of truth: `launcher/src-tauri/src/db/project_state.rs`
(tables `project_agents`, `project_skills`, `project_hooks`,
`project_kg_bindings`, `project_codegraph_bindings`).

Frontend invocations are defined in
`launcher/src-tauri/src/commands/project_state_cmd.rs`. Each tab calls
`list_*` on mount and `set_*_enabled` / `register_*` / `unregister_*`
on user actions.

---

### Agents tab

#### What this tab is for

Lists every Claude Code subagent the launcher knows about for this
project. The harness loads agents from `.claude/agents/*.md`
(project-scope) and `~/.claude/agents/*.md` (user-scope) at session
start; this tab is the launcher's **index** of those files plus a
per-project `enabled` flag and a `source` label.

#### How rows get there

- Auto-populated by `populate_agents()`: scans
  `<project>/.claude/agents/`, reads each `*.md`, parses YAML
  frontmatter for `name`, `description`, `model`, inserts one row per
  file via `Db::register_project_agent(...)` with `source = "bundled"`.
- The `agent_name` is taken from frontmatter `name:` if present,
  otherwise from the file stem (e.g. `coder.md` → `coder`).
- Manual: the **+ Register** button (top-right of the tab) calls the
  `register_project_agent` Tauri command. This adds a registry row
  *without* creating a `.md` file — useful for tracking an agent
  defined elsewhere (user-scope `~/.claude/agents/`, or a paid module).
- Adding a new file under `.claude/agents/` after onboarding does
  **not** make it appear automatically. You must re-run populate
  (re-onboard the project) or manually register it.

#### Field reference

| Column / field | Meaning |
|---|---|
| `agent_name` | Logical name. Unique per `(project_id, agent_name)`. From frontmatter `name:` or file stem. |
| `source` | One of `bundled`, `user`, `paid-module`, `project`. The validator (`VALID_SOURCE`) rejects anything else. `bundled` = shipped with the orchestrator; `user` = user-scope `~/.claude/agents/`; `paid-module` = installed by a paid module; `project` = manually added in this project only. |
| `source_module` | Optional module slug if `source = paid-module` (e.g. `vct-coordination`). |
| `model` | Frontmatter `model:` value: `sonnet`, `opus`, `haiku`, `inherit`, or full model id. Display-only — the harness reads this from the `.md` itself. |
| `enabled` | User-owned toggle. The harness still loads the `.md` regardless; `enabled=false` is a launcher-managed signal that downstream code can consult. **It does not delete the file or block the harness directly.** |
| `file_path` | Absolute path to the `.md`. `null` for rows manually registered without a file. |
| `config` (JSON) | Currently stores `{"description": "..."}` from frontmatter. Reserved for future per-row overrides (`tools`, `effort`, `mcpServers`). |
| `installed_at` / `updated_at` | Unix-millis timestamps. |

#### Frontmatter fields the launcher tracks

The populate scanner only reads `name`, `description`, `model` (see
`parse_frontmatter()` — it deliberately filters everything else to
keep the map small). Other Claude Code agent frontmatter
(`tools`, `disallowedTools`, `permissionMode`, `maxTurns`, `effort`,
`isolation`, `background`, `memory`, `skills`, `mcpServers`, `hooks`)
**is honored by the harness when it loads the `.md`**, but is not
mirrored in the launcher DB. To change those, edit the `.md` directly.

#### Common operations

- Disable a noisy/expensive agent for one project without removing
  the file: toggle `enabled = false`. The flag survives re-onboarding.
- Track a user-scope agent so it shows up in the per-project view:
  use **+ Register** with `source = user`, `name = <agent>`,
  `file_path = null`.
- Remove a row whose `.md` was deleted: click **Unregister**. Re-runs
  of populate will not re-add it.

#### Gotchas

- The toggle is **advisory** unless something downstream of the
  launcher reads the `project_agents.enabled` column. As of v1.0 the
  Claude Code harness itself does not read this DB; it loads `.md`
  files directly. Treat `enabled` as a launcher-internal preference.
- Two agent files with the same `name:` in frontmatter collide on the
  `(project_id, agent_name)` unique key — the second insert upserts
  over the first.
- The `enabled` flag is preserved across re-populates. Other fields
  (`model`, `file_path`, `config`, `source`) are **overwritten** on
  each re-populate from the `.md`.
- The launcher's populate scanner does not read `~/.claude/agents/`.
  User-scope agents have to be registered manually if you want them
  in this view.

---

### Skills tab

#### What this tab is for

Lists Claude Code skills (slash-invoked helpers). Same registry
pattern as Agents but skills live in **directories**
(`<dir>/SKILL.md`) rather than single files, and they have two
natural scopes: user-wide (`~/.claude/skills/`) and project
(`.claude/skills/`).

#### How rows get there

- Auto-populated by `populate_skills()`: scans
  `<project>/.claude/skills/`, looks for subdirectories containing
  a `SKILL.md`, parses its frontmatter, inserts one row per skill via
  `Db::register_project_skill(...)` with `source = "bundled"`.
- A skill directory without `SKILL.md` is silently skipped.
- Manual: the **+ Register** button registers a row without requiring
  a directory.

#### Distinguishing scopes

The launcher's populate scanner reads only project-scope
(`<project>/.claude/skills/`). User-scope skills under
`~/.claude/skills/` are loaded by the harness at runtime but are
**not** mirrored into the project's registry automatically. If you
want user-scope skills in this view, register them manually with
`source = user`.

#### Field reference

| Column | Meaning |
|---|---|
| `skill_name` | From `name:` in `SKILL.md` frontmatter, or directory name as fallback. |
| `source` | Same vocabulary as agents: `bundled` / `user` / `paid-module` / `project`. |
| `model` | Frontmatter `model:`. Display-only (harness reads it from the file). |
| `enabled` | User-owned toggle. Same caveat as agents: advisory unless downstream consumers read it. |
| `file_path` | Absolute path to `SKILL.md`. |
| `config.description` | From frontmatter `description:`. |

#### Common operations

- Hide a noisy skill from a project's slash menu (when downstream
  honors it): toggle `enabled = false`.
- Surface a user-scope skill in this view: **+ Register** with
  `source = user`.

#### Gotchas

- Frontmatter fields the launcher does **not** track but the harness
  may honor: `argument-hint`, `disable-model-invocation`,
  `user-invocable`, `allowed-tools`, `context`, `agent`, `hooks`.
  Edit `SKILL.md` directly to change these.
- VS Code's skill schema warns on `model`, `effort`, `allowed-tools`,
  `context`, `agent`, `hooks` — they still work at runtime in the
  CLI. Don't strip them from `SKILL.md` based on the warning alone.

---

### Hooks tab

#### What this tab is for

Mirrors `<project>/.claude/settings.json`'s `hooks` block as
queryable rows, with a per-row `enabled` toggle. Hooks are lifecycle
callbacks the harness runs on events (file edits, session start,
compaction, etc.) and are the **only** mechanism for *automated*
harness behavior — memory and instructions cannot fulfill "each time
X" requirements.

#### How rows get there

- Auto-populated by `populate_hooks()`: parses
  `<project>/.claude/settings.json`, walks the
  `hooks: { Event: [ { matcher?, hooks: [ { command, type,
  timeout?, background?, ... } ] } ] }` schema, inserts one row per
  *innermost* command via `Db::register_project_hook(...)` with
  `source = "project"`.
- The `timeout` field in `settings.json` is in **seconds**; the DB
  column `timeout_ms` is in **milliseconds** — populate multiplies
  by 1000.
- Manual: the **+ Register** button calls `register_project_hook`.
  It does **not** edit `settings.json` — the row exists in the DB
  only unless something else writes it back.
- A missing `settings.json` is fine (no warning). A malformed one
  emits a warning to the populate report and skips hook population
  (KG/codegraph bindings still write).

#### Schema (`settings.json` side)

```jsonc
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit(*.py)",
        "hooks": [
          {"type": "command", "command": "ruff check --fix", "timeout": 5},
          {"type": "command", "command": "pyright", "background": true}
        ]
      }
    ]
  }
}
```
Each innermost `{command, type, timeout?, background?}` becomes one
row.

#### Field reference

| Column | Meaning |
|---|---|
| `id` | Auto-increment primary key (hooks have no natural unique name). |
| `event` | Event name. The launcher's "common events" dropdown lists: `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `StopFailure`, `PreCompact`, `PostCompact`, `TeammateIdle`, `TaskCompleted`. The harness supports more (see CLAUDE.md hook table); the dropdown is convenience-only — any string is accepted. |
| `matcher` | Tool-name pattern. Empty = match all. Examples: `Edit(*.py)`, `Edit(knowledge/**/*.md)`, `Edit(*)\|Write(*)`, `*`. Matcher syntax is harness-defined. |
| `command` | Shell command run on the event. The harness invokes `bash -c <command>` with env scrubbed for secrets. |
| `source` | `bundled` / `user` / `paid-module` / `project`. Auto-populated rows are `project`. |
| `timeout_ms` | Per-hook timeout in milliseconds. `null` = harness default. |
| `enabled` | Advisory toggle. **Critical caveat: turning a row off here does NOT remove the hook from `settings.json`.** The harness reads `settings.json`, not this table. To actually disable a hook, edit `settings.json`. |
| `config` (JSON) | The full hook entry from `settings.json` — preserves `type`, `background`, and any other keys for the GUI to render. |

#### Blocking behavior

This is harness-side, not launcher-side, but critical to understand:
only some events can block. From CLAUDE.md hook table —
`PreToolUse`, `UserPromptSubmit`, `PermissionRequest`,
`SubagentStop`, `Stop`, `ConfigChange`, `TeammateIdle`,
`TaskCompleted`, `WorktreeCreate`, `Elicitation`,
`ElicitationResult` can block. Returning exit code 2 from these
blocks the action. Other events (`PostToolUse`, `Notification`,
`PostCompact`, `SessionEnd`, etc.) cannot block.

#### Common operations

- Inspect what hooks are firing for a project: just look at this tab.
- Identify a slow hook: check `timeout_ms`. Then investigate the
  script under `.claude/hooks/<name>.sh` (or wherever the command
  points).

#### Gotchas

- `enabled = false` does NOT actually disable the hook unless you
  also edit `settings.json`. Treat the toggle as a launcher-internal
  flag.
- Re-running populate **does not delete** rows that are no longer in
  `settings.json`. Stale rows from removed hooks must be cleaned via
  the **Delete** button.
- Hook commands run with secret env vars scrubbed (see CLAUDE.md
  "Env Scrubbing"). Do not assume `GITHUB_TOKEN`, `OPENAI_API_KEY`,
  etc. are visible in hook scripts — read from `~/.vct-secrets/<name>`
  instead.
- The unique key for hooks is `(project_id, event, matcher, command)`.
  Two `PostToolUse / Edit(*.py) / ruff check` rows cannot coexist;
  the second will upsert over the first.

---

### KG bindings tab (top half of "KG / Codegraph bindings")

#### What this tab is for

Records which Weaviate collection(s) this project's knowledge graph
reads/writes. A project has at most one binding per `role` —
currently `primary`, `shared`, or `archive`. Other code (the
`weaviate-kg` MCP server, the orchestrator's `hybrid_search`)
consults `KG_COLLECTION` env vars set per-workspace, **not** this
DB; the binding row is the launcher's record of what those should be.

#### How rows get there

- Auto-populated by `populate_kg_bindings()`:
  - `primary` row → `collection_name = sanitize_kg_collection(project_name) + "_KnowledgeGraph"`. For a project named `Acme` that's `Acme_KnowledgeGraph`. The sanitizer strips non-alphanumerics and TitleCases (`my project name` → `MyProjectName`).
  - `shared` row → `collection_name = "VibeCodedOrchestrator_KnowledgeGraph"` (cross-project shared KG used by all projects; renamed from `VibeCodedTools_KnowledgeGraph` in v0.2.12 — see the Identity tab "Manage shared KG collection" picker for migration).
  - Both default to `weaviate_url = http://localhost:8081`,
    `embedding_model = qwen3-embedding:0.6b`, `embedding_dim = 1024`.
- Idempotence: populate **only inserts a binding if no row exists for that role**. User edits to the row survive re-onboarding (in contrast to agents/skills/hooks where `model/file_path/source` get overwritten).

#### Primary vs shared

- **`primary`** = the project's own KG. Notes you write while working
  on this project go here.
- **`shared`** = `VibeCodedOrchestrator_KnowledgeGraph` (renamed from
  `VibeCodedTools_KnowledgeGraph` in v0.2.12). Cross-project
  patterns, team-wide concepts, anything you want every project's
  `hybrid_search` to find.
- **`archive`** = reserved (no auto-population). Use it manually if
  you rotate a KG collection out of active use but want to keep it
  queryable.

#### `KG_COLLECTION` env interaction

The harness's `weaviate-kg` MCP server reads `KG_COLLECTION` and
`SHARED_KG_COLLECTION` from its env (set in
`<project>/.claude/settings.json` under `env` — the canonical
channel since v0.2.12 / PR-27, 2026-05-16). The launcher writes
the binding row AND the per-project env via `write_project_env_files`,
so editing a binding row is normally accompanied by a launcher-
driven env refresh. The historical `.vscode/settings.json`
`claude-code.env` surface was removed because that block didn't
propagate to MCP subprocesses on Linux Claude Code 2.1.143 —
see `docs/CLAUDE_CODE_COMPATIBILITY.md` for the empirical-trace
reference. If you edit a binding by hand outside the launcher,
also update `.claude/settings.json` `env` for the change to take
effect in MCP subprocesses.

#### Field reference

| Column | Meaning |
|---|---|
| `role` | `primary` / `shared` / `archive`. Validated against `VALID_KG_ROLE`. |
| `collection_name` | Weaviate collection. Must satisfy Weaviate's "starts with [A-Z], alphanumeric only" rule — `sanitize_kg_collection` enforces this. |
| `embedding_model` | Default `qwen3-embedding:0.6b`. Display/reference; the actual embedding model is determined at runtime by `ACTIVE_EMBEDDING` env. |
| `embedding_dim` | Default 1024 (qwen3, also matches snowflake-arctic-embed2). |
| `kg_dir_path` | Optional override of the `knowledge/` directory. Usually `null` (defaults to `<project>/knowledge/`). |
| `weaviate_url` | Default `http://localhost:8081`. |
| `config` | Reserved JSON blob. |

#### Common operations

- Point a project at a different Weaviate instance: edit
  `weaviate_url`. Remember to also update `.claude/settings.json`'s
  `env.WEAVIATE_URL` (the canonical MCP-env channel since
  v0.2.12 / PR-27).
- Switch from per-project KG to shared-only: delete the `primary`
  binding (the harness will fall back to whatever `KG_COLLECTION`
  is set in the env).

#### Gotchas

- The collection must already exist in Weaviate (or be auto-created
  by the orchestrator install step). The launcher does not create
  collections.
- Sanitization is one-way: renaming a project does not rename its
  collection. To rename, manually create the new collection, copy
  data, and update this binding.

---

### Codegraph bindings tab (bottom half of "KG / Codegraph bindings")

#### What this tab is for

Code graph collections (`CodeFunction`, `CodeClass`, `CodeModule`,
`CodeAPI`, `CodeInteraction`) are **namespaced per project** by a
prefix. This row records the prefix and the embedding model used.
Without a binding, `search_code_graph` cannot find the project's
code entities.

#### Namespaced classes

A project with prefix `Acme` ends up with `Acme_CodeFunction`,
`Acme_CodeClass`, `Acme_CodeModule`, `Acme_CodeAPI`,
`Acme_CodeInteraction` in Weaviate.

| Project name | `collection_prefix` | Resulting classes |
|---|---|---|
| `Acme` | `Acme` | `Acme_CodeFunction`, … |
| `my project name` | `MyProjectName` | `MyProjectName_CodeFunction`, … |
| `ImageDataset` | `ImageDataset` | `ImageDataset_CodeFunction`, … |

The prefix is derived by `sanitize_kg_collection(project_name)` —
same function as KG bindings.

#### How the row gets there

- Auto-populated by `populate_codegraph_binding()`:
  - `collection_prefix = sanitize_kg_collection(project_name)`
  - `embedding_model = codesage-large-v2`
  - `embedding_dim = 2048`
  - `enabled = true`
- Idempotence: populate **only inserts if no codegraph binding
  exists for the project**. The `enabled` flag and any user edits
  survive re-onboarding. (The DB-level upsert *would* clobber
  `enabled`, hence the pre-check in populate.)

#### Field reference

| Column | Meaning |
|---|---|
| `collection_prefix` | Prefix prepended to `_CodeFunction` etc. Must be alphanumeric, starting with a letter. |
| `embedding_model` | `codesage-large-v2` default (2048-dim, served by the code embedding service on port 11440). Legacy alternative: `unclemusclez/jina-embeddings-v2-base-code` (768-dim, CPU-only Ollama fallback). |
| `embedding_dim` | 2048 (CodeSage) or 768 (Jina). Must match the model. |
| `last_analyzed_commit` | Git SHA of the last `code-graph-analyze` run. Used to decide whether re-analysis is needed. |
| `last_analyzed_at` | Unix-millis of the last analysis. |
| `enabled` | If `false`, the launcher should not auto-trigger code graph re-analysis. (Manual `code-graph-analyze` runs still work.) |
| `config` | Reserved. |

#### When to rebuild

- After a significant refactor (new modules, removed files, renamed
  classes). The analyzer is incremental but only catches changes
  per file — a directory move benefits from a clean re-run.
- After switching `embedding_model` or `embedding_dim`. Embeddings
  are not portable between dimensions; you must re-analyze.
- When `last_analyzed_commit` is many commits behind `HEAD`.
- Run: `.claude/scripts/code-graph-analyze . --project "<ProjectName>"`
  from the project root.

#### Common operations

- Disable code graph for a project (e.g. it's not a code project):
  toggle `enabled = false` and skip running the analyzer.
- Switch embedding model: change `embedding_model` and
  `embedding_dim`, then run `code-graph-analyze` to re-embed.

#### Gotchas

- Renaming a project does not rename its prefix automatically. If
  you change `collection_prefix`, you must rebuild the code graph
  from scratch — the old `Acme_CodeFunction` collection won't be
  queried under the new prefix.
- `embedding_dim` mismatches with `embedding_model` will produce
  garbage results silently. The launcher does not validate the
  pairing.
- The CodeSage service (port 11440) must be running. If you set
  `CODE_EMBED_BACKEND=ollama` in the env, the launcher row is
  display-only — actual embeddings come from Ollama.

---

### For Claude Code agents operating on a user's behalf

These rules apply when an agent (you) is making changes to a project
the user has registered in the launcher. The launcher's per-project
tabs are user preferences, not facts to be silently overwritten.

1. **Never enable/disable an agent, skill, or hook without user
   confirmation.** The `enabled` column in `project_agents`,
   `project_skills`, and `project_hooks` is owned by the user. If
   you think a row should be off, raise it as a recommendation; do
   not invoke `set_project_agent_enabled` (or its skill/hook
   counterparts) silently.
2. **Adding a new `.md` under `.claude/agents/` or `.claude/skills/`
   does not make it appear in the launcher GUI.**
   `populate_project_state_from_filesystem` runs only at project
   creation. To surface a new agent: either re-onboard the project,
   ask the user to re-scan, or call `register_project_agent` via
   the Tauri command if you're inside the launcher process.
3. **Editing `.claude/settings.json`'s `hooks` block does not update
   the Hooks tab** — same reason. Re-populate is required.
4. **Do not assume `enabled = false` blocks the harness.** The
   Claude Code harness reads `.md` files and `settings.json`
   directly. The launcher's `enabled` toggle is currently advisory.
   To truly disable an agent or hook, edit/remove the underlying
   file or `settings.json` entry.
5. **Re-running populate preserves user toggles** but **overwrites**
   `model`, `file_path`, `description`, `source`, and the `config`
   blob from frontmatter. Do not store user preferences in any of
   those — they will be clobbered.
6. **Do not silently change `collection_name` or
   `collection_prefix`.** Renaming a binding without rebuilding the
   underlying Weaviate data orphans the old collection and produces
   empty search results. If a rename is necessary, do all three:
   update the binding, update `.claude/settings.json` env (the
   canonical MCP-env channel since v0.2.12 / PR-27), and
   re-run the analyzer (for code graph) or `kg-sync --all` (for KG).
7. **`source = "bundled"` is reserved for orchestrator-shipped
   rows.** Manually-registered rows should use `source = "project"`
   (or `user` / `paid-module` if appropriate). Do not insert with
   `source = "bundled"` from agent code.
8. **Unregistering a row does not delete the file.** When the user
   asks "remove this agent", clarify whether they mean (a) the
   registry row (use `unregister_project_agent`), (b) the file
   (delete `.claude/agents/<name>.md`), or (c) both. Do not assume.
9. **Secrets never live in any of these tabs.** Secret references
   live in the Secrets tab and point to `~/.vct-secrets/` paths or
   keychain entries — never inline values. If you find a
   secret-looking string in a `config` column, treat it as a bug
   and surface it.
10. **Audit log is automatic.** Every mutation goes through
    `db.audit(...)`. Do not try to suppress it.
11. **Project creation must never fail over a populate hiccup.**
    Per-row errors during populate are logged as warnings; if you
    are extending populate, follow that contract — never propagate
    a single bad frontmatter or unreadable file as a hard error.
