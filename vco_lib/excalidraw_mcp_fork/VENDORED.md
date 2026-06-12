# Vendored Excalidraw MCP — Phase 2

## Origin

- **Upstream package**: [`excalidraw-mcp-server`](https://www.npmjs.com/package/excalidraw-mcp-server) v2.0.0
- **Upstream repository**: <https://github.com/debu-sinha/excalidraw-mcp-server>
- **Source of vendor copy**: `npm pack excalidraw-mcp-server@2.0.0`, unpacked verbatim into this directory.
- **Upstream tarball pin**: `dist.shasum = 15afa7b636830ebb97d0f474d2001253016d9bb1`,
  `dist.integrity = sha512-ibx/RzqltM5oxLMt4Rd3vbCXsfVJn1gPzubZgc7a+XVdFzLa8Yyvv2sGImc3qQlVfcJA05mxDRkneC9w9wxz5Q==`
  (from `npm view excalidraw-mcp-server@2.0.0 dist.shasum dist.integrity`).
  A reviewer can re-pack the upstream tarball and verify the unmodified
  files in this tree match it byte-for-byte; the deliberate local
  modifications are enumerated under "Modifications vs upstream" below.
- **Vendor date**: 2026-05-25 (bundling pass: 2026-06-12)
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
├── package.json         # upstream's manifest + VCO bundling edits (see Modifications)
├── npm-shrinkwrap.json  # full dependency-tree pin (VCO addition, see Modifications)
├── .gitignore           # ignores node_modules/ (build-time only, never ships)
└── dist/
    ├── mcp/             # the MCP server — what we actually spawn
    │   └── index.bundled.js   # esbuild self-contained bundle (VCO addition)
    ├── canvas/          # web canvas app (unused by us, kept for completeness)
    ├── shared/          # shared utilities used by mcp/
    └── widget/          # iframe widget (unused by us)
```

Entry point for spawning: `dist/mcp/index.bundled.js` (referenced by `package.json::bin.excalidraw-mcp-server`). The unbundled `dist/mcp/index.js` is kept as upstream shipped it; the wrapper falls back to it only if the bundle is missing AND node_modules is installed.

The unused `dist/canvas/` and `dist/widget/` add ~9.5 MB to the vendored copy but we keep them intact rather than risk breaking dynamic-import paths inside `dist/mcp/` that may reach into siblings. If a future audit confirms the MCP module never touches `canvas/`/`widget/`, we can trim — but the conservative move today is to vendor the whole tarball as-shipped.

## Modifications vs upstream

All upstream **source** files (`dist/mcp/*.js` except the bundle, `dist/shared/`, `dist/canvas/`, `dist/widget/`, `README.md`, `LICENSE`) remain bit-identical with the published tarball — a reviewer can verify by re-running `npm pack excalidraw-mcp-server@2.0.0` and diffing against the upstream pin recorded above.

The deliberate VCO additions/edits (2026-06-12, P0-3 fix):

1. **`dist/mcp/index.bundled.js`** (NEW) — esbuild self-contained bundle of `dist/mcp/index.js` with every transitive dependency inlined. Rationale: the vendored tree ships without `node_modules`, and `npm install -g <dir>` does not reliably resolve the caret-ranged transitives at install time on user machines — so the unbundled entry was dead-on-arrival (`Cannot find package 'pino'` on the first import). The bundle runs with a bare `node`, no installation step at all. Regeneration: see "Bundling recipe" below.
2. **`npm-shrinkwrap.json`** (NEW) — full dependency-tree pin generated from a clean `npm install` at bundle time. Unlike `package-lock.json`, a shrinkwrap SHIPS inside the npm package and is honored by `npm install -g <dir>` — so even the legacy global-install path now resolves the exact tested transitive versions instead of whatever the registry serves that day.
3. **`package.json`** — `main`, `bin.excalidraw-mcp-server`, and `scripts.start` repointed from `dist/mcp/index.js` to `dist/mcp/index.bundled.js`; added `scripts.bundle` (the canonical regeneration command) and `devDependencies.esbuild` pinned EXACT (`0.28.1`, no caret) so re-bundles are reproducible.
4. **`.gitignore`** (NEW) — ignores `node_modules/` (needed only at bundle/shrinkwrap-generation time, never shipped).

## Bundling recipe

To regenerate `dist/mcp/index.bundled.js` (required whenever `dist/` is re-vendored from a new upstream tarball, or when a dependency pin in `npm-shrinkwrap.json` changes):

```bash
cd vco_lib/excalidraw_mcp_fork
npm install --ignore-scripts        # honors npm-shrinkwrap.json → exact pinned tree
npm run bundle                      # → dist/mcp/index.bundled.js
```

`scripts.bundle` expands to:

```bash
esbuild dist/mcp/index.js --bundle --platform=node --format=esm \
  --outfile=dist/mcp/index.bundled.js \
  --define:process.env.NODE_ENV='"production"' \
  --banner:js='import { createRequire as __vcoCreateRequire } from "node:module"; const require = __vcoCreateRequire(import.meta.url);'
```

Flag rationale:

- `--format=esm` — the entry is ESM (`"type": "module"`, top-level `import`). esbuild preserves the `#!/usr/bin/env node` shebang from the entry file.
- `--banner:js=createRequire shim` — CJS dependencies inside the bundle (pino's ecosystem) call `require()` of node builtins; ESM output has no ambient `require`, so we provide one via `createRequire(import.meta.url)`. Without the banner the bundle throws `Dynamic require of "fs" is not supported` at startup.
- `--define:process.env.NODE_ENV='"production"'` — bakes production mode into the bundle so `dist/shared/logger.js` takes the `pino.destination(2)` path (in-process stderr writer). The dev path (`pino.transport({target:'pino-pretty'})`) spawns a worker thread that resolves `pino-pretty` BY MODULE PATH at runtime — which cannot work from inside a bundle with no node_modules. Baking NODE_ENV closes that failure mode permanently.
- esbuild version is pinned exact in `devDependencies` (`0.28.1`). **Bump it deliberately**: update the pin, re-run the recipe, re-run `python -m pytest tests/test_excalidraw_mcp_smoke.py` (live JSON-RPC `initialize` round-trip against the regenerated bundle).

Regenerate `npm-shrinkwrap.json` only when upstream pins change (new vendored version or deliberate dependency bump): delete `node_modules/` + `npm-shrinkwrap.json`, run `npm install --ignore-scripts`, then `npm shrinkwrap`, then re-bundle + re-run the smoke test.

If we ever need to patch upstream source itself (e.g. upstream removes a tool we depend on, or a security fix lands), document the patch in this file with the diff + rationale + date — same discipline as the additions above.

## How VCO consumes it

1. **Pin reference**: `bundled_mcp_versions.toml::[npm.excalidraw_mcp]` carries `package = "file:vco_lib/excalidraw_mcp_fork"` and `version = "git+vendored-2.0.0-2026-05-25"`. The `file:` prefix tells `install.py::_install_pinned_npm` to take the local-path branch (`npm install -g <local-dir>`) instead of the registry branch. (Moved from `claude_mcp_servers/excalidraw_mcp_fork/` to `vco_lib/excalidraw_mcp_fork/` in v0.2.34 so the vendored tree ships in the Python wheel — see `vco_lib/bundled_versions.py` docstring for the rationale.)

2. **Wrapper MCP**: `claude_mcp_servers/wrappers/excalidraw_proxy.py` spawns the upstream as:
   ```
   <node> <repo-root>/vco_lib/excalidraw_mcp_fork/dist/mcp/index.bundled.js
   ```
   (falling back to the unbundled `dist/mcp/index.js` only if the bundle is missing). The wrapper proxies stdio JSON-RPC and applies per-project allowlist + scoped-path enforcement on tool calls. See `claude_mcp_servers/wrappers/_base.py` for the base contract.

3. **Allowlist defaults**: the per-tool defaults for the wrapper live in `launcher/src-tauri/vct-hub/src/mcp_tool_grants_api.rs::EXCALIDRAW_DEFAULT_ALLOWLIST`. Adapted to v2's actual element-centric tool surface (not the scene-centric names guessed in the plan — the plan §3 Phase 2 item 3 anticipated this revision with "subject to revision once the real upstream tool list is enumerated").

## Update policy

**Never auto-update.** A bump is a deliberate human act:

1. Run `npm view excalidraw-mcp-server` to confirm a newer version is published.
2. `npm pack excalidraw-mcp-server@<new-ver>` → unpack into a scratch directory.
3. Audit the diff vs the currently vendored version: `diff -r vco_lib/excalidraw_mcp_fork/dist /tmp/scratch/package/dist` (focus on `dist/mcp/`).
4. If clean (no surprise dependencies, no malicious-looking additions, license still MIT): replace the directory contents, RE-APPLY the VCO modifications (package.json repoint + regenerate npm-shrinkwrap.json + re-run the "Bundling recipe" above), update `version` in `bundled_mcp_versions.toml::[npm.excalidraw_mcp]`, update this file's "Vendor date" + upstream tarball pin, run `pytest tests/test_excalidraw_proxy.py tests/test_install_excalidraw_pinned_npm.py tests/test_excalidraw_mcp_smoke.py`, ship in a VCO release with a changelog note.
5. If the diff is dirty: file an upstream issue, defer the bump.

The trade-off is explicit: we don't get free security patches, in exchange for build reproducibility and known-good behaviour. This is the same trade documented in plan §4 Risk 3a for all bundled deps.

## Cross-OS notes

- Spawning the upstream uses `shutil.which("node")` (cached) — handles `node.exe` on Windows.
- All path joins inside the wrapper proxy use `pathlib.Path`.
- The vendored `dist/` is plain ASCII / UTF-8 JavaScript; no platform-specific binaries.
- `dist/mcp/index.js` has a shebang `#!/usr/bin/env node` but VCO invokes it via `node <path>` rather than relying on the executable bit (more portable + survives Windows where the shebang is ignored).
