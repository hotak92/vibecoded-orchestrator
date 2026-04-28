# VCT Launcher — User Guide

_(Multi-agent contribution: Secrets/Permissions section by general-purpose agent, run 2026-04-28)_

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
