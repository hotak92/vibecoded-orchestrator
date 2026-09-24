# Bundled Module Manifests

Manifests for the **free core infrastructure modules** that ship with the VCT Launcher. They are embedded in the launcher and hub binaries (`vct_launcher_core::bundled_manifests`) and written to `~/.vct/bundled_manifests/` on every launcher and hub start. Every file here must parse with the real parser — CI (`manifest-validate.yml`) and `cargo test` check it, and a test pins the embedded list to this directory. A module that is not an MCP declares no `mcp_registration` block.

The components themselves ship with the orchestrator; these manifests carry their metadata and settings. They are installed for every project (the catalog's `bundled` kind): the hub's `/env` serves their settings with no per-project install row.

## Taxonomy

| Manifest | Module ID | License | Role |
|---|---|---|---|
| `vct-kg.json` | vct-kg | AGPL-3.0 | Knowledge graph MCP (Weaviate-backed) |
| `vct-codegraph.json` | vct-codegraph | AGPL-3.0 | Code graph MCP (AST entities) |
| `vct-search.json` | vct-search | AGPL-3.0 | Web/code/paper search MCP |
| `vct-code-embedding.json` | vct-code-embedding | AGPL-3.0 | GPU/CPU code embeddings service |
| `vct-hub-api.json` | vct-hub-api | AGPL-3.0 | Inter-app hub (port 7700) |
| `vct-session-state.json` | vct-session-state | AGPL-3.0 | CONTEXT_STATE.md + memory |

All are `category: "core"`, `license.required: false`, compatible with both base and MAO hosts.

See `docs/VCT_MODULE_MANIFEST_SPEC.md` (in the Claude Orchestrator meta-project) for the full manifest schema reference.
