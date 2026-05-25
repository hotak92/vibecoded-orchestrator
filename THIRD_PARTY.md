# Third-party software bundled by vibecoded-orchestrator

The orchestrator (this repo) is licensed **AGPL-3.0-or-later** (see `LICENSE`). It bundles and/or installs the following third-party software. None of these dependencies' licenses restrict our ability to redistribute them alongside the AGPL source.

For the launcher binary's compiled-in Rust crates, see `launcher/dist/THIRD_PARTY_LICENSES.txt` (regenerated at release time via `cargo about generate`). This file covers the npm packages and Python packages that the orchestrator's MCP servers + launcher webview bundle.

## Diagrams stack (Phase 1 + Phase 2 of the diagrams-integration plan, 2026-05-24/25)

### Mermaid MCP — `claude-mermaid@1.6.3`

- **License:** MIT
- **Pin:** `bundled_mcp_versions.toml::[npm.mermaid_mcp]`
- **Install path:** `npm install -g claude-mermaid@1.6.3` via `install.py::_install_pinned_npm`
- **Wrapped by:** `claude_mcp_servers/wrappers/mermaid_proxy.py`
- **Upstream:** <https://github.com/claude-mermaid-mcp/claude-mermaid>

### Mermaid library (embedded in launcher) — `mermaid@11.15.0`

- **License:** MIT
- **Pin:** `bundled_mcp_versions.toml::[npm.mermaid_lib]` (mirror in `launcher/package.json`)
- **Used by:** `launcher/src/lib/project-state/DiagramsTab.svelte` for the embedded preview
- **Upstream:** <https://github.com/mermaid-js/mermaid>

### Excalidraw MCP — `excalidraw-mcp-server@2.0.0` (vendored in-tree)

- **License:** MIT, Copyright (c) 2025 Debu Sinha (`claude_mcp_servers/excalidraw_mcp_fork/LICENSE`)
- **Pin:** `bundled_mcp_versions.toml::[npm.excalidraw_mcp]` (`file:claude_mcp_servers/excalidraw_mcp_fork`)
- **Vendor audit:** `claude_mcp_servers/excalidraw_mcp_fork/VENDORED.md`
- **Wrapped by:** `claude_mcp_servers/wrappers/excalidraw_proxy.py`
- **Upstream:** <https://github.com/debu-sinha/excalidraw-mcp-server>

### Excalidraw library (embedded in launcher) — `@excalidraw/excalidraw@0.18.1`

- **License:** MIT
- **Pin:** `bundled_mcp_versions.toml::[npm.excalidraw_lib]` (mirror in `launcher/package.json`)
- **Used by:** `launcher/src/lib/project-state/ExcalidrawEditor.svelte` for the embedded editor
- **Peer dependencies bundled by the launcher:** `react@^18.3.1`, `react-dom@^18.3.1` (both MIT, Copyright Meta Platforms, Inc.)
- **Upstream:** <https://github.com/excalidraw/excalidraw>

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

The orchestrator is AGPL-3.0; all bundled third-party software is under permissive (MIT, Apache 2.0, BSD) or LGPL licenses. AGPL is compatible with redistribution of MIT/Apache/BSD-licensed dependencies under their own license terms. Vendored copies (such as `claude_mcp_servers/excalidraw_mcp_fork/`) retain their original LICENSE files verbatim; modifying any of those vendor directories triggers the vendoring discipline in their respective `VENDORED.md` files.

If you're packaging vibecoded-orchestrator for redistribution, this file plus the per-dependency LICENSE files inside each vendor directory satisfies the MIT/Apache attribution requirements. The AGPL terms apply to the orchestrator code itself.
