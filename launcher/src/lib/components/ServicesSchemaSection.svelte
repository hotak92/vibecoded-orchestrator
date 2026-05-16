<script lang="ts">
  // PR-37 (v0.2.12 / 2026-05-16): Services-page schema-health section.
  //
  // Mounts on /services under the per-service action table. Surfaces:
  //   - Schema-correctness status badge (green/yellow/red).
  //   - "Run schema migrations…" button that opens the consent-gated
  //     SchemaMigrationModal. The button is disabled (with a clear
  //     "Weaviate not reachable" hint) when the status reader's
  //     `weaviate_reachable=false` — running migration scripts against
  //     an offline Weaviate is a no-op but produces noisy logs, so we
  //     gate it at the UI.
  //
  // The badge is informational only — clicking it does NOT auto-open
  // the modal. User must explicitly click the "Run…" button. Mirrors
  // the consent-first pattern from PR-8's LegacyCollectionsModal entry
  // point on the Identity tab.

  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import SchemaMigrationModal from '$lib/components/SchemaMigrationModal.svelte';

  interface DevelopmentCollectionSchema {
    class_name: string;
    temporal_props_present: string[];
    temporal_props_missing: string[];
  }
  interface SchemaMigrationStatusReport {
    weaviate_reachable: boolean;
    weaviate_url: string;
    development_collections: DevelopmentCollectionSchema[];
    shared_kg_class: string;
    shared_kg_exists: boolean | null;
    shared_kg_index_null_state: boolean | null;
    badge: string;
  }

  let status = $state<SchemaMigrationStatusReport | null>(null);
  let loading = $state(true);
  let showModal = $state(false);

  async function refresh() {
    loading = true;
    try {
      status = await invoke<SchemaMigrationStatusReport>('schema_migration_status');
    } catch (e) {
      toast.error(e);
      status = null;
    } finally {
      loading = false;
    }
  }

  async function onCompleted() {
    showModal = false;
    await refresh();
  }

  onMount(refresh);

  const devNeedsMigration = $derived(
    (status?.development_collections ?? []).some((c) => c.temporal_props_missing.length > 0),
  );
  const sharedNeedsMigration = $derived(
    status?.shared_kg_exists === true && status?.shared_kg_index_null_state === false,
  );
</script>

