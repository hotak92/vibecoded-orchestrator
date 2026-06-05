<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // v0.2.47: Extra codegraph paths panel.
  //
  // Surfaces project_codegraph_extra_paths rows for the current project —
  // read-only folders that contribute to this project's codegraph
  // collection without being launcher projects themselves.
  //
  // See:
  //   .claude/context/plans/v0.2.47-project-extra-codegraph-paths-2026-06-05.md
  //   knowledge/concepts/project-extra-codegraph-paths-2026-06-05.md
  //   (Agent A) launcher/src-tauri/src/commands/project_codegraph_extras.rs
  //
  // Behaviour summary:
  //   - List shown newest-first (added_at DESC).
  //   - "Add path" → native directory picker → add command.
  //   - If add returns disambiguation_required, render the
  //     ExtrasDisambiguationModal so the user picks between access-
  //     matrix grant vs forced add.
  //   - After every mutation (add+force, remove, enable, disable),
  //     trigger the right analyzer run via ExtrasSyncProgressModal:
  //       * add        → syncExtraPath(incremental=false) on the new row
  //       * remove     → reindexAfterExtrasChange(prune_stale=true)
  //       * disable    → reindexAfterExtrasChange(prune_stale=true)
  //       * enable     → reindexAfterExtrasChange(prune_stale=false)
  //   - Sync modal is minimisable; reindex modal is minimisable; the
  //     disambiguation modal is NOT (user must pick).
  //
  // Reactive: re-runs `load()` whenever `projectId` changes.

  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { pickDirectory } from '$lib/dialog';
  import { toast } from '$lib/stores/toast';
  import { projects as projectsStore } from '$lib/stores/projects';
  import ExtrasDisambiguationModal from '$lib/components/ExtrasDisambiguationModal.svelte';
  import ExtrasSyncProgressModal from '$lib/components/ExtrasSyncProgressModal.svelte';
  import type { SyncModalState } from '$lib/components/ExtrasSyncProgressModal.svelte';
  import type {
    ExtraPath,
    ProjectMeta,
    SyncOutcome,
  } from '$lib/types/codegraph-extras';
  import {
    addExtraPath,
    grantCodegraphReadAccess,
    listExtraPaths,
    reindexAfterExtrasChange,
    removeExtraPath,
    setExtraPathEnabled,
    syncExtraPath,
  } from '$lib/api/codegraph_extras';

  let { projectId }: { projectId: string } = $props();

  let rows = $state<ExtraPath[]>([]);
  let loading = $state(true);
  let adding = $state(false);
  // Per-row in-flight markers so we can disable just the row's buttons
  // during its own mutation. Keyed by path.
  let rowBusy = $state<Record<string, boolean>>({});

  // Disambiguation modal state.
  let showDisambig = $state(false);
  let disambigPath = $state('');
  let disambigExisting = $state<ProjectMeta | null>(null);

  // Sync/reindex modal state.
  let syncOpen = $state(false);
  let syncTitle = $state('');
  let syncBody = $state('');
  let syncState = $state<SyncModalState>('running');
  let syncError = $state<string | null>(null);
  // Track the operation that's currently in flight so Retry knows what
  // to re-invoke. Reused across all three flows.
  let pendingOp = $state<null | (() => Promise<SyncOutcome>)>(null);
  // Minimised pill — when true the modal is closed but the operation
  // is still running. Click pill to re-open.
  let pillVisible = $state(false);
  let pillPath = $state('');
  // `bind:this` on Svelte 5 components yields the component's exported
  // members, not an InstanceType. We type as `any` for the imperative
  // `reopen()` call and call it through an optional chain so the
  // runtime is forgiving.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let syncModalRef = $state<any>(null);

  // Project name for prose. Derived from the projects store so it stays
  // fresh if the user renames the project mid-session.
  const currentProjectName = $derived.by(() => {
    const ps = $projectsStore;
    const proj = ps.projects.find((p) => p.id === projectId);
    return proj?.name ?? 'this project';
  });

  // ─── Data loading ──────────────────────────────────────────────────

  async function load() {
    loading = true;
    try {
      const list = await listExtraPaths(projectId);
      // Server sorts newest-first; mirror it client-side as defence.
      list.sort((a, b) => b.added_at - a.added_at);
      rows = list;
    } catch (e) {
      toast.error(e);
      rows = [];
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    void load();
  });
  $effect(() => {
    if (projectId) void load();
  });

  // ─── Helpers ───────────────────────────────────────────────────────

  function basename(p: string): string {
    if (!p) return '';
    const trimmed = p.replace(/[\\/]+$/, '');
    const idx = Math.max(
      trimmed.lastIndexOf('/'),
      trimmed.lastIndexOf('\\'),
    );
    return idx === -1 ? trimmed : trimmed.slice(idx + 1);
  }

  // Middle-truncate paths longer than 56 chars so the row stays
  // legible without wrapping. Keep the prefix + suffix readable.
  function truncatePath(p: string, max = 56): string {
    if (p.length <= max) return p;
    const keep = Math.floor((max - 1) / 2);
    return `${p.slice(0, keep)}…${p.slice(p.length - keep)}`;
  }

  function relativeTime(ms: number | null | undefined): string {
    if (!ms || ms <= 0) return 'never';
    const delta = Date.now() - ms;
    if (delta < 60_000) return 'just now';
    const m = Math.floor(delta / 60_000);
    if (m < 60) return `${m} min ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h} hr ago`;
    const d = Math.floor(h / 24);
    if (d < 30) return `${d} day${d === 1 ? '' : 's'} ago`;
    const mo = Math.floor(d / 30);
    if (mo < 12) return `${mo} mo ago`;
    const y = Math.floor(mo / 12);
    return `${y} yr${y === 1 ? '' : 's'} ago`;
  }

  function setRowBusy(path: string, busy: boolean) {
    if (busy) {
      rowBusy = { ...rowBusy, [path]: true };
    } else {
      const next = { ...rowBusy };
      delete next[path];
      rowBusy = next;
    }
  }

  // ─── Sync modal driver ────────────────────────────────────────────

  /**
   * Run a sync/reindex operation behind the progress modal. The op
   * function is stashed in `pendingOp` so Retry can re-invoke it
   * without the caller needing to re-bind.
   */
  async function runSyncOp(opts: {
    title: string;
    body: string;
    pillPath?: string;
    op: () => Promise<SyncOutcome>;
    successToast: (outcome: SyncOutcome) => string;
    onAfter?: () => Promise<void> | void;
  }) {
    syncTitle = opts.title;
    syncBody = opts.body;
    syncState = 'running';
    syncError = null;
    pendingOp = opts.op;
    pillPath = opts.pillPath ?? '';
    syncOpen = true;
    pillVisible = false;

    try {
      const outcome = await opts.op();
      syncState = 'succeeded';
      toast.success(opts.successToast(outcome));
      syncOpen = false;
      pillVisible = false;
      if (opts.onAfter) await opts.onAfter();
    } catch (e) {
      syncState = 'failed';
      syncError = e instanceof Error ? e.message : String(e ?? 'unknown');
      // Modal stays open in 'failed' state. Pill is also kept hidden
      // so the user sees the error directly. If the user hid the
      // modal and the op THEN failed, the modal re-opens.
      if (!syncOpen) syncOpen = true;
      pillVisible = false;
    } finally {
      // Only clear pendingOp on terminal success — on failure we keep
      // it so Retry can fire.
      if (syncState === 'succeeded') pendingOp = null;
    }
  }

  function closeSyncModal() {
    syncOpen = false;
    pillVisible = false;
    pendingOp = null;
  }

  async function retrySync() {
    if (!pendingOp) return;
    const op = pendingOp;
    syncState = 'running';
    syncError = null;
    try {
      const outcome = await op();
      syncState = 'succeeded';
      toast.success(`Indexed ${outcome.entities_indexed} entities`);
      syncOpen = false;
      pillVisible = false;
      pendingOp = null;
      await load();
    } catch (e) {
      syncState = 'failed';
      syncError = e instanceof Error ? e.message : String(e ?? 'unknown');
    }
  }

  function reopenFromPill() {
    pillVisible = false;
    syncOpen = true;
    syncModalRef?.reopen?.();
  }

  // ─── Add path flow ─────────────────────────────────────────────────

  async function clickAddPath() {
    if (adding) return;
    adding = true;
    try {
      const picked = await pickDirectory({
        title: 'Select extra codegraph folder',
      });
      if (!picked) return; // user cancelled
      await tryAddPath(picked, false);
    } catch (e) {
      toast.error(e);
    } finally {
      adding = false;
    }
  }

  async function tryAddPath(path: string, force: boolean) {
    try {
      const res = await addExtraPath(projectId, path, { force });
      if (res.action === 'disambiguation_required') {
        disambigPath = res.path;
        disambigExisting = res.existing_project;
        showDisambig = true;
        return;
      }
      // res.action === 'added' — refresh + sync.
      await load();
      await runSyncOp({
        title: `Syncing ${basename(res.row.path)} into ${currentProjectName} codegraph`,
        body: `Indexing files from ${res.row.path}. This may take a few minutes for large repos.`,
        pillPath: res.row.path,
        op: () => syncExtraPath(projectId, res.row.path, false),
        successToast: (o) =>
          `Indexed ${o.entities_indexed} entities from ${basename(res.row.path)}`,
        onAfter: () => load(),
      });
    } catch (e) {
      toast.error(e);
    }
  }

  // ─── Disambiguation modal handlers ─────────────────────────────────

  async function disambigAddAsProject() {
    if (!disambigExisting) return;
    try {
      // grantor = existing project, grantee = current project.
      // Result: current project gets READ access on existing project's
      // codegraph (per spec §13.1 / §14.3).
      await grantCodegraphReadAccess(disambigExisting.id, projectId);
      toast.success(`Access granted to ${disambigExisting.name}'s codegraph`);
      // Refresh the projects store so any peer panels (CrossProjectAccess)
      // pick up the new grant.
      await projectsStore.load();
    } catch (e) {
      toast.error(e);
    } finally {
      // Always close the modal — the user already committed to one
      // path. Errors surface as a toast.
      showDisambig = false;
      disambigExisting = null;
      disambigPath = '';
    }
  }

  async function disambigAddAsPathAnyway() {
    const path = disambigPath;
    showDisambig = false;
    disambigExisting = null;
    disambigPath = '';
    await tryAddPath(path, true);
  }

  function disambigCancel() {
    showDisambig = false;
    disambigExisting = null;
    disambigPath = '';
  }

  // ─── Per-row actions ───────────────────────────────────────────────

  async function clickSyncNow(row: ExtraPath) {
    if (rowBusy[row.path]) return;
    setRowBusy(row.path, true);
    try {
      // Incremental if we have a stored SHA — analyzer falls back to
      // full scan on non-git roots or when the SHA is missing.
      const incremental = !!row.last_indexed_commit;
      await runSyncOp({
        title: `Syncing ${row.display_label} into ${currentProjectName} codegraph`,
        body: `Indexing files from ${row.path}.`,
        pillPath: row.path,
        op: () => syncExtraPath(projectId, row.path, incremental),
        successToast: (o) =>
          `Indexed ${o.entities_indexed} entities from ${row.display_label}`,
        onAfter: () => load(),
      });
    } finally {
      setRowBusy(row.path, false);
    }
  }

  async function clickToggleEnabled(row: ExtraPath) {
    if (rowBusy[row.path]) return;
    const newEnabled = !row.enabled;
    setRowBusy(row.path, true);
    try {
      await setExtraPathEnabled(projectId, row.path, newEnabled);
      await load();
      // Reindex with prune-stale when DISABLING; without when ENABLING.
      // The post-mutation snapshot is what the backend uses.
      const pruneStale = !newEnabled;
      await runSyncOp({
        title: newEnabled
          ? `Re-syncing after re-enabling path`
          : `Re-syncing ${currentProjectName} codegraph after disabling path`,
        body: newEnabled
          ? `Re-indexing ${row.path} into the codegraph.`
          : `Removing entries originally sourced from ${row.display_label}.`,
        pillPath: row.path,
        op: () => reindexAfterExtrasChange(projectId, pruneStale),
        successToast: (o) =>
          newEnabled
            ? `Re-enabled and indexed ${o.entities_indexed} entities`
            : `Re-synced; entries from ${row.display_label} pruned`,
        onAfter: () => load(),
      });
    } catch (e) {
      toast.error(e);
      // Revert UI state on failure by reloading.
      await load();
    } finally {
      setRowBusy(row.path, false);
    }
  }

  async function clickRemove(row: ExtraPath) {
    if (rowBusy[row.path]) return;
    const ok = confirm(
      `Remove extra path '${row.path}' from this project?\n\n` +
        `Codegraph entries originally sourced from this folder will be ` +
        `pruned in the follow-up re-sync.`,
    );
    if (!ok) return;
    setRowBusy(row.path, true);
    try {
      await removeExtraPath(projectId, row.path);
      await load();
      await runSyncOp({
        title: `Re-syncing ${currentProjectName} codegraph after path removal`,
        body: `Removing entries originally sourced from ${row.display_label}.`,
        pillPath: row.path,
        op: () => reindexAfterExtrasChange(projectId, true),
        successToast: () =>
          `Re-synced; entries from ${row.display_label} pruned`,
        onAfter: () => load(),
      });
    } catch (e) {
      toast.error(e);
      await load();
    } finally {
      setRowBusy(row.path, false);
    }
  }

  // ─── Tauri event subscription (best-effort) ────────────────────────

  // If Agent A's commands emit a `module://codegraph-extras-sync-progress`
  // event, subscribe so we can refresh the visible list when an analyze
  // completes out-of-band (e.g. triggered by a hook). Soft-fail: the
  // listener returns a no-op unlisten in browser mode. We can't make
  // onMount async itself (its return must be the sync cleanup fn), so
  // we kick off the subscription in a self-running IIFE and clean up
  // via a captured handle.
  let unlistenProgress: (() => void) | null = null;
  onMount(() => {
    void (async () => {
      try {
        const { listen } = await import('$lib/tauri');
        unlistenProgress = await listen<unknown>(
          'module://codegraph-extras-sync-progress',
          () => {
            void load();
          },
        );
      } catch {
        // Event channel unavailable in this runtime — ignore.
      }
    })();
    return () => {
      unlistenProgress?.();
    };
  });

  // ─── Dev-only: expose invoke for parent tab fallbacks. ─────────────
  // (Some other tabs use a top-level invoke import; keep ours scoped
  // to the API wrapper.)
  void invoke;
