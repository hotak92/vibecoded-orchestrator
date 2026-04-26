# MCP server integration

The launcher's `/mcp` route lets users add, remove, and toggle Model
Context Protocol (MCP) servers that Claude Code will launch on startup.

## Where the launcher writes

When you click **+ Add custom MCP server** and submit the form, the
backend (`add_custom_mcp_server` in
`src-tauri/src/commands/dashboard.rs`) makes three writes:

1. **`~/.vct/orchestrator.json`** — the launcher's own config. Stores
   the full `McpServerConfig` (id, name, description, enabled, command,
   args, env, min_tier, port, configurable, settings) so the launcher
   can re-render the list on next start.

2. **`~/.claude.json`** — *this is the file Claude Code actually reads*.
   The launcher patches the JSON object at
   `mcpServers.<id> = { type: "stdio", command, args, env }` via
   `mcp_registration::register_mcp`. The write uses an atomic
   `write -> rename` and is guarded by an OS-level pidfile lock so
   concurrent edits from another launcher session can't corrupt it.

3. **`<install_path>/.claude/settings.json` `env` block** — the
   orchestrator install's own settings file gets MCP-derived env keys
   so the orchestrator's own subprocess can read them. This is a
   secondary path; the primary integration point with Claude Code is
   step 2.

## What's preserved

`register_mcp` reads the existing file (or initialises an empty `{}` if
absent), inserts/replaces only the `mcpServers.<id>` key, and writes
the rest of the file back unchanged. Existing keys like
`permissions.allow`, `feedbackSurveyState`, or any other Claude Code
config keys are **not** touched. There is a regression test for this:
`mcp_registration::tests::register_preserves_existing_top_level_keys`.

## Removing

Clicking the **Remove** button on a non-built-in MCP card calls
`remove_mcp_server`, which:

1. Drops the entry from `~/.vct/orchestrator.json`.
2. Calls `deregister_mcp` to drop `mcpServers.<id>` from
   `~/.claude.json`.

Built-in MCP servers (`weaviate-kg`, `ollama`, `search`, `code-embed`)
cannot be removed — they're managed by the orchestrator. Use the
toggle to disable them instead.

## Verification

After adding a server, you can verify the write landed:

```bash
jq '.mcpServers' ~/.claude.json
```

The new server should appear under its `id` with `type: "stdio"` and
the command/args you configured.

You may need to restart Claude Code for it to pick up the new server.

## Test coverage

- `mcp_registration::tests::register_creates_file_and_writes_mcpservers_block`
  — end-to-end: register into a non-existent target, verify
  `mcpServers.<name>` block matches the entry passed in.
- `mcp_registration::tests::register_then_deregister_removes_only_named_entry`
  — deregister of one server leaves siblings intact.
- `mcp_registration::tests::register_preserves_existing_top_level_keys`
  — pre-seed file with `permissions` + `feedbackSurveyState`, register
  a server, verify both pre-existing keys still match exactly.

Run with: `cargo test --lib mcp_registration:: --manifest-path
launcher/src-tauri/Cargo.toml`.
