<!--
  RL Reranker dashboard widget (v0.2.31 Agent J).

  Reads the container-owned `rl_weights_state` row for the active
  embedding source via the hub's typed REST surface:

      GET /api/v1/modules/vct-rl-reranker/db/projects/{project_id}
          /rows/rl_weights_state/{embedding_source}
          ?fields=local_version,last_finetuned_at

  Why hub reads (not direct DB reads):
    * The `rl_weights_state` table is owned by vct-rl-reranker v0.2.6
      (its module-shipped migration `db/0002_rl_weights_state.sql`,
      applied via Agent I's mechanism at install time).
    * The container is the sole writer; the hub mediates reads with
      per-(module, project) bearer-token auth (Agent I, migration 019).
    * Same code path the container would take to refresh its own
      state — validates the rows API end-to-end.

  Soft-fail behaviour:
    * Loading: small spinner / "Loading…" gray text.
    * Hub unreachable (timeout / network): "Container not running".
    * 404 (row absent / no migrations applied): "—" placeholder.
    * Any other error: "—" placeholder with the error logged to
      console.error (NOT toasted — this is a passive widget; we don't
      want every transient hub blip to spam the user).
    * The widget NEVER crashes the parent page.

  Refresh policy (v0.2.31 simplification):
    * Read once on mount.
    * "Refresh" button (icon: ↻) for manual re-fetch.
    * No auto-polling. v0.2.32 may add interval polling once we measure
      whether the missing live updates matter for the UX.

  Out of scope (v0.2.32):
    * `global_weights_status` display — depends on vct-rl-reranker's
      `db/0005_rl_global_weights_available.sql` which is NOT in v0.2.6.
      The TODO marker below is where that wiring goes.
-->

<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke, tauriAvailable } from '$lib/tauri';

  // Module identity. Hard-coded for v0.2.31 — only one container-based
  // RL module ships. A future v0.2.32+ would parameterise this via a
  // prop / store once a second RL module exists.
  const MODULE_ID = 'vct-rl-reranker';
  const TABLE = 'rl_weights_state';
  const FIELDS = ['local_version', 'last_finetuned_at'];

  // Wire shape the hub returns when the row exists. Matches the column
  // names declared by vct-rl-reranker v0.2.6's `db/0002_*.sql`.
  interface WeightsStateRow {
    local_version?: string | null;
    last_finetuned_at?: number | null;
  }

  let {
    projectId,
    embeddingSource = 'qwen3',
  }: {
    projectId: string;
    embeddingSource?: string;
  } = $props();

  let loading = $state(true);
  let row = $state<WeightsStateRow | null>(null);
  let errorKind = $state<'none' | 'unreachable' | 'absent' | 'other'>('none');

  async function load() {
    loading = true;
    errorKind = 'none';
    if (!tauriAvailable() || !projectId) {
      // Browser mode / unselected project — render placeholders, never crash.
      loading = false;
      row = null;
      errorKind = 'absent';
      return;
    }
    try {
      // `null` return ⇒ hub returned 404 (row absent OR module has no
      // migrations applied yet — both render as "—" / "never" below).
      const result = await invoke<WeightsStateRow | null>(
        'module_db_read_row',
        {
          moduleId: MODULE_ID,
          projectId,
          table: TABLE,
          key: embeddingSource,
          fields: FIELDS,
        },
      );
      row = result;
      if (result === null) {
        errorKind = 'absent';
      }
    } catch (e) {
      // Hub down / token-issue failure / timeout — distinguish
      // "container not running" (network error) from other failures
      // for the user-visible copy.
      const msg = e instanceof Error ? e.message : String(e);
      // The Rust client returns these strings; keying off them lets the
      // widget show user-meaningful copy without parsing structured errors.
      if (msg.includes('hub GET') || msg.includes('read hub.port')) {
        errorKind = 'unreachable';
      } else {
        errorKind = 'other';
      }
      console.error('[RlRerankerDashboardWidget] hub read failed:', msg);
      row = null;
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    void load();
  });

  // ─── Display helpers ────────────────────────────────────────────────

  // The empty-string ⇒ never-trained convention is shared with the
  // container's column default; we treat null + empty alike.
  const weightsVersion = $derived(
    row?.local_version && row.local_version.length > 0 ? row.local_version : '—',
  );

  // last_finetuned_at is unix-ms; 0 / null / undefined ⇒ "never".
  // We format as a relative span so the user doesn't have to mental-
  // math the timestamp. Threshold: under 60 s ⇒ "just now"; otherwise
  // pick the largest unit that yields a whole-number quantity.
  function relativeTime(unixMs: number | null | undefined): string {
    if (!unixMs || unixMs <= 0) return 'never';
    const now = Date.now();
    const delta = now - unixMs;
    if (delta < 60_000) return 'just now';
    const minutes = Math.floor(delta / 60_000);
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} hr ago`;
    const days = Math.floor(hours / 24);
    if (days < 30) return `${days} day${days === 1 ? '' : 's'} ago`;
    const months = Math.floor(days / 30);
    if (months < 12) return `${months} mo ago`;
    const years = Math.floor(months / 12);
    return `${years} yr${years === 1 ? '' : 's'} ago`;
  }

  const lastTraining = $derived(relativeTime(row?.last_finetuned_at));

  // User-facing copy for non-OK states. Each tier maps to one line.
  const fallbackCopy = $derived.by(() => {
    if (loading) return '';
    if (errorKind === 'unreachable') return 'Container not running';
    if (errorKind === 'absent' && row === null) return '—';
    return '';
  });
</script>

<div class="rl-dashboard-widget">
  <div class="header">
    <h3>RL Reranker — weights state</h3>
    <button
      class="refresh-btn"
      title="Refresh"
      aria-label="Refresh"
      disabled={loading}
      onclick={() => load()}
    >↻</button>
  </div>

  <dl class="state-grid">
    <div class="row">
      <dt>Weights version</dt>
      <dd class:gray={!row}>
        {#if loading}
          <span class="spinner" aria-label="Loading">…</span>
        {:else if fallbackCopy}
          {fallbackCopy}
        {:else}
          {weightsVersion}
        {/if}
      </dd>
    </div>

    <div class="row">
      <dt>Last training</dt>
      <dd class:gray={!row || (row.last_finetuned_at ?? 0) === 0}>
        {#if loading}
          <span class="spinner" aria-label="Loading">…</span>
        {:else if fallbackCopy}
          {fallbackCopy}
        {:else}
          {lastTraining}
        {/if}
      </dd>
    </div>

    <!-- TODO v0.2.32: wire global_weights_status display once
         vct-rl-reranker ships db/0005_rl_global_weights_available.sql.
         Will read via the same module_db_read_row Tauri command with
         table='rl_global_weights_available'. -->
  </dl>
</div>

<style>
  .rl-dashboard-widget {
    background: var(--color-bg-elev, rgba(255, 255, 255, 0.03));
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .header h3 {
    margin: 0;
    font-size: 13px;
    font-weight: 600;
    color: var(--color-fg, #f3f4f6);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .refresh-btn {
    background: transparent;
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: var(--color-mid, #9ca3af);
    width: 28px;
    height: 28px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 14px;
    line-height: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .refresh-btn:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.06);
    color: var(--color-fg, #f3f4f6);
  }
  .refresh-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .state-grid {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: 0;
    padding: 0;
  }

  .row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    padding: 6px 0;
    border-bottom: 1px dashed rgba(255, 255, 255, 0.06);
  }
  .row:last-child {
    border-bottom: none;
  }

  dt {
    font-size: 13px;
    color: var(--color-mid, #9ca3af);
  }

  dd {
    margin: 0;
    font-size: 13px;
    font-family: var(--font-mono, ui-monospace, 'SF Mono', Menlo, monospace);
    color: var(--color-fg, #f3f4f6);
  }
  dd.gray {
    color: var(--color-mid, #9ca3af);
  }

  .spinner {
    color: var(--color-mid, #9ca3af);
    font-style: italic;
  }
</style>
