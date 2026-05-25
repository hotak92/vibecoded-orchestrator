<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // DevAffordanceToast — v0.2.33 catalog-bound dev hint.
  //
  // Surfaces exactly when `CatalogResponse.dev_affordance_hint` is
  // non-null (review §10.c, §J3-c). Backed by three preconditions
  // evaluated on the Rust side at every `list_module_catalog` call:
  //
  //   1. `<install_root>/paid-modules/` exists.
  //   2. `VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH` is unset (the user
  //      hasn't explicitly opted into dev passthrough).
  //   3. The user hasn't dismissed the toast (persisted in
  //      `launcher.db.app_state[module_catalog.dev_affordance_dismissed]`).
  //
  // Tells the module-developer "we see your dev paid-modules — set
  // the env var if you want them to render". Dismissal calls
  // `dismiss_dev_affordance_hint` Tauri command via the store (Agent
  // B's wire-up); subsequent catalog loads return
  // `dev_affordance_hint: null` and the toast stays hidden.
  //
  // Why not reuse `Toast.svelte`: that store auto-dismisses after 4
  // seconds. This affordance must persist until the user actively
  // dismisses (the env var instruction needs reading time + the
  // ability to copy the path). We render in the same
  // bottom-right region so the visual language stays consistent.

  import type { DevAffordanceHint } from '$lib/types/launcher';

  let {
    hint,
    onDismiss,
  }: {
    hint: DevAffordanceHint | null;
    onDismiss: () => Promise<void> | void;
  } = $props();

  let dismissing = $state(false);

  async function handleDismiss() {
    if (dismissing) return;
    dismissing = true;
    try {
      await onDismiss();
    } finally {
      dismissing = false;
    }
  }
</script>

{#if hint}
  <div class="dev-toast" role="status" aria-live="polite">
    <div class="head">
      <span class="title">Dev <code>paid-modules/</code> detected</span>
      <button
        type="button"
        class="close"
        onclick={handleDismiss}
        disabled={dismissing}
        aria-label="Dismiss this hint"
        title="Dismiss"
      >
        ×
      </button>
    </div>
    <p class="body">
      Found at <code class="path">{hint.paid_modules_path}</code>.
      Set <code>{hint.env_var_name}=1</code> to enable in the catalog.
    </p>
  </div>
{/if}

<style>
  .dev-toast {
    position: fixed;
    bottom: 16px;
    right: 16px;
    max-width: 380px;
    min-width: 280px;
    padding: 12px 14px;
    background: #1a1a22;
    border: 1px solid rgba(123, 95, 255, 0.40);
    border-radius: 8px;
    color: var(--color-text);
    font-size: 12px;
    line-height: 1.4;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
    animation: dev-toast-in 200ms ease-out;
    z-index: 2000;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
  }
  .title {
    flex: 1;
    font-weight: 600;
    color: var(--color-text);
  }
  .title code {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
    padding: 1px 4px;
    border-radius: 3px;
    background: rgba(255, 255, 255, 0.06);
    font-weight: 400;
  }
  .close {
    background: none;
    border: none;
    color: #888;
    cursor: pointer;
    font-size: 16px;
    line-height: 1;
    padding: 0 4px;
    flex-shrink: 0;
  }
  .close:hover:not(:disabled) {
    color: var(--color-text);
  }
  .close:disabled {
    opacity: 0.5;
    cursor: wait;
  }
  .body {
    margin: 0;
    color: var(--color-mid);
    word-break: break-word;
  }
  .body code {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
    padding: 1px 5px;
    border-radius: 3px;
    background: rgba(255, 255, 255, 0.06);
  }
  .body code.path {
    color: var(--color-teal);
    word-break: break-all;
  }
  @keyframes dev-toast-in {
    from {
      transform: translateY(8px);
      opacity: 0;
    }
    to {
      transform: translateY(0);
      opacity: 1;
    }
  }
</style>
