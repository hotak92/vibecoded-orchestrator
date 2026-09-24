# Bundled Module Manifests

Manifests for the **free core infrastructure modules** that ship with the VCT Launcher. They are embedded in the launcher and hub binaries (`vct_launcher_core::bundled_manifests`) and written to `~/.vct/bundled_manifests/` on every launcher and hub start. Every file here must parse with the real parser — CI (`manifest-validate.yml`) and `cargo test` check it, and a test pins the embedded list to this directory. A module that is not an MCP declares no `mcp_registration` block.

The components themselves ship with the orchestrator; these manifests carry their metadata and settings. They are installed for every project (the catalog's `bundled` kind): the hub's `/env` serves their settings with no per-project install row.

## Taxonomy

| Manifest | Module ID | License | Role |
|---|---|---|---|
| `vct-kg.json` | vct-kg | AGPL-3.0 | Knowledge graph MCP (Weaviate-backed) |
| `vct-codegraph.json` | vct-codegraph | AGPL-3.0 | Code graph (AST entities); its tools are served by vct-kg's `weaviate-kg` MCP |
| `vct-search.json` | vct-search | AGPL-3.0 | Web/code/paper search MCP |
| `vct-code-embedding.json` | vct-code-embedding | AGPL-3.0 | GPU/CPU code embeddings service |
| `vct-hub-api.json` | vct-hub-api | AGPL-3.0 | Inter-app hub (port 7700 unless its `VCT_HUB_PORT` setting says otherwise; its URLs name the port as `{hub_port}`) |
| `vct-session-state.json` | vct-session-state | AGPL-3.0 | CONTEXT_STATE.md + memory |

All are `category: "core"`, `license.required: false`, compatible with both base and MAO hosts.

A URL naming a port the user can change is written with a placeholder, never the default spelled out: `vct-hub-api`'s `runtime.health_check.url` and `provides[].base_url` use `{hub_port}`, which `PlaceholderCtx::resolve` replaces with the running hub's port (`<vct root>/hub.port` → `VCT_HUB_PORT` → 7700). Pinned by `bundled_manifests::tests::hub_api_urls_follow_the_running_hubs_port`. Likewise `vct-code-embedding`'s `runtime.health_check.url` uses `{code_embed_port}` — the code-embedding service's port as the launcher resolves it (app_state override → `services.toml` adoption → 11440; `{weaviate_port}` and `{ollama_port}` work the same way). Pinned by `bundled_manifests::tests::code_embedding_manifest_names_the_services_real_default_port`.

`runtime.health_check` is polled by the hub (`launcher/src-tauri/vct-hub/src/module_health.rs`) for every bundled module — they are installed for every project — and the result is the status pill on the module's tile in the launcher's module catalog. `http_get` checks are probed on loopback only; `stdio_ping` checks (`vct-kg`, `vct-search`) are shown as "Status unknown", because the MCP process belongs to the Claude Code session, not to the hub.

`runtime.env_from_settings` and `runtime.env_from_secrets` are read only when VCO itself spawns a `container` / `service` module (`vct_launcher_core::module_settings_env`, `module_secrets_env`). No bundled module is spawned that way: the MCPs are started by Claude Code and read their settings and secrets through the hub's `/env` (e.g. `vct-search`'s `OPENALEX_EMAIL`), and `vct-code-embedding` runs as the `code_embed` compose service, which takes its settings from `infrastructure/.env`. See `docs/VCT_MODULE_MANIFEST_SPEC.md` §7 and §8.

See `docs/VCT_MODULE_MANIFEST_SPEC.md` (in this repository) for the full manifest schema reference.
