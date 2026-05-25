<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // L0StatusIndicator — v0.2.33 catalog freshness surface.
  //
  // Renders one of three states based on `CatalogResponse.l0_status`:
  //
  //   * `kind: 'ok'`          → no indicator (default happy-path).
  //   * `kind: 'stale'`       → quiet grey one-liner with the cached
  //                             timestamp + last error. Catalog still
  //                             works (cached values served).
  //   * `kind: 'unavailable'` → prominent yellow banner with Retry CTA.
  //                             Paid modules disappear from the
  //                             catalog until the connection is back.
  //
  // The Retry button calls `refresh_module_catalog` (registered in
  // v0.2.33 by Agent A) via the parent component's callback so the
  // store updates atomically with the rest of the catalog state.
  //
  // Design note: this lives between the Modules-tab title block and
  // the filter pills so the user reads "tab title → catalog state
  // → controls" top-to-bottom. The stale variant uses a thin neutral
  // line (the cache is still serving real data); the unavailable
  // variant uses the same amber palette as `ManifestParseErrorBanner`
  // because both signal "something went wrong with the catalog
  // fetch" even though the root cause differs.

  import type { L0Status } from '$lib/types/launcher';

  let {
    status,
    onRetry,
    retrying,
  }: {
    status: L0Status | null;
    onRetry: () => Promise<void> | void;
    retrying: boolean;
  } = $props();

  /** Render an ISO-8601 timestamp as a short relative string ("3m ago"). */
  function relativeTime(iso: string): string {
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return iso;
    const deltaMs = Date.now() - t;
    if (deltaMs < 0) return 'just now';
    const sec = Math.floor(deltaMs / 1000);
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr}h ago`;
    const day = Math.floor(hr / 24);
    return `${day}d ago`;
  }
</script>

{#if status?.kind === 'stale'}
  <div class="l0-stale" role="status" aria-live="polite">
    <span class="dot" aria-hidden="true">●</span>
    <span class="text">
      Catalog cached {relativeTime(status.cached_fetched_at)} ago —
      fetch failed: <span class="mono">{status.last_error}</span>
    </span>
  </div>
{:else if status?.kind === 'unavailable'}
  <div class="l0-unavailable" role="alert">
    <span class="icon" aria-hidden="true">⚠</span>
    <span class="message">
      Couldn't reach catalog server. Paid modules unavailable until
      connection restored.
      <span class="error-detail">({status.error})</span>
    </span>
    <button
      type="button"
      class="retry-btn"
      onclick={() => onRetry()}
      disabled={retrying}
    >
      {retrying ? 'Retrying…' : 'Retry'}
    </button>
  </div>
{/if}

<style>
  .l0-stale {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0 0 12px 0;
    padding: 6px 10px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
    color: var(--color-muted);
    font-size: 11px;
    line-height: 1.4;
  }
  .l0-stale .dot {
    font-size: 8px;
    color: var(--color-muted);
  }
  .l0-stale .text {
    flex: 1;
  }

  .l0-unavailable {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 0 0 16px 0;
    padding: 10px 14px;
    background: rgba(241, 196, 15, 0.12);
    border: 1px solid rgba(241, 196, 15, 0.36);
    border-radius: 10px;
    color: #f1c40f;
    font-size: 13px;
    line-height: 1.4;
  }
  .l0-unavailable .icon {
    font-size: 16px;
    line-height: 1;
    flex-shrink: 0;
  }
  .l0-unavailable .message {
    flex: 1;
  }
  .l0-unavailable .error-detail {
    margin-left: 4px;
    color: rgba(241, 196, 15, 0.8);
    font-size: 11px;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }
  .retry-btn {
    background: rgba(241, 196, 15, 0.18);
    color: #f1c40f;
    border: 1px solid rgba(241, 196, 15, 0.45);
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    padding: 4px 12px;
    cursor: pointer;
    flex-shrink: 0;
  }
  .retry-btn:hover:not(:disabled) {
    background: rgba(241, 196, 15, 0.28);
  }
  .retry-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .mono {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
  }
</style>
