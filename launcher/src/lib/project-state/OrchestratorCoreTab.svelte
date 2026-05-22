<!--
  SPDX-License-Identifier: AGPL-3.0-or-later

  v0.2.23 F2 (2026-05-21): the orchestrator-root project's "special"
  Settings tab. Renders the existing `ModuleConfigTab` schema renderer
  with the orchestrator-core's `gui.config_tab` from the repo-root
  `vct-module.json`.

  Why this lives in `project-state/` (alongside AgentsTab, SettingsTab,
  …) rather than as a sidebar landing page: the user explicitly said
  "treat the orchestrator root as a special project", with the
  Orchestrator Core controls surfacing as one more tab in the standard
  per-project tab strip when (and only when) the active project's host
  is `orchestrator_root`. The previous standalone "Orchestrator Core"
  sidebar entry (rendered via the dynamic `get_module_nav_items` group)
  is suppressed by the manifest's new `show_in_sidebar: false` flag —
  the route `/modules/orchestrator/config` still works as a deep link
  but no longer appears in the sidebar nav.

  Reuse principle: this component does NOT reimplement any of the
  orchestrator-core controls. The schema (sections, buttons, info
  banners, Tauri command names) lives in `vct-module.json`. The
  rendering logic lives in `ModuleConfigTab.svelte`. The six Tauri
  commands in `commands/orchestrator_core.rs` are untouched. All this
  component does is wire those three together for the per-project
  Settings surface.

  Soft-fail: a missing manifest, a parse error, or a browser-mode
  runtime all yield a friendly placeholder (NOT a crash). Mirrors the
  fallback shape used by `routes/modules/[id]/config/+page.svelte`.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke, tauriAvailable } from '$lib/tauri';
  import ModuleConfigTab from '$lib/components/ModuleConfigTab.svelte';

  // Same `unknown`-typed contract that `routes/modules/[id]/config`
  // uses: the Rust manifest layer is the schema source of truth, so
  // re-declaring the discriminated union in TS doubles the
  // maintenance surface for no benefit. `ModuleConfigTab` owns the
  // canonical typing on the rendering side.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  type ConfigTab = any;

  interface ModuleNavItem {
    module_id: string;
    title: string;
    icon: string | null;
    route: string;
    config_tab: ConfigTab;
  }

  // The orchestrator-core module id is hard-pinned in `vct-module.json`
  // (`"id": "orchestrator"`). The test
  // `orchestrator_core_manifest_with_gui_tab_deserializes` enforces
  // this; if it ever changes here we'd diverge from the manifest.
  const ORCHESTRATOR_MODULE_ID = 'orchestrator';

  let configTab = $state<ConfigTab | null>(null);
  let loadError = $state<string | null>(null);
  let loading = $state<boolean>(true);

  async function load() {
    if (!tauriAvailable()) {
      loadError = 'The Clone integrity tab requires the desktop launcher.';
      loading = false;
      return;
    }
    try {
      const items = await invoke<ModuleNavItem[]>('get_module_nav_items');
      const match = items.find((m) => m.module_id === ORCHESTRATOR_MODULE_ID);
      if (!match) {
        loadError =
          'Orchestrator-core manifest not found. The repo-root ' +
          '`vct-module.json` should declare `gui.config_tab`; if you ' +
          "see this in a normal install something's gone wrong with " +
          'manifest discovery — check `manifest_scan_paths()` in ' +
          '`launcher/src-tauri/src/commands/module_gui.rs`.';
      } else {
        configTab = match.config_tab;
      }
    } catch (e) {
      loadError = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  onMount(load);
</script>

<div class="orch-core-tab">
  {#if loading}
    <p class="loading">Loading…</p>
  {:else if loadError}
    <div class="error-card">
      <h2>Unable to load the Clone integrity controls</h2>
      <p>{loadError}</p>
    </div>
  {:else if configTab}
    <!-- `moduleId="orchestrator"` is the same id the manifest declares
         and the same key under which `module_settings` rows would land
         if any of these controls were stateful. Today the six controls
         are buttons + info banners (stateless), so the persistence
         path is dormant — but wiring the right id now means a future
         stateful control (e.g. a "default KG tier threshold" select)
         drops in without a second refactor. -->
    <ModuleConfigTab configTab={configTab} moduleId={ORCHESTRATOR_MODULE_ID} />
  {/if}
</div>

<style>
  .orch-core-tab {
    /* The renderer (ModuleConfigTab.svelte) already centres at 920px and
       owns its own padding; this wrapper just provides a scroll context
       consistent with the other per-project tabs. */
    width: 100%;
  }
  .loading {
    padding: 32px;
    color: var(--color-muted, #888);
    font-size: 13px;
  }
  .error-card {
    margin: 32px auto;
    max-width: 560px;
    padding: 20px 24px;
    border-radius: 10px;
    border: 1px solid rgba(231, 76, 60, 0.30);
    background: rgba(231, 76, 60, 0.06);
    color: #e74c3c;
  }
  .error-card h2 {
    margin: 0 0 8px 0;
    font-size: 16px;
  }
  .error-card p {
    margin: 0;
    font-size: 13px;
    color: var(--color-text, #e8e8ee);
  }
</style>
