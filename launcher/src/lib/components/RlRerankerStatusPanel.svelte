<!--
  v0.2.40 H2 — RL Reranker status panel (wrapper component).

  Re-wires the orphan `RlRerankerDashboardWidget.svelte` (created v0.2.31
  Agent J but never mounted) into the RL module's config tab. Bundles:

    1. The existing dashboard widget (weights version + last training time,
       read live from the hub via `module_db_read_row`).
    2. A compact status block summarising the three persisted per-project
       flags (`rl_use_global`, `rl_online_training_disabled`,
       `rl_global_training_source_flag`), read via the v0.2.40 H2 getter
       Tauri commands counterpart to the existing setters.

  Mount site: `/modules/[id]/config/+page.svelte` renders this above the
  generic `ModuleConfigTab` when the active module is `vct-rl-reranker`.
  The schema-rendered controls below still own the read/write path for
  the flags — this panel only mirrors the current state so the user
  doesn't have to scroll to see "am I actually training right now?".

  Soft-fail design: any of the four reads (one hub read inside the
  widget, three getter calls here) may fail independently without
  collapsing the panel. Failed reads default to a placeholder line
  ("status unavailable") rather than blanking the whole component.

  Architecture note: we intentionally do NOT put this section into the
  module manifest as an `info_dynamic` control because the dashboard
  widget's hub-read code path is rl-specific (uses the `rl_weights_state`
  table by name); the info_dynamic kind is generic over modules. A
  future v0.2.41+ refactor may move this content into the manifest if
  the dashboard hub-read becomes a stable cross-module pattern.
-->

<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke, tauriAvailable } from '$lib/tauri';
  import RlRerankerDashboardWidget from './RlRerankerDashboardWidget.svelte';
  import { summarizeRlFlags } from '$lib/rl-settings-summary';

  let { projectId }: { projectId: string } = $props();

  // The three flags. `undefined` means "not yet loaded / read failed";
  // each call is independent so partial-success states render gracefully.
  let useGlobal = $state<boolean | undefined>(undefined);
  let onlineDisabled = $state<boolean | undefined>(undefined);
  let globalSource = $state<boolean | undefined>(undefined);

  let loading = $state(true);
  let loadError = $state<string | null>(null);

  async function loadFlags() {
    loading = true;
    loadError = null;
    if (!tauriAvailable() || !projectId) {
      // Browser mode / no project picked — render placeholders rather
      // than triggering Tauri errors.
      useGlobal = undefined;
      onlineDisabled = undefined;
      globalSource = undefined;
      loading = false;
      return;
    }
    try {
      // Three independent reads in parallel. Each failure path is
      // narrow: `get_bool_flag` defaults a missing row to `false` so
      // the only real error is a DB-open / IPC failure, both rare.
      const [a, b, c] = await Promise.all([
        invoke<boolean>('get_rl_use_global', { projectId }),
        invoke<boolean>('get_rl_online_training_disabled', { projectId }),
        invoke<boolean>('get_rl_global_training_source_flag', { projectId }),
      ]);
      useGlobal = a;
      onlineDisabled = b;
      globalSource = c;
    } catch (e) {
      // Soft-fail: render "status unavailable" copy below. NOT toasted —
      // this is a passive read-only panel, transient hub/db failures
      // shouldn't spam the user.
      const msg = e instanceof Error ? e.message : String(e);
      console.warn('[RlRerankerStatusPanel] flag load failed:', msg);
      loadError = msg;
    } finally {
      loading = false;
    }
  }

  // Re-load whenever `projectId` changes (the parent route may switch
  // active project without re-mounting the panel).
  $effect(() => {
    void loadFlags();
  });

  onMount(() => {
    void loadFlags();
  });

  const flagsReady = $derived(
    useGlobal !== undefined &&
      onlineDisabled !== undefined &&
      globalSource !== undefined,
  );

  const summary = $derived(
    flagsReady
      ? summarizeRlFlags(useGlobal!, onlineDisabled!, globalSource!)
      : null,
  );
