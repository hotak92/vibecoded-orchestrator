# Audit log

The launcher records every state-changing action across your projects to
a local SQLite table (`audit_log` in `~/.vct/launcher.db`) so NDA-bound
consultant work, SOC2 deliverables, and "who changed what, when"
investigations have a single, queryable source.

## What gets logged

Every Tauri command that mutates persistent state calls
`Db::audit(operation, project_id, module_id, detail)`. The list below is
exhaustive as of this writing — derived by greping for `db.audit(` across
`src-tauri/src/`.

| Source file | Operations |
|---|---|
| `commands/projects_v2.rs` | `project_create`, `project_rename`, `project_delete`, `project_switch_host` |
| `commands/modules.rs` | `module_install`, `module_uninstall`, `module_enable`, `module_disable` |
| `commands/secrets_cmd.rs` | `secret_set`, `secret_clear` |
| `commands/licensing.rs` | `license_activate`, `license_deactivate` |
| `commands/kg.rs` | `kg_set_collection_access`, `kg_set_collection_access_mode`, `kg_set_node_access`, `kg_set_node_access_bulk`, `kg_promote_to_shared`, `kg_ensure_node_access_schema` |
| `commands/codegraph.rs` | `codegraph_grant_access`, `codegraph_set_entity_access_bulk` |
| `commands/coordination.rs` | `coordination_set_config`, `coordination_apply_schema` |
| `commands/mcp_reg.rs` | `mcp_register_module`, `mcp_deregister_module` |
| `commands/telemetry_cmd.rs` | `telemetry_set_consent`, `telemetry_clear_queue` |
| `commands/project_state_cmd.rs` | agent/skill/hook register, enable, disable, unregister; permission add/delete; secret-ref set/delete; KG/codegraph binding set |
| `hub/cli_api.rs` | mirror of the above when invoked via the hub CLI (`cli_*` variants tagged with `via: cli`); plus `cli.kg.search`, `cli.codegraph.search` for read-only search calls (query truncated to 200 chars in `detail`) |

Operations not yet audited (intentionally — they're either read-only or
non-persistent): `list_audit_events`, `kg_search` (Tauri),
`kg_load_graph`, `get_feature_flags`, `coordination_team_status`, all
`get_*` / `list_*` queries.

Note: the CLI-facing `cli.kg.search` and `cli.codegraph.search` operations
*are* logged (one row per call) so power-user / CI activity is visible in
the audit trail. The Tauri-side `kg_search` command remains read-only +
non-audited because it's invoked many times per second by the GUI's
auto-search box.

## Schema

```sql
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    operation   TEXT NOT NULL,         -- e.g. "project_create"
    project_id  TEXT,                  -- nullable
    module_id   TEXT,                  -- nullable
    detail      TEXT NOT NULL,         -- JSON blob, op-specific
    actor       TEXT NOT NULL,         -- OS user ($USER), "system" for legacy rows
    created_at  INTEGER NOT NULL       -- epoch ms
);
```

`detail` is opaque JSON the operation chooses; the `/audit` route does
not interpret it, just renders it. Examples:
- `project_create` detail: `{"host": "base", "name": "myproj", "slug": "myproj"}`
- `secret_set` detail: `{"key_name": "OPENAI_API_KEY", "module_id": "rag"}` (value is **never** logged)
- `module_install` detail: `{"version": "1.2.3", "manifest_url": "..."}`

## Reading the log

The `/audit` route in the launcher renders the table with filters
(project, actor, time range, free-text search). Filters are pushed into
SQLite via `Db::audit_list` so the wire payload only carries matching
rows — earlier revisions filtered client-side over a 500-event window
and fell over once audit logs grew past a few thousand events.

Programmatic access (Rust):

```rust
db.audit_list(
    project_id_opt,    // exact-match filter or None
    actor_opt,         // exact-match filter or None
    since_ms_opt,      // inclusive lower bound, epoch ms
    until_ms_opt,      // inclusive upper bound
    search_opt,        // substring match on operation OR detail
    limit,             // clamped to 10_000 server-side
)
```

CLI:

```bash
sqlite3 ~/.vct/launcher.db \
  "SELECT created_at, operation, project_id, actor, detail \
   FROM audit_log ORDER BY created_at DESC LIMIT 50"
```

## Compliance notes

- **PII**: actor is the OS `$USER` (not an email or display name). If
  you need full-name binding, map `$USER` to identity in your own
  records.
- **Secrets**: secret values are never written to `detail`. Only key
  names (`OPENAI_API_KEY`, `SUPABASE_KEY`, ...) and metadata.
- **Retention**: no automatic pruning. The DB grows linearly with
  mutating activity. Vacuum manually or set up a rotation job if the
  table grows beyond ~1M rows.
- **Tamper resistance**: the log is a plain SQLite table on the user's
  disk. It is not cryptographically signed. Don't rely on it for
  legal-grade non-repudiation; for that, mirror events to a write-only
  external sink (Supabase, Datadog, etc.).
- **NDA / SOC2 use**: the per-project filter + actor + time-range query
  surface in `/audit` is the deliverable. Export to CSV is on the
  backlog (`KNOWN_ISSUES.md`).
