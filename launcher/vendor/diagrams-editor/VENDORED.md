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
documented rendering issues with both libraries on Wayland + webkit2gtk.

## Scope decision

Both editors are now **self-hosted visual editors** wired to the local
`/file` + `/save` endpoints — draw in the browser, hit Save, the on-disk
`.claude/diagrams/<cat>/<name>.{mmd,excalidraw}` file updates, and Claude
reads the same file. No CDN, no excalidraw.com round-trip.

| Editor    | What we ship                                                                                                                                                            |
|-----------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Mermaid   | **Custom minimal visual editor** — single HTML page with a textarea, live `mermaid.min.js` preview, Save button posting to `/save`. ~3.3 MB. Self-hosted, no CDN.       |
| Excalidraw| **Self-hosted Excalidraw editor** — an esbuild bundle of `@excalidraw/excalidraw` + react + react-dom + glue (`excalidraw.bundle.js`, ~7.8 MB minified) + the upstream canvas fonts (`fonts/`, ~14 MB). Renders the real Excalidraw canvas in the user's default browser; Save serializes the scene to `.excalidraw` and POSTs `/save`. Self-hosted, no CDN. |

### History

An earlier (v0.2.36) ship made Excalidraw a **bridge page** (link out to
`excalidraw.com`, export `.excalidraw`, drag-import into the DiagramsTab
drop zone) because making `@excalidraw/excalidraw` run standalone needs a
bundler and the upstream fonts re-shipped. v0.2.61 closed that gap: the
package's ESM entry (bare imports — `react`, `react-dom`, `jotai`,
`@radix-ui/*`, …) is bundled once via esbuild into a self-contained IIFE
and committed (like `mermaid.min.js`), so installs never run a bundler.
The font self-hosting requirement is met by copying
`dist/prod/fonts` into `fonts/` and pointing `window.EXCALIDRAW_ASSET_PATH`
at the served editor root (`/excalidraw/`).

The old embedded `ExcalidrawEditor.svelte` (broken on Wayland+webkit2gtk)
is unrelated to this server-served editor and can be removed once its
deep-linked tests are re-checked.

## Files

### `mermaid/mermaid.min.js`

- **Source**: `node_modules/mermaid/dist/mermaid.min.js`
- **Upstream**: <https://github.com/mermaid-js/mermaid>
- **Version**: pinned via `launcher/package.json` (`"mermaid": "11.15.0"`)
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

### `excalidraw/` (self-hosted editor)

| File                              | What                                                                                                  | License |
|-----------------------------------|-------------------------------------------------------------------------------------------------------|---------|
| `index.html`                      | Our HTML shell — toolbar (file path + Save + status) + `#root` for the Excalidraw canvas; sets `window.EXCALIDRAW_ASSET_PATH = "/excalidraw/"` then loads the bundle. | AGPL-3.0-or-later (ours) |
| `src/main.jsx`                     | Our React entry — mounts `<Excalidraw>`, loads via `GET /file`, saves via `serializeAsJSON` → `POST /save` (Bearer `#token`). Bundled, not served. | AGPL-3.0-or-later (ours) |
| `src/build.mjs`                    | esbuild build script (run via `npm run build:excalidraw-editor`). Bundled-from, not served. | AGPL-3.0-or-later (ours) |
| `excalidraw.bundle.js`            | esbuild output: `@excalidraw/excalidraw` + react + react-dom + scheduler + pako + our glue, IIFE, minified. ~7.8 MB. | MIT (3rd-party) |
| `excalidraw.bundle.js.LEGAL.txt`  | Per-dependency license notices esbuild collected from the bundled sources (`legalComments: "external"`). | — |
| `excalidraw.bundle.css`           | `@excalidraw/excalidraw` stylesheet (collected by esbuild from the CSS import). ~144 KB. | MIT (3rd-party) |
| `css-fonts/*.woff2`               | The "Assistant" UI font referenced by the stylesheet's `@font-face` `url()`s (esbuild `file` loader output). | OFL/MIT (3rd-party) |
| `fonts/**`                        | The hand-drawn CANVAS fonts (Excalifont, Virgil, Cascadia, Comic Shanns, Nunito, Lilita, Liberation, Xiaolai, Assistant) copied from `dist/prod/fonts`; loaded at runtime via `EXCALIDRAW_ASSET_PATH`. ~14 MB / 234 files. | OFL/MIT (3rd-party) |
| `LICENSE-excalidraw`              | MIT attribution for the vendored Excalidraw assets + pointer to the bundle's LEGAL.txt for React/pako. | — |

