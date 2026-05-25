<script lang="ts">
  // Codegraph dashboard — card grid mirroring /kg.
  //
  // One card per project that has codegraph data, with the five
  // entity counts (modules / classes / functions / APIs / interactions)
  // and a Browse button that drills into an entity table view.
  // Drill-in TBD; for v0 the Browse button toasts "viewer coming
  // soon". A force-directed graph of every entity was considered and
  // rejected — it doesn't scale past a few hundred nodes and doesn't
  // answer the question users actually have ("which projects have a
  // code graph and how big is each one").
  //
  // Source: codegraph_list_projects (Rust). Returns one row per
  // <prefix>_CodeFunction-style class group, with the five counts
  // pre-aggregated server-side.

  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { invoke } from '$lib/tauri';
  import { selectedProject, projects } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';
  import NoProjectBanner from '$lib/components/NoProjectBanner.svelte';

  interface CodegraphProjectSummary {
    project_name: string;
    prefix: string;
    module_count: number;
    class_count: number;
    function_count: number;
    api_count: number;
    interaction_count: number;
    access: 'read' | 'write' | 'none';
  }

  let summaries = $state<CodegraphProjectSummary[]>([]);
  let loading = $state(true);
  const acting = $derived($selectedProject);

  async function load() {
    if (!acting) {
      loading = false;
      return;
    }
    loading = true;
    try {
      // v0.2.16 (W4 / 0.11): pass include_untracked_projects=false so
      // the dashboard hides collections whose prefix doesn't map to a
      // currently-tracked project (data from since-deleted projects
      // stays in Weaviate but isn't visually cluttering this view).
      // The advanced /preferences/weaviate-untracked route surfaces
      // the full inventory.
      summaries = await invoke<CodegraphProjectSummary[]>('codegraph_list_projects', {
        projectId: acting.id,
        includeUntrackedProjects: false,
      });
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  onMount(load);
  $effect(() => {
    if (acting) void load();
  });

  function totalEntities(s: CodegraphProjectSummary): number {
    return (
      s.module_count +
      s.class_count +
      s.function_count +
      s.api_count +
      s.interaction_count
    );
  }

  // Assign-access modal state. Mirrors the KG dashboard's flow:
  // pick which other projects are granted read access to this
  // project's codegraph. Owner project (write access) is always
  // allowed. Granting goes through codegraph_grant_access.
  let assignModalFor = $state<CodegraphProjectSummary | null>(null);
  let assignAllProjects = $state<Array<{ id: string; name: string }>>([]);
  let assignChecked = $state<Set<string>>(new Set());
  let assignSaving = $state(false);
  let assignError = $state<string | null>(null);

  async function openAssign(s: CodegraphProjectSummary) {
    // Open the modal regardless of which project is currently "active"
    // in the launcher. The cross-project access dropdown was previously
    // gated on s.access === 'write' (i.e. the active project owns this
    // codegraph) but that restriction is purely cosmetic — there's no
    // security boundary between projects on the same local machine,
    // and forcing the user to switch active context first to grant
    // read access is friction without payoff. Reported 2026-04-28.
    //
    // We DO need to know which project owns this codegraph (it's
    // the grantor in codegraph_grant_access). Resolve it from the
    // prefix via the project list.
    assignModalFor = s;
    assignError = null;
    assignChecked = new Set();
    try {
      assignAllProjects = await invoke<Array<{ id: string; name: string }>>('list_projects_v2');
      const owner = assignAllProjects.find((p) => p.name === s.project_name);
      if (!owner) {
        assignError = `No project record found for codegraph prefix "${s.prefix}". The Weaviate collection exists but the launcher DB has no matching project — recreate the project to manage access.`;
        return;
      }
      // Pre-fill: query existing grants for the owner project. Best
      // effort — if codegraph_list_access isn't available we silently
      // start with an empty set.
      try {
        const matrix = await invoke<{ allowed?: Array<{ id: string }>; can_read_from?: Array<{ id: string }> }>(
          'codegraph_list_access',
          { projectId: owner.id },
        ).catch(() => null) as any;
        if (matrix?.allowed) {
          assignChecked = new Set(matrix.allowed.map((p: any) => p.id));
        }
      } catch { /* swallow */ }
    } catch (e) {
      assignError = e instanceof Error ? e.message : String(e);
    }
  }

  function toggleAssign(id: string) {
    if (assignChecked.has(id)) assignChecked.delete(id);
    else assignChecked.add(id);
    assignChecked = new Set(assignChecked);
  }

  async function saveAssign() {
    if (!assignModalFor) return;
    assignSaving = true;
    assignError = null;
    try {
      // Resolve owner from the card (NOT from acting) — the user can
      // manage access on any of their codegraphs from this dashboard
      // regardless of which project is currently "active".
      const owner = assignAllProjects.find((p) => p.name === assignModalFor!.project_name);
      if (!owner) {
        assignError = `No project record found for "${assignModalFor.project_name}".`;
        return;
      }
      const ownerId = owner.id;
      // For each candidate project: if checked → grant read; if
      // unchecked but was previously granted → revoke (access_level
      // = "none"). We send all of them in parallel.
      const ops = assignAllProjects
        .filter((p) => p.id !== ownerId)
        .map((p) =>
          invoke('codegraph_grant_access', {
            req: {
              grantor_project_id: ownerId,
              grantee_project_id: p.id,
              access_level: assignChecked.has(p.id) ? 'read' : 'none',
            },
          }),
        );
      await Promise.all(ops);
      toast.success(`Codegraph access updated for ${assignModalFor.project_name}`);
      assignModalFor = null;
      await load();
    } catch (e) {
      assignError = e instanceof Error ? e.message : String(e);
    } finally {
      assignSaving = false;
    }
  }

  function closeAssign() {
    assignModalFor = null;
  }
</script>

<svelte:head>
  <title>Code Graph — VCT Launcher</title>
</svelte:head>

<Toast />

<div class="cg-page">
  <header class="cg-header">
    <button class="cg-back" onclick={() => goto('/')}>← Back</button>
    <h1>Code Graph</h1>
    <button class="cg-refresh" onclick={load} disabled={loading}>
      {loading ? 'Loading…' : 'Refresh'}
    </button>
  </header>

  {#if !acting}
    <NoProjectBanner />
  {:else if loading && summaries.length === 0}
    <p class="cg-empty">Loading…</p>
  {:else if summaries.length === 0}
    <p class="cg-empty">
      No codegraph data found in Weaviate. Run <code>code-graph-analyze</code>
      on a project to generate the entity classes (or wait for the auto-build
      to finish — see the status pill in the project header).
    </p>
  {:else}
    <div class="cg-grid">
      {#each summaries as s (s.prefix)}
        <article class="cg-card" class:owned={s.access === 'write'}>
          <header class="cg-card-head">
            <h3>{s.project_name}</h3>
            <span class="cg-card-access cg-card-access-{s.access}">{s.access.toUpperCase()}</span>
          </header>
          <p class="cg-card-prefix"><code>{s.prefix}_*</code></p>
          <div class="cg-card-stats">
            <span class="cg-stat" style="--c:#3aa3ff"><strong>{s.module_count}</strong> modules</span>
            <span class="cg-stat" style="--c:#9b59b6"><strong>{s.class_count}</strong> classes</span>
            <span class="cg-stat" style="--c:#1abc9c"><strong>{s.function_count}</strong> functions</span>
            {#if s.api_count > 0}
              <span class="cg-stat" style="--c:#ff9b3d"><strong>{s.api_count}</strong> APIs</span>
            {/if}
            {#if s.interaction_count > 0}
              <span class="cg-stat" style="--c:#ff6f9e"><strong>{s.interaction_count}</strong> interactions</span>
            {/if}
          </div>
          <p class="cg-card-total">{totalEntities(s)} entities total</p>
          <div class="cg-card-actions">
            <button class="cg-btn" onclick={() => openAssign(s)}>
              Assign access…
            </button>
          </div>
        </article>
      {/each}
    </div>
  {/if}
</div>

{#if assignModalFor}
  <div class="cg-modal-back" role="presentation" onclick={closeAssign}>
    <div class="cg-modal" role="dialog" aria-modal="true" tabindex="-1" onclick={(e) => e.stopPropagation()}>
      <header class="cg-modal-head">
        <h3>Codegraph access — {assignModalFor.project_name}</h3>
        <button class="cg-close" onclick={closeAssign} aria-label="Close">×</button>
      </header>
      <p class="cg-modal-help">
        Pick which other projects can read this codegraph. Owners
        always have write access; granting "read" lets the grantee
        see entities + run cross-project searches but not modify.
      </p>
      {#if assignError}
        <p class="cg-modal-error">{assignError}</p>
      {/if}
      <ul class="cg-modal-list">
        {#each assignAllProjects.filter((p) => p.name !== assignModalFor?.project_name) as p (p.id)}
          <li>
            <label>
              <input
                type="checkbox"
                checked={assignChecked.has(p.id)}
                onchange={() => toggleAssign(p.id)}
              />
              <span>{p.name}</span>
            </label>
          </li>
        {/each}
        {#if assignAllProjects.filter((p) => p.name !== assignModalFor?.project_name).length === 0}
          <li class="cg-modal-empty">
            No other projects to grant access to.
          </li>
        {/if}
      </ul>
      <div class="cg-modal-actions">
        <button class="cg-btn" onclick={closeAssign} disabled={assignSaving}>Cancel</button>
        <button
          class="cg-btn cg-btn-primary"
          onclick={saveAssign}
          disabled={assignSaving || assignAllProjects.filter((p) => p.name !== assignModalFor?.project_name).length === 0}
        >
          {assignSaving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .cg-page { padding: 24px; max-width: 1200px; margin: 0 auto; }
  .cg-header { display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }
  .cg-header h1 { margin: 0; font-size: 22px; flex: 1; }
  .cg-back, .cg-refresh {
    padding: 6px 12px; border-radius: 4px; cursor: pointer;
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
    color: inherit; font-size: 13px;
  }
  .cg-back:hover, .cg-refresh:hover:not(:disabled) {
    background: rgba(255,255,255,0.1);
  }
  .cg-refresh:disabled { opacity: 0.5; cursor: default; }
  .cg-empty { color: #888; padding: 40px; text-align: center; }
  .cg-empty code {
    background: rgba(255,255,255,0.05); padding: 2px 6px; border-radius: 3px;
    font-family: ui-monospace, monospace; font-size: 12px;
  }

  .cg-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 12px;
  }
  .cg-card {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px;
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .cg-card.owned {
    border-color: rgba(0,191,166,0.35);
    background: rgba(0,191,166,0.05);
  }
  .cg-card-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .cg-card-head h3 {
    margin: 0; font-size: 15px;
    /* Truncate long project names so the access badge can't overlap
       the title text. Same fix as KG CollectionList 2026-04-28. */
    flex: 1 1 auto; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .cg-card-access {
    padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600;
    letter-spacing: 0.04em;
    flex-shrink: 0;
  }
  .cg-card-access-write {
    background: rgba(0,191,166,0.15); color: rgb(0,191,166);
    border: 1px solid rgba(0,191,166,0.3);
  }
  .cg-card-access-read {
    background: rgba(155,89,182,0.15); color: rgb(155,89,182);
    border: 1px solid rgba(155,89,182,0.3);
  }
  .cg-card-access-none {
    background: rgba(255,255,255,0.05); color: #888;
    border: 1px solid rgba(255,255,255,0.12);
  }
  .cg-card-prefix {
    margin: 0; font-size: 11px; color: #888;
  }
  .cg-card-prefix code {
    background: rgba(255,255,255,0.05); padding: 1px 5px; border-radius: 3px;
    font-family: ui-monospace, monospace;
  }
  .cg-card-stats {
    display: flex; flex-wrap: wrap; gap: 4px;
    margin: 4px 0;
  }
  .cg-stat {
    padding: 2px 8px; border-radius: 10px; font-size: 11px;
    border: 1px solid var(--c, #888);
    background: color-mix(in srgb, var(--c, #888) 8%, transparent);
    color: var(--c, #ccc);
  }
  .cg-stat strong { font-weight: 700; color: #fff; }
  .cg-card-total {
    margin: 0; font-size: 11px; color: #aaa;
  }
  .cg-card-actions {
    display: flex; justify-content: flex-end; gap: 6px;
    margin-top: 4px;
  }
  .cg-btn {
    padding: 4px 12px; border-radius: 4px; font-size: 12px;
    background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15);
    color: inherit; cursor: pointer;
  }
  .cg-btn:hover:not(:disabled) { background: rgba(255,255,255,0.12); }
  .cg-btn:disabled { opacity: 0.4; cursor: default; }
  .cg-btn-primary {
    background: rgb(0,191,166); color: #001a17; border-color: rgb(0,191,166);
  }
  .cg-btn-primary:hover:not(:disabled) { background: rgb(0,210,180); }

  /* Assign-access modal */
  .cg-modal-back {
    position: fixed; inset: 0; z-index: 9000;
    background: rgba(0,0,0,0.55);
    display: flex; align-items: center; justify-content: center;
  }
  .cg-modal {
    background: #1a1d24;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 8px;
    box-shadow: 0 12px 36px rgba(0,0,0,0.5);
    width: 480px; max-width: 90vw;
    padding: 18px 20px 14px;
    color: #ddd;
  }
  .cg-modal-head {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 8px;
  }
  .cg-modal-head h3 { margin: 0; font-size: 14px; }
  .cg-close {
    background: none; border: none; color: #888; font-size: 18px;
    cursor: pointer; padding: 0 4px;
  }
  .cg-close:hover { color: #fff; }
  .cg-modal-help { margin: 0 0 12px; font-size: 12px; color: #aaa; }
  .cg-modal-error {
    margin: 0 0 10px; padding: 6px 10px;
    background: rgba(255,79,160,0.10);
    border: 1px solid rgba(255,79,160,0.3);
    border-radius: 4px; color: rgb(255,79,160);
    font-size: 12px; font-family: ui-monospace, monospace;
  }
  .cg-modal-list {
    list-style: none; padding: 0; margin: 0 0 14px;
    max-height: 280px; overflow-y: auto;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 6px;
  }
  .cg-modal-list li {
    padding: 8px 12px;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .cg-modal-list li:last-child { border-bottom: none; }
  .cg-modal-list li.cg-modal-empty {
    color: #888; text-align: center; font-size: 12px;
    padding: 16px; border-bottom: none;
  }
  .cg-modal-list label {
    display: flex; align-items: center; gap: 8px;
    cursor: pointer; font-size: 13px;
  }
  .cg-modal-list input[type="checkbox"] {
    accent-color: rgb(0,191,166);
  }
  .cg-modal-actions {
    display: flex; justify-content: flex-end; gap: 8px;
  }
</style>
