<script lang="ts">
  // Audit log surface. Reads from the launcher.db `audit_log` table via
  // `list_audit_events`. Mutating operations across the launcher already
  // call `Db::audit(...)` so this view is the existing log, not a new
  // one. NDA-bound consultant work needs a who-changed-what record;
  // this is it.

  import { onMount } from 'svelte';
  import { safeInvoke, isTauriRuntime } from '$lib/tauri';
  import { selectedProject, projects } from '$lib/stores/projects';
  import Dropdown from '$lib/components/Dropdown.svelte';

  interface AuditEvent {
    id: number;
    operation: string;
    project_id: string | null;
    module_id: string | null;
    /** JSON-encoded string. */
    detail: string;
    /** OS user who performed the operation. */
    actor: string;
    /** epoch ms */
    created_at: number;
  }

  let events = $state<AuditEvent[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let filterProject = $state<'all' | string>('all');
  let filterText = $state('');
  let filterActor = $state('');

  // Bug 9: dismissible plain-English explainer. Most users never need
  // this tab; the explainer tells them what it's for and how to use
  // it. Persists `vct.audit_intro_dismissed=true` in localStorage so
  // power-users can collapse it after first read.
  const INTRO_KEY = 'vct.audit_intro_dismissed';
  let introDismissed = $state(false);
  if (typeof localStorage !== 'undefined') {
    try { introDismissed = localStorage.getItem(INTRO_KEY) === 'true'; }
    catch {}
  }
  function dismissIntro() {
    introDismissed = true;
    try { localStorage.setItem(INTRO_KEY, 'true'); } catch {}
  }

  // Time-range filter. 'custom' uses customFrom/customTo (epoch ms or
  // null = open). Bounds and the search/actor filters are pushed into
  // the SQL layer of `list_audit_events` so the wire payload only
  // carries matching rows. Earlier revisions filtered client-side over
  // a 500-event window and fell over for high-volume audit logs.
  type RangeKey = '24h' | '7d' | '30d' | 'all' | 'custom';
  let filterRange = $state<RangeKey>('all');
  // Hour-precision custom range — datetime-local format (yyyy-MM-ddTHH:mm).
  // Power-user verdict flagged calendar-day-only granularity as too coarse
  // for narrowing down a specific incident window.
  let customFrom = $state('');
  let customTo = $state('');

  const inTauri = isTauriRuntime();
  const allProjects = $derived($projects.projects);

  async function load() {
    loading = true;
    error = null;
    try {
      const project_id = filterProject === 'all' ? undefined : filterProject;
      const [from, to] = rangeBounds(filterRange);
      const actor = filterActor.trim() || undefined;
      const search = filterText.trim() || undefined;
      const result = await safeInvoke<AuditEvent[]>('list_audit_events', {
        projectId: project_id,
        actor,
        sinceMs: from ?? undefined,
        untilMs: to ?? undefined,
        search,
        limit: 5000,
      });
      events = result ?? [];
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  // Re-fetch when any server-side filter changes. Debounced for the
  // free-text inputs so we don't spam SQL on every keystroke.
  let textDebounce: ReturnType<typeof setTimeout> | null = null;
  function scheduleReload(immediate: boolean = false) {
    if (textDebounce) {
      clearTimeout(textDebounce);
      textDebounce = null;
    }
    if (immediate) {
      load();
      return;
    }
    textDebounce = setTimeout(() => {
      textDebounce = null;
      load();
    }, 250);
  }

  onMount(() => {
    if ($selectedProject) filterProject = $selectedProject.id;
    load();
  });

  function fmtTime(ms: number): string {
    return new Date(ms).toLocaleString();
  }

  // Sentinels used by the secrets layer (and any future scope-level
  // operation) when an audit row isn't tied to a single project. See
  // `commands/secrets_cmd.rs` SENTINEL_GLOBAL / SENTINEL_SHARED.
  const SENTINEL_GLOBAL = '_global_';
  const SENTINEL_SHARED = '_user_shared_';

  /** Plain text for CSV export only. */
  function projectName(id: string | null): string {
    if (!id) return '—';
    if (id === SENTINEL_GLOBAL) return 'global';
    if (id === SENTINEL_SHARED) return 'shared';
    const p = allProjects.find((x) => x.id === id);
    return p ? p.name : id.slice(0, 8);
  }

  /** Cell label + scope-chip class for the rendered table. */
  function projectCell(id: string | null): { label: string; chip: 'project' | 'global' | 'shared' | 'none' } {
    if (!id) return { label: '—', chip: 'none' };
    if (id === SENTINEL_GLOBAL) return { label: 'global', chip: 'global' };
    if (id === SENTINEL_SHARED) return { label: 'shared', chip: 'shared' };
    const p = allProjects.find((x) => x.id === id);
    return { label: p ? p.name : id.slice(0, 8), chip: 'project' };
  }

  function detailSummary(s: string): string {
    // Try to render a compact JSON summary; fall back to raw.
    try {
      const obj = JSON.parse(s);
      if (obj && typeof obj === 'object') {
        const keys = Object.keys(obj);
        if (keys.length === 0) return '{}';
        return keys
          .slice(0, 4)
          .map((k) => `${k}=${JSON.stringify(obj[k])}`)
          .join(', ');
      }
      return String(obj);
    } catch {
      return s;
    }
  }

  /** Returns [from, to] epoch ms (inclusive) or [null, null] for 'all'. */
  function rangeBounds(r: RangeKey): [number | null, number | null] {
    const now = Date.now();
    if (r === '24h') return [now - 24 * 3600 * 1000, null];
    if (r === '7d') return [now - 7 * 24 * 3600 * 1000, null];
    if (r === '30d') return [now - 30 * 24 * 3600 * 1000, null];
    if (r === 'custom') {
      // datetime-local format: yyyy-MM-ddTHH:mm — interpreted as local time.
      // new Date(s) on a missing-seconds string in local TZ is well-defined
      // in modern browsers (and the Tauri WebView).
      const from = customFrom ? new Date(customFrom).getTime() : null;
      const to = customTo ? new Date(customTo).getTime() : null;
      return [from, to];
    }
    return [null, null];
  }

  // The server-side filters (project, actor, time range, search) already
  // narrowed the rowset; we just expose `events` as `filtered` so the
  // table + CSV exporter keep their current callsite. Note the SQL
  // search matches operation OR detail; the previous JS pass also
  // matched against project_id / module_id / actor for the same query,
  // but those are now first-class filter inputs, so the loss is
  // intentional.
  const filtered = $derived(events);

  // Render module column. Secrets carry a real `module_id` (sub-scope
  // for keychain key derivation), but it's not the user-meaningful
  // subject of the action — the secret key + scope already identify
  // what changed and live in the detail blob. The MODULE column is
  // reserved for module-install / module-license operations where the
  // module IS the subject. For everything else, render `—`. CSV export
  // keeps the raw `module_id` regardless.
  function moduleCell(e: AuditEvent): string {
    if (e.operation.startsWith('secret_')) return '—';
    return e.module_id ?? '—';
  }

  // Hide the column entirely when no row in the current view uses it
  // (every cell would render as `—`). Avoids the dead-column visual
  // noise on the common "secrets only" filter while keeping the column
  // for module-install / license / catalog operations.
  const showModuleCol = $derived(
    filtered.some((e) => moduleCell(e) !== '—')
  );

  /** RFC 4180-ish CSV cell escape: wrap in quotes, double inner quotes. */
  function csvCell(s: string | number | null | undefined): string {
    const raw = s == null ? '' : String(s);
    if (/[",\n\r]/.test(raw)) return `"${raw.replace(/"/g, '""')}"`;
    return raw;
  }

  function exportCsv() {
    const header = ['timestamp_iso', 'timestamp_ms', 'operation', 'actor', 'project_id', 'project_name', 'module_id', 'detail'];
    const lines = [header.join(',')];
    for (const e of filtered) {
      lines.push([
        csvCell(new Date(e.created_at).toISOString()),
        csvCell(e.created_at),
        csvCell(e.operation),
        csvCell(e.actor),
        csvCell(e.project_id),
        csvCell(projectName(e.project_id)),
        csvCell(e.module_id),
        csvCell(e.detail),
      ].join(','));
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    a.download = `audit-log-${ts}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }
</script>

<svelte:head>
  <title>Audit — VCT Launcher</title>
</svelte:head>

<div class="page">
  <header class="page-header">
    <h1>Audit log</h1>
    <p class="lede">
      Every state-changing action across your projects (project create / rename
      / delete, secret rotations, license events, module installs, KG /
      codegraph access changes, hook + agent toggles). Useful for compliance,
      NDA-bound consultant work, and SOC2 deliverables.
      Stored locally in <code>~/.vct/launcher.db</code> — see
      <a href="https://github.com/VibeCoded-Tools/orchestrator/blob/main/launcher/docs/AUDIT.md" target="_blank" rel="noopener">docs/AUDIT.md</a>
      for the full schema and operation list.
    </p>
  </header>

  {#if !introDismissed}
    <div class="audit-intro">
      <button class="audit-intro-dismiss" onclick={dismissIntro} aria-label="Hide introduction">× hide</button>
      <h3>What's this page for?</h3>
      <p>
        Every time you create a project, set a secret, install a module,
        activate a license, change KG access — that action gets logged here
        automatically. This page is a read-only history; you can't change
        settings from here (use Settings → Secrets, /project/&lt;id&gt;,
        etc. for that).
      </p>
      <p>Most users won't need this tab. It's useful when:</p>
      <ul>
        <li>You're a freelance consultant proving to a client what changed in their project (export to CSV)</li>
        <li>Something broke after a configuration change and you want to see when/by whom</li>
        <li>You need a SOC2-style audit deliverable</li>
      </ul>
      <p class="muted">
        Tip: filter by project, by actor, by date range, or search inside
        the operation/detail. Hit Export CSV to send to a client.
      </p>
    </div>
  {/if}

  <div class="controls">
    <label>
      <span>Project:</span>
      <div class="dd-shell">
        <Dropdown
          options={[
            { value: 'all', label: 'All projects' },
            { value: SENTINEL_SHARED, label: 'shared (cross-project)' },
            { value: SENTINEL_GLOBAL, label: 'global' },
            ...allProjects.map((p) => ({ value: p.id, label: p.name })),
          ]}
          value={filterProject}
          onChange={(v: string) => { filterProject = v; scheduleReload(true); }}
        />
      </div>
    </label>
    <div class="range-pills" role="group" aria-label="Time range">
      <span class="range-label">When:</span>
      {#each [['24h', '24h'], ['7d', '7d'], ['30d', '30d'], ['all', 'All'], ['custom', 'Custom']] as [val, label]}
        <button
          type="button"
          class="range-pill"
          class:range-pill-active={filterRange === val}
          onclick={() => { filterRange = val as RangeKey; scheduleReload(true); }}
        >{label}</button>
      {/each}
    </div>
    {#if filterRange === 'custom'}
      <label class="custom-range">
        <span>From:</span>
        <input type="datetime-local" bind:value={customFrom} onchange={() => scheduleReload(true)} />
      </label>
      <label class="custom-range">
        <span>To:</span>
        <input type="datetime-local" bind:value={customTo} onchange={() => scheduleReload(true)} />
      </label>
    {/if}
    <label class="actor">
      <span>Actor:</span>
      <input
        type="text"
        bind:value={filterActor}
        placeholder="username"
        oninput={() => scheduleReload()}
      />
    </label>
    <label class="search">
      <span>Search:</span>
      <input
        type="text"
        bind:value={filterText}
        placeholder="operation, detail…"
        oninput={() => scheduleReload()}
      />
    </label>
    <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={load} disabled={loading}>
      {loading ? 'Loading…' : 'Refresh'}
    </button>
    <button
      class="btn-3d btn-3d-ghost btn-3d-sm"
      onclick={exportCsv}
      disabled={filtered.length === 0}
      title="Export the currently filtered table as CSV"
    >
      Export CSV ({filtered.length})
    </button>
  </div>

  {#if !inTauri}
    <p class="note">
      Audit log requires the Tauri desktop runtime. In browser preview the
      table will be empty.
    </p>
  {/if}

  {#if error}
    <p class="error">{error}</p>
  {/if}

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="col-time">Time</th>
          <th class="col-op">Operation</th>
          <th class="col-actor">Actor</th>
          <th class="col-project">Project</th>
          {#if showModuleCol}<th class="col-module">Module</th>{/if}
          <th>Detail</th>
        </tr>
      </thead>
      <tbody>
        {#each filtered as e (e.id)}
          {@const pc = projectCell(e.project_id)}
          <tr>
            <td class="col-time">{fmtTime(e.created_at)}</td>
            <td class="col-op"><code>{e.operation}</code></td>
            <td class="col-actor"><code>{e.actor ?? 'unknown'}</code></td>
            <td class="col-project">
              {#if pc.chip === 'global' || pc.chip === 'shared'}
                <span class="scope-chip scope-{pc.chip}">{pc.label}</span>
              {:else}
                {pc.label}
              {/if}
            </td>
            {#if showModuleCol}<td class="col-module">{moduleCell(e)}</td>{/if}
            <td class="col-detail" title={e.detail}>{detailSummary(e.detail)}</td>
          </tr>
        {/each}
        {#if !loading && filtered.length === 0}
          <tr>
            <td colspan={showModuleCol ? 6 : 5} class="empty">No audit events match the current filter.</td>
          </tr>
        {/if}
      </tbody>
    </table>
  </div>
</div>

<style>
  .page {
    padding: 24px 28px 60px;
  }

  .page-header {
    margin-bottom: 18px;
  }

  h1 {
    font-size: 22px;
    font-weight: 800;
    color: var(--color-text);
    letter-spacing: -0.5px;
    margin-bottom: 4px;
  }
  .lede {
    font-size: 13px;
    color: var(--color-mid);
    max-width: 720px;
  }

  .audit-intro {
    position: relative;
    margin: 0 0 18px;
    padding: 14px 18px;
    background: rgba(0, 191, 166, 0.06);
    border: 1px solid rgba(0, 191, 166, 0.18);
    border-radius: 12px;
    color: var(--color-text);
    max-width: 760px;
  }
  .audit-intro h3 {
    font-size: 13px;
    font-weight: 700;
    color: var(--color-teal, #0fc);
    margin: 0 0 8px;
  }
  .audit-intro p {
    font-size: 13px;
    color: var(--color-mid);
    line-height: 1.55;
    margin: 0 0 8px;
  }
  .audit-intro ul {
    margin: 0 0 8px 0;
    padding-left: 20px;
    font-size: 12px;
    color: var(--color-mid);
    line-height: 1.55;
  }
  .audit-intro li {
    margin-bottom: 3px;
  }
  .audit-intro p.muted {
    color: var(--color-muted);
    font-size: 12px;
    font-style: italic;
  }
  .audit-intro-dismiss {
    position: absolute;
    top: 8px;
    right: 10px;
    background: none;
    border: none;
    color: var(--color-muted);
    font-size: 11px;
    cursor: pointer;
    padding: 4px 6px;
    border-radius: 4px;
  }
  .audit-intro-dismiss:hover {
    color: var(--color-text);
    background: rgba(255, 255, 255, 0.06);
  }

  .controls {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
    margin-bottom: 16px;
    padding: 12px 14px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 12px;
  }
  .controls label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--color-mid);
  }
  .controls input {
    padding: 5px 8px;
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 6px;
    color: var(--color-text);
    font-size: 12px;
  }
  .dd-shell { min-width: 180px; }
  .controls .search {
    flex: 1;
    min-width: 220px;
  }
  .controls .search input {
    flex: 1;
  }
  .range-pills {
    display: flex; align-items: center; gap: 4px; flex-wrap: wrap;
  }
  .range-label {
    font-size: 12px; color: var(--color-mid); margin-right: 4px;
  }
  .range-pill {
    padding: 4px 10px;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 999px;
    color: var(--color-mid);
    font-size: 11px; font-weight: 600;
    cursor: pointer;
    transition: all 0.12s ease;
  }
  .range-pill:hover {
    background: rgba(255,255,255,0.08);
    color: var(--color-text);
  }
  .range-pill-active {
    background: rgba(0, 191, 166, 0.16);
    border-color: rgba(0, 191, 166, 0.5);
    color: var(--color-teal);
  }
  .custom-range {
    display: flex; align-items: center; gap: 4px;
  }
  .custom-range input[type="datetime-local"] {
    padding: 4px 6px;
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 6px;
    color: var(--color-text);
    font-size: 11px;
    color-scheme: dark;
  }
  .actor input {
    width: 130px;
  }

  .note {
    margin: 8px 0 12px;
    font-size: 12px;
    color: #ffb066;
    padding: 8px 12px;
    background: rgba(255, 159, 64, 0.08);
    border: 1px solid rgba(255, 159, 64, 0.2);
    border-radius: 8px;
  }
  .error {
    margin: 8px 0 12px;
    font-size: 12px;
    color: var(--color-pink);
    padding: 8px 12px;
    background: rgba(255, 79, 160, 0.08);
    border: 1px solid rgba(255, 79, 160, 0.2);
    border-radius: 8px;
  }

  .table-wrap {
    overflow-x: auto;
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 10px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  th, td {
    text-align: left;
    padding: 8px 10px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    vertical-align: top;
  }
  th {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: var(--color-muted);
    background: rgba(255, 255, 255, 0.03);
  }
  td {
    color: var(--color-mid);
  }
  td code {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
    color: var(--color-teal);
  }
  .col-time {
    white-space: nowrap;
    width: 170px;
    color: var(--color-muted);
  }
  .col-op {
    width: 200px;
  }
  .col-actor {
    width: 120px;
    white-space: nowrap;
  }
  .col-actor code {
    color: var(--color-purple, #c4b3ff);
  }
  .col-project, .col-module {
    width: 140px;
    color: var(--color-text);
  }
  .scope-chip {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    line-height: 1.4;
  }
  .scope-global {
    background: rgba(196, 179, 255, 0.14);
    color: #c4b3ff;
    border: 1px solid rgba(196, 179, 255, 0.35);
  }
  .scope-shared {
    background: rgba(0, 191, 166, 0.14);
    color: var(--color-teal);
    border: 1px solid rgba(0, 191, 166, 0.4);
  }
  .col-detail {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
    color: var(--color-muted);
    /* Long JSON details (e.g. a Windows folder_path with doubled
       backslashes) previously used `word-break: break-all`, which wrapped
       into a tall multi-line cell and — because every other column is
       `vertical-align: top` — left those columns stranded at the top-left
       while the detail sprawled down the row. Clamp the column to a single
       truncated line instead; the full value is still available via the
       cell's `title` tooltip on hover. */
    max-width: 520px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  td.empty {
    text-align: center;
    padding: 28px 12px;
    color: var(--color-muted);
  }
</style>
