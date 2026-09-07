<script lang="ts">
  import { onMount } from 'svelte';
  import { currentUser } from '$lib/stores/auth';
  import { tauriAvailable } from '$lib/tauri';
  import {
    MODE_PILL_TOOLTIP,
    describeMode,
    describeModeResult,
    getModelGatewayStatus,
    getPanelMode,
    listVSCodeTargets,
    modeSwitchDisabledReason,
    setPanelMode,
  } from '$lib/api/model_gateway';
  import type { PanelModeReport, SettablePanelMode } from '$lib/api/model_gateway';
  import type { ModelGatewayStatus, VSCodeTarget } from '$lib/types/model-gateway';

  let appCount = $derived($currentUser?.apps?.length ?? 0);

  // v0.2.43 (contributor branch feat/launcher-logo-circular-white):
  // version string moved to the right-sidebar brand footer
  // (RightSidebar.svelte `.rs-brand-footer`) so the statusbar
  // is no longer a duplicate display surface.
  // v0.2.91 (P2-B1): the "Connected" dot was never wired to any
  // real connectivity signal (no Weaviate/Ollama/hub check backed
  // it) and always rendered, even when those services were down.
  // Removed rather than wired to an unverified claim — StatusBar
  // now shows only the bound app-count.
  //
  // v0.2.93: the Multimodel <-> Remote Control switch. Claude Code's
  // Remote Control is endpoint-gated (>= 2.1.196 refuses it whenever
  // ANTHROPIC_BASE_URL is not api.anthropic.com) and the extension's env
  // block is VS Code machine-scope, so the user has ONE of {gateway
  // picker, Remote Control} at a time, machine-wide. That is why the
  // switch lives here — in the frame, on every page — and not on the
  // Services page. Every decision (what to strip, stash, restore) is the
  // Python writer's (`python -m vco_lib.vscode_settings mode`); the copy
  // and the disabled-reasons live in `$lib/api/model_gateway.ts` so the
  // vitest can reach them. What stays here is markup and wiring.
  //
  // The write is immediate. The user restarts VS Code themselves — the
  // notice says so and stays until dismissed; nothing here automates it.

  let gw = $state<ModelGatewayStatus | null>(null);
  let targets = $state<VSCodeTarget[]>([]);
  let selected = $state('');
  let report = $state<PanelModeReport | null>(null);
  let loadError = $state<string | null>(null);
  let busy = $state(false);
  let notice = $state<{ tone: 'ok' | 'err'; text: string } | null>(null);

  const mode = $derived(describeMode(report?.mode ?? null));
  const disabledMulti = $derived(
    modeSwitchDisabledReason('multimodel', gw, targets, report),
  );
  const disabledRemote = $derived(
    modeSwitchDisabledReason('remote-control', gw, targets, report),
  );
  const neutralTooltip = $derived(
    loadError ? `Panel mode unavailable: ${loadError}` : mode.tooltip,
  );

  function pillTitle(target: SettablePanelMode, disabledReason: string): string {
    if (busy) return 'Applying…';
    if (disabledReason) return disabledReason;
    const base = MODE_PILL_TOOLTIP[target];
    return mode.active === target ? `${base} (current)` : base;
  }

  async function refreshMode() {
    if (!selected) {
      report = null;
      return;
    }
    report = await getPanelMode(selected);
  }

  async function refresh() {
    try {
      gw = await getModelGatewayStatus();
    } catch (e) {
      // The card on the Services page reports this; the frame only needs
      // the Multimodel pill to stay disabled with a reason.
      gw = null;
      loadError = String(e);
    }
    try {
      targets = await listVSCodeTargets();
      if (!targets.some((t) => t.path === selected)) {
        selected = targets[0]?.path ?? '';
      }
      await refreshMode();
      loadError = null;
    } catch (e) {
      report = null;
      loadError = String(e);
    }
  }

  async function apply(target: SettablePanelMode) {
    if (busy || !selected) return;
    if (mode.active === target) return;
    busy = true;
    try {
      const result = await setPanelMode(selected, target);
      notice = {
        tone: result.ok ? 'ok' : 'err',
        text: describeModeResult(result),
      };
    } catch (e) {
      notice = { tone: 'err', text: String(e) };
    } finally {
      busy = false;
    }
    try {
      await refreshMode();
    } catch (e) {
      loadError = String(e);
    }
  }

  async function onTargetChange() {
    notice = null;
    try {
      await refreshMode();
    } catch (e) {
      report = null;
      loadError = String(e);
    }
  }

  onMount(() => {
    if (!tauriAvailable()) return;
    void refresh();
    // The gateway can be started/stopped from the Services page while this
    // frame stays mounted; a slow poll keeps the disabled-reason honest.
    const timer = setInterval(() => void refresh(), 20_000);
    return () => clearInterval(timer);
  });