- **Version**: pinned via `launcher/package.json` (`"@excalidraw/excalidraw": "^0.18.1"`; bundled version 0.18.1). React/react-dom `^18.3.1`.
- **Why a bundle (not a `dist/` copy like mermaid)**: Excalidraw 0.18 ships ESM-only with split chunks + bare imports (no UMD/IIFE build), so a one-shot esbuild bundle is required to serve it as a plain static page. (Mermaid ships a UMD `mermaid.min.js`, so it needs no bundler.)
- **The `?file=` / `#token=` contract** matches `mermaid/index.html`: `GET /file?path=<rel>` on load (404/empty = new file), `POST /save?path=<rel>` with `Authorization: Bearer <token>` on save.

## Refreshing the vendored bundles

### Mermaid

When upgrading mermaid's `package.json` pin:

```bash
cd launcher
npm install mermaid@<new-version>
cp node_modules/mermaid/dist/mermaid.min.js vendor/diagrams-editor/mermaid/mermaid.min.js
# Update the version line in this file's table above.
# Smoke-test the editor by clicking "Draw Mermaid (visual)" in the Diagrams tab.
```

### Excalidraw

When upgrading the `@excalidraw/excalidraw` (or `react`/`react-dom`)
`package.json` pin — rebuild the bundle AND re-copy the fonts (the font
files are content-hashed by upstream, so a version bump changes their
names):

```bash
cd launcher
npm ci                                   # match the lockfile-pinned versions
npm run build:excalidraw-editor          # -> excalidraw.bundle.{js,css} + css-fonts/ + .LEGAL.txt
rm -rf vendor/diagrams-editor/excalidraw/fonts
cp -r node_modules/@excalidraw/excalidraw/dist/prod/fonts \
      vendor/diagrams-editor/excalidraw/fonts
# Update the version line in this file's table above.
# Smoke-test: click "Draw Excalidraw (visual)" in the Diagrams tab → draw → Save
#   → confirm the .claude/diagrams/.../<name>.excalidraw file updates on disk.
```

The build is reproducible (no content-hash in `excalidraw.bundle.js`'s
own name; identical inputs → byte-identical output), so a no-op rebuild
produces no git diff.

## License notes

- **Mermaid** is MIT-licensed; vendoring the minified output and
  serving it from a local HTTP server falls inside that license's
  permissive terms. Attribution is preserved by keeping a copy of
  the upstream LICENSE file at `mermaid/LICENSE-mermaid` (see below).
- **Excalidraw** (`@excalidraw/excalidraw`) is MIT-licensed, as are the
  bundled React / react-dom / scheduler / pako dependencies (pako is
  MIT AND Zlib). The vendored bundle + assets carry:
  - `excalidraw/LICENSE-excalidraw` — the Excalidraw MIT notice + an
    index of which files are 3rd-party.
  - `excalidraw/excalidraw.bundle.js.LEGAL.txt` — esbuild-collected
    per-dependency notices for everything inside the bundle.
  The Excalidraw fonts are distributed by upstream under OFL/MIT and
  are vendored verbatim from `dist/prod/fonts`.
- **Our HTML/JS wrappers** (`mermaid/index.html`, `excalidraw/index.html`,
  `excalidraw/src/*`, and any future static glue) are AGPL-3.0-or-later
  (same as the rest of the launcher), with the SPDX header in each file.
