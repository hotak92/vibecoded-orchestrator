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
  // v0.2.40 H2: re-wire the orphan dashboard widget. Mounted only for
  // the RL Reranker module — other modules show only the generic
  // schema-rendered config tab. See `RlRerankerStatusPanel.svelte`.
  import RlRerankerStatusPanel from '$lib/components/RlRerankerStatusPanel.svelte';
  import { selectedProject } from '$lib/stores/projects';
  import { modules } from '$lib/stores/modules';
  // v0.2.42 W6 (UX-1): paid-modules-agnostic gate. The status panel +
  // Reset button are only shown when the RL module is both installed AND
  // its container is running.
  import { moduleIsActive, RL_RERANKER_MODULE_ID } from '$lib/module-active-gate';

  const RL_MODULE_ID = RL_RERANKER_MODULE_ID;

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
  // v0.2.40 H2: status panel binds to the globally selected project.
  // Renders placeholders ("—") when no project is picked yet.
  const activeProjectId = $derived($selectedProject?.id ?? '');

  // v0.2.42 W6 (UX-1): show RL-specific UI only when the module is
  // both installed AND its container is actively running. This keeps
  // the launcher agnostic about paid modules — Store/Modules tabs stay
  // fully browsable, but operational controls are hidden when the module
  // isn't running (no inference available, flags have no effect).
  const rlIsActive = $derived(moduleIsActive(RL_MODULE_ID, $modules.installed));
  const showStatusPanel = $derived(moduleId === RL_MODULE_ID && rlIsActive);

  let configTab = $state<ConfigTab | null>(null);
  let loadError = $state<string | null>(null);
  let loading = $state<boolean>(true);

  // v0.2.42 RT-4: "Reset to global weights" button state.
  // Wires the Tauri command registered by W3 (`reset_weights_to_global`).
  // Button is only rendered when the RL module is active (rlIsActive gate).
  let resettingWeights = $state(false);
  let resetWeightsError = $state<string | null>(null);

  async function handleResetWeights() {
    if (!tauriAvailable() || !activeProjectId) return;
    resettingWeights = true;
    resetWeightsError = null;
    try {
      await invoke('reset_weights_to_global', { projectId: activeProjectId });
    } catch (e) {
      resetWeightsError = e instanceof Error ? e.message : String(e);
    } finally {
      resettingWeights = false;
    }
  }

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
    {#if showStatusPanel}
      <!-- v0.2.40 H2: dashboard widget + flag-state summary above the
           schema-rendered controls. Re-wires the v0.2.31 orphan widget.
           v0.2.42 W6 (UX-1): only rendered when RL module is running. -->
      <div class="status-panel-wrapper">
        <RlRerankerStatusPanel projectId={activeProjectId} />

        <!-- v0.2.42 RT-4: Reset to global weights. Calls the Tauri command
             registered by W3. Gated by rlIsActive (same gate as this block). -->
        <div class="weights-reset-row">
          <button
            class="weights-reset-btn"
            disabled={resettingWeights}
            title="Discard project-specific fine-tuning and revert to the current global weights model."
            onclick={() => void handleResetWeights()}
          >
            {resettingWeights ? 'Resetting…' : 'Reset to global weights'}
          </button>
          {#if resetWeightsError}
            <p class="weights-reset-error" role="alert">{resetWeightsError}</p>
          {/if}
        </div>
      </div>
    {/if}
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
    border: 1px solid rgba(231, 76, 60, 0.3);
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
  /* v0.2.40 H2: keeps the status panel padded the same as the tab body
     beneath it. ModuleConfigTab.svelte has its own .tab padding so we
     can't share — we mirror the 24px gutter instead. */
  .status-panel-wrapper {
    padding: 24px 24px 0 24px;
    max-width: 920px;
    margin: 0 auto;
  }

  /* v0.2.42 RT-4: Reset weights row sits below the status panel. */
  .weights-reset-row {
    margin-top: 12px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .weights-reset-btn {
    align-self: flex-start;
    padding: 6px 14px;
    border-radius: 6px;
    border: 1px solid var(--color-border, #2a2f3a);
    background: var(--color-bg, #11141c);
    color: var(--color-fg, #e8edf6);
    font-size: 13px;
    cursor: pointer;
  }

  .weights-reset-btn:hover:not(:disabled) {
    border-color: var(--color-teal, #00bfa6);
    color: var(--color-teal, #00bfa6);
  }

  .weights-reset-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .weights-reset-error {
    margin: 0;
    font-size: 12px;
    color: #ff8a8a;
  }
</style>