</script>

<footer class="status-bar">
  <div class="status-right">
    {#if tauriAvailable()}
      <div class="mode-switch" role="group" aria-label="Claude Code panel mode">
        {#if targets.length > 1}
          <select
            class="mode-target"
            bind:value={selected}
            onchange={onTargetChange}
            title="Which editor's settings.json the switch writes"
            disabled={busy}
          >
            {#each targets as t (t.path)}
              <option value={t.path}>{t.display_name}</option>
            {/each}
          </select>
        {/if}
        <button
          type="button"
          class="mode-pill"
          class:active={mode.active === 'multimodel'}
          title={pillTitle('multimodel', disabledMulti)}
          aria-pressed={mode.active === 'multimodel'}
          disabled={busy || !!disabledMulti}
          onclick={() => apply('multimodel')}
        >
          Multimodel
        </button>
        <button
          type="button"
          class="mode-pill"
          class:active={mode.active === 'remote-control'}
          title={pillTitle('remote-control', disabledRemote)}
          aria-pressed={mode.active === 'remote-control'}
          disabled={busy || !!disabledRemote}
          onclick={() => apply('remote-control')}
        >
          Remote Control
        </button>
        {#if mode.active === null}
          <span class="mode-neutral" title={neutralTooltip}>{mode.label}</span>
        {/if}
      </div>
      {#if notice}
        <span class="mode-notice" class:err={notice.tone === 'err'} role="status">
          <span class="mode-notice-text" title={notice.text}>{notice.text}</span>
          <button
            type="button"
            class="mode-dismiss"
            aria-label="Dismiss"
            title="Dismiss"
            onclick={() => (notice = null)}
          >
            ×
          </button>
        </span>
      {/if}
    {/if}
    <span>{appCount} app{appCount !== 1 ? 's' : ''} activated</span>
  </div>
</footer>

<style>
  .status-bar {
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 16px;
    background: rgba(5, 11, 31, 0.9);
    border-top: 1px solid rgba(255, 255, 255, 0.04);
    flex-shrink: 0;
    font-size: 11px;
    color: var(--color-muted);
  }

  .status-right {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  /* Segmented control: two pills in one rounded track, ≤ 22px tall. */
  .mode-switch {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    padding: 2px;
    border-radius: 999px;
    background: var(--color-card);
    border: 1px solid var(--color-border);
  }

  .mode-pill {
    height: 18px;
    padding: 0 9px;
    border-radius: 999px;
    border: 1px solid transparent;
    background: transparent;
    color: var(--color-mid);
    font: inherit;
    font-size: 11px;
    font-weight: 600;
    line-height: 1;
    cursor: pointer;
    transition:
      background 0.2s ease,
      color 0.2s ease,
      border-color 0.2s ease;
  }

  .mode-pill:hover:not(:disabled):not(.active) {
    border-color: var(--color-teal);
    color: var(--color-text);
  }

  .mode-pill.active {
    background: var(--color-teal);
    border-color: var(--color-teal);
    color: var(--color-bg);
  }

  .mode-pill:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }

  .mode-pill.active:disabled {
    opacity: 1;
  }

  .mode-neutral {
    padding: 0 6px;
    color: var(--color-muted);
    font-style: italic;
    white-space: nowrap;
  }

  .mode-target {
    height: 18px;
    padding: 0 4px;
    border-radius: 999px;
    border: 1px solid var(--color-border);
    background: var(--color-bg2);
    color: var(--color-mid);
    font: inherit;
    font-size: 11px;
  }

  /* Persistent notice — teal tint on success, pink tint on a refusal. */
  .mode-notice {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    max-width: 46vw;
    height: 22px;
    padding: 0 4px 0 10px;
    border-radius: 999px;
    border: 1px solid rgba(var(--color-teal-rgb), 0.35);
    background: rgba(var(--color-teal-rgb), 0.1);
    color: var(--color-text);
  }

  .mode-notice.err {
    border-color: rgba(255, 79, 160, 0.4);
    background: rgba(255, 79, 160, 0.1);
  }

  .mode-notice-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .mode-dismiss {
    width: 16px;
    height: 16px;
    border: 0;
    border-radius: 999px;
    background: transparent;
    color: inherit;
    font-size: 13px;
    line-height: 1;
    cursor: pointer;
  }

  .mode-dismiss:hover {
    background: var(--color-border);
  }
</style>
