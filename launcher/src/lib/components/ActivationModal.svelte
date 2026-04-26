<script lang="ts">
  // Activation modal — wired to Tauri license commands.
  //
  // Flow:
  //   1. license.load() on mount → reads cached tier from launcher.db
  //   2. activate() → secrets::set(VIBECODED_LICENSE_KEY) + license_refresh()
  //   3. refresh() → POST /validate-tier with current key
  //   4. deactivate() → secrets::delete + tier_cache→free
  //
  // The legacy localStorage `licenses` store is still wired in the rest of
  // the app (per-app activation for Transcrypt, Arzillibus, etc.). This
  // modal is specifically for the *orchestrator tier* license that gates
  // paid orchestrator modules.

  import { onMount } from 'svelte';
  import { license, formatGrace } from '$lib/stores/license';
  import type { TierCacheView } from '$lib/types/launcher';

  let { open = $bindable(false) }: { open: boolean } = $props();

  let code = $state('');
  let confirmingDeactivate = $state(false);

  const viewState = $derived($license);
  const cache = $derived<TierCacheView | null>(viewState.cache);
  const tier = $derived(cache?.orchestrator_tier ?? 'free');
  const hasLicense = $derived(tier !== 'free');

  onMount(() => {
    license.load();
  });

  async function handleActivate() {
    if (!code.trim()) return;
    const ok = await license.activate(code.trim());
    if (ok) code = '';
  }

  async function handleRefresh() {
    await license.refresh();
  }

  async function handleDeactivate() {
    await license.deactivate();
    confirmingDeactivate = false;
  }

  function handleClose() {
    open = false;
    license.clearError();
    code = '';
    confirmingDeactivate = false;
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') handleClose();
  }

  function fmtTimestamp(ms: number): string {
    if (!ms) return 'never';
    return new Date(ms).toLocaleString();
  }
</script>

