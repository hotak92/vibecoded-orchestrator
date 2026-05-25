# Vendored Excalidraw MCP — Phase 2

## Origin

- **Upstream package**: [`excalidraw-mcp-server`](https://www.npmjs.com/package/excalidraw-mcp-server) v2.0.0
- **Upstream repository**: <https://github.com/debu-sinha/excalidraw-mcp-server>
- **Source of vendor copy**: `npm pack excalidraw-mcp-server@2.0.0`, unpacked verbatim into this directory.
- **Vendor date**: 2026-05-25
- **Audit by**: Claude (Phase 2 of the diagrams-integration plan)
- **License**: MIT (see `LICENSE`). Compatible with VCO's AGPL-3.0 bundle (MIT is permissive; we redistribute the dependency under its own license terms — no AGPL contamination of upstream).

## Why this package (vs the plan-named fork)

The plan (`.claude/context/plans/diagrams-integration-excalidraw-mermaid-2026-05-24.md` §3 Phase 2 + §8 follow-up 3) named `@sanjibdevnathlabs/mcp-excalidraw-local` as the candidate fork. That package is **not published to the npm registry** as of 2026-05-25 (404 from `npm view`). The plan documents three resolution paths; we took path **(c) adopt an alternate published package** because:

1. **`excalidraw-mcp@1.0.0`** (by yctimlin) — 1-year-old, no recent activity, no shipped LICENSE file in tarball (despite stating MIT in `package.json`), smaller surface (`create_element` etc., no `export_scene`).
2. **`excalidraw-mcp-server@2.0.0`** (by Debu Sinha) — actively maintained 2025-era code, security-hardened (helmet + express-rate-limit), MIT-licensed with LICENSE shipped, bundles `@excalidraw/excalidraw@0.18.0` (matches our `bundled_mcp_versions.toml::[npm.excalidraw_lib]` pin), includes `export_scene` (PNG + SVG) — clear winner.

Vendored as path (b) of plan §8.3 (`git+ref`-equivalent: we don't have a git remote because the vendor lives in-tree, but the same offline-install + manual-update discipline applies).

## What's here

```
excalidraw_mcp_fork/
├── VENDORED.md          # this file
├── LICENSE              # MIT, Copyright (c) 2025 Debu Sinha
├── README.md            # upstream's docs (unmodified)
├── package.json         # upstream's manifest (unmodified)
└── dist/
    ├── mcp/             # the MCP server — what we actually spawn
    ├── canvas/          # web canvas app (unused by us, kept for completeness)
    ├── shared/          # shared utilities used by mcp/
    └── widget/          # iframe widget (unused by us)
```

Entry point for spawning: `dist/mcp/index.js` (referenced by `package.json::bin.excalidraw-mcp-server`).

The unused `dist/canvas/` and `dist/widget/` add ~9.5 MB to the vendored copy but we keep them intact rather than risk breaking dynamic-import paths inside `dist/mcp/` that may reach into siblings. If a future audit confirms the MCP module never touches `canvas/`/`widget/`, we can trim — but the conservative move today is to vendor the whole tarball as-shipped.

## Modifications vs upstream

**None** — vendored verbatim. No patches applied. This is deliberate: keeping the vendored copy bit-identical with the published tarball means:

- A reviewer can verify integrity by re-running `npm pack excalidraw-mcp-server@2.0.0` and diffing.
- Future updates are a clean `cp -r` from a fresh tarball; no merge conflicts on local patches.
- License + attribution are preserved verbatim (MIT requires this; we wouldn't strip them anyway).

If we ever need to patch (e.g. upstream removes a tool we depend on, or a security fix lands), document the patch in this file under a new `## Modifications` section with the diff + rationale + date.

## How VCO consumes it

1. **Pin reference**: `bundled_mcp_versions.toml::[npm.excalidraw_mcp]` carries `package = "file:vco_lib/excalidraw_mcp_fork"` and `version = "git+vendored-2.0.0-2026-05-25"`. The `file:` prefix tells `install.py::_install_pinned_npm` to take the local-path branch (`npm install -g <local-dir>`) instead of the registry branch. (Moved from `claude_mcp_servers/excalidraw_mcp_fork/` to `vco_lib/excalidraw_mcp_fork/` in v0.2.34 so the vendored tree ships in the Python wheel — see `vco_lib/bundled_versions.py` docstring for the rationale.)

2. **Wrapper MCP**: `claude_mcp_servers/wrappers/excalidraw_proxy.py` spawns the upstream as:
   ```
   <node> <repo-root>/vco_lib/excalidraw_mcp_fork/dist/mcp/index.js
   ```
   The wrapper proxies stdio JSON-RPC and applies per-project allowlist + scoped-path enforcement on tool calls. See `claude_mcp_servers/wrappers/_base.py` for the base contract.

3. **Allowlist defaults**: the per-tool defaults for the wrapper live in `launcher/src-tauri/vct-hub/src/mcp_tool_grants_api.rs::EXCALIDRAW_DEFAULT_ALLOWLIST`. Adapted to v2's actual element-centric tool surface (not the scene-centric names guessed in the plan — the plan §3 Phase 2 item 3 anticipated this revision with "subject to revision once the real upstream tool list is enumerated").

## Update policy

**Never auto-update.** A bump is a deliberate human act:

1. Run `npm view excalidraw-mcp-server` to confirm a newer version is published.
2. `npm pack excalidraw-mcp-server@<new-ver>` → unpack into a scratch directory.
3. Audit the diff vs the currently vendored version: `diff -r vco_lib/excalidraw_mcp_fork/dist /tmp/scratch/package/dist` (focus on `dist/mcp/`).
4. If clean (no surprise dependencies, no malicious-looking additions, license still MIT): replace the directory contents, update `version` in `bundled_mcp_versions.toml::[npm.excalidraw_mcp]`, update this file's "Vendor date" + adjust the version reference, run `pytest tests/test_excalidraw_proxy.py tests/test_install_excalidraw_pinned_npm.py`, ship in a VCO release with a changelog note.
5. If the diff is dirty: file an upstream issue, defer the bump.

The trade-off is explicit: we don't get free security patches, in exchange for build reproducibility and known-good behaviour. This is the same trade documented in plan §4 Risk 3a for all bundled deps.

## Cross-OS notes

- Spawning the upstream uses `shutil.which("node")` (cached) — handles `node.exe` on Windows.
- All path joins inside the wrapper proxy use `pathlib.Path`.
- The vendored `dist/` is plain ASCII / UTF-8 JavaScript; no platform-specific binaries.
- `dist/mcp/index.js` has a shebang `#!/usr/bin/env node` but VCO invokes it via `node <path>` rather than relying on the executable bit (more portable + survives Windows where the shebang is ignored).
