<script lang="ts">
  // Full-width status banner for the initial code-graph build kicked off
  // by `create_project_v2` (and re-triggered by `rebuild_code_graph`).
  //
  // Was previously `CodeGraphBuildPill.svelte`. Promoted to a full-width
  // banner 2026-05-12 for better visibility — the pill in the header
  // didn't draw the eye enough during long builds. Banner styling mirrors
  // `BrowserModeBanner` (status-row at the top of the page; theme tokens
  // are local because there's no shared theme module — same approach as
  // the existing `.orch-banner` in this route).
  //
  // The banner renders nothing in `success` / `skipped` terminal states
  // older than ~hide-after-fresh threshold AND nothing when the project
  // has no build row at all (older projects pre-Gap 2). Failure state
  // expands inline (no floating popover) — `clicked → expanded` shows the
  // error message, log tail, and a "Retry build" button.

  import { onDestroy, onMount } from 'svelte';
  import { listen, safeInvoke } from '$lib/tauri';
  import type {
    CodeGraphBuildView,
    CodeGraphBuildStatus,
  } from '$lib/types/launcher';

  interface Props {
    projectId: string;
    /** When set, banner stays mounted in terminal states (success/skipped)
     *  for `hideTerminalAfterMs` after `finished_at_iso`, then unmounts.
     *  Defaults to 30s so the user has time to read "Indexed · N files". */
    hideTerminalAfterMs?: number;
  }

  let { projectId, hideTerminalAfterMs = 30_000 }: Props = $props();

  let view = $state<CodeGraphBuildView | null>(null);
  let unlisten: (() => void) | null = null;
  let expanded = $state(false);
  let rerunning = $state(false);
  let dismissed = $state(false);
  let now = $state(Date.now());
  // Tick the clock once per second only while we're in a terminal state
  // that needs auto-hide. Cheaper than a constant 1Hz timer.
  let tickHandle: ReturnType<typeof setInterval> | null = null;

  async function rerun() {
    if (rerunning) return;
    rerunning = true;
    try {
      await safeInvoke<void>('rebuild_code_graph', { projectId });
      expanded = false;
      dismissed = false;
    } catch (e) {
      if (view) view = { ...view, error_message: e instanceof Error ? e.message : String(e) };
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

  function detailLine(v: CodeGraphBuildView): string {
    const parts: string[] = [];
    // Partial: lead with the stale-row warning (error_message carries the
    // "N stale row(s) could not be pruned" text set by the reader). It's
    // informational, not a failure — inserts all succeeded.
    if (v.status === 'partial' && v.error_message) parts.push(v.error_message);
    if (v.languages.length > 0) parts.push(`Languages: ${v.languages.join(', ')}`);
    if (v.duration_ms != null) parts.push(`Took ${(v.duration_ms / 1000).toFixed(1)}s`);
    // v0.2.73 (CG-3): Joern CFG/PDG removed (zero readers) — joern_used is now
    // always false; the "Joern: enabled" line is dead and removed.
    return parts.join(' · ');
  }

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
  <div
    class="bg-banner status-{view.status}"
    role={view.status === 'failed' ? 'alert' : 'status'}
    aria-live="polite"
  >
    <div class="bg-row">
      <span class="bg-glyph" class:spin={view.status === 'running'} aria-hidden="true">
        {statusGlyph(view.status)}
      </span>
      <div class="bg-text">
        <div class="bg-label">{statusLabel(view)}</div>
        {#if detailLine(view)}
          <div class="bg-detail">{detailLine(view)}</div>
        {/if}
      </div>
      <div class="bg-actions">
        {#if view.status === 'failed'}
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
            {rerunning ? 'Retrying…' : 'Retry build'}
          </button>
        {/if}
        {#if view.status === 'partial'}
          <button
            type="button"
            class="bg-btn-primary"
            onclick={rerun}
            disabled={rerunning}
          >
            {rerunning ? 'Rebuilding…' : 'Rebuild'}
          </button>
        {/if}
        {#if view.status === 'success' || view.status === 'skipped' || view.status === 'partial'}
          <button
            type="button"
            class="bg-btn-x"
            aria-label="Dismiss banner"
            onclick={() => (dismissed = true)}
          >×</button>
        {/if}
      </div>
    </div>

    {#if expanded && view.status === 'failed'}
      <div class="bg-expand" role="dialog" aria-label="Code graph build failure detail">
        <div class="bg-expand-row">
          <strong>Error</strong>
          <pre class="bg-pre">{view.error_message ?? 'No error message persisted (check launcher logs).'}</pre>
        </div>
        {#if view.log_tail}
          <div class="bg-expand-row">
            <strong>Log tail</strong>
            <pre class="bg-pre">{view.log_tail}</pre>
          </div>
        {/if}
      </div>
    {/if}
  </div>
{/if}

<style>
  /* Banner styling mirrors `.orch-banner` in `routes/project/[id]/+page.svelte`
     for action-row layout, and `BrowserModeBanner` for the status-row
     decoration (left glyph, soft-tinted background, border-bottom). Kept
     local rather than factored to a shared `_banner.css` because there's
     no shared style module today and the existing `.orch-banner` keeps
     its styles inline — same convention. */
  .bg-banner {
    display: block;
    border-bottom: 1px solid transparent;
    font-size: 13px;
    line-height: 1.4;
  }
  .bg-row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 24px;
  }
  .bg-glyph {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    font-family: ui-monospace, monospace;
    font-size: 12px;
    font-weight: 600;
    flex-shrink: 0;
  }
  .bg-glyph.spin { animation: bg-spin 1.4s linear infinite; }
  @keyframes bg-spin {
    from { transform: rotate(0deg); }
    to   { transform: rotate(360deg); }
  }
  .bg-text { flex: 1; min-width: 0; }
  .bg-label { font-weight: 600; }
  .bg-detail { font-size: 12px; color: rgba(255,255,255,0.55); margin-top: 2px; }
  .bg-actions {
    display: flex;
    gap: 8px;
    flex-shrink: 0;
  }

  .bg-btn-secondary, .bg-btn-primary {
    padding: 4px 12px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    border: 1px solid transparent;
    font-family: inherit;
  }
  .bg-btn-secondary {
    background: rgba(255,255,255,0.06);
    color: #ccc;
    border-color: rgba(255,255,255,0.12);
  }
  .bg-btn-secondary:hover { background: rgba(255,255,255,0.1); }
  .bg-btn-primary {
    background: rgb(0,191,166);
    color: #001a17;
  }
  .bg-btn-primary:hover:not(:disabled) { background: rgb(0,210,180); }
  .bg-btn-primary:disabled { opacity: 0.5; cursor: default; }
  .bg-btn-x {
    background: none; border: none; color: inherit;
    font-size: 18px; line-height: 1; cursor: pointer;
    padding: 0 8px; border-radius: 6px;
    opacity: 0.6;
  }
  .bg-btn-x:hover { opacity: 1; background: rgba(255,255,255,0.06); }

  .bg-expand {
    padding: 8px 24px 14px 56px;
    font-size: 12px;
    border-top: 1px dashed rgba(255,255,255,0.08);
  }
  .bg-expand-row { margin-bottom: 8px; }
  .bg-expand-row strong {
    display: block;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: rgba(255,255,255,0.5);
    margin-bottom: 4px;
  }
  .bg-pre {
    margin: 0;
    padding: 6px 8px;
    background: rgba(255,255,255,0.04);
    border-radius: 4px;
    font-family: ui-monospace, monospace;
    font-size: 11px;
    line-height: 1.45;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 220px;
    overflow-y: auto;
    color: rgba(255,255,255,0.85);
  }

  /* Per-status tinting. Colour values cloned from `CodeGraphBuildPill`
     so the pill-era and banner-era stay visually consistent. */
  .status-pending {
    background: rgba(255,255,255,0.04);
    border-bottom-color: rgba(255,255,255,0.10);
    color: var(--color-mid, #999);
  }
  .status-pending .bg-glyph {
    background: rgba(255,255,255,0.06);
    color: #999;
  }
  .status-running {
    background: rgba(0,191,166,0.08);
    border-bottom-color: rgba(0,191,166,0.30);
    color: rgb(0,191,166);
  }
  .status-running .bg-glyph {
    background: rgba(0,191,166,0.15);
    color: rgb(0,191,166);
  }
  .status-success {
    background: rgba(70, 200, 120, 0.08);
    border-bottom-color: rgba(70, 200, 120, 0.30);
    color: rgb(120, 220, 160);
  }
  .status-success .bg-glyph {
    background: rgba(70, 200, 120, 0.18);
    color: rgb(120, 220, 160);
  }
  /* Partial (v0.2.73 C-11): inserts succeeded, stale prune incomplete.
     Amber warning tint — distinct from the pink `failed` (a hard error)
     and the green `success`. Reuses the skipped-amber hue. */
  .status-partial {
    background: rgba(245, 179, 66, 0.10);
    border-bottom-color: rgba(245, 179, 66, 0.35);
    color: rgb(245, 179, 66);
  }
  .status-partial .bg-glyph {
    background: rgba(245, 179, 66, 0.20);
    color: rgb(245, 179, 66);
  }
  .status-failed {
    background: rgba(255, 79, 160, 0.10);
    border-bottom-color: rgba(255, 79, 160, 0.35);
    color: rgb(255, 130, 180);
  }
  .status-failed .bg-glyph {
    background: rgba(255, 79, 160, 0.18);
    color: rgb(255, 130, 180);
  }
  .status-skipped {
    background: rgba(245, 179, 66, 0.08);
    border-bottom-color: rgba(245, 179, 66, 0.30);
    color: rgb(245, 179, 66);
  }
  .status-skipped .bg-glyph {
    background: rgba(245, 179, 66, 0.18);
    color: rgb(245, 179, 66);
  }
</style>
