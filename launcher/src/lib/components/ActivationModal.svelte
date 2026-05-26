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
  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import {
    friendlyRebindMessage,
    shouldShowRebindButton,
  } from '$lib/admin-rebind';

  let { open = $bindable(false) }: { open: boolean } = $props();

  let code = $state('');
  let confirmingDeactivate = $state(false);
  // v0.2.36: transient toast for the admin-rebind affordance.
  // Kept local to the modal (vs the global store error) so the
  // success/error message stays scoped to the rebind interaction.
  let rebindToast = $state<{ kind: 'success' | 'error'; message: string } | null>(null);

  const viewState = $derived($license);
  const cache = $derived<TierCacheView | null>(viewState.cache);
  const tier = $derived(cache?.orchestrator_tier ?? 'free');
  const hasLicense = $derived(tier !== 'free');
  // v0.2.36: admin-tier card gates the "Rebind to this machine" button.
  // The predicate lives in `$lib/admin-rebind` so the visibility logic
  // has a single test surface in `admin-rebind.test.ts`.
  const showRebindButton = $derived(shouldShowRebindButton(tier));
  // v0.2.32 §D1: per-module license rows for the new section.
  const moduleLicenses = $derived(viewState.moduleLicenses ?? []);

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

  /**
   * v0.2.36: rebind the admin token to this machine.
   *
   * Closes the recovery gap surfaced in v0.2.35: when an admin
   * reinstalls their OS or swaps laptop, /validate-tier returns
   * `machine_mismatch` (TOFU binding still points at the old
   * machine_id_hash). The Rust command orchestrates the rebind so
   * the license_key never crosses the IPC boundary.
   *
   * Server-side errors surface verbatim ({error, detail}) to give
   * the user enough info to triage without re-opening logs.
   */
  async function handleRebind() {
    rebindToast = null;
    const result = await license.rebindAdminToken();
    rebindToast = friendlyRebindMessage(result);
  }

  // v0.2.32 §D1: per-module refresh / deactivate handlers.
  async function refreshModule(moduleId: string) {
    await license.refreshModule(moduleId);
  }

  async function deactivateModule(moduleId: string) {
    await license.deactivateModule(moduleId);
  }

  function handleClose() {
    open = false;
    license.clearError();
    code = '';
    confirmingDeactivate = false;
    rebindToast = null;
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

<!-- Bug 26: native <dialog> top-layer rendering via DialogRoot. -->
<DialogRoot bind:open onClose={handleClose}>
  {#snippet header()}
      <div class="modal-header-row">
        <h2>Orchestrator License</h2>
        <button class="modal-close" onclick={handleClose} aria-label="Close">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M18 6L6 18"/><path d="M6 6l12 12"/>
          </svg>
        </button>
      </div>
  {/snippet}
  {#snippet body()}
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
              placeholder="License key…"
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
            {#if showRebindButton}
              <!-- v0.2.36: admin-only "Rebind to this machine" affordance.
                   Closes the recovery gap when the admin's OS reinstall
                   leaves the Vault entry's machine_id_hash pinned to the
                   old machine — /validate-tier returns machine_mismatch
                   until the binding is updated. See the
                   `rebind-admin-token` edge function. -->
              <button
                class="btn-3d btn-3d-ghost btn-3d-sm"
                data-testid="rebind-admin-button"
                onclick={handleRebind}
                disabled={viewState.rebinding}
              >
                {viewState.rebinding ? 'Rebinding…' : 'Rebind to this machine'}
              </button>
            {/if}
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

        {#if rebindToast}
          <div
            class="msg"
            class:msg-error={rebindToast.kind === 'error'}
            class:msg-success={rebindToast.kind === 'success'}
            role="status"
          >
            {rebindToast.message}
          </div>
        {/if}

        {#if viewState.error}
          <div class="msg msg-error">{viewState.error}</div>
        {/if}

        <!-- v0.2.32 §D1: Per-module licenses section.
             Always rendered (with empty state) so users understand the
             distinction between orchestrator-tier coverage vs per-module
             overrides. Empty state copy ships even when no modules are
             active so a first-time user has the explanation handy. -->
        <section class="per-module-licenses">
          <h3>Per-module licenses</h3>
          {#if moduleLicenses.length === 0}
            <p class="empty">
              No per-module licenses active yet. Modules with Pro/MAO/Enterprise
              tier are unlocked by your orchestrator tier; this list shows
              individual module overrides.
            </p>
          {:else}
            <ul class="module-rows">
              {#each moduleLicenses as row (row.module_id)}
                <li class="module-row">
                  <div class="module-row-main">
                    <strong class="module-name">{row.display_name}</strong>
                    <span class="tier-badge">{row.tier}</span>
                  </div>
                  {#if row.activated_at}
                    <span class="activated-at mono">
                      Activated: {row.activated_at}
                    </span>
                  {/if}
                  <div class="module-row-actions">
                    <button
                      class="btn-3d btn-3d-ghost btn-3d-sm"
                      onclick={() => refreshModule(row.module_id)}
                      disabled={viewState.loading}
                    >
                      Refresh
                    </button>
                    <button
                      class="btn-3d btn-3d-ghost btn-3d-sm danger"
                      onclick={() => deactivateModule(row.module_id)}
                      disabled={viewState.loading}
                    >
                      Deactivate
                    </button>
                  </div>
                </li>
              {/each}
            </ul>
          {/if}
        </section>
  {/snippet}
</DialogRoot>

<style>
  /* Bug 26: backdrop / sizing now handled by DialogRoot. */
  .modal-header-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .modal-header-row h2 {
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

  /* v0.2.36: success toast for the admin-rebind affordance. Reuses the
     same teal accent the tier-card uses for the paid-license card. */
  .msg-success {
    background: rgba(0, 191, 166, 0.08);
    border: 1px solid rgba(0, 191, 166, 0.25);
    color: var(--color-text);
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

  /* v0.2.32 §D1: per-module licenses section. */
  .per-module-licenses {
    margin-top: 22px;
    padding-top: 18px;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
  }

  .per-module-licenses h3 {
    font-size: 13px;
    font-weight: 700;
    color: var(--color-text);
    margin-bottom: 10px;
  }

  .per-module-licenses .empty {
    font-size: 12px;
    color: var(--color-mid);
    line-height: 1.5;
    margin: 0;
  }

  .module-rows {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .module-row {
    padding: 12px 14px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 10px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .module-row-main {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  .module-name {
    font-size: 13px;
    color: var(--color-text);
  }

  .tier-badge {
    display: inline-block;
    padding: 2px 8px;
    background: rgba(0, 191, 166, 0.12);
    border: 1px solid rgba(0, 191, 166, 0.3);
    border-radius: 6px;
    font-size: 11px;
    color: var(--color-text);
    text-transform: capitalize;
    font-weight: 600;
  }

  .activated-at {
    font-size: 11px;
    color: var(--color-mid);
  }

  .module-row-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
</style>
