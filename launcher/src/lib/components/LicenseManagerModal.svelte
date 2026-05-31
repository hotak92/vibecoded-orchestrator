<script lang="ts">
  // v0.2.40 L1: per-paid-module license keys.
  //
  // Surfaces the multi-key licensing model in the launcher GUI. Each
  // paid module (RL Reranker, MAO, future agent packs) gets its own
  // row with key input + Validate button + status badge. The legacy
  // single-key orchestrator-root slot is rendered at the top as
  // "Orchestrator tier (root)" for users who want to manage it from
  // this modal too (it's also still managed via the legacy
  // ActivationModal — both surfaces write to the same underlying
  // keychain entry through the `__orchestrator__` reserved slot).
  //
  // Composition pattern: `DialogRoot` + `bind:open` (same shape as
  // `ActivationModal.svelte`). Per the A3 collision audit
  // (`.claude/context/reviews/v0240-pre-push-2026-05-30/discovery-A3-fabio-branch-collision-audit.md`)
  // we use the `showLicenseManager` store flag (NOT `showLicense`,
  // `showModal`, or `showKeyManager`) so Fabio's parallel
  // orchestrator-update-progress modal can land without a rebase
  // conflict on `stores/ui.ts`.

  import { onMount } from 'svelte';
  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import { moduleLicenseKeys, formatTimestamp, statusBadge } from '$lib/stores/moduleLicenseKeys';
  import type { LicenseKeySummary } from '$lib/types/launcher';

  let { onClose }: { onClose: () => void } = $props();

  let open = $state(true);
  // v0.2.41 CI-gate hotfix: renamed from `state` to `summary` because
  // svelte-check resolved `$state<...>` (the rune) as an auto-subscribe
  // to a store named `state`, producing 6 false-positive type errors at
  // every `$state<Generic>(...)` site below. Renaming the derived-store
  // alias removes the symbol collision.
  const summary = $derived($moduleLicenseKeys);

  // Per-row key inputs. Keyed by module_id so each row has its own
  // independent textbox state. Initialised lazily on first render.
  let pendingKeys = $state<Record<string, string>>({});

  // Per-row last validation result. Shown inline under the row's
  // status badge so the user gets immediate feedback after clicking
  // Validate (instead of having to reason about the broader `error`
  // field, which only fires for definitive failures).
  let lastResults = $state<
    Record<string, { tier: string; valid: boolean; stale: boolean; error: string | null }>
  >({});

  onMount(() => {
    moduleLicenseKeys.load();
  });

  function pendingFor(moduleId: string): string {
    return pendingKeys[moduleId] ?? '';
  }

  function setPending(moduleId: string, value: string) {
    pendingKeys = { ...pendingKeys, [moduleId]: value };
  }

  async function onSaveAndValidate(moduleId: string) {
    const key = pendingFor(moduleId).trim();
    if (!key) return;
    const ok = await moduleLicenseKeys.setKey(moduleId, key);
    if (!ok) return;
    // Clear the textbox so the user can't accidentally re-submit the
    // same key. The redacted form is now visible in the row's
    // "Current key" column.
    setPending(moduleId, '');
    const result = await moduleLicenseKeys.validate(moduleId);
    if (result) {
      lastResults = {
        ...lastResults,
        [moduleId]: {
          tier: result.tier,
          valid: result.valid,
          stale: result.stale,
          error: result.error,
        },
      };
    }
  }

  async function onValidateExisting(moduleId: string) {
    const result = await moduleLicenseKeys.validate(moduleId);
    if (result) {
      lastResults = {
        ...lastResults,
        [moduleId]: {
          tier: result.tier,
          valid: result.valid,
          stale: result.stale,
          error: result.error,
        },
      };
    }
  }

  async function onClear(moduleId: string) {
    const row = summary.keys.find((k) => k.module_id === moduleId);
    if (!row) return;
    const label = row.display_name;
    // Confirm before destroying the key. Using native confirm() because
    // the launcher's confirm-modal UX is heavyweight and this is a
    // one-shot user-initiated action.
    if (!window.confirm(`Remove license key for ${label}?`)) return;
    await moduleLicenseKeys.clear(moduleId);
    // Drop the per-row result if present.
    if (lastResults[moduleId]) {
      const { [moduleId]: _removed, ...rest } = lastResults;
      lastResults = rest;
    }
  }

  function handleClose() {
    open = false;
    onClose();
  }

  $effect(() => {
    if (!open) onClose();
  });

  function rowBusy(row: LicenseKeySummary): boolean {
    return summary.busyModuleId === row.module_id;
  }
</script>

<DialogRoot
  bind:open
  onClose={handleClose}
  width="720px"
  ariaLabelledBy="license-manager-title"