</script>

<div class="rl-status-panel">
  <RlRerankerDashboardWidget {projectId} />

  <div class="flags-card">
    <h3>RL Reranker — training mode</h3>
    {#if loading && !flagsReady}
      <p class="loading">Loading…</p>
    {:else if loadError && !flagsReady}
      <p class="error">Status unavailable — see console for details.</p>
    {:else if summary}
      <dl class="flag-grid">
        <div class="row">
          <dt>Training</dt>
          <dd
            class="value training-{summary.trainingModeKey}"
            data-testid="rl-training-mode"
          >
            {summary.trainingMode}
          </dd>
        </div>
        <div class="row">
          <dt>Global corpus</dt>
          <dd
            class="value global-{summary.globalSourceKey}"
            data-testid="rl-global-source"
          >
            {summary.globalSource}
          </dd>
        </div>
      </dl>
      <p class="hint">
        Adjust these in the controls below. Changes apply immediately.
      </p>
    {/if}
    <!-- v0.2.91 (#23 USER rider, MC-11): the two rows above describe the
         LOCAL TRAINING pipeline's write mode — whether events update the
         local model. Neither says whether reranking is influencing search
         results, and the default flag state renders as "Online training
         active", from which a reader can reasonably infer that it is. It is
         not: no trained model has been produced yet, so results are
         unaffected regardless of these rows or the module's enable switch.
         State it here rather than leaving the inference standing. Delete
         this note when a trained model ships. -->
    <p class="dormant" data-testid="rl-reranking-dormant">
      <strong>Reranking:</strong> not live yet — no trained model has been produced,
      so search results are unaffected whatever these settings say. Event collection
      is separate and continues regardless.
    </p>
  </div>
</div>

<style>
  .rl-status-panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-bottom: 20px;
  }

  .flags-card {
    background: var(--color-bg-elev, rgba(255, 255, 255, 0.03));
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .flags-card h3 {
    margin: 0;
    font-size: 13px;
    font-weight: 600;
    color: var(--color-fg, #f3f4f6);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .loading,
  .error {
    margin: 0;
    color: var(--color-mid, #9ca3af);
    font-size: 13px;
    font-style: italic;
  }
  .error {
    color: #e74c3c;
    font-style: normal;
  }

  .flag-grid {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: 0;
    padding: 0;
  }

  .row {
    display: flex;
    align-items: baseline;
    gap: 12px;
    padding: 6px 0;
    border-bottom: 1px dashed rgba(255, 255, 255, 0.06);
  }
  .row:last-child {
    border-bottom: none;
  }

  dt {
    flex: 0 0 110px;
    font-size: 13px;
    color: var(--color-mid, #9ca3af);
  }

  dd {
    margin: 0;
    font-size: 13px;
    color: var(--color-fg, #f3f4f6);
  }

  /* Subtle status colouring — frozen/read-only are a bit muted, active
     is full-strength. No tone for the global-corpus row (binary opt-in
     state, no value judgement). */
  dd.training-local-active {
    color: var(--color-success, #00bfa6);
  }
  dd.training-read-only-global,
  dd.training-frozen,
  dd.training-frozen-and-global {
    color: var(--color-mid, #9ca3af);
  }

  .hint {
    margin: 4px 0 0 0;
    color: var(--color-mid, #9ca3af);
    font-size: 12px;
    font-style: italic;
  }
  /* v0.2.91: current-state note, not an error — quiet, with the purple rule
     the other "informational, nothing is broken" surfaces use. */
  .dormant {
    margin: 10px 0 0 0;
    padding-left: 8px;
    border-left: 2px solid var(--color-purple, #7b5fff);
    color: var(--color-mid, #9ca3af);
    font-size: 12px;
    line-height: 1.45;
  }
</style>
