<script lang="ts">
  // Full-width status banner for the initial code-graph build kicked off
  // by `create_project_v2` (and re-triggered by `rebuild_code_graph`).
  //
  // Was previously `CodeGraphBuildPill.svelte`. Promoted to a full-width
  // banner 2026-05-12 for better visibility — the pill in the header
  // didn't draw the eye enough during long builds.
  //
  // v0.2.95 R7: banner chrome (status row + ~160 lines of CSS that three
  // sibling banners carried verbatim copies of) now lives ONCE in
  // `StatusBannerShell.svelte`; the per-status palette is keyed on the
  // tone vocabulary in `status-banner-tone.ts`. This file keeps the build
  // labels, the prune-failure escalation and the retry verbs.
  //
  // The banner renders nothing in `success` / `skipped` terminal states
  // older than ~hide-after-fresh threshold AND nothing when the project
  // has no build row at all (older projects pre-Gap 2). Failure state
  // expands inline (no floating popover) — `clicked → expanded` shows the
  // error message, log tail, and a "Retry build" button.

  import { onDestroy, onMount } from 'svelte';
  import { listen, invoke, safeInvoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type {
    CodeGraphBuildView,
    CodeGraphBuildStatus,
  } from '$lib/types/launcher';
  import CodeGraphReanalysisModal from './CodeGraphReanalysisModal.svelte';
  import {
    isPruneFailurePartial as computeIsPruneFailurePartial,
    buildDropRecreateCommand,
    buildDetailLine as detailLine,
  } from './codegraph-build-banner-logic';
  import StatusBannerShell from './StatusBannerShell.svelte';
  import { toneForCodeGraphBuildStatus } from './status-banner-tone';

  interface Props {
    projectId: string;
    /** Project display name — used to build the C-11b drop-and-recreate
     *  command and to filter the re-analysis modal's progress events. */
    projectName?: string;
    /** When set, banner stays mounted in terminal states (success/skipped)
     *  for `hideTerminalAfterMs` after `finished_at_iso`, then unmounts.
     *  Defaults to 30s so the user has time to read "Indexed · N files". */
    hideTerminalAfterMs?: number;
  }

  let { projectId, projectName = '', hideTerminalAfterMs = 30_000 }: Props = $props();

  let view = $state<CodeGraphBuildView | null>(null);
  let unlisten: (() => void) | null = null;
  let expanded = $state(false);
  let rerunning = $state(false);
  let dismissed = $state(false);
  // C-11b (v0.2.75 P2d): the prune-failure escalation modal.
  let showReanalysis = $state(false);

  // Prune-failure detection + drop-command construction live in
  // ./codegraph-build-banner-logic (unit-tested; one home). The signature
  // string there MUST MATCH launcher/src-tauri/src/commands/codegraph.rs.
  let isPruneFailurePartial = $derived(computeIsPruneFailurePartial(view));

  // The real drop-and-recreate command (analyzer's `--force-recreate` flag).
  // Displayed by the modal for the user to run manually — never auto-executed.
  // Validated by tests/test_deferral_command_argparse_sweep.py (svelte + ts scan).
  // v0.2.92 (BLOCKER-2): identity via --from-resolver, NOT the display name —
  // see codegraph-build-banner-logic.ts.
  let dropCommand = buildDropRecreateCommand();
  let now = $state(Date.now());
  // Tick the clock once per second only while we're in a terminal state
  // that needs auto-hide. Cheaper than a constant 1Hz timer.
  let tickHandle: ReturnType<typeof setInterval> | null = null;

  async function rerun() {
    if (rerunning) return;
    rerunning = true;
    try {
      // Strict invoke so a failed rebuild surfaces — matches the project
      // header's "Re-build code graph" button (invoke + toast), which the
      // safeInvoke here previously diverged from.
      await invoke<void>('rebuild_code_graph', { projectId });
      expanded = false;
      dismissed = false;
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (view) view = { ...view, error_message: msg };
      toast.error(`Code graph rebuild failed: ${msg}`);
    } finally {
      rerunning = false;
    }
  }

  async function load() {
    view = await safeInvoke<CodeGraphBuildView | null>(
      'get_code_graph_build_status',
      { projectId },
    );
  }

  function maybeStartTick(v: CodeGraphBuildView) {
    const terminal =
      v.status === 'success' || v.status === 'skipped' || v.status === 'partial';
    if (terminal && v.finished_at_iso && tickHandle === null) {
      tickHandle = setInterval(() => { now = Date.now(); }, 1000);
    }
    if (!terminal && tickHandle !== null) {
      clearInterval(tickHandle);
      tickHandle = null;
    }
  }

  onMount(async () => {
    await load();
    if (view) maybeStartTick(view);
    unlisten = await listen<CodeGraphBuildView>(
      'code-graph-build-progress',
      (e) => {
        if (e.payload.project_id !== projectId) return;
        // Live events only carry status + files_analyzed + current_phase
        // + error. Merge into our local view so we keep stored timestamps
        // from `get_code_graph_build_status`.
        view = {
          ...(view ?? {
            project_id: projectId,
            status: e.payload.status,
            started_at_iso: null,
            finished_at_iso: null,
            duration_ms: null,
            files_analyzed: 0,
            languages: [],
            joern_used: false,
            error_message: null,
            log_tail: null,
            current_phase: null,
          }),
          status: e.payload.status,
          files_analyzed: e.payload.files_analyzed ?? view?.files_analyzed ?? 0,
          current_phase: e.payload.current_phase,
          error_message: e.payload.error_message ?? view?.error_message ?? null,
        };

        // On terminal events, reload from DB so we pick up the canonical
        // timestamps + languages + log_tail (event omits them to keep
        // payload small).
        if (
          e.payload.status === 'success' ||
          e.payload.status === 'partial' ||
          e.payload.status === 'failed' ||
          e.payload.status === 'skipped'
        ) {
          void load().then(() => { if (view) maybeStartTick(view); });
        }
        // A new run resets the dismiss flag — user shouldn't have to
        // un-dismiss to see a fresh failure.
        if (e.payload.status === 'pending' || e.payload.status === 'running') {
          dismissed = false;
        }
      },
    );
  });

  onDestroy(() => {
    unlisten?.();
    if (tickHandle !== null) clearInterval(tickHandle);
  });

  function statusGlyph(s: CodeGraphBuildStatus): string {
    switch (s) {
      case 'pending': return '·';
      case 'running': return '⟳';
      case 'success': return '✓';
      case 'partial': return '⚠';
      case 'failed': return '!';
      case 'skipped': return '∅';
    }
  }

  function statusLabel(v: CodeGraphBuildView): string {
    switch (v.status) {
      case 'pending': return 'Code graph: queued';
      case 'running':
        return v.current_phase === 'scan'
          ? 'Code graph: scanning source files…'
          : 'Code graph: indexing…';
      case 'success':
        if (v.files_analyzed === 0) return 'Code graph: indexed';
        return `Code graph: indexed ${v.files_analyzed} file${v.files_analyzed === 1 ? '' : 's'}`;
      case 'partial':
        return v.files_analyzed === 0
          ? 'Code graph: built with stale-row cleanup warnings'
          : `Code graph: indexed ${v.files_analyzed} file${v.files_analyzed === 1 ? '' : 's'} (stale-row cleanup warnings)`;
      case 'failed': return 'Code graph: build failed';
      case 'skipped': return 'Code graph: no source files found';
    }
  }

  // `detailLine` is `buildDetailLine` from ./codegraph-build-banner-logic —
  // one home, unit-tested there. v0.2.96 (L-4): it now surfaces
  // `error_message` on `failed` as well as on `partial`, so the state that
  // never auto-hides says WHY without a "Show details" click.

  // Reactive: should the banner be visible at all? Terminal states fade
  // out after `hideTerminalAfterMs`; failed never auto-hides; dismissed
  // never shows. `now` is a $state so this re-evaluates on each tick.
  let visible = $derived.by(() => {
    if (!view) return false;
    if (dismissed) return false;
    if (view.status === 'failed' || view.status === 'pending' || view.status === 'running') {
      return true;
    }
    // success / skipped / partial: visible until hideTerminalAfterMs after
    // finish. `partial` is a non-alert warning — inserts succeeded, so it
    // auto-hides like success rather than sticking like `failed`.
    if (view.finished_at_iso) {
      const finishedMs = Date.parse(view.finished_at_iso);
      return Number.isFinite(finishedMs) && (now - finishedMs) < hideTerminalAfterMs;
    }
    return true;
  });
</script>

{#if view && visible}
  <!-- v0.2.93 (G): `spinning` also covers OUR rebuild invoke being pending,
       not only the background poll reporting `running`. -->
  <StatusBannerShell
    tone={toneForCodeGraphBuildStatus(view.status)}
    glyph={statusGlyph(view.status)}
    spinning={view.status === 'running' || rerunning}
    title={statusLabel(view)}
    detail={detailLine(view)}
    alert={view.status === 'failed'}
    showExpanded={expanded && view.status === 'failed'}
    expandedLabel="Code graph build failure detail"
  >
    {#snippet actions()}
      {#if view?.status === 'failed'}
        <button
          type="button"
          class="bg-btn-secondary"
          onclick={() => (expanded = !expanded)}
          aria-expanded={expanded}
        >
          {expanded ? 'Hide details' : 'Show details'}
        </button>
        <button
          type="button"
          class="bg-btn-primary"
          onclick={rerun}
          disabled={rerunning}
        >
          {#if rerunning}
            <span class="bg-glyph-spin" aria-hidden="true">⟳</span> Rebuilding…
          {:else}
            Retry build
          {/if}
        </button>
      {/if}
      {#if view?.status === 'partial'}
        <button
          type="button"
          class="bg-btn-primary"
          onclick={rerun}
          disabled={rerunning}
        >
          {#if rerunning}
            <span class="bg-glyph-spin" aria-hidden="true">⟳</span> Rebuilding…
          {:else}
            Rebuild
          {/if}
        </button>
        {#if isPruneFailurePartial}
          <!-- C-11b (v0.2.75 P2d): a plain Rebuild retries the SAME failing
               deletes against persistent shard state. Offer the drop-and-
               recreate escalation (via the re-analysis modal, which also
               carries the manual drop command). Never auto-drops. -->
          <button
            type="button"
            class="bg-btn-secondary"
            onclick={() => (showReanalysis = true)}
          >
            Drop &amp; rebuild…
          </button>
        {/if}
      {/if}
      {#if view?.status === 'success' || view?.status === 'skipped' || view?.status === 'partial'}
        <button
          type="button"
          class="bg-btn-x"
          aria-label="Dismiss banner"
          onclick={() => (dismissed = true)}
        >×</button>
      {/if}
    {/snippet}

    {#snippet expandedContent()}
      <div class="bg-expand-row">
        <strong>Error</strong>
        <pre class="bg-pre">{view?.error_message ?? 'No error message persisted (check launcher logs).'}</pre>
      </div>
      {#if view?.log_tail}
        <div class="bg-expand-row">
          <strong>Log tail</strong>
          <pre class="bg-pre">{view.log_tail}</pre>
        </div>
      {/if}
    {/snippet}
  </StatusBannerShell>
{/if}

{#if showReanalysis}
  <!-- C-11b: prune-failure escalation. Runs the (safe, idempotent) re-analysis
       AND surfaces the manual drop-and-recreate command. Never auto-drops. -->
  <CodeGraphReanalysisModal
    projectId={projectId}
    projectName={projectName}
    language={null}
    dropCommand={dropCommand}
    onClose={() => (showReanalysis = false)}
  />
{/if}