>
  {#snippet header()}
    <h2 id="license-manager-title">License keys</h2>
    <p class="subtitle">
      Per-paid-module license keys. Each paid module — RL Reranker, MAO,
      Specialist Agent Packs, etc. — owns its own key, validation cycle,
      and tier in the cache. Activating a key for one module does not
      touch the others.
    </p>
  {/snippet}

  {#snippet body()}
    {#if summary.loading && summary.keys.length === 0}
      <p class="loading">Loading license keys…</p>
    {:else if summary.keys.length === 0}
      <p class="empty">
        No paid-module license keys yet. Activate one through the
        Orchestrator tier dialog, or paste a key below once you have one.
      </p>
    {/if}

    {#if summary.error}
      <div class="error" role="alert">
        {summary.error}
        <button class="link" onclick={() => moduleLicenseKeys.clearError()}
          >Dismiss</button
        >
      </div>
    {/if}

    <ul class="key-list">
      {#each summary.keys as row (row.module_id)}
        {@const badge = statusBadge(row)}
        {@const lastResult = lastResults[row.module_id]}
        <li class="key-row">
          <header class="row-header">
            <div class="row-title">
              <strong>{row.display_name}</strong>
              <code class="module-id">{row.module_id}</code>
            </div>
            <span
              class="badge"
              data-severity={badge.severity}
              aria-label="Status: {badge.label}"
            >
              {badge.label}
            </span>
          </header>

          <div class="row-meta">
            <div>
              <span class="label">Current key:</span>
              <code>{row.redacted_key}</code>
            </div>
            <div>
              <span class="label">Last validated:</span>
              {formatTimestamp(row.validated_at)}
            </div>
          </div>

          {#if row.last_validation_error}
            <p class="row-error">
              Last error: {row.last_validation_error}
            </p>
          {/if}

          {#if lastResult}
            <p class="row-result" data-severity={lastResult.valid ? 'ok' : 'err'}>
              {#if lastResult.valid}
                Validated: tier = <strong>{lastResult.tier}</strong>
              {:else if lastResult.stale}
                Cached state in use ({lastResult.tier}) — {lastResult.error}
              {:else}
                Validation failed — {lastResult.error}
              {/if}
            </p>
          {/if}

          <div class="row-actions">
            <input
              type="password"
              autocomplete="off"
              spellcheck="false"
              placeholder="Paste a new key to rotate"
              value={pendingFor(row.module_id)}
              oninput={(e) =>
                setPending(row.module_id, (e.currentTarget as HTMLInputElement).value)}
              disabled={rowBusy(row)}
            />
            <button
              class="primary"
              onclick={() => onSaveAndValidate(row.module_id)}
              disabled={rowBusy(row) || !pendingFor(row.module_id).trim()}
            >
              Save &amp; Validate
            </button>
            <button
              onclick={() => onValidateExisting(row.module_id)}
              disabled={rowBusy(row)}
            >
              Re-validate
            </button>
            <button
              class="danger"
              onclick={() => onClear(row.module_id)}
              disabled={rowBusy(row)}
            >
              Remove
            </button>
          </div>
        </li>
      {/each}
    </ul>
  {/snippet}

  {#snippet footer()}
    <button onclick={handleClose}>Close</button>
  {/snippet}
</DialogRoot>

<style>
  .subtitle {
    color: var(--color-mid);
    font-size: 13px;
    margin: 4px 0 0;
  }

  .loading,
  .empty {
    color: var(--color-mid);
    text-align: center;
    margin: 24px 0;
  }

  .error {
    background: var(--color-err-bg, #3b1d1d);
    color: var(--color-err-fg, #ffb4b4);
    padding: 8px 12px;
    border-radius: 6px;
    margin-bottom: 12px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }
  .link {
    background: none;
    border: none;
    color: inherit;
    text-decoration: underline;
    cursor: pointer;
    padding: 0;
  }

  .key-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .key-row {
    border: 1px solid var(--color-border, #2a2f3a);
    border-radius: 8px;
    padding: 12px 14px;
    background: var(--color-bg-elev, #1c2030);
  }

  .row-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 8px;
  }

  .row-title strong {
    font-size: 15px;
  }
  .row-title .module-id {
    color: var(--color-mid);
    font-size: 11px;
    margin-left: 8px;
  }

  .badge {
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
    background: var(--color-bg, #11141c);
  }
  .badge[data-severity='ok'] {
    background: rgba(0, 191, 166, 0.18);
    color: #6fe0c8;
  }
  .badge[data-severity='warn'] {
    background: rgba(255, 196, 0, 0.18);
    color: #ffd863;
  }
  .badge[data-severity='err'] {
    background: rgba(255, 82, 82, 0.18);
    color: #ff8a8a;
  }
  .badge[data-severity='neutral'] {
    background: rgba(150, 150, 150, 0.15);
    color: var(--color-mid);
  }

  .row-meta {
    display: flex;
    gap: 24px;
    font-size: 12px;
    color: var(--color-mid);
    margin-bottom: 6px;
  }
  .row-meta .label {
    color: var(--color-fg-soft, #aab0c0);
    margin-right: 4px;
  }

  .row-error {
    color: #ff8a8a;
    font-size: 12px;
    margin: 4px 0;
  }
  .row-result {
    font-size: 12px;
    margin: 4px 0;
  }
  .row-result[data-severity='ok'] {
    color: #6fe0c8;
  }
  .row-result[data-severity='err'] {
    color: #ff8a8a;
  }

  .row-actions {
    display: flex;
    gap: 6px;
    margin-top: 8px;
    align-items: center;
  }
  .row-actions input[type='password'] {
    flex: 1;
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid var(--color-border, #2a2f3a);
    background: var(--color-bg, #11141c);
    color: var(--color-fg, #e8edf6);
    font-family: var(--font-mono, monospace);
  }
  .row-actions button {
    padding: 6px 12px;
    border-radius: 6px;
    border: 1px solid var(--color-border, #2a2f3a);
    background: var(--color-bg, #11141c);
    color: var(--color-fg, #e8edf6);
    cursor: pointer;
    font-size: 12px;
  }
  .row-actions button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .row-actions button.primary {
    background: var(--color-teal, #00bfa6);
    color: var(--color-bg, #11141c);
    border-color: transparent;
    font-weight: 600;
  }
  .row-actions button.danger:not(:disabled):hover {
    border-color: #ff8a8a;
    color: #ff8a8a;
  }
</style>
