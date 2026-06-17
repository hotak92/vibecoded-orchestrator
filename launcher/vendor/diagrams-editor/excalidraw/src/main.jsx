// SPDX-License-Identifier: AGPL-3.0-or-later
//
// React entry for the self-hosted Excalidraw visual editor served by the
// launcher's local diagrams-editor HTTP server
// (commands/diagrams_local_server.rs). This file is BUNDLED by
// `build.mjs` (esbuild) into `../excalidraw.bundle.js` together with
// @excalidraw/excalidraw + react + react-dom, producing one
// self-contained IIFE. The built bundle is COMMITTED (like
// mermaid/mermaid.min.js) so installs never run a bundler.
//
// It is the Excalidraw analogue of mermaid/index.html's inline script —
// same URL + I/O contract:
//
//   URL:  http://127.0.0.1:<port>/excalidraw/?file=<rel_path>#token=<hex>
//   load: GET  /file?path=<rel_path>           (404 / empty body = new file)
//   save: POST /save?path=<rel_path>           (Authorization: Bearer <token>)
//
// The page opens in the user's DEFAULT BROWSER (not the Tauri WebView —
// Excalidraw renders broken on Wayland+webkit2gtk; that's the whole
// reason for the local-server-in-browser design).

import React from "react";
import { createRoot } from "react-dom/client";
import { Excalidraw, serializeAsJSON } from "@excalidraw/excalidraw";
import "@excalidraw/excalidraw/index.css";

// ─── URL contract (mirrors mermaid/index.html) ──────────────────────
// `?file=<rel_path>` is the project-relative path; forwarded verbatim to
// /file and /save (the launcher re-validates it against the project root
// in diagrams_local_server::resolve_diagrams_path). The save token rides
// in the URL FRAGMENT (`#token=`) so it never travels in request lines /
// logs; we echo it back as `Authorization: Bearer` on POST /save.
const params = new URLSearchParams(window.location.search);
const filePath = params.get("file") || "";
const hashParams = new URLSearchParams(window.location.hash.replace(/^#/, ""));
const saveToken = hashParams.get("token") || "";

// ─── DOM handles (the chrome lives in index.html) ───────────────────
const filePathEl = document.getElementById("filePath");
const statusEl = document.getElementById("status");
const saveBtn = document.getElementById("saveBtn");
const rootEl = document.getElementById("root");
if (filePathEl) filePathEl.textContent = filePath || "(no file specified)";

function markStatus(text, kind) {
  if (!statusEl) return;
  statusEl.textContent = text;
  statusEl.className = "status" + (kind ? " " + kind : "");
}

// ─── Imperative API handle + dirty tracking ─────────────────────────
// We pull the scene out of the imperative API on Save rather than
// threading React state through onChange — Excalidraw mutates a large
// element array on every pointer move, so reading it once at save time
// is cheaper and avoids re-render churn.
let excalidrawAPI = null;
let dirty = false;

function setDirty(next) {
  dirty = next;
  if (saveBtn) saveBtn.disabled = !next;
}

// ─── Load the file → initialData ────────────────────────────────────
// Returns the parsed { elements, appState, files } for <Excalidraw
// initialData>. A 404 or empty body is the new-file case → start blank.
// Excalidraw's own restore() runs internally on initialData, so we hand
// it the raw parsed scene without pre-restoring.
async function loadInitialData() {
  if (!filePath) {
    markStatus("No file", "error");
    return null;
  }
  try {
    const resp = await fetch("/file?path=" + encodeURIComponent(filePath));
    if (resp.status === 404) {
      markStatus("New file", "success");
      return null;
    }
    if (!resp.ok) {
      const txt = await resp.text();
      throw new Error(`GET /file failed (${resp.status}): ${txt}`);
    }
    const text = await resp.text();
    if (!text.trim()) {
      markStatus("New file", "success");
      return null;
    }
    const scene = JSON.parse(text);
    markStatus("Loaded", "success");
    // Force light theme + suppress the per-file scroll-to-content prompt;
    // keep whatever the file stored otherwise.
    return {
      elements: scene.elements || [],
      appState: { ...(scene.appState || {}), collaborators: undefined },
      files: scene.files || undefined,
      scrollToContent: true,
    };
  } catch (err) {
    console.error("[excalidraw-editor] load failed:", err);
    markStatus(`Load failed: ${err.message}`, "error");
    return null;
  }
}

// ─── Save: serialize scene → POST /save ─────────────────────────────
async function save() {
  if (!filePath) {
    markStatus("No file path", "error");
    return;
  }
  if (!excalidrawAPI) {
    markStatus("Editor not ready", "error");
    return;
  }
  setDirty(false);
  markStatus("Saving…");
  try {
    const elements = excalidrawAPI.getSceneElements();
    const appState = excalidrawAPI.getAppState();
    const files = excalidrawAPI.getFiles();
    // "local" emits the `.excalidraw` document shape (type
    // "excalidraw", versioned) that the upstream editor and the
    // drag-import path both understand.
    const json = serializeAsJSON(elements, appState, files, "local");
    const resp = await fetch("/save?path=" + encodeURIComponent(filePath), {
      method: "POST",
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        Authorization: "Bearer " + saveToken,
      },
      body: json,
    });
    if (!resp.ok) {
      const txt = await resp.text();
      throw new Error(`POST /save failed (${resp.status}): ${txt}`);
    }
    markStatus("Saved", "success");
  } catch (err) {
    console.error("[excalidraw-editor] save failed:", err);
    markStatus(`Save failed: ${err.message}`, "error");
    setDirty(true);
  }
}

// ─── App component ──────────────────────────────────────────────────
function App({ initialData }) {
  return React.createElement(Excalidraw, {
    initialData,
    theme: "dark",
    excalidrawAPI: (api) => {
      excalidrawAPI = api;
    },
    // Excalidraw fires onChange on mount + every edit. Skip the initial
    // synthetic change(s) so loading a file doesn't mark it dirty; only
    // a genuine user edit enables Save.
    onChange: () => {
      if (!dirty && booted) setDirty(true);
    },
  });
}

// ─── Boot ───────────────────────────────────────────────────────────
let booted = false;

(async function boot() {
  const initialData = await loadInitialData();
  const root = createRoot(rootEl);
  root.render(React.createElement(App, { initialData }));
  // Mark booted on the next macrotask so the mount-time onChange(s)
  // (which fire synchronously during render) don't flip dirty.
  setTimeout(() => {
    booted = true;
  }, 0);

  if (saveBtn) saveBtn.addEventListener("click", save);
  window.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (!saveBtn || !saveBtn.disabled) save();
    }
  });
})();