</script>

<section class="ps-section extras-panel">
  <header class="extras-panel-head">
    <div>
      <h4>Extra codegraph paths</h4>
      <p class="ps-form-hint">
        Index additional folders into this project's codegraph without
        making them launcher projects. Useful for read-only reference
        clones (e.g. a sibling git repo you want to query alongside your
        own code). Edits to files in these folders are auto-indexed.
        Symlinks are resolved on add — paths show their real on-disk
        location.
      </p>
    </div>
    <button
      type="button"
      class="ps-btn-primary"
      onclick={clickAddPath}
      disabled={loading || adding}
      aria-label="Add a new extra codegraph path"
    >
      {adding ? 'Picking…' : 'Add path'}
    </button>
  </header>

  {#if loading}
    <p class="extras-loading">Loading paths…</p>
  {:else if rows.length === 0}
    <p class="extras-empty">
      No extra paths added. Click "Add path" to index a reference folder.
    </p>
  {:else}
    <ul class="extras-list" aria-label="Extra codegraph paths">
      {#each rows as row (row.path)}
        {@const busy = !!rowBusy[row.path]}
        <li class="extras-row" class:extras-row-disabled={!row.enabled}>
          <div class="extras-row-meta">
            <div class="extras-row-label">
              <strong>{row.display_label}</strong>
              {#if !row.enabled}
                <span
                  class="extras-row-badge"
                  aria-label="This path is currently disabled"
                >
                  disabled
                </span>
              {/if}
            </div>
            <code
              class="extras-row-path"
              title={row.path}
              aria-label={`Absolute path: ${row.path}`}
            >
              {truncatePath(row.path)}
            </code>
            <div class="extras-row-times">
              <span>
                added <time datetime={new Date(row.added_at).toISOString()}>
                  {relativeTime(row.added_at)}
                </time>
              </span>
              <span>
                indexed
                {#if row.last_indexed_at}
                  <time
                    datetime={new Date(row.last_indexed_at).toISOString()}
                  >
                    {relativeTime(row.last_indexed_at)}
                  </time>
                {:else}
                  <time>never</time>
                {/if}
              </span>
            </div>
          </div>
          <div class="extras-row-actions">
            <button
              type="button"
              class="ps-btn-secondary"
              onclick={() => clickSyncNow(row)}
              disabled={busy || !row.enabled}
              aria-label={`Sync ${row.path} now`}
            >
              Sync now
            </button>
            <button
              type="button"
              class="ps-btn-secondary"
              onclick={() => clickToggleEnabled(row)}
              disabled={busy}
              aria-label={
                row.enabled
                  ? `Disable indexing of ${row.path}`
                  : `Enable indexing of ${row.path}`
              }
            >
              {row.enabled ? 'Disable' : 'Enable'}
            </button>
            <button
              type="button"
              class="ps-btn-secondary extras-row-remove"
              onclick={() => clickRemove(row)}
              disabled={busy}
              aria-label={`Remove ${row.path} from this project`}
            >
              Remove
            </button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</section>

{#if pillVisible}
  <!-- Minimised status indicator (top-right). Click re-opens the modal. -->
  <button
    type="button"
    class="extras-sync-pill"
    onclick={reopenFromPill}
    aria-label={`Re-open codegraph sync progress for ${pillPath || 'project'}`}
  >
    <span class="extras-sync-pill-dot" aria-hidden="true"></span>
    Syncing {pillPath ? basename(pillPath) : currentProjectName}…
  </button>
{/if}

{#if showDisambig && disambigExisting}
  <ExtrasDisambiguationModal
    bind:open={showDisambig}
    path={disambigPath}
    existingProject={disambigExisting}
    currentProjectName={currentProjectName}
    onAddAsProject={disambigAddAsProject}
    onAddAsPathAnyway={disambigAddAsPathAnyway}
    onCancel={disambigCancel}
  />
{/if}

{#if syncOpen || syncState === 'failed'}
  <ExtrasSyncProgressModal
    bind:this={syncModalRef}
    bind:open={syncOpen}
    title={syncTitle}
    bodyText={syncBody}
    phase={syncState}
    errorMessage={syncError}
    onRetry={pendingOp ? retrySync : undefined}
    onClose={closeSyncModal}
  />
{/if}

<style>
  .extras-panel {
    border-left: 3px solid rgba(123, 95, 255, 0.35);
  }
  .extras-panel-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 14px;
  }
  .extras-panel-head h4 {
    font-size: 13px;
    margin: 0 0 4px;
    color: #c4b3ff;
  }
  .extras-panel-head .ps-form-hint {
    max-width: 540px;
    margin: 0;
  }
  .extras-loading,
  .extras-empty {
    color: #888;
    font-size: 12px;
    margin: 8px 0 0;
    font-style: italic;
  }
  .extras-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .extras-row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 12px;
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 6px;
  }
  .extras-row-disabled {
    opacity: 0.65;
  }
  .extras-row-meta {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .extras-row-label {
    font-size: 13px;
    color: #ddd;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .extras-row-badge {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    padding: 1px 6px;
    background: rgba(255, 255, 255, 0.06);
    border-radius: 8px;
    color: #888;
  }
  .extras-row-path {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    color: #aaa;
    background: rgba(255, 255, 255, 0.04);
    padding: 2px 6px;
    border-radius: 3px;
    word-break: break-all;
    align-self: flex-start;
    max-width: 100%;
  }
  .extras-row-times {
    font-size: 11px;
    color: #888;
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }
  .extras-row-times time {
    color: #aaa;
  }
  .extras-row-actions {
    display: flex;
    flex-direction: column;
    gap: 6px;
    flex-shrink: 0;
    align-items: stretch;
  }
  .extras-row-remove {
    color: #ffb4b4;
  }
  .extras-row-remove:hover:not(:disabled) {
    background: rgba(255, 99, 99, 0.10);
    border-color: rgba(255, 99, 99, 0.30);
  }

  /* Minimised "sync in progress" pill — fixed top-right. */
  .extras-sync-pill {
    position: fixed;
    top: 14px;
    right: 16px;
    z-index: 9999;
    display: flex;
    align-items: center;
    gap: 8px;
    background: rgba(13, 23, 53, 0.97);
    border: 1px solid rgba(0, 191, 166, 0.45);
    color: #0fc;
    padding: 6px 12px;
    border-radius: 14px;
    font-size: 12px;
    cursor: pointer;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.4);
  }
  .extras-sync-pill:hover {
    background: rgba(13, 23, 53, 1);
  }
  .extras-sync-pill:focus-visible {
    outline: 2px solid rgb(0, 191, 166);
    outline-offset: 2px;
  }
  .extras-sync-pill-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: rgb(0, 191, 166);
    animation: extras-pill-pulse 1.4s ease-in-out infinite;
  }
  @keyframes extras-pill-pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.85); }
  }

  /* Section base styles (mirror IdentityTab.svelte's .ps-section /
     .ps-form-hint so the panel reads correctly when nested in the
     identity tab's CSS scope). Svelte's scoped CSS doesn't reach
     parent classes, so we re-declare. */
  .ps-section {
    margin-bottom: 20px;
    padding: 14px 16px;
    background: rgba(255, 255, 255, 0.03);
    border-radius: 6px;
  }
  .ps-form-hint {
    font-size: 11px;
    color: #777;
    line-height: 1.4;
  }
  .ps-btn-primary {
    background: rgb(0, 191, 166);
    border: none;
    color: #000;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
  }
  .ps-btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .ps-btn-secondary {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: inherit;
    padding: 4px 10px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    white-space: nowrap;
  }
  .ps-btn-secondary:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.08);
  }
  .ps-btn-secondary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
