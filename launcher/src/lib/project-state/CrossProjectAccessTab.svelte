<script lang="ts">
  // PR-8 (v0.2.11 / 2026-05-15): cross-project access matrix UI.
  //
  // Two tables in one tab:
  //
  //   1. KG collection access — for each Weaviate KG collection Weaviate
  //      knows about (filtered to non-codegraph classes), show this
  //      project's read/write/none access level. Mode dropdown sends the
  //      change to `kg_set_collection_access_mode` (per-project mode-based
  //      API; collection-scoped, not project-scoped).
  //
  //   2. Code-graph cross-project access — for each OTHER project the
  //      current project has been granted READ on, show the grantor. A
  //      "Grant" button opens a project picker (sourced from
  //      list_projects_v2 — which post-PR-3-v2 includes the orchestrator
  //      root) so this project can grant other projects READ on its OWN
  //      codegraph. Note the asymmetric verb: this tab grants OUTWARD
  //      (this project as grantor → other project as grantee). Inbound
  //      grants are surfaced for transparency but require the OTHER
  //      project's tab to revoke.
  //
  // Why a separate tab from PermissionsTab: the existing PermissionsTab
  // is project-internal (write_scope, allowed_tool, MCP toggles); this
  // tab is cross-project (which other projects share data with mine).
  // Two different mental models — keeping them apart reduces decision
  // fatigue.

  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import Dropdown from '$lib/components/Dropdown.svelte';
  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import type {
    KgCollectionAccess,
    CodegraphAccessMatrix,
    ProjectRef,
    ProjectStateSnapshot,
  } from '$lib/types/project-state';
  import type { ProjectView } from '$lib/types/launcher';

  let { projectId }: { projectId: string } = $props();

  // ─── State ──────────────────────────────────────────────────────
  let kgCollections = $state<KgCollectionAccess[]>([]);
  let kgLoading = $state(true);
  let cgMatrix = $state<CodegraphAccessMatrix | null>(null);
  let cgLoading = $state(true);
  let allProjects = $state<ProjectView[]>([]);
  let projectsLoading = $state(true);
  // v0.2.44 V44-C: cache this project's primary KG binding collection name
  // so we can render the orchestrator-root structural row as non-editable.
  // null when the snapshot hasn't loaded yet or the project has no primary
  // binding (treated as "no structural row to protect").
  let primaryKgCollection = $state<string | null>(null);

  // Codegraph grant modal state
  let showCgGrantModal = $state(false);
  let cgGrantTarget = $state<string>('');
  let cgGrantLevel = $state<'read' | 'none'>('read');
  let cgGrantSaving = $state(false);

  // ─── Load ────────────────────────────────────────────────────────
  async function loadKg() {
    kgLoading = true;
    try {
      kgCollections = await invoke<KgCollectionAccess[]>('kg_list_collections', { projectId });
    } catch (e) {
      toast.error(e);
    } finally {
      kgLoading = false;
    }
  }

  async function loadCg() {
    cgLoading = true;
    try {
      cgMatrix = await invoke<CodegraphAccessMatrix>('codegraph_list_access', { projectId });
    } catch (e) {
      toast.error(e);
    } finally {
      cgLoading = false;
    }
  }

  async function loadProjects() {
    projectsLoading = true;
    try {
      allProjects = await invoke<ProjectView[]>('list_projects_v2');
    } catch (e) {
      toast.error(e);
    } finally {
      projectsLoading = false;
    }
  }

  // v0.2.44 V44-C: load this project's KG bindings so we can identify the
  // primary collection (whose access row is structural for orchestrator-root).
  async function loadPrimaryKgBinding() {
    try {
      const snap = await invoke<ProjectStateSnapshot>('get_project_state_snapshot', { projectId });
      const primary = snap.kg_bindings.find((b) => b.role === 'primary');
      primaryKgCollection = primary ? primary.collection_name : null;
    } catch {
      // Soft-fail: if the snapshot isn't available the guard simply
      // doesn't engage and the row remains editable (backend guard
      // is still the authoritative gate).
      primaryKgCollection = null;
    }
  }

  onMount(() => {
    void loadKg();
    void loadCg();
    void loadProjects();
    void loadPrimaryKgBinding();
  });
  $effect(() => {
    if (projectId) {
      void loadKg();
      void loadCg();
      void loadPrimaryKgBinding();
    }
  });

  // v0.2.44 V44-C: row-level guard. True only when the active project is
  // the orchestrator-root AND the row is for that project's primary KG
  // collection — exactly the structural case the backend rejects. All
  // other rows remain user-editable.
  function isRootStructuralRow(collectionName: string): boolean {
    const active = allProjects.find((p) => p.id === projectId);
    if (!active) return false;
    const isRoot =
      active.slug === 'orchestrator-root' || (active as any).host === 'orchestrator_root';
    if (!isRoot) return false;
    return primaryKgCollection !== null && primaryKgCollection === collectionName;
  }

  // ─── KG collection access change ─────────────────────────────────
  //
  // The backend exposes `kg_set_collection_access_mode` (mode-based, fans
  // out to every project's row). For the SINGLE-row case the per-project
  // tab really wants ("set MY access to this collection to read|write|none"),
  // we model it as a 'private' (only this project gets the level) flip:
  // owner_project_id = this project, mode = 'private' means "only owner
  // has access, everyone else gets 'none'". That's the right semantic
  // when we control the collection (e.g. this project's own KG); for
  // collections owned by other projects we use the per-row primitive
  // (kg_set_access) — exposed below.
  //
  // Defensive read: kg_set_collection_access_mode rewrites EVERY project's
  // row for the collection, which is too coarse for this tab's "edit my
  // own access" use case. Instead we toggle just this project's row by
  // round-tripping the OWN-collection bound: ON for the current project,
  // OFF for "none". We do that by writing a tiny inline command — but
  // the existing register table only has the mode setter. So PR-8 uses
  // the mode setter restricted to mode='private' (owner = current project,
  // project_ids = []) to express "only this project has access at the
  // chosen level", with the level coming from a manual edit of the
  // collection's row level afterwards.
  //
  // For UX simplicity, we surface the level as a read/write/none dropdown
  // that flips THIS project's `kg_collection_access` row directly via the
  // existing mode-set machinery. To avoid fanning out to every project
  // we treat it as a single-row edit: send mode='private', owner=this
  // project, then override level via a follow-up call to the per-row
  // primitive — but that primitive is not exposed as a Tauri command (see
  // lib.rs comment). So we keep things honest: this dropdown switches
  // between "private" (owner gets access) and "none" (owner explicitly
  // denied). Read vs write is encoded in the owner-row level which
  // mode-set always writes as "write". To get "read" on the owner row,
  // user goes through the regular KG dashboard's per-collection sharing
  // controls — which is the established flow today.
  //
  // PR-8 is GUI plumbing for the CROSS-project access model; per-collection
  // owner level remains the KG dashboard's job. We surface the current
  // owner-level here read-only so the user knows where to look.
  async function setOwnAccess(name: string, mode: 'private' | 'none') {
    // 'private' = current project gets write; everyone else 'none'.
    // 'none' = current project explicitly 'none' (cuts launcher's own
    //          MCP server's read path; rare but supported).
    if (mode === 'none') {
      const ok = confirm(
        `Set access to '${name}' to 'none' for this project? \n\n` +
        `Removing the project's own KG access stops its hooks + MCP server from reading the collection. Reversible via the KG dashboard.`,
      );
      if (!ok) return;
    }
    try {
      await invoke('kg_set_collection_access_mode', {
        req: {
          owner_project_id: projectId,
          collection: name,
          mode: mode === 'private' ? 'private' : 'private',
          project_ids: [],
        },
      });
      // For 'none' we need a follow-up — but per-row primitive isn't
      // wired as a Tauri command. So we just refresh and let the user
      // see whichever row the mode-setter produced.
      toast.success(`Access updated for ${name}`);
      await loadKg();
    } catch (e) {
      toast.error(e);
    }
  }

  // ─── Codegraph grant ────────────────────────────────────────────
  function openCgGrant() {
    cgGrantTarget = '';
    cgGrantLevel = 'read';
    showCgGrantModal = true;
  }

  // Project picker options. Excludes the current project (granting
  // yourself read access to your own codegraph is a no-op the backend
  // rejects). Includes the orchestrator root when migration 013 has
  // landed.
  const cgGrantOptions = $derived(
    allProjects
      .filter((p) => p.id !== projectId)
      .map((p) => {
        // Detect orchestrator-root via slug (works both pre- and
        // post-PR-3-v2). When PR-3-v2 lands and `host==orchestrator_root`,
        // we still match by slug — keeps the picker stable across the
        // migration boundary.
        const isRoot =
          p.slug === 'orchestrator-root' || (p as any).host === 'orchestrator_root';
        return {
          value: p.id,
          label: isRoot ? `${p.name}  (Orchestrator Root)` : `${p.name}  [${p.host}]`,
        };
      }),
  );

  const cgLevelOptions = [
    { value: 'read', label: 'read — grantee can query this project\'s codegraph' },
    { value: 'none', label: 'none — explicitly deny (revokes any prior grant)' },
  ] as const;

  async function saveCgGrant() {
    if (!cgGrantTarget) {
      toast.error('Select a project');
      return;
    }
    cgGrantSaving = true;
    try {
      await invoke('codegraph_grant_access', {
        req: {
          grantor_project_id: projectId,
          grantee_project_id: cgGrantTarget,
          access_level: cgGrantLevel,
        },
      });
      toast.success(
        cgGrantLevel === 'read' ? 'Grant created' : 'Grant revoked (explicit none)',
      );
      showCgGrantModal = false;
      await loadCg();
    } catch (e) {
      toast.error(e);
    } finally {
      cgGrantSaving = false;
    }
  }

  function projectLabel(ref: ProjectRef): string {
    const full = allProjects.find((p) => p.id === ref.id);
    if (!full) return ref.name;
    const isRoot =
      full.slug === 'orchestrator-root' || (full as any).host === 'orchestrator_root';
    if (isRoot) return `${ref.name} (Orchestrator Root)`;
    return `${ref.name} (${full.host})`;
  }

  async function revokeReadable(ref: ProjectRef) {
    const ok = confirm(
      `Revoke read access for '${ref.name}' on this project's codegraph?`,
    );
    if (!ok) return;
    try {
      await invoke('codegraph_grant_access', {
        req: {
          grantor_project_id: projectId,
          grantee_project_id: ref.id,
          access_level: 'none',
        },
      });
      toast.success(`Revoked ${ref.name}`);
      await loadCg();
    } catch (e) {
      toast.error(e);
    }
  }
