# Third-party software bundled by vibecoded-orchestrator

The orchestrator (this repo) is licensed **AGPL-3.0-or-later** (see `LICENSE`). It bundles and/or installs the following third-party software. None of these dependencies' licenses restrict our ability to redistribute them alongside the AGPL source.

For the launcher binary's compiled-in Rust crates, see `launcher/dist/THIRD_PARTY_LICENSES.txt` (regenerated at release time via `cargo about generate`). This file covers the npm packages and Python packages that the orchestrator's MCP servers + launcher webview bundle.

## Other MCP servers

### Playwright MCP — `@playwright/mcp` (lazy-installed)

- **License:** Apache-2.0
- **Install path:** `npx -y @playwright/mcp@latest` (no version pin — see install.py)
- **Bundled by browser fetch:** Chromium binary downloaded on demand
- **Upstream:** <https://github.com/microsoft/playwright-mcp>

## Embedding / inference

### Ollama (text + code embeddings)

- **License:** MIT
- **Runtime:** dynamically started by the orchestrator's `ensure-containers` hook
- **Models loaded by default:** `qwen3-embedding:0.6b` (Apache 2.0)
- **Upstream:** <https://github.com/ollama/ollama>

### Weaviate (vector DB)

- **License:** BSD-3-Clause
- **Runtime:** containerised via podman/docker compose
- **Upstream:** <https://github.com/weaviate/weaviate>

### CodeSage-Large-v2 (code embeddings)

- **License:** Apache 2.0
- **Loaded by:** `claude_mcp_servers/code_embed_service` (FastAPI shim around sentence-transformers)
- **Upstream:** <https://huggingface.co/codesage/codesage-large-v2>

## License compatibility note

The orchestrator is AGPL-3.0; all bundled third-party software is under permissive (MIT, Apache 2.0, BSD) or LGPL licenses. AGPL is compatible with redistribution of MIT/Apache/BSD-licensed dependencies under their own license terms. Vendored copies retain their original LICENSE files verbatim; modifying any vendor directory triggers the vendoring discipline in its `VENDORED.md` file. (The mermaid/excalidraw diagrams stack this file documented through v0.2.101 was removed in v0.2.102; removing the code removes the attribution obligation.)

If you're packaging vibecoded-orchestrator for redistribution, this file plus the per-dependency LICENSE files inside each vendor directory satisfies the MIT/Apache attribution requirements. The AGPL terms apply to the orchestrator code itself.
