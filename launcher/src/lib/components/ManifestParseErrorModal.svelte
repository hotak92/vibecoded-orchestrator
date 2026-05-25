<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // ManifestParseErrorModal — detail view for L9 parse-error banner.
  //
  // v0.2.33 (Agent E, 2026-05-25): rendered when the user clicks the
  // `ManifestParseErrorBanner`. Lists every failure with module_id,
  // source (file path or `L0:<endpoint>`), and the underlying error
  // text exactly as captured by `list_module_catalog_impl_with_l0`'s
  // `parse_errors` accumulator.
  //
  // Reload button: triggers `modules.loadCatalog()` (passed in via
  // `onReload`). If the malformed manifest has been replaced /
  // re-published since the user opened the banner, the new round-trip
  // resolves the errors and the modal closes.
  //
  // Layout: uses `DialogRoot` (the same native-`<dialog>` top-layer
  // wrapper every other launcher modal uses). Backdrop / esc-close /
  // sizing handled there.

  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import type { ManifestParseError } from '$lib/types/launcher';

  let {
    open,
    errors,
    onClose,
    onReload,
  }: {
    open: boolean;
    errors: ManifestParseError[];
    onClose: () => void;
    onReload: () => Promise<void> | void;
  } = $props();

  let reloading = $state(false);

  async function handleReload() {
    if (reloading) return;
    reloading = true;
    try {
      await onReload();
    } finally {
      reloading = false;
    }
  }

  function moduleLabel(e: ManifestParseError): string {
    // Empty module_id can happen for L0-envelope failures (no parsed
    // module id available) and for dev-paid-modules manifests that
    // failed parse before their `id` field was read. Fall back to
    // the source path so the user has a stable string to grep on.
    if (e.module_id && e.module_id.length > 0) return e.module_id;
    return e.source;
  }

  function sourceLabel(e: ManifestParseError): string {
    if (e.source.startsWith('L0:')) {
      return `L0 endpoint: ${e.source.slice(3)}`;
    }
    return `File: ${e.source}`;
  }
</script>

{#if open}
  <DialogRoot open={true} width="640px" onClose={onClose}>
    {#snippet header()}
      <h2 class="modal-title">
        Manifest parse errors
        <span class="count-pill">{errors.length}</span>
      </h2>
    {/snippet}
    {#snippet body()}
      <p class="modal-intro">
        The launcher couldn't parse the following module
        manifest{errors.length === 1 ? '' : 's'}. The affected
        module{errors.length === 1 ? '' : 's'} fall back to the catalog
        builtin (where available) or are surfaced with a synthetic
        placeholder. Fix the manifest at the source then click
        <strong>Reload</strong> to retry.
      </p>

      <ul class="error-list">
        {#each errors as err (err.source)}
          <li class="error-item">
            <div class="error-row">
              <span class="error-key">Module</span>
              <span class="error-value">{moduleLabel(err)}</span>
            </div>
            <div class="error-row">
              <span class="error-key">Source</span>
              <span class="error-value mono">{sourceLabel(err)}</span>
            </div>
            <div class="error-row">
              <span class="error-key">Error</span>
              <pre class="error-detail">{err.error}</pre>
            </div>
          </li>
        {/each}
      </ul>

      <p class="modal-foot">
        Each failure is also appended to
        <code>state/logs/launcher_errors.jsonl</code>
        (or <code>~/.vct/launcher_errors.jsonl</code> as a fallback)
        for postmortem.
      </p>

      <div class="modal-actions">
        <button
          type="button"
          class="btn-3d btn-3d-ghost btn-3d-sm"
          onclick={onClose}
        >
          Close
        </button>
        <button
          type="button"
          class="btn-3d btn-3d-primary btn-3d-sm"
          onclick={handleReload}
          disabled={reloading}
        >
          {reloading ? 'Reloading…' : 'Reload'}
        </button>
      </div>
    {/snippet}
  </DialogRoot>
{/if}

<style>
  .modal-title {
    margin: 0;
    font-size: 16px;
    font-weight: 700;
    color: var(--color-text);
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .count-pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 22px;
    height: 22px;
    padding: 0 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 700;
    background: rgba(241, 196, 15, 0.18);
    color: #f1c40f;
    border: 1px solid rgba(241, 196, 15, 0.30);
  }
  .modal-intro {
    font-size: 13px;
    color: var(--color-mid);
    line-height: 1.5;
    margin: 0 0 12px 0;
  }
  .error-list {
    list-style: none;
    margin: 0 0 12px 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-height: 360px;
    overflow-y: auto;
  }
  .error-item {
    padding: 10px 12px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .error-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    font-size: 12px;
  }
  .error-key {
    flex-shrink: 0;
    width: 64px;
    color: var(--color-muted);
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 0.5px;
    font-weight: 700;
  }
  .error-value {
    flex: 1;
    color: var(--color-text);
    word-break: break-word;
  }
  .mono {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
  }
  .error-detail {
    flex: 1;
    margin: 0;
    padding: 8px 10px;
    background: rgba(0, 0, 0, 0.25);
    border-radius: 6px;
    color: #f1c40f;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .modal-foot {
    font-size: 11px;
    color: var(--color-muted);
    line-height: 1.5;
    margin: 0 0 14px 0;
  }
  .modal-foot code {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 10px;
    padding: 1px 5px;
    border-radius: 3px;
    background: rgba(255, 255, 255, 0.06);
  }
  .modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
</style>
