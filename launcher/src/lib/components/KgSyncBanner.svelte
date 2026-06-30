<script lang="ts">
  // Full-width status banner for the initial `kg-sync --all` run kicked
  // off by `create_project_v2` (and re-triggered by `retry_kg_sync`).
  //
  // Was previously `KgSyncPill.svelte`. Promoted to a full-width banner
  // 2026-05-12 for better visibility — for a project with 50+ pre-existing
  // KG nodes the embed pass takes 30-60s, and a small pill in the header
  // didn't make it clear that anything was happening.
  //
  // Behaviour identical to `CodeGraphBuildBanner.svelte` (intentional —
  // see the "two parallel banner components" rationale in
  // `.claude/context/kg-autosync-patch-2026-05-12.md`). The two banners
  // stack vertically in the project page; render order is wired in
  // `routes/project/[id]/+page.svelte`.

  import { onDestroy, onMount } from 'svelte';
  import { listen, safeInvoke } from '$lib/tauri';
  import type { KgSyncStatus, KgSyncView } from '$lib/types/launcher';

  interface Props {
    projectId: string;
    /** When set, banner stays mounted in terminal states (success/skipped)
     *  for `hideTerminalAfterMs` after `finished_at_iso`, then unmounts.
     *  Defaults to 30s so the user has time to read "Synced N nodes". */
    hideTerminalAfterMs?: number;
  }

  let { projectId, hideTerminalAfterMs = 30_000 }: Props = $props();

  let view = $state<KgSyncView | null>(null);
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
      await safeInvoke<void>('retry_kg_sync', { projectId });
      expanded = false;
      dismissed = false;
    } catch (e) {
      if (view) view = { ...view, error_message: e instanceof Error ? e.message : String(e) };
    } finally {
      retrying = false;
    }
  }

  async function load() {
    view = await safeInvoke<KgSyncView | null>('get_kg_sync_status', { projectId });
  }

  function maybeStartTick(v: KgSyncView) {
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
    unlisten = await listen<KgSyncView>(
      'kg-sync-progress',
      (e) => {
        if (e.payload.project_id !== projectId) return;
        view = {
          ...(view ?? {
            project_id: projectId,
            status: e.payload.status,
            started_at_iso: null,
            finished_at_iso: null,
            duration_ms: null,
            kg_total: 0,
            kg_succeeded: 0,
            kg_failed: 0,
            docs_total: 0,
            docs_succeeded: 0,
            docs_failed: 0,
            error_message: null,
            log_tail: null,
            current_phase: null,
          }),
          status: e.payload.status,
          kg_total: e.payload.kg_total ?? view?.kg_total ?? 0,
          kg_succeeded: e.payload.kg_succeeded ?? view?.kg_succeeded ?? 0,
          kg_failed: e.payload.kg_failed ?? view?.kg_failed ?? 0,
          docs_total: e.payload.docs_total ?? view?.docs_total ?? 0,
          docs_succeeded: e.payload.docs_succeeded ?? view?.docs_succeeded ?? 0,
          docs_failed: e.payload.docs_failed ?? view?.docs_failed ?? 0,
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

  function statusGlyph(s: KgSyncStatus): string {
    switch (s) {
      case 'pending': return '·';
      case 'running': return '⟳';
      case 'success': return '✓';
      case 'failed': return '!';
      case 'skipped': return '∅';
    }
  }

  function progressCounter(v: KgSyncView): string {
    const done = v.kg_succeeded + v.docs_succeeded;
    const total = v.kg_total + v.docs_total;
    if (total === 0) return '';
    return `${done} / ${total}`;
  }

  function statusLabel(v: KgSyncView): string {
    switch (v.status) {
      case 'pending':
        return 'KG sync: queued';
      case 'running': {
        const counter = progressCounter(v);
        if (v.current_phase === 'scan') return 'KG sync: scanning knowledge/ and docs/…';
        // v0.2.71 Piece 5a: waiting on the process-global single-flight lane
        // (another project's KG sync is running first; queued syncs WAIT).
        if (v.current_phase === 'queued') return 'KG sync: waiting for the embed lane…';
        if (counter) {
          if (v.current_phase === 'docs') return `KG sync: embedding docs (${counter})`;
          return `KG sync: embedding (${counter})`;
        }
        return 'KG sync: embedding…';
      }
      case 'success': {
        const total = v.kg_total + v.docs_total;
        if (total === 0) return 'KG sync: complete';
        return `KG sync: indexed ${total} node${total === 1 ? '' : 's'}`;
      }
      case 'failed':
        return 'KG sync: failed';
      case 'skipped':
        return 'KG sync: no knowledge/ or docs/ content to sync';
    }
  }

  function detailLine(v: KgSyncView): string {
    const parts: string[] = [];
    if (v.kg_total > 0 || v.kg_succeeded > 0) {
      parts.push(`knowledge/: ${v.kg_succeeded}/${v.kg_total}` +
        (v.kg_failed > 0 ? ` (${v.kg_failed} failed)` : ''));
    }
    if (v.docs_total > 0 || v.docs_succeeded > 0) {
      parts.push(`docs/: ${v.docs_succeeded}/${v.docs_total}` +
        (v.docs_failed > 0 ? ` (${v.docs_failed} failed)` : ''));
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
            onclick={retry}
            disabled={retrying}
          >
            {retrying ? 'Retrying…' : 'Retry sync'}
          </button>
        {/if}
        {#if view.status === 'success' || view.status === 'skipped'}
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
      <div class="bg-expand" role="dialog" aria-label="KG sync failure detail">
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
  /* Styles cloned verbatim from CodeGraphBuildBanner.svelte. The two
     banners are visually identical — only the labels, glyphs, and the
     event names differ. Kept inline rather than factored to a shared
     stylesheet to match how `.orch-banner` and `BrowserModeBanner`
     already live with their own inline styles (no shared theme module
     exists today). */
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
