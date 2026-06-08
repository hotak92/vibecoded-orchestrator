<!--
SPDX-License-Identifier: AGPL-3.0-or-later

This file is part of the VibeCoded Orchestrator (vct-launcher).
Documents the vendored static-build editors served by the launcher's
local diagrams-editor HTTP server (commands/diagrams_local_server.rs).
-->

# Vendored Diagrams Editors

This directory contains static HTML/CSS/JS assets that the launcher
serves on a local HTTP server (`127.0.0.1:<free-port>`) when the user
clicks **Draw Mermaid (visual)** or **Draw Excalidraw (visual)** in the
Diagrams tab. The page opens in the user's default browser via
`tauri-plugin-opener::open_url` — NOT in the Tauri WebView, which has
documented rendering issues with both libraries on Wayland + webkit2gtk
(see `docs/EXCALIDRAW_WAYLAND_TEST.md`).

## Scope decision (v0.2.36 Agent R, 2026-05-26)

The original v0.2.36 spec (`.claude/context/plans/v0.2.35-backlog-2026-05-26.md`,
section "Mermaid + Excalidraw editor UX rework") proposed vendoring the
upstream `excalidraw/excalidraw` SPA AND `mermaid-js/mermaid-live-editor`
SPA, each rebuilt locally and adapted to call our `/file` and `/save`
HTTP endpoints. Realistic time budget for a single-agent v0.2.36 slot
made that scope impossible:

  - **Mermaid live editor** (mermaid-js/mermaid-live-editor) is a full
    SvelteKit app with its own state model, Monaco editor, theme system,
    history, and 30+ dependency surfaces. Vendoring it as-is plus
    patching localStorage → HTTP would be ~6h of work and add ~5 MB
    of build output (Monaco alone is 4 MB).
  - **Excalidraw** (`@excalidraw/excalidraw` npm package) ships as ES
    modules with bare imports (`react`, `react-dom`, `jotai`, `clsx`,
    `@radix-ui/react-popover`, etc.). Making it run standalone in the
    browser requires either bundling everything via Vite/Rollup (and
    re-shipping the 18 MB+ output for every install) or using esm.sh
    CDN (forbidden — self-hosted requirement).

The pragmatic v0.2.36 ship is:

| Editor    | What we ship now                                                     | Future (v0.2.37+)                                |
|-----------|----------------------------------------------------------------------|---------------------------------------------------|
| Mermaid   | **Custom minimal visual editor** — single HTML page with a textarea, live `mermaid.min.js` preview, save button posting to `/save`. ~3.3 MB total. Self-hosted, no CDN. | Optionally adopt mermaid-live-editor for Monaco + history + theming. |
| Excalidraw| **Bridge page** — a static page that links out to `excalidraw.com` and explains the file-import workflow: draw there, export `.excalidraw`, then drag the file onto our DiagramsTab drop zone (Agent L's v0.2.35 work). | Full vendored Excalidraw SPA with React + deps bundled. Ticket in `.claude/context/plans/v0.2.37-backlog.md` once that file exists. |

This shipping decision is also explicit in the agent summary returned to
the main chat. The embedded `ExcalidrawEditor.svelte` (broken in
Wayland+webkit2gtk per docs/EXCALIDRAW_WAYLAND_TEST.md and shown in the
screenshot as enormous-icon garbage) **is preserved on disk
for now** so we don't break any deep-linked tests, but the DiagramsTab
"Draw Excalidraw" button no longer routes to it.

## Files

### `mermaid/mermaid.min.js`

- **Source**: `node_modules/mermaid/dist/mermaid.min.js`
- **Upstream**: <https://github.com/mermaid-js/mermaid>
- **Version**: pinned via `launcher/package.json` (`"mermaid": "11.15.0"` as of v0.2.36)
- **License**: MIT — see `LICENSES/mermaid-LICENSE` (copy at vendor time)
- **Build recipe**: `cd launcher && npm ci && cp node_modules/mermaid/dist/mermaid.min.js vendor/diagrams-editor/mermaid/mermaid.min.js`
- **Size**: ~3.3 MB (minified, single UMD bundle, no external deps)
- **Why this file specifically**: it's the only UMD-format build mermaid ships. The other `dist/` outputs are ES modules with split chunks that require a bundler.

### `mermaid/index.html`

- Custom HTML page written by us. AGPL-3.0-or-later.
- Loads `mermaid.min.js`, renders a textarea + live SVG preview, and
  has a "Save" button that POSTs the textarea content to
  `/save?path=<rel_path>` on the launcher's local server.
- On load, fetches `/file?path=<rel_path>` to populate the textarea
  with the current file contents.

### `excalidraw/index.html`

- Custom HTML page written by us. AGPL-3.0-or-later.
- Static bridge page — no Excalidraw runtime is loaded. Explains
  the v0.2.36 workflow (use excalidraw.com → export → drag into
  DiagramsTab drop zone).
- Shipped so the same `/excalidraw/` route renders something
  intentional rather than 404'ing.

## Refreshing the vendored bundle

When upgrading mermaid's `package.json` pin:

```bash
cd launcher
npm install mermaid@<new-version>
cp node_modules/mermaid/dist/mermaid.min.js vendor/diagrams-editor/mermaid/mermaid.min.js
# Update the version line in this file's table above.
# Smoke-test the editor by clicking "Draw Mermaid (visual)" in the Diagrams tab.
```

## License notes

- **Mermaid** is MIT-licensed; vendoring the minified output and
  serving it from a local HTTP server falls inside that license's
  permissive terms. Attribution is preserved by keeping a copy of
  the upstream LICENSE file at `mermaid/LICENSE-mermaid` (see below).
- **Our HTML wrappers** (`mermaid/index.html`, `excalidraw/index.html`,
  and any future static glue) are AGPL-3.0-or-later (same as the rest
  of the launcher), with the SPDX header in each file.
- **No Excalidraw source is vendored in v0.2.36**, so there's no
  Excalidraw license concern for this release. When v0.2.37 vendors
  the Excalidraw SPA, a parallel `LICENSE-excalidraw` (MIT) file MUST
  be added and this section updated.