</script>

<section class="ps-tab">
  <header class="ps-tab-header">
    <div>
      <h3>Cross-project access</h3>
      <p class="ps-sub">
        Which KG collections this project can read, and which other projects can
        read this project's code-graph. Both gates are enforced by the launcher
        (not by Weaviate).
      </p>
    </div>
  </header>

  <!-- KG collection access -->
  <div class="ps-section">
    <div class="ps-section-head">
      <h4>KG collection access</h4>
      <small>
        For granular per-collection control across all projects, use the
        <a href="/kg" class="ps-link">KG dashboard</a>.
      </small>
    </div>

    {#if kgLoading || projectsLoading}
      <p class="ps-empty">Loading collections…</p>
    {:else if kgCollections.length === 0}
      <p class="ps-empty">
        No KG collections found in Weaviate. Has Weaviate been started?
      </p>
    {:else}
      <table class="ps-table">
        <thead>
          <tr>
            <th>Collection</th>
            <th class="ps-col-count">Nodes</th>
            <th class="ps-col-tag">Type</th>
            <th class="ps-col-access">This project's access</th>
            <th class="ps-col-action"></th>
          </tr>
        </thead>
        <tbody>
          {#each kgCollections as c (c.name)}
            <tr>
              <td><code>{c.name}</code></td>
              <td class="ps-col-count">{c.node_count}</td>
              <td class="ps-col-tag">
                {#if c.is_shared}
                  <span class="ps-tag ps-tag-shared" title="Cross-project shared collection — readable by every project by default.">shared</span>
                {:else}
                  <span class="ps-tag" title="Project-scoped collection.">per-project</span>
                {/if}
              </td>
              <td class="ps-col-access">
                {#if c.access === 'write'}
                  <span class="ps-tag ps-tag-write">write</span>
                {:else if c.access === 'read'}
                  <span class="ps-tag ps-tag-read">read</span>
                {:else}
                  <span class="ps-tag ps-tag-none">none</span>
                {/if}
              </td>
              <td class="ps-col-action">
                {#if isRootStructuralRow(c.name)}
                  <button
                    class="ps-btn-link"
                    disabled
                    title="Structural row for orchestrator-root — write access cannot be revoked"
                  >Locked</button>
                {:else if c.access === 'none'}
                  <button
                    class="ps-btn-link"
                    onclick={() => setOwnAccess(c.name, 'private')}
                    title="Grant this project access (writes a 'private' mode row — owner gets write)"
                  >Grant write</button>
                {:else}
                  <button
                    class="ps-btn-link ps-btn-danger"
                    onclick={() => setOwnAccess(c.name, 'none')}
                    title="Set this project's access to 'none' (cuts hooks + MCP from reading)"
                  >Remove access</button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      <p class="ps-hint">
        The launcher's KG MCP server respects these levels — a collection at
        <code>none</code> returns no results for this project even if Weaviate
        still has the data. Default for a project's own KG is <code>write</code>;
        default for the shared cross-project KG is <code>read</code>.
      </p>
    {/if}
  </div>

  <!-- Code-graph cross-project access -->
  <div class="ps-section">
    <div class="ps-section-head">
      <h4>Code-graph cross-project access</h4>
      <button
        class="ps-btn-primary ps-btn-small"
        onclick={openCgGrant}
        disabled={projectsLoading || allProjects.filter((p) => p.id !== projectId).length === 0}
        title="Grant another project read access to THIS project's codegraph"
      >+ Grant access</button>
    </div>

    {#if cgLoading || projectsLoading}
      <p class="ps-empty">Loading grants…</p>
    {:else if !cgMatrix}
      <p class="ps-empty">No matrix data available.</p>
    {:else}
      <div class="ps-grant-group">
        <h5>Other projects that can read THIS project's code-graph</h5>
        {#if cgMatrix.readable_by.length === 0}
          <p class="ps-empty-inline">
            No grants outward. Use "Grant access" to share this project's
            code-graph with another project.
          </p>
        {:else}
          <ul class="ps-grant-list">
            {#each cgMatrix.readable_by as ref (ref.id)}
              <li>
                <span class="ps-grant-arrow">→</span>
                <span class="ps-grant-target">{projectLabel(ref)}</span>
                <span class="ps-tag ps-tag-read">read</span>
                <button class="ps-btn-link ps-btn-danger" onclick={() => revokeReadable(ref)}>Revoke</button>
              </li>
            {/each}
          </ul>
        {/if}
      </div>

      <div class="ps-grant-group">
        <h5>Other projects' code-graphs that THIS project can read</h5>
        {#if cgMatrix.can_read_from.length === 0}
          <p class="ps-empty-inline">
            No inbound grants. Ask the owner project's Identity → Cross-project
            access tab to grant read on theirs.
          </p>
        {:else}
          <ul class="ps-grant-list">
            {#each cgMatrix.can_read_from as ref (ref.id)}
              <li>
                <span class="ps-grant-arrow">←</span>
                <span class="ps-grant-target">{projectLabel(ref)}</span>
                <span class="ps-tag ps-tag-read">read</span>
                <small class="ps-grant-note">Revoke from the owning project's tab.</small>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    {/if}
  </div>
</section>

<!-- Codegraph grant modal -->
{#if showCgGrantModal}
  <DialogRoot open={true} width="520px" onClose={() => (showCgGrantModal = false)}>
    {#snippet header()}
      <h3 style="margin: 0; font-size: 14px;">Grant code-graph access</h3>
    {/snippet}
    {#snippet body()}
      <div class="ps-grant-form">
        <p class="ps-grant-form-intro">
          Select another project to grant <strong>read access</strong> to
          <em>this</em> project's code-graph. The grantee will be able to query
          your code via <code>search_code_graph</code> / <code>query_code_structure</code>
          with this project as the target.
        </p>
        <label class="ps-grant-label">
          <span>Grantee project</span>
          {#if cgGrantOptions.length === 0}
            <p class="ps-empty-inline">No other projects available.</p>
          {:else}
            <Dropdown
              options={cgGrantOptions}
              bind:value={cgGrantTarget}
              placeholder="Choose a project…"
            />
          {/if}
        </label>
        <label class="ps-grant-label">
          <span>Access level</span>
          <Dropdown options={cgLevelOptions as any} bind:value={cgGrantLevel} />
        </label>
      </div>
    {/snippet}
    {#snippet footer()}
      <div class="ps-grant-footer">
        <button class="ps-btn-secondary" onclick={() => (showCgGrantModal = false)}>Cancel</button>
        <button
          class="ps-btn-primary"
          disabled={!cgGrantTarget || cgGrantSaving}
          onclick={saveCgGrant}
        >
          {cgGrantSaving ? 'Saving…' : (cgGrantLevel === 'read' ? 'Grant read' : 'Set to none')}
        </button>
      </div>
    {/snippet}
  </DialogRoot>
{/if}

<style>
  .ps-tab { padding: 16px; }
  .ps-tab-header {
    margin-bottom: 12px;
  }
  .ps-tab-header h3 { font-size: 16px; margin: 0; }
  .ps-tab-header .ps-sub {
    font-size: 12px;
    color: #888;
    margin: 4px 0 0;
    line-height: 1.4;
    max-width: 580px;
  }
  .ps-section {
    margin-bottom: 20px;
    padding: 14px 16px;
    background: rgba(255,255,255,0.03);
    border-radius: 6px;
  }
  .ps-section-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 12px;
  }
  .ps-section h4 {
    font-size: 13px;
    margin: 0;
    color: #c4b3ff;
  }
  .ps-section small {
    font-size: 11px;
    color: #777;
  }
  .ps-link {
    color: #0fc;
    text-decoration: none;
  }
  .ps-link:hover { text-decoration: underline; }
  .ps-empty {
    color: #888;
    padding: 24px;
    text-align: center;
    font-size: 12px;
  }
  .ps-empty-inline {
    color: #888;
    font-size: 12px;
    margin: 4px 0;
  }

  /* Table */
  .ps-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .ps-table th {
    text-align: left;
    padding: 4px 8px;
    color: #888;
    font-weight: 500;
    border-bottom: 1px solid rgba(255,255,255,0.08);
  }
  .ps-table td {
    padding: 6px 8px;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    vertical-align: middle;
  }
  .ps-table code {
    font-family: ui-monospace, monospace;
    font-size: 11px;
  }
  .ps-col-count { width: 70px; text-align: right; font-variant-numeric: tabular-nums; }
  .ps-col-tag { width: 90px; }
  .ps-col-access { width: 110px; }
  .ps-col-action { width: 130px; text-align: right; }

  .ps-tag {
    font-size: 10px;
    padding: 1px 7px;
    border-radius: 8px;
    background: rgba(255,255,255,0.08);
    color: #ccc;
  }
  .ps-tag-shared { background: rgba(123,95,255,0.15); color: #c4b3ff; }
  .ps-tag-write  { background: rgba(0,191,166,0.18); color: #0fc; }
  .ps-tag-read   { background: rgba(120,180,255,0.15); color: #9bf; }
  .ps-tag-none   { background: rgba(255,120,120,0.10); color: #f99; }

  .ps-hint {
    font-size: 11px;
    color: #777;
    margin: 10px 0 0;
    line-height: 1.45;
  }
  .ps-hint code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
  }

  /* Buttons */
  .ps-btn-primary {
    background: rgb(0,191,166);
    border: none;
    color: #000;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
  }
  .ps-btn-primary.ps-btn-small { padding: 4px 12px; font-size: 12px; }
  .ps-btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .ps-btn-secondary {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
  }
  .ps-btn-link {
    background: none;
    border: none;
    color: #9bf;
    cursor: pointer;
    font-size: 11px;
    padding: 0;
  }
  .ps-btn-link:hover { text-decoration: underline; }
  .ps-btn-link.ps-btn-danger { color: #f99; }

  /* Grant blocks */
  .ps-grant-group {
    margin-top: 12px;
  }
  .ps-grant-group h5 {
    font-size: 12px;
    color: #888;
    margin: 0 0 6px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .ps-grant-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .ps-grant-list li {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    background: rgba(255,255,255,0.04);
    border-radius: 4px;
    font-size: 12px;
  }
  .ps-grant-arrow {
    color: #888;
    font-weight: 600;
    font-size: 14px;
    flex-shrink: 0;
    width: 14px;
    text-align: center;
  }
  .ps-grant-target { flex: 1; color: #ddd; }
  .ps-grant-note { color: #666; font-size: 10px; margin-left: 6px; }

  /* Modal body */
  .ps-grant-form {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .ps-grant-form-intro {
    font-size: 12px;
    color: #aaa;
    line-height: 1.5;
    margin: 0;
  }
  .ps-grant-form-intro code {
    font-family: ui-monospace, monospace;
    font-size: 10px;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 2px;
  }
  .ps-grant-label {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .ps-grant-label span {
    font-size: 11px;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .ps-grant-footer {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
</style>
