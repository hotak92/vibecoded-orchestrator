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

  // v0.2.95 R7: the chrome (markup skeleton + the ~150 CSS lines this file
  // admitted were "cloned verbatim from KgSyncBanner / CodeGraphBuildBanner")
  // moved to the shared `StatusBannerShell.svelte`. Labels, counters, retry
  // semantics and visibility policy stay here — the rendered result is
  // unchanged.

  import { onDestroy, onMount } from 'svelte';
  import { listen, invoke, safeInvoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { KgSummaryStatus, KgSummaryView } from '$lib/types/launcher';
  import StatusBannerShell from './StatusBannerShell.svelte';
  import { toneForKgSummaryStatus } from './status-banner-tone';

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
  <StatusBannerShell
    tone={toneForKgSummaryStatus(view.status)}
    glyph={statusGlyph(view.status)}
    spinning={view.status === 'running'}
    title={statusLabel(view)}
    detail={detailLine(view)}
    alert={view.status === 'failed'}
    showExpanded={expanded && (view.status === 'failed' || view.status === 'skipped')}
    expandedLabel="KG summary failure detail"
  >
    {#snippet actions()}
      {#if view?.status === 'failed' || view?.status === 'skipped'}
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
      {#if view?.status === 'success' || view?.status === 'skipped'}
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
        <strong>{view?.status === 'skipped' ? 'Reason' : 'Error'}</strong>
        <pre class="bg-pre">{view?.error_message ?? 'No detail persisted (check launcher logs).'}</pre>
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
