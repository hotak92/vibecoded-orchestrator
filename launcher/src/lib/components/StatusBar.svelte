<script lang="ts">
  import { onMount } from 'svelte';
  import { currentUser } from '$lib/stores/auth';
  import { tauriAvailable } from '$lib/tauri';
  import {
    MODE_PILL_TOOLTIP,
    clearPanelDefaultModel,
    describeModeReport,
    describeModeResult,
    describeSwitchOutcome,
    endpointWarning,
    gatewayIsLive,
    getModelGatewayStatus,
    getPanelMode,
    listVSCodeTargets,
    modeSwitchDisabledReason,
    multimodelPillLabel,
    setPanelMode,
    startModelGateway,
    switchToMultimodel,
    vendorDefaultWarning,
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
  //
  // v0.2.94, from the 2026-09-08 incident, three changes here:
  //   * The Multimodel pill no longer sits disabled behind "start the
  //     gateway once (Services page)". It STARTS the gateway and then
  //     points — including from a prototype endpoint, which is the whole
  //     migration path off the pre-product gateway.
  //   * A stopped gateway is named in the pill's own label, with a Start
  //     action beside it, instead of surfacing as a token-file error after
  //     the click.
  //   * A vendor ANTHROPIC_MODEL already in the file is shown as a warning
  //     with a one-click Clear, because THAT value — not the picker — is
  //     what a panel restart resumes on.

  let gw = $state<ModelGatewayStatus | null>(null);
  let targets = $state<VSCodeTarget[]>([]);
  let selected = $state('');
  let report = $state<PanelModeReport | null>(null);
  let loadError = $state<string | null>(null);
  let busy = $state(false);
  let notice = $state<{ tone: 'ok' | 'err'; text: string } | null>(null);

  const mode = $derived(describeModeReport(report));
  const disabledMulti = $derived(
    modeSwitchDisabledReason('multimodel', gw, targets, report),
  );
  const disabledRemote = $derived(
    modeSwitchDisabledReason('remote-control', gw, targets, report),
  );
  const neutralTooltip = $derived(
    loadError ? `Panel mode unavailable: ${loadError}` : mode.tooltip,
  );
  // Two independent readings (review R2-3): `gateway` is the VCO gateway on
  // this machine — it drives the pill label and the Start action — while
  // `endpoint` is whatever THIS panel currently talks to, which starting a
  // gateway does not fix and which therefore only ever produces a warning.
  const multiLabel = $derived(multimodelPillLabel(report));
  const gatewayStopped = $derived(report?.gateway === 'stopped');
  const vendorDefault = $derived(vendorDefaultWarning(report));
  const endpointDown = $derived(endpointWarning(report));

  function pillTitle(target: SettablePanelMode, disabledReason: string): string {
    if (busy) return 'Applying…';
    if (disabledReason) return disabledReason;
    const base = MODE_PILL_TOOLTIP[target];
    if (target === 'multimodel' && gatewayStopped) {
      return `${base} The gateway is not running; clicking this starts it first.`;
    }
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
    // Review R2 #7: a focus/visibility probe landing during apply() must not
    // race apply's own re-probe and leave the pill on the old mode.
    if (busy) return;
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
      if (target === 'multimodel') {
        // Starts the gateway when it is not running, points, and retries
        // once on the token-file race a fresh start can lose. Every one of
        // those decisions lives in `$lib/api/model_gateway` so the vitest
        // can reach it; this file only awaits it.
        notice = describeSwitchOutcome(await switchToMultimodel(selected, report));
      } else {
        const result = await setPanelMode(selected, target);
        notice = { tone: result.ok ? 'ok' : 'err', text: describeModeResult(result) };
      }
    } catch (e) {
      notice = { tone: 'err', text: String(e) };
    } finally {
      busy = false;
    }
    try {
      await refresh();
    } catch (e) {
      loadError = String(e);
    }
  }

  /** Start the gateway from the frame, then re-probe. */
  async function startGateway() {
    if (busy) return;
    busy = true;
    try {
      const status = await startModelGateway();
      gw = status;
      // Only claim it started when it ANSWERED (review R1-7): the Rust side
      // polls /health for up to 5 s and reports what it found, so a status
      // that is still not reachable means the daemon did not come up.
      notice = gatewayIsLive(status)
        ? { tone: 'ok', text: `Model gateway started on port ${status.port}.` }
        : {
            tone: 'err',
            text: `The model gateway was asked to start on port ${status.port} but is not answering: ${
              status.health_error ?? 'no reason given'
            }. Check the Services page.`,
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

  /** Remove ANTHROPIC_MODEL — that key only, with a backup. */
  async function clearDefault() {
    if (busy || !selected) return;
    busy = true;
    try {
      const result = await clearPanelDefaultModel(selected);
      notice = { tone: result.ok ? 'ok' : 'err', text: result.message };
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
    // frame stays mounted. Re-probe when the window regains focus or becomes
    // visible (and after every apply, see setMode) instead of polling: each
    // probe spawns a python process, and a 20 s timer in a frame that is
    // always mounted is a python spawn every 20 s for the app's lifetime.
    const onFocus = () => void refresh();
    const onVisible = () => {
      if (document.visibilityState === 'visible') void refresh();
    };
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisible);
    };
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
          {multiLabel}
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
      {#if gatewayStopped}
        <button
          type="button"
          class="mode-action"
          title="Start the local model gateway now, then re-check."
          disabled={busy}
          onclick={startGateway}
        >
          Start gateway
        </button>
      {/if}
      {#if endpointDown}
        <span class="mode-warning" role="status">
          <span class="mode-notice-text" title={endpointDown}>{endpointDown}</span>
        </span>
      {/if}
      {#if vendorDefault}
        <span class="mode-warning" role="status">
          <span class="mode-notice-text" title={vendorDefault}>{vendorDefault}</span>
          <button
            type="button"
            class="mode-action"
            title="Remove ANTHROPIC_MODEL from the settings file. Nothing else is touched."
            disabled={busy}
            onclick={clearDefault}
          >
            Clear default
          </button>
        </span>
      {/if}
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

  /* Inline action beside the switch (Start gateway / Clear default). */
  .mode-action {
    height: 18px;
    padding: 0 9px;
    border-radius: 999px;
    border: 1px solid var(--color-teal);
    background: transparent;
    color: var(--color-teal);
    font: inherit;
    font-size: 11px;
    font-weight: 600;
    line-height: 1;
    white-space: nowrap;
    cursor: pointer;
    transition:
      background 0.2s ease,
      color 0.2s ease;
  }

  .mode-action:hover:not(:disabled) {
    background: var(--color-teal);
    color: var(--color-bg);
  }

  .mode-action:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }

  /* Vendor-Default warning: pink tint, the brand's error/highlight accent. */
  .mode-warning {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    max-width: 42vw;
    height: 22px;
    padding: 0 4px 0 10px;
    border-radius: 999px;
    border: 1px solid rgba(var(--color-pink-rgb), 0.4);
    background: rgba(var(--color-pink-rgb), 0.1);
    color: var(--color-text);
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
