<script lang="ts">
  /**
   * /preferences/weaviate-untracked — advanced view: full Weaviate
   * code-graph collection inventory, including prefixes whose project
   * is no longer registered with the launcher.
   *
   * v0.2.16 (W4 / 0.11): introduces this page as the "advanced"
   * counterpart to the GUI defaults (which filter to tracked projects).
   * User direction 2026-05-18: dead-project data (MediaLibrary_*,
   * TestInstall_*, ARTup_*, etc.) should STAY in Weaviate (so the user
   * can re-import the project later) but should NOT clutter every
   * day-to-day surface. This page is the explicit "show me the
   * everything" view, gated by a Preferences link.
   *
   * The page calls `list_legacy_codegraph_collections({ include_untracked_projects: true })`
   * to get the full inventory. Each untracked group is rendered with
   * a "Delete this prefix" affordance that calls
   * `cleanup_orphan_codegraph_collections` (same backend the wizard uses).
   */
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';
  import type {
    LegacyCodegraphReport,
    OrphanCollectionGroup,
    CleanupLegacyReport,
  } from '$lib/types/identity';

  let report = $state<LegacyCodegraphReport | null>(null);
  let loading = $state(true);
  let pendingDeletePrefix = $state<string | null>(null);
  let deletingPrefix = $state<string | null>(null);

  async function load() {
    loading = true;
    try {
      report = await invoke<LegacyCodegraphReport>('list_legacy_codegraph_collections', {
        includeUntrackedProjects: true,
      });
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  /** Split the inventory into tracked-project orphans vs untracked. */
  const trackedGroups = $derived.by(() => {
    if (!report) return [] as OrphanCollectionGroup[];
    return report.orphan_groups.filter((g) => g.matched_project_id !== '');
  });

  const untrackedGroups = $derived.by(() => {
    if (!report) return [] as OrphanCollectionGroup[];
    return report.orphan_groups.filter((g) => g.matched_project_id === '');
  });

  async function deletePrefix(group: OrphanCollectionGroup) {
    if (pendingDeletePrefix !== group.prefix) {
      // First click arms the delete; second confirms.
      pendingDeletePrefix = group.prefix;
      return;
    }
    deletingPrefix = group.prefix;
    try {
      const result = await invoke<CleanupLegacyReport>(
        'cleanup_orphan_codegraph_collections',
        { req: { classes: group.collections.map((c) => c.class) } },
      );
      if (result.failed.length === 0) {
        toast.success(`Deleted ${result.deleted.length} class(es) under prefix ${group.prefix}_*.`);
      } else {
        toast.error(
          `Cleanup partial for ${group.prefix}_*: ${result.deleted.length} deleted, ${result.failed.length} failed.`,
        );
      }
      pendingDeletePrefix = null;
      await load();
    } catch (e) {
      toast.error(e);
    } finally {
      deletingPrefix = null;
    }
  }

  function cancelPendingDelete() {
    pendingDeletePrefix = null;
  }

  onMount(load);
</script>

<svelte:head>
  <title>Untracked Weaviate Collections — VCT Launcher</title>
</svelte:head>

<div class="page">
  <nav class="crumb">
    <a href="/preferences">Preferences</a>
    <span class="sep">/</span>
    <span class="current">Untracked Weaviate Collections</span>
  </nav>

  <header class="hdr">
    <h1>Untracked Weaviate collections</h1>
    <p>
      Full inventory of code-graph data in Weaviate. Day-to-day GUI surfaces
      hide collections whose prefix isn't bound to a currently-tracked
      project — this page surfaces them all so you can clean up dead-project
      leftovers (or re-import a project to start tracking its data again).
      Nothing is auto-deleted.
    </p>
    <button class="refresh-btn" onclick={load} disabled={loading}>
      {loading ? 'Loading…' : 'Refresh'}
    </button>
  </header>

  {#if loading && !report}
    <p class="empty">Scanning Weaviate…</p>
  {:else if !report}
    <p class="empty">No report available.</p>
  {:else}
    <!-- Tracked-but-orphan groups: prefix maps to a project, but
         differs from its current canonical. These also appear in
         the wizard. Repeated here for symmetry. -->
    {#if trackedGroups.length > 0}
      <section class="section">
        <h2>Orphan code-graph for currently-tracked projects</h2>
        <p class="hint">
          Prefixes that case-insensitively match a tracked project but
          use a different sanitizer generation than its current canonical.
          These appear in the legacy-collection wizard too — listed here
          for completeness.
        </p>
        <ul class="group-list">
          {#each trackedGroups as group (group.prefix)}
            <li class="group">
              <div class="group-head">
                <div class="group-name">
                  <code>{group.prefix}_*</code>
                  <span class="arrow">→</span>
                  <code class="current">{group.current_prefix}</code>
                  <span class="project">({group.matched_project_name})</span>
                </div>
                <div class="group-meta">
                  {group.collections.length} class{group.collections.length === 1 ? '' : 'es'},
                  {group.total_objects} object{group.total_objects === 1 ? '' : 's'} total
                </div>
              </div>
              <ul class="class-list">
                {#each group.collections as c (c.class)}
                  <li><code>{c.class}</code> — {c.object_count} object{c.object_count === 1 ? '' : 's'}</li>
                {/each}
              </ul>
              <div class="group-actions">
                {#if pendingDeletePrefix === group.prefix}
                  <button
                    class="btn btn-danger"
                    disabled={deletingPrefix === group.prefix}
                    onclick={() => deletePrefix(group)}
                  >
                    {deletingPrefix === group.prefix ? 'Deleting…' : `Confirm delete ${group.collections.length} class(es)`}
                  </button>
                  <button class="btn" onclick={cancelPendingDelete} disabled={deletingPrefix === group.prefix}>
                    Cancel
                  </button>
                {:else}
                  <button class="btn btn-danger" onclick={() => deletePrefix(group)}>
                    Delete this prefix…
                  </button>
                {/if}
              </div>
            </li>
          {/each}
        </ul>
      </section>
    {/if}

    <!-- Untracked groups: prefix doesn't map to any current project.
         This is the unique value of this page over the wizard. -->
    <section class="section">
      <h2>Untracked collections (no project currently linked)</h2>
      <p class="hint">
        These prefixes have data in Weaviate but no matching project in
        the launcher. Likely dead-project leftovers (MediaLibrary_*,
        TestInstall_*, ARTup_*, etc.) — safe to delete unless you plan
        to re-import the project. Deleting the prefix removes only the
        five code-graph classes (CodeModule / CodeClass / CodeFunction
        / CodeAPI / CodeInteraction). KG / Development / other class
        shapes under the same prefix are NEVER touched here.
      </p>
      {#if untrackedGroups.length === 0}
        <p class="empty-inline">
          No untracked code-graph collections. Either Weaviate is empty
          of orphan data, or every prefix maps to a registered project.
        </p>
      {:else}
        <ul class="group-list">
          {#each untrackedGroups as group (group.prefix)}
            <li class="group">
              <div class="group-head">
                <div class="group-name">
                  <code>{group.prefix}_*</code>
                  <span class="untracked-tag">untracked</span>
                </div>
                <div class="group-meta">
                  {group.collections.length} class{group.collections.length === 1 ? '' : 'es'},
                  {group.total_objects} object{group.total_objects === 1 ? '' : 's'} total
                </div>
              </div>
              <ul class="class-list">
                {#each group.collections as c (c.class)}
                  <li><code>{c.class}</code> — {c.object_count} object{c.object_count === 1 ? '' : 's'}</li>
                {/each}
              </ul>
              <div class="group-actions">
                {#if pendingDeletePrefix === group.prefix}
                  <button
                    class="btn btn-danger"
                    disabled={deletingPrefix === group.prefix}
                    onclick={() => deletePrefix(group)}
                  >
                    {deletingPrefix === group.prefix ? 'Deleting…' : `Confirm delete ${group.collections.length} class(es)`}
                  </button>
                  <button class="btn" onclick={cancelPendingDelete} disabled={deletingPrefix === group.prefix}>
                    Cancel
                  </button>
                {:else}
                  <button class="btn btn-danger" onclick={() => deletePrefix(group)}>
                    Delete this prefix…
                  </button>
                {/if}
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </section>
  {/if}

  <Toast />
</div>

<style>
  .page {
    padding: 1.5rem;
    max-width: 1100px;
    margin: 0 auto;
  }

  .crumb {
    margin-bottom: 1rem;
    font-size: 0.9rem;
    color: var(--text-muted, #888);
  }

  .crumb a {
    color: var(--accent, #00bfa6);
    text-decoration: none;
  }

  .crumb a:hover {
    text-decoration: underline;
  }

  .crumb .sep {
    margin: 0 0.4rem;
    color: #555;
  }

  .crumb .current {
    color: #ddd;
  }

  .hdr {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    margin-bottom: 1.5rem;
    position: relative;
  }

  .hdr h1 {
    margin: 0;
    font-size: 1.4rem;
  }

  .hdr p {
    color: #aaa;
    font-size: 0.85rem;
    line-height: 1.55;
    max-width: 80ch;
    margin: 0;
  }

  .refresh-btn {
    position: absolute;
    top: 0;
    right: 0;
    padding: 6px 14px;
    border-radius: 6px;
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit;
    font-size: 12px;
    cursor: pointer;
  }
  .refresh-btn:hover:not(:disabled) {
    background: rgba(255,255,255,0.1);
  }
  .refresh-btn:disabled { opacity: 0.5; cursor: default; }

  .empty {
    color: #888;
    padding: 32px;
    text-align: center;
    font-size: 13px;
  }

  .empty-inline {
    color: #888;
    font-size: 12px;
    padding: 12px 0;
  }

  .section {
    margin-bottom: 2rem;
  }

  .section h2 {
    margin: 0 0 0.4rem;
    font-size: 1rem;
    color: #c4b3ff;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .hint {
    color: #888;
    font-size: 0.8rem;
    line-height: 1.55;
    margin: 0 0 1rem;
    max-width: 80ch;
  }

  .group-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .group {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px;
    padding: 12px 16px;
  }

  .group-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 8px;
  }

  .group-name {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    font-size: 13px;
    color: #ddd;
  }

  .group-name code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 12px;
  }

  .group-name .arrow { color: #777; }

  .group-name .current {
    background: rgba(120,255,140,0.08);
    color: #aaffaa;
  }

  .group-name .project {
    color: #888;
    font-size: 11px;
  }

  .untracked-tag {
    background: rgba(245,179,66,0.12);
    border: 1px solid rgba(245,179,66,0.35);
    color: #f5b342;
    padding: 1px 8px;
    border-radius: 10px;
    font-size: 10px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .group-meta {
    color: #888;
    font-size: 11px;
  }

  .class-list {
    list-style: none;
    padding: 0;
    margin: 0 0 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .class-list li {
    font-size: 11px;
    color: #aaa;
    padding: 4px 8px;
    background: rgba(255,255,255,0.02);
    border-radius: 4px;
  }

  .class-list code {
    font-family: ui-monospace, monospace;
    color: #ddd;
  }

  .group-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 4px;
  }

  .btn {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
  }

  .btn:hover:not(:disabled) {
    background: rgba(255,255,255,0.10);
  }

  .btn:disabled { opacity: 0.5; cursor: default; }

  .btn-danger {
    background: rgba(245,80,80,0.18);
    border-color: rgba(245,80,80,0.4);
    color: #f88;
  }

  .btn-danger:hover:not(:disabled) {
    background: rgba(245,80,80,0.28);
  }
</style>
