<!--
  SPDX-License-Identifier: AGPL-3.0-or-later
  v0.2.31 — module-deprecation banner (Layer 1, GUI).

  Renders a top-of-page amber banner when a module is deprecated. Wired into
  ModuleCatalog today; per-module dashboards can opt-in by importing this
  component and binding to their catalog entry (e.g. the RL Reranker
  dashboard at `/p/<slug>/rl-reranker` will pick it up in v0.2.32 polish).

  Dismissible per-session (a local boolean) — NOT per-install. We want the
  banner to re-surface next session until the user migrates (spec §
  "Dashboard widget" — re-surface every session until migration).
-->
<script lang="ts">
  import type { ModuleCatalogEntry } from '$lib/types/launcher';

  let { module: m }: { module: ModuleCatalogEntry } = $props();

  // Per-session dismiss state. Lives in component memory only — closed
  // and reopened next session by design (spec § Layer 1).
  let dismissed = $state(false);

  const visible = $derived(!!m.deprecated && !dismissed);
</script>

{#if visible}
  <div class="dep-banner" role="status" aria-live="polite">
    <div class="dep-icon" aria-hidden="true">⚠</div>
    <div class="dep-body">
      <div class="dep-title">
        Module <strong>{m.name}</strong> is deprecated
      </div>
      <div class="dep-message">
        {m.deprecation_message || 'The publisher has marked this module deprecated. It continues to work normally — plan a migration before end-of-life.'}
        {#if m.deprecation_eol_date}
          <span class="dep-meta">· EOL: {m.deprecation_eol_date}</span>
        {/if}
      </div>
      {#if m.deprecation_migration_url}
        <a
          class="dep-link"
          href={m.deprecation_migration_url}
          target="_blank"
          rel="noopener noreferrer"
        >Open migration guide →</a>
      {/if}
    </div>
    <button
      class="dep-dismiss"
      onclick={() => (dismissed = true)}
      aria-label="Dismiss deprecation banner"
      title="Dismiss for this session"
    >×</button>
  </div>
{/if}

<style>
  .dep-banner {
    display: flex;
    gap: 12px;
    align-items: flex-start;
    padding: 12px 14px;
    background: rgba(255, 159, 28, 0.08);
    border: 1px solid rgba(255, 159, 28, 0.30);
    border-radius: 10px;
    margin-bottom: 14px;
  }

  .dep-icon {
    font-size: 18px;
    color: rgb(255, 159, 28);
    line-height: 1;
    flex-shrink: 0;
    margin-top: 1px;
  }

  .dep-body {
    flex: 1;
    min-width: 0;
  }

  .dep-title {
    font-size: 13px;
    font-weight: 700;
    color: var(--color-text);
    margin-bottom: 3px;
  }

  .dep-message {
    font-size: 12px;
    color: var(--color-mid);
    line-height: 1.5;
  }

  .dep-meta {
    color: var(--color-muted);
    font-size: 11px;
    margin-left: 4px;
  }

  .dep-link {
    display: inline-block;
    margin-top: 6px;
    font-size: 11px;
    color: rgb(255, 159, 28);
    text-decoration: none;
    font-weight: 600;
  }
  .dep-link:hover {
    text-decoration: underline;
  }

  .dep-dismiss {
    background: transparent;
    border: 0;
    color: var(--color-muted);
    font-size: 18px;
    line-height: 1;
    cursor: pointer;
    padding: 2px 6px;
    margin: -2px -4px 0 0;
    flex-shrink: 0;
  }
  .dep-dismiss:hover {
    color: var(--color-text);
  }
</style>
