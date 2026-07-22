<script lang="ts">
  // Full-width status banner for the initial `generate-kg-summary.py`
  // backfill kicked off by `create_project_v2` (and re-triggered by
  // `retry_kg_summary`).
  //
  // Mirrors `KgSyncBanner.svelte` 1:1 — same self-managed visibility
  // (terminal states fade after 30s, failed never auto-hides), same
  // inline expand-on-click for failure details, same retry affordance.
  // Same styling tokens. The two banners stack vertically in the project
  // page; render order is wired in `routes/project/[id]/+page.svelte`
  // (KG summary on top — newest task in the add-project spawn sequence).
  //
  // Why a third parallel banner instead of squashing all background
  // tasks into one "Pipeline" component: each task has its own failure
  // mode (Weaviate down vs. Ollama down vs. no `claude` CLI vs. venv
  // missing), its own retry semantics, and runs at its own cadence.
  // Combining them would mean either rendering 3 sub-rows inside one
  // banner (no visual benefit over stacking 3 banners) or hiding
  // independent failures behind a single status indicator. Keeping
  // them parallel preserves the v0.2.2 mental model.

  import { onDestroy, onMount } from 'svelte';
  import { listen, invoke, safeInvoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { KgSummaryStatus, KgSummaryView } from '$lib/types/launcher';

  interface Props {
    projectId: string;
    /** When set, banner stays mounted in terminal states (success/skipped)
     *  for `hideTerminalAfterMs` after `finished_at_iso`, then unmounts.
     *  Defaults to 30s so the user has time to read "Summarised N nodes". */
    hideTerminalAfterMs?: number;
  }

  let { projectId, hideTerminalAfterMs = 30_000 }: Props = $props();

  let view = $state<KgSummaryView | null>(null);
  let unlisten: (() => void) | null = null;
  let expanded = $state(false);
  let retrying = $state(false);
  let dismissed = $state(false);
  let now = $state(Date.now());
  let tickHandle: ReturnType<typeof setInterval> | null = null;

  async function retry() {
    if (retrying) return;
    retrying = true;
    try {
      // Strict invoke so a failed retry surfaces rather than no-op'ing.
      await invoke<void>('retry_kg_summary', { projectId });
      expanded = false;
      dismissed = false;
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (view) view = { ...view, error_message: msg };
      toast.error(`KG summary retry failed: ${msg}`);
    } finally {
      retrying = false;
    }
  }

  async function load() {
    view = await safeInvoke<KgSummaryView | null>('get_kg_summary_status', { projectId });
  }

  function maybeStartTick(v: KgSummaryView) {
    const terminal = v.status === 'success' || v.status === 'skipped';
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
    unlisten = await listen<KgSummaryView>(
      'kg-summary-progress',
      (e) => {
        if (e.payload.project_id !== projectId) return;
        view = {
          ...(view ?? {
            project_id: projectId,
            status: e.payload.status,
            started_at_iso: null,
            finished_at_iso: null,
            duration_ms: null,
            nodes_total: 0,
            nodes_succeeded: 0,
            nodes_unchanged: 0,
            nodes_failed: 0,
            nodes_skipped: 0,
            backend: null,
            error_message: null,
            log_tail: null,
            current_phase: null,
          }),
          status: e.payload.status,
          nodes_total: e.payload.nodes_total ?? view?.nodes_total ?? 0,
          nodes_succeeded: e.payload.nodes_succeeded ?? view?.nodes_succeeded ?? 0,
          nodes_unchanged: e.payload.nodes_unchanged ?? view?.nodes_unchanged ?? 0,
          nodes_failed: e.payload.nodes_failed ?? view?.nodes_failed ?? 0,
          nodes_skipped: e.payload.nodes_skipped ?? view?.nodes_skipped ?? 0,
          backend: e.payload.backend ?? view?.backend ?? null,
          current_phase: e.payload.current_phase,
          error_message: e.payload.error_message ?? view?.error_message ?? null,
        };

        if (
          e.payload.status === 'success' ||
          e.payload.status === 'failed' ||
          e.payload.status === 'skipped'
        ) {
          void load().then(() => { if (view) maybeStartTick(view); });
        }
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

  function statusGlyph(s: KgSummaryStatus): string {
    switch (s) {
      case 'pending': return '·';
      case 'running': return '⟳';
      case 'success': return '✓';
      case 'failed': return '!';
      case 'skipped': return '∅';
    }
  }

  function progressCounter(v: KgSummaryView): string {
    // Count all "processed" nodes (anything no longer pending).
    const done =
      v.nodes_succeeded + v.nodes_unchanged + v.nodes_failed + v.nodes_skipped;
    if (v.nodes_total === 0) return '';
    return `${done} / ${v.nodes_total}`;
  }

  function statusLabel(v: KgSummaryView): string {
    switch (v.status) {
      case 'pending':
        return 'KG summaries: queued';
      case 'running': {
        const counter = progressCounter(v);
        if (v.current_phase === 'scan') return 'KG summaries: scanning knowledge/…';
        if (counter) {
          if (v.backend) return `KG summaries: ${v.backend} (${counter})`;
          return `KG summaries: summarising (${counter})`;
        }
        return 'KG summaries: summarising…';
      }
      case 'success': {
        const total = v.nodes_total;
        if (total === 0) return 'KG summaries: complete';
        // Aggregate "new + unchanged" → "synced N"; failures show
        // separately if any.
        const ok = v.nodes_succeeded + v.nodes_unchanged;
        if (v.nodes_failed > 0) {
          return `KG summaries: ${ok} of ${total} (${v.nodes_failed} failed)`;
        }
        return `KG summaries: summarised ${total} node${total === 1 ? '' : 's'}`;
      }
      case 'failed':
        return 'KG summaries: failed';
      case 'skipped': {
        // The most likely skipped reason — no backend — is communicated
        // via the error_message expansion. The headline stays terse.
        return 'KG summaries: skipped';
      }
    }
  }

  function detailLine(v: KgSummaryView): string {
    const parts: string[] = [];
    if (v.backend && v.status !== 'skipped') parts.push(`backend: ${v.backend}`);
    if (v.nodes_succeeded > 0) parts.push(`new: ${v.nodes_succeeded}`);
    if (v.nodes_unchanged > 0) parts.push(`unchanged: ${v.nodes_unchanged}`);
    if (v.nodes_failed > 0) parts.push(`failed: ${v.nodes_failed}`);
    // Suppress nodes_skipped in the detail line for the common "every
    // node skipped because no backend" case — the headline already
    // says "skipped" and the inline expansion has the actionable hint.
    if (v.nodes_skipped > 0 && v.status !== 'skipped') {
      parts.push(`skipped: ${v.nodes_skipped}`);
    }
    if (v.duration_ms != null) parts.push(`Took ${(v.duration_ms / 1000).toFixed(1)}s`);
    return parts.join(' · ');
  }

  let visible = $derived.by(() => {
    if (!view) return false;
    if (dismissed) return false;
    if (view.status === 'failed' || view.status === 'pending' || view.status === 'running') {
      return true;
    }
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
        {#if view.status === 'failed' || view.status === 'skipped'}
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
            onclick={retry}
            disabled={retrying}
          >
            {retrying ? 'Retrying…' : 'Retry'}
          </button>
        {/if}
        {#if view.status === 'success'}
          <button
            type="button"
            class="bg-btn-x"
            aria-label="Dismiss banner"
            onclick={() => (dismissed = true)}
          >×</button>
        {/if}
      </div>
    </div>

    {#if expanded && (view.status === 'failed' || view.status === 'skipped')}
      <div class="bg-expand" role="dialog" aria-label="KG summary failure detail">
        <div class="bg-expand-row">
          <strong>{view.status === 'skipped' ? 'Reason' : 'Error'}</strong>
          <pre class="bg-pre">{view.error_message ?? 'No detail persisted (check launcher logs).'}</pre>
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
  /* Styles cloned verbatim from KgSyncBanner / CodeGraphBuildBanner —
     the three banners share the same visual language. Kept inline rather
     than factored to a shared stylesheet to match how `.orch-banner` and
     `BrowserModeBanner` already live with their own inline styles (no
     shared theme module exists today). */
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
