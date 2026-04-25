# Bundled Module Manifests

Manifests for the **free core infrastructure modules** that ship with the VCT Launcher. On first launch, the launcher copies these to `~/.vct/bundled_manifests/` and scans them during catalog discovery.

The modules themselves are NOT bundled with the launcher binary — the manifest's `install.method` tells the launcher how to fetch each one (git_clone from the public orchestrator repo, pip install, etc.). This keeps the launcher binary small while letting us distribute modules independently.

## Taxonomy

| Manifest | Module ID | License | Role |
|---|---|---|---|
| `vct-kg.json` | vct-kg | AGPL-3.0 | Knowledge graph MCP (Weaviate-backed) |
| `vct-codegraph.json` | vct-codegraph | AGPL-3.0 | Code graph MCP (AST entities) |
| `vct-ollama.json` | vct-ollama | AGPL-3.0 | Local LLM inference MCP |
| `vct-search.json` | vct-search | AGPL-3.0 | Web/code/paper search MCP |
| `vct-code-embedding.json` | vct-code-embedding | AGPL-3.0 | GPU/CPU code embeddings service |
| `vct-hub-api.json` | vct-hub-api | AGPL-3.0 | Inter-app hub (port 7700) |
| `vct-session-state.json` | vct-session-state | AGPL-3.0 | CONTEXT_STATE.md + memory |

All are `category: "core"`, `license.required: false`, compatible with both base and MAO hosts, and configured to auto-install on launcher first run.

See `docs/VCT_MODULE_MANIFEST_SPEC.md` (in the Claude Orchestrator meta-project) for the full manifest schema reference.