<svelte:window onkeydown={open ? handleKeydown : undefined} />

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-backdrop" onclick={handleClose} onkeydown={() => {}}>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="modal-content" onclick={(e) => e.stopPropagation()} onkeydown={() => {}}>
      <div class="modal-header">
        <h2>Orchestrator License</h2>
        <button class="modal-close" onclick={handleClose} aria-label="Close">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M18 6L6 18"/><path d="M6 6l12 12"/>
          </svg>
        </button>
      </div>

      <div class="modal-body">
        <!-- Current tier -->
        <div class="tier-card" class:tier-card-paid={hasLicense}>
          <div class="tier-row">
            <span class="tier-label">Current tier</span>
            <span class="tier-value">{tier}</span>
          </div>
          {#if cache}
            <div class="tier-row">
              <span class="tier-label">Last validated</span>
              <span class="tier-value mono">
                {fmtTimestamp(cache.last_validated)}
              </span>
            </div>
            {#if cache.grace_period_remaining_ms !== null}
              <div class="tier-row">
                <span class="tier-label">Offline grace</span>
                <span class="tier-value mono">
                  {formatGrace(cache.grace_period_remaining_ms)}
                </span>
              </div>
            {/if}
            {#if cache.last_error}
              <div class="tier-row tier-row-error">
                <span class="tier-label">Last error</span>
                <span class="tier-value">{cache.last_error}</span>
              </div>
            {/if}
          {/if}
        </div>

        {#if !hasLicense}
          <p class="modal-desc">
            Enter your activation code to unlock Pro features.
          </p>
          <div class="activate-form">
            <input
              type="text"
              bind:value={code}
              placeholder="XXXX-XXXX-XXXX-XXXX"
              class="activate-input"
              onkeydown={(e) => { if (e.key === 'Enter') handleActivate(); }}
            />
            <button
              class="btn-3d btn-3d-primary"
              onclick={handleActivate}
              disabled={viewState.activating || !code.trim()}
            >
              {#if viewState.activating}
                <span class="spinner"></span>
              {:else}
                Activate
              {/if}
            </button>
          </div>
        {:else}
          <div class="actions-row">
            <button
              class="btn-3d btn-3d-ghost btn-3d-sm"
              onclick={handleRefresh}
              disabled={viewState.loading}
            >
              {viewState.loading ? 'Refreshing…' : 'Refresh'}
            </button>
            {#if !confirmingDeactivate}
              <button
                class="btn-3d btn-3d-ghost btn-3d-sm danger"
                onclick={() => (confirmingDeactivate = true)}
              >
                Deactivate
              </button>
            {:else}
              <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={() => (confirmingDeactivate = false)}>
                Cancel
              </button>
              <button class="btn-3d btn-3d-accent btn-3d-sm" onclick={handleDeactivate}>
                Confirm deactivate
              </button>
            {/if}
          </div>
        {/if}

        {#if viewState.error}
          <div class="msg msg-error">{viewState.error}</div>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    backdrop-filter: blur(8px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 300;
    animation: fade-in 0.15s ease-out;
    padding: 16px;
    overflow-y: auto;
  }

  @keyframes fade-in {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  .modal-content {
    width: 480px;
    max-width: 90vw;
    max-height: 80vh;
    overflow-y: auto;
    background: rgba(13, 23, 53, 0.97);
    backdrop-filter: blur(24px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 20px;
    box-shadow: 0 30px 80px rgba(0, 0, 0, 0.6);
    animation: modal-enter 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
  }

  @keyframes modal-enter {
    from { opacity: 0; transform: scale(0.95) translateY(10px); }
    to { opacity: 1; transform: scale(1) translateY(0); }
  }

  .modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 20px 24px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  }

  .modal-header h2 {
    font-size: 16px;
    font-weight: 700;
    color: var(--color-text);
  }

  .modal-close {
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 1px solid transparent;
    border-radius: 8px;
    color: var(--color-mid);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .modal-close:hover {
    color: var(--color-text);
    background: rgba(255, 255, 255, 0.06);
    border-color: rgba(255, 255, 255, 0.08);
  }

  .modal-body {
    padding: 22px 24px;
  }

  .modal-desc {
    font-size: 13px;
    color: var(--color-mid);
    margin-bottom: 14px;
  }

  .tier-card {
    padding: 14px 16px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 12px;
    margin-bottom: 18px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .tier-card-paid {
    background: rgba(0, 191, 166, 0.06);
    border-color: rgba(0, 191, 166, 0.2);
  }

  .tier-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 12px;
  }

  .tier-row-error .tier-value {
    color: var(--color-pink);
  }

  .tier-label {
    color: var(--color-mid);
  }

  .tier-value {
    color: var(--color-text);
    font-weight: 600;
    text-transform: capitalize;
  }

  .mono {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
    text-transform: none;
  }

  .activate-form {
    display: flex;
    gap: 10px;
  }

  .activate-input {
    flex: 1;
    padding: 10px 16px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 12px;
    color: var(--color-text);
    font-size: 14px;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    letter-spacing: 1px;
    outline: none;
    transition: border-color 0.2s ease;
  }

  .activate-input::placeholder {
    color: var(--color-muted);
    letter-spacing: 2px;
  }

  .activate-input:focus {
    border-color: rgba(0, 191, 166, 0.5);
    box-shadow: 0 0 0 3px rgba(0, 191, 166, 0.1);
  }

  .actions-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }

  .danger {
    color: var(--color-pink);
  }
  .danger:hover {
    border-color: var(--color-pink) !important;
  }

  .msg {
    margin-top: 14px;
    padding: 10px 14px;
    border-radius: 10px;
    font-size: 13px;
  }

  .msg-error {
    background: rgba(255, 79, 160, 0.1);
    border: 1px solid rgba(255, 79, 160, 0.25);
    color: var(--color-pink);
  }

  .spinner {
    display: inline-block;
    width: 16px;
    height: 16px;
    border: 2px solid rgba(5, 11, 31, 0.3);
    border-top-color: var(--color-bg);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }
</style>
