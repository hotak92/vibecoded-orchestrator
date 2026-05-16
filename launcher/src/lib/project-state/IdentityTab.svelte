<script lang="ts">
  // PR-8 (v0.2.11 / 2026-05-15): per-project Identity tab.
  //
  // Surfaces three identity fields that previously could only be edited
  // by hand-patching `.vscode/settings.json::claude-code.env`:
  //   1. PROJECT_NAME            — display name + sanitization seed for KG /
  //                                code-graph collection prefixes.
  //   2. KG_COLLECTION           — Weaviate KG collection this project's
  //                                hooks + MCP server write to.
  //   3. CODE_GRAPH_PROJECT      — prefix for the five
  //                                <Prefix>_CodeModule|CodeClass|...
  //                                Weaviate classes.
  //
  // The orchestrator-root project row (PR-3-v2 migration 013, auto-
  // registered with slug='orchestrator-root') gets special treatment:
  //   - identity_source reads from .claude/settings.json (the clone is
  //     the orchestrator itself; no .vscode/settings.json is canonical).
  //   - Re-detect from disk pulls the display name from vct-module.json.
  //   - Host badge renders teal "Orchestrator Root".
  //
  // Backend contract: commands::project_identity::{get,update,redetect}_project_identity.
  //
  // Saves are idempotent — re-clicking Save with unchanged values is a
  // ~50ms no-op (the binding upserts hit ON CONFLICT, env writers
  // deep-merge to the same final state). Warnings are surfaced as
  // toasts; they don't block the save.

  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import { projects as projectsStore } from '$lib/stores/projects';
  import LegacyCollectionsModal from '$lib/components/LegacyCollectionsModal.svelte';
  import type {
    ProjectIdentity,
    UpdateProjectIdentityRequest,
    UpdateProjectIdentityResult,
  } from '$lib/types/identity';

  let { projectId }: { projectId: string } = $props();

  let identity = $state<ProjectIdentity | null>(null);
  let loading = $state(true);
  let saving = $state(false);
  let redetecting = $state(false);

  // PR-8: manual entry point for the legacy-collections cleanup, gated to
  // the orchestrator-root tab only (per the brief's C.3 — "separate
  // 'Clean up legacy collections' button in the orchestrator-root's
  // Identity tab"). The launcher's `+layout.svelte` handles the
  // auto-show on first startup; this button is the explicit re-entry
  // point for users who dismissed the auto-notice.
  let showLegacyModal = $state(false);

  // Edit-buffer state. Bound to the inputs; flushed to the backend on Save.
  let editKg = $state('');
  let editCg = $state('');

  // Track the snapshot we loaded from the backend so we can offer "Discard"
  // and know whether changes need saving.
  let loadedKg = $state('');
  let loadedCg = $state('');

  const isDirty = $derived(
    !!identity && (editKg.trim() !== loadedKg || editCg.trim() !== loadedCg),
  );

  async function load() {
    loading = true;
    try {
      identity = await invoke<ProjectIdentity>('get_project_identity', { projectId });
      loadedKg = identity.kg_collection;
      loadedCg = identity.code_graph_project;
      editKg = loadedKg;
      editCg = loadedCg;
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  async function save() {
    if (!identity) return;
    if (!isDirty) {
      toast.success('No changes to save');
      return;
    }
    saving = true;
    try {
      const req: UpdateProjectIdentityRequest = {
        kg_collection: editKg.trim() !== loadedKg ? editKg.trim() : null,
        code_graph_project: editCg.trim() !== loadedCg ? editCg.trim() : null,
      };
      const res = await invoke<UpdateProjectIdentityResult>('update_project_identity', {
        projectId,
        req,
      });
      identity = res.identity;
      loadedKg = res.identity.kg_collection;
      loadedCg = res.identity.code_graph_project;
      editKg = loadedKg;
      editCg = loadedCg;
      if (res.warnings.length > 0) {
        for (const w of res.warnings) toast.error(`Identity warning: ${w}`);
      }
      toast.success('Identity saved');
    } catch (e) {
      toast.error(e);
    } finally {
      saving = false;
    }
  }

  async function discard() {
    if (!identity) return;
    editKg = loadedKg;
    editCg = loadedCg;
  }

  async function redetect() {
    if (!identity) return;
    redetecting = true;
    try {
      const res = await invoke<UpdateProjectIdentityResult>('redetect_project_identity', {
        projectId,
      });
      identity = res.identity;
      loadedKg = res.identity.kg_collection;
      loadedCg = res.identity.code_graph_project;
      editKg = loadedKg;
      editCg = loadedCg;
      if (res.warnings.length > 0) {
        for (const w of res.warnings) toast.error(`Re-detect warning: ${w}`);
      }
      // Display name may have changed (orchestrator-root reads vct-module.json::name).
      // Refresh the projects store so the page header + sidebar update.
      await projectsStore.load();
      toast.success(
        res.warnings.length > 0
          ? `Re-detected (${res.warnings.length} warning${res.warnings.length === 1 ? '' : 's'})`
          : 'Re-detected from disk',
      );
    } catch (e) {
      toast.error(e);
    } finally {
      redetecting = false;
    }
  }

  function hostBadgeLabel(id: ProjectIdentity): string {
    if (id.is_orchestrator_root) return 'Orchestrator Root';
    if (id.host === 'base') return 'Base';
    if (id.host === 'mao') return 'MAO';
    return id.host;
  }

  function hostBadgeClass(id: ProjectIdentity): string {
    if (id.is_orchestrator_root) return 'host-orchestrator';
    if (id.host === 'base') return 'host-base';
    if (id.host === 'mao') return 'host-mao';
    return 'host-other';
  }

  onMount(load);
  $effect(() => {
    if (projectId) void load();
  });
</script>

<section class="ps-tab">
  {#if loading}
    <p class="ps-loading">Loading…</p>
  {:else if !identity}
    <p class="ps-loading">Identity unavailable.</p>
  {:else}
    <header class="ps-tab-header">
      <div>
        <h3>Identity</h3>
        <p class="ps-sub">
          {#if identity.is_orchestrator_root}
            The orchestrator clone itself. KG / code-graph names here drive every
            cross-project access matrix entry that points at the root.
          {:else}
            How this project identifies itself to KG / code-graph collections,
            hooks, and MCP servers.
          {/if}
        </p>
      </div>
      <button
        class="ps-btn-secondary"
        onclick={redetect}
        disabled={redetecting || saving}
        title="Re-read identity from on-disk settings ({identity.identity_source})"
      >
        {redetecting ? 'Re-detecting…' : 'Re-detect from disk'}
      </button>
    </header>

    <!-- Read-only meta block. Folder + slug + host badge. -->
    <div class="ps-section">
      <div class="ps-meta-grid">
        <div>
          <label class="ps-meta-label">Display name</label>
          <p class="ps-meta-value">
            <strong>{identity.name}</strong>
            <span class="ps-host-badge {hostBadgeClass(identity)}">
              {hostBadgeLabel(identity)}
            </span>
          </p>
          {#if identity.is_orchestrator_root}
            <p class="ps-meta-hint">
              The orchestrator-root display name comes from
              <code>vct-module.json::name</code>. Click "Re-detect from disk" to
              pick up changes after editing that file.
            </p>
          {:else}
            <p class="ps-meta-hint">
              Display name changes use the project rename action (Settings tab)
              — it regenerates the slug and re-writes env surfaces.
            </p>
          {/if}
        </div>
        <div>
          <label class="ps-meta-label">Slug</label>
          <p class="ps-meta-value"><code>{identity.slug}</code></p>
        </div>
        <div class="ps-span2">
          <label class="ps-meta-label">Folder</label>
          <p class="ps-meta-value"><code>{identity.folder_path}</code></p>
        </div>
        <div class="ps-span2">
          <label class="ps-meta-label">Identity source on disk</label>
          <p class="ps-meta-value"><code>{identity.identity_source}</code></p>
        </div>
        {#if identity.vct_module_version}
          <div class="ps-span2">
            <label class="ps-meta-label">vct-module.json version</label>
            <p class="ps-meta-value"><code>v{identity.vct_module_version}</code></p>
          </div>
        {/if}
      </div>
    </div>

    <!-- Editable identity fields. -->
    <div class="ps-section">
      <h4>Collection identity</h4>
      <div class="ps-form-grid">
        <label class="ps-span2">
          <span>KG collection (Weaviate)</span>
          <input
            type="text"
            bind:value={editKg}
            placeholder="MyProject_KnowledgeGraph"
            spellcheck="false"
            disabled={saving || redetecting}
          />
          <small class="ps-form-hint">
            Sets <code>KG_COLLECTION</code> in all three env surfaces
            (<code>.claude/env</code>, <code>.claude/settings.json::env</code>,
            <code>.vscode/settings.json::claude-code.env</code>). Used by the
            project's hooks, MCP server, and search scripts to read / write the
            per-project KG. Must start with a letter; A–Z, a–z, 0–9, _ only.
          </small>
        </label>
        <label class="ps-span2">
          <span>Code-graph prefix</span>
          <input
            type="text"
            bind:value={editCg}
            placeholder="MyProject"
            spellcheck="false"
            disabled={saving || redetecting}
          />
          <small class="ps-form-hint">
            Sets <code>CODE_GRAPH_PROJECT</code> (and the
            <code>project_codegraph_bindings.collection_prefix</code> row).
            Drives the five
            <code>&lt;Prefix&gt;_CodeModule|CodeClass|CodeFunction|CodeAPI|CodeInteraction</code>
            Weaviate classes that
            <code>.claude/scripts/code-graph-analyze</code> writes to. Changing
            this without re-analysing leaves the old classes intact but
            disconnected; consider rebuilding from the project header
            afterwards.
          </small>
        </label>
      </div>
      <div class="ps-actions">
        <button
          class="ps-btn-primary"
          onclick={save}
          disabled={saving || redetecting || !isDirty}
        >
          {saving ? 'Saving…' : 'Save identity'}
        </button>
        <button
          class="ps-btn-secondary"
          onclick={discard}
          disabled={saving || redetecting || !isDirty}
        >
          Discard changes
        </button>
        {#if isDirty}
          <span class="ps-dirty-marker">Unsaved changes</span>
        {/if}
      </div>
    </div>

    <!-- PR-8 (v0.2.11): orchestrator-root only — manual entry for the
         legacy code-graph collection cleanup. The auto-detect / one-shot
         banner is wired in `+layout.svelte`; this button re-opens the
         same modal so root-tab users can clean up at any time, including
         after they previously clicked "Dismiss". -->
    {#if identity.is_orchestrator_root}
      <div class="ps-section ps-section-danger">
        <h4>Legacy <code>ClaudeOrchestrator_*</code> collections</h4>
        <p class="ps-form-hint" style="margin-bottom: 10px;">
          Pre-0.2.11 installs accumulated code-graph data under the
          hardcoded <code>ClaudeOrchestrator_*</code> Weaviate prefix.
          Open the cleanup dialog to re-analyse affected projects and
          (optionally, with explicit confirmation) delete the stale
          classes.
        </p>
        <button
          class="ps-btn-secondary"
          onclick={() => (showLegacyModal = true)}
        >
          Manage legacy collections…
        </button>
      </div>
    {/if}
  {/if}
</section>

{#if showLegacyModal}
  <LegacyCollectionsModal onClose={() => (showLegacyModal = false)} />
{/if}

<style>
  .ps-tab { padding: 16px; }
  .ps-tab-header {
    margin-bottom: 12px;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }
  .ps-tab-header h3 { font-size: 16px; margin: 0; }
  .ps-tab-header .ps-sub {
    font-size: 12px;
    color: #888;
    margin: 4px 0 0;
    line-height: 1.4;
    max-width: 540px;
  }
  .ps-loading { color: #888; padding: 24px; text-align: center; }
  .ps-section {
    margin-bottom: 20px;
    padding: 14px 16px;
    background: rgba(255,255,255,0.03);
    border-radius: 6px;
  }
  .ps-section-danger {
    background: rgba(245,179,66,0.06);
    border-left: 3px solid rgba(245,179,66,0.35);
  }
  .ps-section-danger h4 {
    color: #f5b342;
  }
  .ps-section-danger code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
  }
  .ps-section h4 {
    font-size: 13px;
    margin: 0 0 12px;
    color: #c4b3ff;
  }
  .ps-meta-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px 24px;
  }
  .ps-meta-label {
    font-size: 11px;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    display: block;
  }
  .ps-meta-value {
    margin: 2px 0 0;
    font-size: 13px;
  }
  .ps-meta-value code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 12px;
    word-break: break-all;
  }
  .ps-meta-hint {
    font-size: 11px;
    color: #777;
    margin: 4px 0 0;
    line-height: 1.4;
  }
  .ps-meta-hint code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
  }
  .ps-span2 { grid-column: span 2; }

  .ps-form-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 14px;
    margin-bottom: 12px;
  }
  .ps-form-grid label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 11px;
    color: #888;
  }
  .ps-form-grid input {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit;
    padding: 6px 10px;
    border-radius: 4px;
    font-size: 13px;
    font-family: ui-monospace, monospace;
  }
  .ps-form-grid input:focus-visible {
    outline: none;
    border-color: rgba(0,191,166,0.55);
    box-shadow: 0 0 0 3px rgba(0,191,166,0.10);
  }
  .ps-form-hint {
    font-size: 11px;
    color: #777;
    line-height: 1.4;
  }
  .ps-form-hint code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
  }

  .ps-actions {
    display: flex;
    gap: 8px;
    align-items: center;
  }
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
  .ps-btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .ps-btn-secondary {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
  }
  .ps-btn-secondary:hover:not(:disabled) {
    background: rgba(255,255,255,0.08);
  }
  .ps-btn-secondary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .ps-dirty-marker {
    font-size: 11px;
    color: #f5b342;
    background: rgba(245,179,66,0.10);
    padding: 2px 8px;
    border-radius: 10px;
  }

  .ps-host-badge {
    display: inline-block;
    padding: 1px 8px;
    border-radius: 10px;
    font-size: 10px;
    text-transform: uppercase;
    font-weight: 600;
    margin-left: 8px;
  }
  .host-base { background: rgba(0,191,166,0.15); color: #0fc; }
  .host-mao { background: rgba(123,95,255,0.15); color: #c4b3ff; }
  /* PR-8: teal-on-teal (matches host-base hue but distinguishable
     because the label reads "Orchestrator Root"). */
  .host-orchestrator { background: rgba(0,191,166,0.22); color: #0fc; border: 1px solid rgba(0,191,166,0.4); }
  .host-other { background: rgba(255,255,255,0.10); color: #ccc; }
</style>