<section class="sss-section">
  <header class="sss-header">
    <h2>Schema health</h2>
    <p class="sss-sub">
      Weaviate collection schema correctness. Migrations are
      non-destructive on already-correct schemas — safe to re-run.
    </p>
  </header>

  <article class="sss-card">
    <header class="sss-card-h">
      <span class="sss-badge sss-badge-{status?.badge ?? 'gray'}">
        {#if loading}
          checking…
        {:else if !status}
          unknown
        {:else if status.badge === 'green'}
          OK
        {:else if status.badge === 'yellow' && !status.weaviate_reachable}
          unreachable
        {:else if status.badge === 'yellow'}
          migration needed
        {:else}
          error
        {/if}
      </span>
      <strong>Collection schemas</strong>
      <button
        class="sss-btn"
        onclick={() => (showModal = true)}
        disabled={loading || !status || !status.weaviate_reachable}
      >
        Run schema migrations…
      </button>
    </header>

    {#if status}
      <p class="sss-meta">
        Weaviate: <code>{status.weaviate_url}</code>
        {#if !status.weaviate_reachable}
          <span class="sss-tag sss-tag-warn">
            Weaviate not reachable — try restarting containers
          </span>
        {/if}
      </p>

      {#if status.weaviate_reachable}
        <!-- Development collections grid: per-class temporal prop status. -->
        <div class="sss-subsection">
          <h4>
            <code>*_Development</code> collections
            <span class="sss-subhead-count">
              ({status.development_collections.length})
            </span>
          </h4>
          {#if status.development_collections.length === 0}
            <p class="sss-empty">
              No <code>*_Development</code> collections found. The
              <code>development</code> collection family is created on
              first <code>docs/**/*.md</code> sync.
            </p>
          {:else}
            <ul class="sss-dev-list">
              {#each status.development_collections as c (c.class_name)}
                <li
                  class="sss-dev-row"
                  class:sss-dev-incomplete={c.temporal_props_missing.length > 0}
                >
                  <code class="sss-dev-name">{c.class_name}</code>
                  <span class="sss-dev-pct">
                    {c.temporal_props_present.length} / 4 temporal props
                  </span>
                  {#if c.temporal_props_missing.length > 0}
                    <span class="sss-tag sss-tag-warn">
                      missing: {c.temporal_props_missing.join(', ')}
                    </span>
                  {:else}
                    <span class="sss-tag sss-tag-ok">complete</span>
                  {/if}
                </li>
              {/each}
            </ul>
          {/if}
        </div>

        <!-- Shared KG schema status. -->
        <div class="sss-subsection">
          <h4>Shared KG class</h4>
          <p class="sss-meta">
            class: <code>{status.shared_kg_class}</code>
          </p>
          {#if status.shared_kg_exists === false}
            <p class="sss-meta">
              <span class="sss-tag">not yet created</span>
              The seed step creates it with the correct schema.
            </p>
          {:else if status.shared_kg_index_null_state === true}
            <p class="sss-meta">
              <span class="sss-tag sss-tag-ok">indexNullState=true</span>
            </p>
          {:else}
            <p class="sss-meta">
              <span class="sss-tag sss-tag-warn">indexNullState=false</span>
              Requires a destructive drop+recreate; the shared-KG
              migration script handles it.
            </p>
          {/if}
        </div>

        {#if !devNeedsMigration && !sharedNeedsMigration && status.development_collections.length > 0}
          <p class="sss-meta sss-meta-ok">
            All schemas correct — no migrations needed.
          </p>
        {/if}
      {/if}
    {/if}
  </article>
</section>

{#if showModal && status}
  <SchemaMigrationModal
    {status}
    onClose={() => (showModal = false)}
    {onCompleted}
  />
{/if}

<style>
  .sss-section {
    margin: 14px 24px 24px;
    padding: 14px 16px;
    background: rgba(255,255,255,0.02);
    border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.06);
  }
  .sss-header h2 {
    font-size: 13px;
    margin: 0;
    color: #c4b3ff;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .sss-sub {
    margin: 4px 0 14px;
    font-size: 11px;
    color: #888;
    line-height: 1.5;
  }
  .sss-card {
    background: rgba(255,255,255,0.03);
    padding: 10px 12px;
    border-radius: 4px;
  }
  .sss-card-h {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
  }
  .sss-card-h strong {
    font-size: 12px;
    flex: 1;
  }
  .sss-badge {
    font-size: 10px;
    padding: 2px 10px;
    border-radius: 10px;
    text-transform: uppercase;
    font-weight: 600;
    letter-spacing: 0.4px;
  }
  .sss-badge-green {
    background: rgba(0,191,166,0.18);
    color: #0fc;
  }
  .sss-badge-yellow {
    background: rgba(245,179,66,0.18);
    color: #f5b342;
  }
  .sss-badge-red {
    background: rgba(255,99,99,0.20);
    color: #f99;
  }
  .sss-badge-gray {
    background: rgba(255,255,255,0.08);
    color: #aaa;
  }
  .sss-btn {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit;
    padding: 4px 12px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 11px;
  }
  .sss-btn:hover:not(:disabled) {
    background: rgba(255,255,255,0.08);
  }
  .sss-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .sss-meta {
    margin: 4px 0;
    font-size: 11px;
    color: #888;
    line-height: 1.5;
  }
  .sss-meta-ok {
    color: #0fc;
    margin-top: 8px;
  }
  .sss-meta code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
    color: #c4b3ff;
  }
  .sss-subsection {
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px solid rgba(255,255,255,0.05);
  }
  .sss-subsection h4 {
    font-size: 11px;
    margin: 0 0 6px;
    color: #ddd;
    font-weight: 500;
  }
  .sss-subhead-count {
    font-size: 10px;
    color: #888;
    font-weight: 400;
  }
  .sss-subsection h4 code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
    color: #c4b3ff;
  }
  .sss-empty {
    font-size: 11px;
    color: #888;
    font-style: italic;
    margin: 4px 0;
  }
  .sss-empty code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
  }
  .sss-dev-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .sss-dev-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 6px;
    background: rgba(255,255,255,0.02);
    border-radius: 3px;
    font-size: 11px;
  }
  .sss-dev-incomplete {
    border-left: 2px solid rgba(245,179,66,0.4);
  }
  .sss-dev-name {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    color: #ddd;
  }
  .sss-dev-pct {
    font-size: 10px;
    color: #888;
    flex: 1;
  }
  .sss-tag {
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 8px;
    text-transform: uppercase;
    font-weight: 600;
    letter-spacing: 0.4px;
    background: rgba(255,255,255,0.08);
    color: #aaa;
  }
  .sss-tag-ok { background: rgba(0,191,166,0.18); color: #0fc; }
  .sss-tag-warn { background: rgba(245,179,66,0.18); color: #f5b342; }
</style>
