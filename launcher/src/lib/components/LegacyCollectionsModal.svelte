<script lang="ts">
  // PR-8 (v0.2.11 / 2026-05-15): one-time legacy code-graph collection notice.
  //
  // Pre-0.2.11 installs hardcoded `PROJECT_NAME=ClaudeOrchestrator` in the
  // bundled hooks + install.py, so every user-project's code-graph landed
  // in `ClaudeOrchestrator_*` Weaviate classes instead of the project-
  // specific `<MyProject>_*` classes. PR-7 fixes the write path. This
  // modal is the read-side counterpart: detect the stale collections AND
  // user projects whose code-graph prefix is NOT the legacy one, and
  // offer to re-analyze each affected project so its data lands in the
  // right namespace.
  //
  // Behaviour rules:
  //   - READ-ONLY by default: detection is non-destructive. The user can
  //     choose to (a) re-analyze the affected projects, (b) explicitly
  //     clean up the legacy classes, or (c) dismiss the notice forever.
  //   - Cleanup requires double-confirmation: a checkbox AND a click on
  //     the danger-tinted Cleanup button. Both KG and code-graph classes
  //     under the legacy prefix would be at risk, but the backend
  //     restricts deletion to the five code-graph suffixes only.
  //   - Dismissal is persistent via `set_legacy_codegraph_notice_dismissed`.

  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import type {
    LegacyCodegraphReport,
    CleanupLegacyReport,
    AffectedProject,
    OrphanCollectionGroup,
  } from '$lib/types/identity';

  let { onClose }: { onClose: () => void } = $props();

  let report = $state<LegacyCodegraphReport | null>(null);
  let loading = $state(true);
  let reanalyzing = $state(false);
  let cleanupConfirmed = $state(false);
  let cleaningUp = $state(false);
  let cleanupReport = $state<CleanupLegacyReport | null>(null);
  let reanalyzeProgress = $state<{ done: number; total: number; failed: string[] } | null>(null);
  // v0.2.15 (0.4): orphan-group cleanup state. Each entry in
  // `selectedOrphanPrefixes` toggles inclusion of one group's classes
  // in the next orphan-delete call.
  let selectedOrphanPrefixes = $state<Set<string>>(new Set());
  let orphanCleanupConfirmed = $state(false);
  let orphanCleanupReport = $state<CleanupLegacyReport | null>(null);
  let orphanCleaningUp = $state(false);

  async function load() {
    loading = true;
    try {
      report = await invoke<LegacyCodegraphReport>('list_legacy_codegraph_collections');
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  // Iterate `rebuild_code_graph` over every affected project. The Tauri
  // command spawns the analyzer in the background and returns immediately
  // (the build banner watches per-project progress), so this loop just
  // dispatches kickoffs sequentially. Per-project failures are surfaced
  // in the progress block but don't abort the loop.
  async function reanalyzeAffected() {
    if (!report) return;
    reanalyzing = true;
    reanalyzeProgress = { done: 0, total: report.affected_projects.length, failed: [] };
    for (const p of report.affected_projects) {
      try {
        await invoke('rebuild_code_graph', { projectId: p.project_id });
      } catch (e) {
        reanalyzeProgress = {
          done: reanalyzeProgress.done,
          total: reanalyzeProgress.total,
          failed: [...reanalyzeProgress.failed, `${p.name}: ${e}`],
        };
      }
      reanalyzeProgress = {
        done: reanalyzeProgress.done + 1,
        total: reanalyzeProgress.total,
        failed: reanalyzeProgress.failed,
      };
    }
    reanalyzing = false;
    if (reanalyzeProgress.failed.length === 0) {
      toast.success(
        `Started re-analysis for ${reanalyzeProgress.total} project${reanalyzeProgress.total === 1 ? '' : 's'}. Watch each project's build banner for progress.`,
      );
    } else {
      toast.error(
        `${reanalyzeProgress.failed.length} of ${reanalyzeProgress.total} kickoffs failed. See modal for details.`,
      );
    }
  }

  async function cleanupLegacy() {
    if (!report || !cleanupConfirmed) return;
    cleaningUp = true;
    try {
      cleanupReport = await invoke<CleanupLegacyReport>('cleanup_legacy_codegraph_collections', {
        req: { classes: report.collections.map((c) => c.class) },
      });
      if (cleanupReport.failed.length === 0) {
        toast.success(`Cleaned up ${cleanupReport.deleted.length} class(es).`);
      } else {
        toast.error(
          `Cleanup partial: ${cleanupReport.deleted.length} deleted, ${cleanupReport.failed.length} failed.`,
        );
      }
      // Re-detect so the user sees the updated state.
      await load();
    } catch (e) {
      toast.error(e);
    } finally {
      cleaningUp = false;
    }
  }

  async function dismiss() {
    try {
      await invoke('set_legacy_codegraph_notice_dismissed', { dismissed: true });
    } catch (e) {
      console.warn('[legacy] dismiss failed', e);
    }
    onClose();
  }

  function affectedLine(p: AffectedProject): string {
    return `${p.name}  —  current prefix: ${p.current_prefix}`;
  }

  // v0.2.15 (0.4): orphan-group selection + cleanup. The user picks
  // groups via checkboxes; the cleanup call collects every class from
  // every selected group and sends it as one batched delete.
  function toggleOrphanGroup(prefix: string) {
    const next = new Set(selectedOrphanPrefixes);
    if (next.has(prefix)) {
      next.delete(prefix);
    } else {
      next.add(prefix);
    }
    selectedOrphanPrefixes = next;
  }

  function selectedOrphanClasses(): string[] {
    if (!report) return [];
    const out: string[] = [];
    for (const group of report.orphan_groups) {
      if (selectedOrphanPrefixes.has(group.prefix)) {
        for (const c of group.collections) out.push(c.class);
      }
    }
    return out;
  }

  async function cleanupOrphans() {
    if (!report || !orphanCleanupConfirmed) return;
    const classes = selectedOrphanClasses();
    if (classes.length === 0) {
      toast.error('Select at least one orphan group before deleting.');
      return;
    }
    orphanCleaningUp = true;
    try {
      orphanCleanupReport = await invoke<CleanupLegacyReport>(
        'cleanup_orphan_codegraph_collections',
        { req: { classes } },
      );
      if (orphanCleanupReport.failed.length === 0) {
        toast.success(
          `Deleted ${orphanCleanupReport.deleted.length} orphan class(es).`,
        );
      } else {
        toast.error(
          `Orphan cleanup partial: ${orphanCleanupReport.deleted.length} deleted, ${orphanCleanupReport.failed.length} failed.`,
        );
      }
      selectedOrphanPrefixes = new Set();
      orphanCleanupConfirmed = false;
      // Re-detect so the user sees fresh state.
      await load();
    } catch (e) {
      toast.error(e);
    } finally {
      orphanCleaningUp = false;
    }
  }

  onMount(load);
</script>

<DialogRoot open={true} width="640px" onClose={onClose}>
  {#snippet header()}
    <div class="legacy-header">
      <h3>Legacy code-graph collections detected</h3>
      <p>
        VCO 0.2.11 fixed a project-name resolution bug. Your projects'
        code-graph data may be sitting in stale <code>ClaudeOrchestrator_*</code>
        Weaviate classes instead of project-specific ones.
      </p>
    </div>
  {/snippet}
  {#snippet body()}
    {#if loading}
      <p class="legacy-empty">Scanning Weaviate…</p>
    {:else if !report || (!report.action_recommended && report.collections.length === 0 && report.affected_projects.length === 0)}
      <p class="legacy-empty">
        Nothing to clean up. Either Weaviate is unreachable, or your
        installation never accumulated legacy data.
      </p>
    {:else}
      <!-- Collections -->
      <section class="legacy-section">
        <h4>Stale Weaviate classes</h4>
        {#if report.collections.length === 0}
          <p class="legacy-empty-inline">
            No <code>ClaudeOrchestrator_*</code> classes in Weaviate.
          </p>
        {:else}
          <ul class="legacy-list">
            {#each report.collections as c (c.class)}
              <li>
                <code>{c.class}</code>
                <span class="legacy-count">{c.object_count} object{c.object_count === 1 ? '' : 's'}</span>
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <!-- Affected projects -->
      <section class="legacy-section">
        <h4>Projects with mismatched code-graph prefix</h4>
        {#if report.affected_projects.length === 0}
          <p class="legacy-empty-inline">
            All registered projects already use the legacy prefix — they are
            the consumers of the data, not victims of the bug.
          </p>
        {:else}
          <ul class="legacy-list">
            {#each report.affected_projects as p (p.project_id)}
              <li class="legacy-affected">
                <span class="legacy-affected-name">{p.name}</span>
                <small>current prefix: <code>{p.current_prefix}</code></small>
              </li>
            {/each}
          </ul>
          <p class="legacy-hint">
            Re-analyzing these projects writes code-graph data under each
            project's own prefix. The launcher kicks off the analysis in the
            background; each project's "Re-build code graph" banner shows
            progress.
          </p>
        {/if}
      </section>

      <!-- v0.2.15 (0.4): orphan code-graph groups -->
      {#if report.orphan_groups.length > 0}
        <section class="legacy-section">
          <h4>Orphan code-graph collections (other naming generations)</h4>
          <p class="legacy-hint">
            Older VCO releases used different project-name → prefix sanitizers,
            so long-lived projects (especially the orchestrator root) may have
            multiple sets of code-graph classes case-insensitively colliding
            with each other. Below is one group per non-canonical prefix
            attributed to a known project. Pick the groups you want to
            delete; <strong>nothing is auto-deleted</strong>.
          </p>
          <ul class="legacy-list">
            {#each report.orphan_groups as group (group.prefix)}
              <li class="legacy-orphan">
                <label class="legacy-orphan-label">
                  <input
                    type="checkbox"
                    checked={selectedOrphanPrefixes.has(group.prefix)}
                    onchange={() => toggleOrphanGroup(group.prefix)}
                  />
                  <div class="legacy-orphan-info">
                    <div class="legacy-orphan-head">
                      <code>{group.prefix}_*</code>
                      <span class="legacy-orphan-arrow">→</span>
                      <code class="legacy-orphan-current">{group.current_prefix}</code>
                      <span class="legacy-orphan-project">({group.matched_project_name})</span>
                    </div>
                    <div class="legacy-orphan-meta">
                      {group.collections.length} class{group.collections.length === 1 ? '' : 'es'},
                      {group.total_objects} object{group.total_objects === 1 ? '' : 's'} total
                    </div>
                  </div>
                </label>
              </li>
            {/each}
          </ul>
          {#if selectedOrphanPrefixes.size > 0}
            <p class="legacy-cleanup-warn">
              <strong>Destructive.</strong> Will delete
              {selectedOrphanClasses().length} class(es) across
              {selectedOrphanPrefixes.size} group(s). Backend restricts the
              delete to the five code-graph suffixes only — KG, Development,
              and other class shapes are NEVER touched.
            </p>
            <label class="legacy-confirm">
              <input type="checkbox" bind:checked={orphanCleanupConfirmed} />
              <span>I understand the selected orphan code-graph data will be permanently deleted.</span>
            </label>
          {/if}
        </section>
      {/if}

      <!-- Orphan cleanup report -->
      {#if orphanCleanupReport}
        <section class="legacy-section">
          <h4>Orphan cleanup result</h4>
          <p>{orphanCleanupReport.deleted.length} class(es) deleted.</p>
          {#if orphanCleanupReport.failed.length > 0}
            <ul class="legacy-list legacy-failed">
              {#each orphanCleanupReport.failed as f (f.class)}
                <li><code>{f.class}</code>: {f.error}</li>
              {/each}
            </ul>
          {/if}
        </section>
      {/if}

      <!-- Re-analyze progress -->
      {#if reanalyzeProgress}
        <section class="legacy-section legacy-progress">
          <h4>Re-analysis kickoff status</h4>
          <p>
            Started for {reanalyzeProgress.done} / {reanalyzeProgress.total} project(s).
          </p>
          {#if reanalyzeProgress.failed.length > 0}
            <ul class="legacy-list legacy-failed">
              {#each reanalyzeProgress.failed as f}
                <li>{f}</li>
              {/each}
            </ul>
          {/if}
        </section>
      {/if}

      <!-- Cleanup confirmation -->
      {#if report.collections.length > 0}
        <section class="legacy-section">
          <h4>Optional: explicit cleanup</h4>
          <p class="legacy-cleanup-warn">
            <strong>Destructive.</strong> Deletes the {report.collections.length}
            <code>ClaudeOrchestrator_*</code> class(es) from Weaviate. Only do
            this AFTER you've re-analyzed (or know you don't need that data).
            Backend restricts deletion to the five code-graph suffixes —
            <code>ClaudeOrchestrator_KnowledgeGraph</code> (if any) is NEVER
            touched.
          </p>
          <label class="legacy-confirm">
            <input type="checkbox" bind:checked={cleanupConfirmed} />
            <span>I understand the legacy code-graph data will be permanently deleted.</span>
          </label>
        </section>
      {/if}

      <!-- Cleanup report -->
      {#if cleanupReport}
        <section class="legacy-section">
          <h4>Cleanup result</h4>
          <p>{cleanupReport.deleted.length} class(es) deleted.</p>
          {#if cleanupReport.failed.length > 0}
            <ul class="legacy-list legacy-failed">
              {#each cleanupReport.failed as f (f.class)}
                <li><code>{f.class}</code>: {f.error}</li>
              {/each}
            </ul>
          {/if}
        </section>
      {/if}
    {/if}
  {/snippet}
  {#snippet footer()}
    <div class="legacy-footer">
      <button class="legacy-btn" onclick={dismiss}>Dismiss</button>
      {#if report && report.affected_projects.length > 0}
        <button
          class="legacy-btn legacy-btn-primary"
          disabled={reanalyzing || cleaningUp}
          onclick={reanalyzeAffected}
        >
          {reanalyzing ? 'Starting…' : `Re-analyze ${report.affected_projects.length} project${report.affected_projects.length === 1 ? '' : 's'}`}
        </button>
      {/if}
      {#if report && report.collections.length > 0}
        <button
          class="legacy-btn legacy-btn-danger"
          disabled={!cleanupConfirmed || cleaningUp || reanalyzing}
          onclick={cleanupLegacy}
        >
          {cleaningUp ? 'Deleting…' : `Delete ${report.collections.length} stale class${report.collections.length === 1 ? '' : 'es'}`}
        </button>
      {/if}
      {#if report && selectedOrphanPrefixes.size > 0}
        <button
          class="legacy-btn legacy-btn-danger"
          disabled={!orphanCleanupConfirmed || orphanCleaningUp || cleaningUp || reanalyzing}
          onclick={cleanupOrphans}
        >
          {orphanCleaningUp ? 'Deleting…' : `Delete ${selectedOrphanClasses().length} selected orphan class${selectedOrphanClasses().length === 1 ? '' : 'es'}`}
        </button>
      {/if}
    </div>
  {/snippet}
</DialogRoot>

<style>
  .legacy-header h3 { margin: 0; font-size: 14px; }
  .legacy-header p {
    margin: 6px 0 0;
    font-size: 12px;
    color: #aaa;
    line-height: 1.5;
  }
  .legacy-header code {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
  }
  .legacy-empty {
    color: #888;
    padding: 24px;
    text-align: center;
    font-size: 12px;
  }
  .legacy-empty-inline {
    color: #888;
    font-size: 12px;
    margin: 4px 0;
  }
  .legacy-section {
    margin-bottom: 18px;
  }
  .legacy-section h4 {
    font-size: 12px;
    margin: 0 0 8px;
    color: #c4b3ff;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .legacy-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .legacy-list li {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 10px;
    background: rgba(255,255,255,0.04);
    border-radius: 4px;
    font-size: 12px;
  }
  .legacy-list code {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    color: #ddd;
  }
  .legacy-count {
    color: #888;
    font-size: 11px;
    margin-left: auto;
  }
  .legacy-affected { gap: 14px; }
  .legacy-affected-name { color: #ddd; }
  .legacy-affected small { color: #888; }
  .legacy-affected small code {
    color: #aaa;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
  }
  .legacy-hint {
    margin: 10px 0 0;
    font-size: 11px;
    color: #777;
    line-height: 1.45;
  }
  .legacy-progress {
    background: rgba(0,191,166,0.05);
    border-left: 3px solid rgba(0,191,166,0.4);
    padding: 8px 12px;
    border-radius: 4px;
  }
  .legacy-progress p { font-size: 12px; color: #ccc; margin: 0; }
  .legacy-failed { margin-top: 6px; }
  .legacy-failed li {
    background: rgba(255,120,120,0.06);
    color: #f99;
    font-size: 11px;
  }
  .legacy-cleanup-warn {
    margin: 0 0 10px;
    padding: 10px 12px;
    background: rgba(245,179,66,0.08);
    border-left: 3px solid rgba(245,179,66,0.4);
    border-radius: 4px;
    font-size: 12px;
    color: #ddd;
    line-height: 1.5;
  }
  .legacy-cleanup-warn code {
    font-family: ui-monospace, monospace;
    font-size: 10px;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
  }
  .legacy-confirm {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: #ddd;
    cursor: pointer;
  }
  .legacy-confirm input { width: auto; }
  .legacy-footer {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    flex-wrap: wrap;
  }
  .legacy-btn {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
  }
  .legacy-btn:hover:not(:disabled) {
    background: rgba(255,255,255,0.10);
  }
  .legacy-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .legacy-btn-primary {
    background: rgb(0,191,166);
    border-color: rgb(0,191,166);
    color: #000;
    font-weight: 600;
  }
  .legacy-btn-primary:hover:not(:disabled) { background: rgb(0,210,180); }
  .legacy-btn-danger {
    background: rgba(245,80,80,0.18);
    border-color: rgba(245,80,80,0.4);
    color: #f88;
  }
  .legacy-btn-danger:hover:not(:disabled) {
    background: rgba(245,80,80,0.28);
  }
  /* v0.2.15 (0.4): orphan group rows. Layout: checkbox + two-line info. */
  .legacy-orphan {
    align-items: flex-start;
    padding: 8px 10px;
  }
  .legacy-orphan-label {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    cursor: pointer;
    width: 100%;
  }
  .legacy-orphan-label input {
    margin-top: 3px;
    width: auto;
    flex-shrink: 0;
  }
  .legacy-orphan-info {
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
    flex: 1;
  }
  .legacy-orphan-head {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    font-size: 12px;
    color: #ddd;
  }
  .legacy-orphan-arrow { color: #777; }
  .legacy-orphan-current {
    color: #aaffaa;
    background: rgba(120,255,140,0.08);
  }
  .legacy-orphan-project {
    color: #888;
    font-size: 11px;
  }
  .legacy-orphan-meta {
    color: #888;
    font-size: 11px;
  }
</style>
