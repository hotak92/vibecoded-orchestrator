<script lang="ts">
  // Stream 2 (2026-05-19): module-contributed config tab landing.
  //
  // URL: /modules/<module_id>/config
  //
  // Reads the URL's `[id]` param, fetches the module's full ConfigTab
  // schema via the same `get_module_nav_items` command the Sidebar
  // uses (no second-source — one source of truth for "what does this
  // tab look like"), and hands it off to ModuleConfigTab.svelte for
  // rendering.
  //
  // Soft-fail across the board: unknown module id, schema load
  // failure, or browser-mode runtime → friendly "not available" page
  // (NOT a crash).

  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { invoke, tauriAvailable } from '$lib/tauri';
  import ModuleConfigTab from '$lib/components/ModuleConfigTab.svelte';

  // The renderer (ModuleConfigTab.svelte) owns the canonical ConfigTab
  // type. We type the wire response as `unknown` here and hand it to
  // the renderer as `any` — the Rust manifest layer is the schema
  // source of truth, and TypeScript can't second-guess a JSON blob
  // crossing the IPC boundary. Re-declaring the discriminated union
  // here just doubles the maintenance burden.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  type ConfigTab = any;

  interface ModuleNavItem {
    module_id: string;
    title: string;
    icon: string | null;
    route: string;
    config_tab: ConfigTab;
  }

  const moduleId = $derived($page.params.id ?? '');

  let configTab = $state<ConfigTab | null>(null);
  let loadError = $state<string | null>(null);
  let loading = $state<boolean>(true);

  onMount(async () => {
    if (!tauriAvailable()) {
      loadError = 'Module configuration tabs require the desktop launcher.';
      loading = false;
      return;
    }
    try {
      const items = await invoke<ModuleNavItem[]>('get_module_nav_items');
      const match = items.find((m) => m.module_id === moduleId);
      if (!match) {
        loadError = `Module "${moduleId}" not found, or it does not contribute a config tab.`;
      } else {
        configTab = match.config_tab;
      }
    } catch (e) {
      loadError = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  });
</script>

<div class="page">
  {#if loading}
    <p class="loading">Loading…</p>
  {:else if loadError}
    <div class="error-card">
      <h2>Unable to load configuration</h2>
      <p>{loadError}</p>
    </div>
  {:else if configTab}
    <ModuleConfigTab configTab={configTab} {moduleId} />
  {/if}
</div>

<style>
  .page {
    height: 100%;
    overflow-y: auto;
  }
  .loading {
    padding: 32px;
    color: var(--color-muted);
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
    color: var(--color-text);
  }
</style>
