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

  import { onMount, onDestroy } from 'svelte';
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
  // W3 / v0.2.16 (plan 0.3): kickoff counters were misleading because
  // `rebuild_code_graph` returns as soon as the subprocess is spawned —
  // the wizard used to report "Started 3/3" indefinitely while the
  // analyzers were still running. We now keep the kickoff counters for
  // the dispatch phase only, and replace the per-project display with
  // live status read from `code_graph_builds` via a poll loop.
  let reanalyzeProgress = $state<{ done: number; total: number; failed: string[]; complete: boolean } | null>(null);
  // Per-project status map keyed by project_id. Refreshed every 2 s by
  // the poll loop after kickoff dispatch. Status vocabulary mirrors
  // `db::code_graph_builds::status` + a synthetic 'missing' for
  // projects whose row hasn't been written yet.
  interface PerProjectBuildStatus {
    project_id: string;
    status: string;
    files_analyzed: number | null;
    error_message: string | null;
    terminal: boolean;
  }
  let perProjectStatuses = $state<Record<string, PerProjectBuildStatus>>({});
  // setInterval handle. Tracked so we can clearInterval on unmount or
  // when the user re-loads the modal mid-poll.
  let statusPollHandle: ReturnType<typeof setInterval> | null = null;
  // W1+W3 wire-up / v0.2.16 (plan 1.4 — addendum H): tracks the
  // wizard checkbox "Clean stale entries during re-analysis".
  // Default true matches the plan ("recommended"). When the user
  // clicks Re-analyze, this value is passed as `pruneStale` to
  // `rebuild_code_graph`, which threads it through to the analyzer
  // subprocess as `--prune-stale`. The analyzer then deletes any
  // per-project code-graph object it did NOT visit this run (cleans
  // up rows for source files deleted since the previous analyze).
  let pruneStale = $state(true);
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

  // W3 / v0.2.16 (plan 0.3): iterate `rebuild_code_graph` over every
  // affected project to dispatch the analyzer subprocess, THEN start a
  // 2-second poll against `get_code_graph_build_status_for_projects` so
  // we can show real per-project progress instead of being stuck at
  // "Started N/N". The Tauri command spawns the analyzer in the
  // background and returns immediately, so dispatch finishes in
  // milliseconds — the slow part is the analyzer subprocesses, which
  // the poll loop watches via the `code_graph_builds` table.
  //
  // The wizard is NOT auto-closed when polling completes — the user
  // should review the final per-project status (files analyzed, any
  // errors) before dismissing.
  async function reanalyzeAffected() {
    if (!report) return;
    // Defensive: if the user clicks Re-analyze twice in quick
    // succession, stop the previous poll loop before starting a fresh
    // one so we don't end up with two intervals overlapping.
    stopStatusPoll();
    reanalyzing = true;
    reanalyzeProgress = {
      done: 0,
      total: report.affected_projects.length,
      failed: [],
      complete: false,
    };
    // Pre-seed perProjectStatuses with 'pending' so the UI immediately
    // renders one row per project (instead of showing nothing until
    // the first poll tick at t+2s).
    const seedStatuses: Record<string, PerProjectBuildStatus> = {};
    for (const p of report.affected_projects) {
      seedStatuses[p.project_id] = {
        project_id: p.project_id,
        status: 'pending',
        files_analyzed: null,
        error_message: null,
        terminal: false,
      };
    }
    perProjectStatuses = seedStatuses;

    for (const p of report.affected_projects) {
      try {
        await invoke('rebuild_code_graph', { projectId: p.project_id, pruneStale });
      } catch (e) {
        reanalyzeProgress = {
          done: reanalyzeProgress.done,
          total: reanalyzeProgress.total,
          failed: [...reanalyzeProgress.failed, `${p.name}: ${e}`],
          complete: false,
        };
        // Kickoff itself failed — mark this project terminal so the
        // poll loop's "everyone done?" check still resolves.
        perProjectStatuses = {
          ...perProjectStatuses,
          [p.project_id]: {
            project_id: p.project_id,
            status: 'failed',
            files_analyzed: null,
            error_message: `kickoff failed: ${e}`,
            terminal: true,
          },
        };
      }
      reanalyzeProgress = {
        done: reanalyzeProgress.done + 1,
        total: reanalyzeProgress.total,
        failed: reanalyzeProgress.failed,
        complete: false,
      };
    }
    reanalyzing = false;
    if (reanalyzeProgress.failed.length > 0) {
      toast.error(
        `${reanalyzeProgress.failed.length} of ${reanalyzeProgress.total} kickoffs failed. See modal for details.`,
      );
    }
    // Start polling regardless — kickoffs that succeeded still need
    // per-project status updates.
    startStatusPoll();
  }

  // Poll `get_code_graph_build_status_for_projects` every 2 seconds.
  // Stops as soon as every per-project status is terminal (success /
  // failed / skipped / missing). Defensive against unmount: the
  // onDestroy hook also clears the handle.
  function startStatusPoll() {
    if (!report) return;
    const projectIds = report.affected_projects.map((p) => p.project_id);
    if (projectIds.length === 0) {
      if (reanalyzeProgress) {
        reanalyzeProgress = { ...reanalyzeProgress, complete: true };
      }
      return;
    }
    // Immediate first poll so the user sees a fresh state right after
    // kickoff dispatch finishes (no 2-second blank-screen window).
    void pollOnce(projectIds);
    statusPollHandle = setInterval(() => {
      void pollOnce(projectIds);
    }, 2000);
  }

  async function pollOnce(projectIds: string[]) {
    try {
      const statuses = await invoke<PerProjectBuildStatus[]>(
        'get_code_graph_build_status_for_projects',
        { projectIds },
      );
      // Replace the whole map in one assignment so Svelte's reactivity
      // picks it up as a single update.
      const next: Record<string, PerProjectBuildStatus> = {};
      for (const s of statuses) {
        next[s.project_id] = s;
      }
      perProjectStatuses = next;

      if (statuses.length > 0 && statuses.every((s) => s.terminal)) {
        stopStatusPoll();
        if (reanalyzeProgress) {
          reanalyzeProgress = { ...reanalyzeProgress, complete: true };
        }
        const failed = statuses.filter((s) => s.status === 'failed');
        if (failed.length === 0) {
          toast.success(
            `Re-analysis finished for ${statuses.length} project${statuses.length === 1 ? '' : 's'}.`,
          );
        } else {
          toast.error(
            `Re-analysis finished with ${failed.length} of ${statuses.length} project${statuses.length === 1 ? '' : 's'} failing. See modal for per-project errors.`,
          );
        }
      }
    } catch (e) {
      // Soft-fail: a single failed poll shouldn't stop the loop. The
      // user can dismiss the modal if the polling stays broken.
      console.warn('[legacy] poll get_code_graph_build_status_for_projects failed', e);
    }
  }

  function stopStatusPoll() {
    if (statusPollHandle !== null) {
      clearInterval(statusPollHandle);
      statusPollHandle = null;
    }
  }

  // Icon vocabulary mirrors the rest of the launcher's lifecycle
  // affordances. Kept here as a single function so future status
  // additions only need to be wired in one place.
  function statusIcon(status: string): string {
    switch (status) {
      case 'success':
        return '✓';
      case 'failed':
        return '✗';
      case 'skipped':
        return '∅';
      case 'missing':
        return '?';
      case 'pending':
      case 'running':
      default:
        return '⏳';
    }
  }

  function statusLabel(status: string): string {
    switch (status) {
      case 'pending':
        return 'queued';
      case 'running':
        return 'analyzing';
      case 'success':
        return 'done';
      case 'failed':
        return 'failed';
      case 'skipped':
        return 'no source files';
      case 'missing':
        return 'no build recorded';
      default:
        return status;
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
    // W3 / v0.2.16 (plan 0.9): the backend still flips the flag to
    // `true`, but the button label ("Dismiss for now") + the
    // companion auto-reset in `rebuild_code_graph` + the
    // "Re-check for legacy collections" button in Preferences make
    // this a session-scoped affordance in practice — re-analyzing OR
    // visiting Preferences re-arms the wizard for the next launcher
    // start.
    stopStatusPoll();
    try {
      await invoke('set_legacy_codegraph_notice_dismissed', { dismissed: true });
    } catch (e) {
      console.warn('[legacy] dismiss failed', e);
    }
    onClose();
  }

  // Defensive cleanup: clear the poll handle if the user navigates
  // away (modal unmount) before the analyzers finish.
  onDestroy(stopStatusPoll);

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

      <!-- W3 / v0.2.16 (plan 0.3): live re-analysis progress.
           Polls `get_code_graph_build_status_for_projects` every 2 s
           and renders a per-project row with icon + status label +
           files-analyzed count. Replaces the old static "Started N/N"
           line that never advanced past kickoff. -->
      {#if reanalyzeProgress}
        <section class="legacy-section legacy-progress">
          <h4>Re-analysis progress</h4>
          <p>
            {#if reanalyzing}
              Dispatching kickoffs: {reanalyzeProgress.done} / {reanalyzeProgress.total} project(s)…
            {:else if reanalyzeProgress.complete}
              All {reanalyzeProgress.total} project(s) finished. Review per-project status below, then dismiss.
            {:else}
              Analyzers running. Status refreshes every 2 seconds.
            {/if}
          </p>
          <ul class="legacy-list legacy-build-rows">
            {#each report.affected_projects as p (p.project_id)}
              {@const st = perProjectStatuses[p.project_id]}
              <li class="legacy-build-row legacy-build-status-{st?.status ?? 'pending'}">
                <span class="legacy-build-icon" aria-hidden="true">{statusIcon(st?.status ?? 'pending')}</span>
                <span class="legacy-build-name">{p.name}</span>
                <span class="legacy-build-status">{statusLabel(st?.status ?? 'pending')}</span>
                {#if st && st.files_analyzed !== null}
                  <span class="legacy-build-files">{st.files_analyzed} file{st.files_analyzed === 1 ? '' : 's'}</span>
                {/if}
                {#if st && st.error_message}
                  <span class="legacy-build-error">{st.error_message}</span>
                {/if}
              </li>
            {/each}
          </ul>
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
      <!-- W3 / v0.2.16 (plan 0.9): label communicates that dismissal
           is session-scoped — re-analyzing OR clicking the
           Preferences "Re-check for legacy collections" button
           re-arms the wizard for the next launcher start. -->
      <button class="legacy-btn" onclick={dismiss}>Dismiss for now</button>
      {#if report && report.affected_projects.length > 0}
        <!-- W1+W3 wire-up / v0.2.16 (plan 1.4 — addendum H): when
             checked, Re-analyze passes --prune-stale to the
             analyzer so any code-graph rows for source files
             deleted since the previous analyze get cleaned up.
             Default checked because the failure mode it prevents
             (stale rows accumulating forever) is more harmful
             than the (rare) case where the user wants to keep
             pre-existing rows untouched. -->
        <label class="legacy-prune-toggle" title="Recommended. Removes code-graph entries for files that have been deleted from the project since the previous analyze.">
          <input
            type="checkbox"
            bind:checked={pruneStale}
            disabled={reanalyzing || cleaningUp}
          />
          Clean stale entries during re-analysis
        </label>
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
    align-items: center;
  }
  .legacy-prune-toggle {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    opacity: 0.85;
    cursor: pointer;
    margin-right: auto;
  }
  .legacy-prune-toggle input[type="checkbox"] {
    width: auto;
    margin: 0;
    cursor: pointer;
  }
  .legacy-prune-toggle:has(input:disabled) {
    cursor: not-allowed;
    opacity: 0.5;
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
  /* W3 / v0.2.16 (plan 0.3): per-project build rows in the
     progress section. Each row: icon | name | status label | files
     count | optional error. Icons mirror the rest of the launcher's
     lifecycle affordances (⏳ pending/running, ✓ success, ✗ failed). */
  .legacy-build-rows {
    margin-top: 8px;
  }
  .legacy-build-row {
    display: grid;
    grid-template-columns: 20px 1fr auto auto;
    align-items: center;
    gap: 10px;
    padding: 6px 10px;
    font-size: 12px;
  }
  .legacy-build-icon {
    font-size: 14px;
    text-align: center;
  }
  .legacy-build-name {
    color: #ddd;
  }
  .legacy-build-status {
    color: #888;
    font-size: 11px;
  }
  .legacy-build-files {
    color: #aaa;
    font-size: 11px;
    font-family: ui-monospace, monospace;
  }
  .legacy-build-error {
    grid-column: 2 / -1;
    color: #f99;
    font-size: 11px;
    margin-top: 2px;
  }
  .legacy-build-status-success .legacy-build-icon { color: rgb(0,210,180); }
  .legacy-build-status-failed .legacy-build-icon { color: #f88; }
  .legacy-build-status-skipped .legacy-build-icon { color: #888; }
  .legacy-build-status-missing .legacy-build-icon { color: #ccaa55; }
  .legacy-build-status-pending .legacy-build-icon,
  .legacy-build-status-running .legacy-build-icon {
    /* Subtle pulse so the user sees activity even when files_analyzed
       hasn't ticked yet (the analyzer's startup phase can take a few
       seconds before any file is counted). */
    animation: legacy-pulse 1.6s ease-in-out infinite;
  }
  @keyframes legacy-pulse {
    0%, 100% { opacity: 0.55; }
    50% { opacity: 1; }
  }
</style>
