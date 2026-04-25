<script lang="ts">
  // Audit log surface. Reads from the launcher.db `audit_log` table via
  // `list_audit_events`. Mutating operations across the launcher already
  // call `Db::audit(...)` so this view is the existing log, not a new
  // one. NDA-bound consultant work needs a who-changed-what record;
  // this is it.

  import { onMount } from 'svelte';
  import { safeInvoke, isTauriRuntime } from '$lib/tauri';
  import { selectedProject, projects } from '$lib/stores/projects';

  interface AuditEvent {
    id: number;
    operation: string;
    project_id: string | null;
    module_id: string | null;
    /** JSON-encoded string. */
    detail: string;
    /** epoch ms */
    created_at: number;
  }

  let events = $state<AuditEvent[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let filterProject = $state<'all' | string>('all');
  let filterText = $state('');

  const inTauri = isTauriRuntime();
  const allProjects = $derived($projects.projects);

  async function load() {
    loading = true;
    error = null;
    try {
      const project_id = filterProject === 'all' ? undefined : filterProject;
      const result = await safeInvoke<AuditEvent[]>('list_audit_events', {
        projectId: project_id,
        limit: 500,
      });
      events = result ?? [];
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    if ($selectedProject) filterProject = $selectedProject.id;
    load();
  });

  function fmtTime(ms: number): string {
    return new Date(ms).toLocaleString();
  }

  function projectName(id: string | null): string {
    if (!id) return '—';
    const p = allProjects.find((x) => x.id === id);
    return p ? p.name : id.slice(0, 8);
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

  const filtered = $derived(
    !filterText
      ? events
      : events.filter((e) => {
          const q = filterText.toLowerCase();
          return (
            e.operation.toLowerCase().includes(q) ||
            (e.project_id ?? '').toLowerCase().includes(q) ||
            (e.module_id ?? '').toLowerCase().includes(q) ||
            e.detail.toLowerCase().includes(q)
          );
        }),
  );
</script>

<div class="page">
  <header class="page-header">
    <h1>Audit log</h1>
    <p class="lede">
      Mutating operations recorded by the launcher: project create / rename /
      delete, module install / uninstall, secret set / clear, license
      activate / deactivate, access grants, and more.
    </p>
  </header>

  <div class="controls">
    <label>
      <span>Project:</span>
      <select bind:value={filterProject} onchange={load}>
        <option value="all">All projects</option>
        {#each allProjects as p}
          <option value={p.id}>{p.name}</option>
        {/each}
      </select>
    </label>
    <label class="search">
      <span>Search:</span>
      <input
        type="text"
        bind:value={filterText}
        placeholder="operation, project, detail…"
      />
    </label>
    <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={load} disabled={loading}>
      {loading ? 'Loading…' : 'Refresh'}
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
          <th class="col-project">Project</th>
          <th class="col-module">Module</th>
          <th>Detail</th>
        </tr>
      </thead>
      <tbody>
        {#each filtered as e (e.id)}
          <tr>
            <td class="col-time">{fmtTime(e.created_at)}</td>
            <td class="col-op"><code>{e.operation}</code></td>
            <td class="col-project">{projectName(e.project_id)}</td>
            <td class="col-module">{e.module_id ?? '—'}</td>
            <td class="col-detail" title={e.detail}>{detailSummary(e.detail)}</td>
          </tr>
        {/each}
        {#if !loading && filtered.length === 0}
          <tr>
            <td colspan="5" class="empty">No audit events match the current filter.</td>
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
  .controls select,
  .controls input {
    padding: 5px 8px;
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 6px;
    color: var(--color-text);
    font-size: 12px;
  }
  .controls .search {
    flex: 1;
    min-width: 220px;
  }
  .controls .search input {
    flex: 1;
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
  .col-project, .col-module {
    width: 140px;
    color: var(--color-text);
  }
  .col-detail {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
    color: var(--color-muted);
    word-break: break-all;
  }
  td.empty {
    text-align: center;
    padding: 28px 12px;
    color: var(--color-muted);
  }
</style>
