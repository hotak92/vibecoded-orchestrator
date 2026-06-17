// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Build script for the self-hosted Excalidraw editor bundle.
//
// Produces (committed, served by commands/diagrams_local_server.rs):
//   ../excalidraw.bundle.js   — IIFE: react + react-dom + @excalidraw + glue
//   ../excalidraw.bundle.css  — Excalidraw's stylesheet (index.css)
//   ../fonts/                 — copied from dist/prod/fonts (see VENDORED.md)
//
// Run from the launcher dir so node_modules resolves:
//   node vendor/diagrams-editor/excalidraw/src/build.mjs
// or via the npm script:
//   npm run build:excalidraw-editor
//
// esbuild is already a (transitive) dev dependency of the launcher
// (node_modules/.bin/esbuild). We pin behaviour via the flags below, not
// a separate install, to match the in-repo esbuild-bundle precedent
// (KG: vendored-npm-fork-esbuild-bundle-recipe).

import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url)); // .../excalidraw/src
const outDir = join(here, ".."); // .../excalidraw

await build({
  entryPoints: [join(here, "main.jsx")],
  bundle: true,
  format: "iife",
  platform: "browser",
  target: ["es2020"],
  // @excalidraw/excalidraw's package `exports` map gates both the JS
  // entry (".") and the stylesheet ("./index.css") behind a
  // `production`/`development` condition. Without this, esbuild's
  // resolver can't pick a branch for the CSS subpath and the build
  // fails with "Could not resolve @excalidraw/excalidraw/index.css".
  conditions: ["production"],
  minify: true,
  sourcemap: false,
  legalComments: "external", // emits .LICENSE.txt next to the bundle
  outfile: join(outDir, "excalidraw.bundle.js"),
  // The bundled JS + collected CSS land directly in outDir (../). The
  // CSS's @font-face url() references (the small `Assistant` UI font)
  // are emitted as separate files under ../css-fonts/ via the `file`
  // loader, with the url() rewritten to point at them — keeps those
  // UI fonts self-hosted. (The hand-drawn CANVAS fonts — Excalifont,
  // Virgil, CJK, etc. — load separately at runtime via
  // window.EXCALIDRAW_ASSET_PATH → the copied ../fonts/ tree.)
  loader: {
    ".js": "jsx",
    ".woff2": "file",
    ".woff": "file",
    ".ttf": "file",
    ".otf": "file",
  },
  // Stable, content-free names so a rebuild with identical inputs
  // produces byte-identical output (no hash churn in git diffs).
  assetNames: "css-fonts/[name]",
  jsx: "automatic",
  define: {
    // React + Excalidraw both branch on this. The production branch
    // strips dev warnings AND avoids dev-only code paths that assume a
    // bundler dev-server (HMR, etc.). Critical — the dev branch renders
    // broken when served as a static file.
    "process.env.NODE_ENV": '"production"',
    // Excalidraw reads import.meta.env in a few spots; neutralise it so
    // the IIFE (no import.meta in that scope) doesn't throw.
    "import.meta.env.MODE": '"production"',
    "import.meta.env.DEV": "false",
    "import.meta.env.PROD": "true",
  },
  // Excalidraw imports its CSS via the entry (`import
  // "@excalidraw/excalidraw/index.css"`). esbuild collects it into a
  // sibling .css next to the JS outfile → excalidraw.bundle.css, which
  // index.html links explicitly.
  logLevel: "info",
});

console.log("[build:excalidraw-editor] wrote excalidraw.bundle.js + .css");
console.log(
  "[build:excalidraw-editor] NOTE: also copy node_modules/@excalidraw/excalidraw/dist/prod/fonts -> ../fonts/ (see VENDORED.md)",
);
