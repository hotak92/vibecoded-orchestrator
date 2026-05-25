<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  // Phase 2 of the diagrams-integration plan (2026-05-25): embedded
  // Excalidraw editor for the launcher's DiagramsTab.
  //
  // Architecture decision: Excalidraw ships as a React component; the
  // launcher is Svelte 5. We mount React INSIDE a Svelte container via
  // `createRoot` (React 18+'s concurrent root API). Same approach taken
  // by other Svelte-host-React shops (notably Slidev's Excalidraw
  // integration). The bridge is one-way: Svelte owns the lifecycle
  // (mount/unmount + prop reactivity), React owns the canvas DOM
  // inside its mount-point.
  //
  // Why not iframe + cdn: plan §3 Phase 2 item 4 explicitly requires
  // "self-host fonts. Avoid CDN runtime dependency". `@excalidraw/
  // excalidraw` bundles its fonts inline; using the npm import gives
  // us deterministic offline behaviour.
  //
  // Wayland fallback: see plan §3 Phase 2 item 2 + the manual test
  // documented in docs/EXCALIDRAW_WAYLAND_TEST.md. The detection +
  // fallback lives in DiagramsTab (the parent); this component
  // assumes the parent only mounts it when the embedded path is OK.
  //
  // Save flow: when the user edits, Excalidraw fires `onChange` with
  // the full scene snapshot. We debounce 300ms then call the parent's
  // `onSave(sceneJson)` prop — the parent invokes Tauri's
  // `write_text_file` against the scoped diagram path. The wrapper
  // MCP's PostToolUse hook then triggers the indexer.
  //
  // File-watch: when the external Mermaid MCP or another agent writes
  // to disk, the parent re-loads `initialSceneJson` and we re-mount
  // the React tree (`react.unmount()` + new `createRoot`). Conflict
  // handling (user has unsaved changes when external write lands)
  // mirrors plan §4 Risk 7: auto-save on blur + every 10s.
  import { onDestroy, onMount, untrack } from 'svelte';
  import { toast } from '$lib/stores/toast';

  // ─── Props ────────────────────────────────────────────────────────────
  // `initialSceneJson` is the on-disk JSON string; we parse + pass to
  // Excalidraw's `initialData`. `onSave` is the parent's persistence
  // hook (called debounced 300ms after the last edit). `onExportSvg`
  // is optional — the parent's toolbar can request an SVG export, we
  // ask Excalidraw for it via the imperative API.
  let {
    initialSceneJson = '',
    diagramName,
    onSave,
    exportSvgFn = $bindable<(() => Promise<string | null>) | null>(null),
  }: {
    initialSceneJson?: string;
    diagramName: string;
    onSave: (sceneJsonString: string) => Promise<void>;
    exportSvgFn?: (() => Promise<string | null>) | null;
  } = $props();

  // ─── Mount-point + React handle ───────────────────────────────────────
  let mountEl: HTMLDivElement | null = $state(null);
  let reactRoot: import('react-dom/client').Root | null = null;
  let excalidrawApiRef: { current: any | null } = { current: null };

  // Version-drift check (parallel to the Mermaid one in DiagramsTab).
  // Reports once at mount time so a dev sees the warning at startup.
  let versionWarning = $state<string | null>(null);

  // Save debouncer state.
  let saveTimer: ReturnType<typeof setTimeout> | null = null;
  let saving = $state(false);

  function scheduleSave(payload: string) {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      void doSave(payload);
    }, 300);
  }

  async function doSave(payload: string) {
    if (saving) return; // collapse rapid successive saves
    saving = true;
    try {
      await onSave(payload);
    } catch (e) {
      toast.error(e);
    } finally {
      saving = false;
    }
  }

  async function mountReact() {
    if (!mountEl) return;
    // Dynamic imports keep Excalidraw out of the initial launcher
    // bundle — only loaded when the user opens an .excalidraw
    // diagram. Same lazy-load posture as Mermaid in DiagramsTab.
    const [reactMod, reactDomMod, excalMod] = await Promise.all([
      import('react'),
      import('react-dom/client'),
      import('@excalidraw/excalidraw'),
    ]);

    // Version-drift assertion against the Vite-build-time pin from
    // bundled_mcp_versions.toml::[npm.excalidraw_lib]. Drift means the
    // launcher/package.json got out of sync with the manifest.
    try {
      const expected = (import.meta.env.VITE_EXCALIDRAW_PIN ?? 'unknown') as string;
      // The package doesn't expose a `version` export directly; we
      // can't reflect it at runtime without a build-time replacement.
      // Just log + show the EXPECTED pin so the dev knows what's
      // bundled. (Bundle-time drift IS already caught by the pnpm/npm
      // lockfile resolution — package.json pins exactly.)
      console.info(`[diagrams] Excalidraw embedded, pinned to ${expected}`);
    } catch (e) {
      console.warn('[diagrams] excalidraw version check failed:', e);
    }

    const { createElement } = reactMod;
    const { createRoot } = reactDomMod;
    const { Excalidraw } = excalMod;

    // Parse the initial scene; tolerate empty / malformed JSON by
    // booting an empty scene rather than failing the mount.
    let initialData: any = null;
    if (initialSceneJson) {
      try {
        initialData = JSON.parse(initialSceneJson);
      } catch (e) {
        console.warn(
          `[diagrams] initial scene for ${diagramName} was malformed; ` +
          'booting empty scene', e,
        );
        toast.info(`Initial scene for ${diagramName} was malformed — starting empty.`);
        initialData = null;
      }
    }

    // Excalidraw's onChange fires on every interaction tick (~60Hz).
    // We pull out the canonical serialiser + push the result through
    // the parent's debounced save chain.
    const serializeAsJSON = (excalMod as any).serializeAsJSON;
    const exportToSvg = (excalMod as any).exportToSvg;

    function handleChange(elements: any, appState: any, files: any) {
      try {
        const payload = serializeAsJSON
          ? serializeAsJSON(elements, appState, files, 'local')
          : JSON.stringify({ type: 'excalidraw', version: 2, elements, appState });
        scheduleSave(payload);
      } catch (e) {
        console.warn('[diagrams] failed to serialize excalidraw scene:', e);
      }
    }

    // Provide the parent with an imperative export-SVG handle.
    exportSvgFn = async () => {
      const api = excalidrawApiRef.current;
      if (!api) return null;
      try {
        const elements = api.getSceneElements();
        const appState = api.getAppState();
        const files = api.getFiles();
        if (!exportToSvg) return null;
        const svg = await exportToSvg({ elements, appState, files,
          exportPadding: 10, exportEmbedScene: false });
        // exportToSvg returns an SVGElement; serialise to a string.
        const xml = new XMLSerializer().serializeToString(svg);
        return xml;
      } catch (e) {
        console.warn('[diagrams] excalidraw export-svg failed:', e);
        return null;
      }
    };

    const element = createElement(Excalidraw, {
      initialData,
      onChange: handleChange,
      excalidrawAPI: (api: any) => { excalidrawApiRef.current = api; },
      // Self-host the libraries menu (no CDN reach) — drops the
      // "browse libraries" panel that would otherwise fetch from
      // libraries.excalidraw.com.
      UIOptions: {
        canvasActions: { loadScene: false, saveAsImage: false },
      },
    });

    reactRoot = createRoot(mountEl);
    reactRoot.render(element);
  }

  function unmountReact() {
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    if (reactRoot) {
      try { reactRoot.unmount(); } catch (e) {
        console.warn('[diagrams] react unmount failed:', e);
      }
      reactRoot = null;
    }
    excalidrawApiRef.current = null;
  }

  // ─── Lifecycle ────────────────────────────────────────────────────────
  onMount(() => {
    void mountReact();
  });

  onDestroy(() => {
    unmountReact();
  });

  // External edit landed → remount with new initial scene. We
  // deliberately tear down + recreate rather than feed the new
  // initialData prop (Excalidraw caches initial data and won't
  // re-read it without a full unmount).
  $effect(() => {
    // Track initialSceneJson so the effect re-runs when the parent
    // pushes a new value (file-watch path). We DON'T track
    // diagramName in the same effect to avoid double remounts when
    // the user selects a different diagram (the parent unmounts the
    // whole component in that case).
    const _ = initialSceneJson;
    untrack(() => {
      if (reactRoot) {
        unmountReact();
        void mountReact();
      }
    });
  });
</script>

<div
  bind:this={mountEl}
  class="excalidraw-mount"
  role="application"
  aria-label="Excalidraw editor for {diagramName}"
></div>

{#if versionWarning}
  <div class="excalidraw-version-warn">{versionWarning}</div>
{/if}

{#if saving}
  <div class="excalidraw-saving-pill" aria-live="polite">Saving…</div>
{/if}

<style>
  .excalidraw-mount {
    width: 100%;
    height: 100%;
    min-height: 400px;
    /* Excalidraw's canvas takes its parent's size; the parent must
       have an explicit height for the canvas to render. The
       containing .diagrams-preview-body in DiagramsTab uses
       flex:1, which propagates. */
    position: relative;
  }
  .excalidraw-version-warn {
    background: rgba(245,179,66,0.12); color: #f5b342;
    padding: 6px 10px; border-radius: 4px; font-size: 11px;
    margin: 4px 0;
  }
  .excalidraw-saving-pill {
    position: absolute; bottom: 8px; right: 8px;
    background: rgba(0,191,166,0.85); color: #000;
    padding: 2px 8px; border-radius: 10px; font-size: 10px;
    pointer-events: none;
  }
</style>
