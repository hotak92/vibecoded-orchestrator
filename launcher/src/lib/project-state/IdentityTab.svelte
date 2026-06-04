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
  import SharedKgPicker from '$lib/components/SharedKgPicker.svelte';
  import type {
    ProjectIdentity,
    UpdateProjectIdentityRequest,
    UpdateProjectIdentityResult,
  } from '$lib/types/identity';

  // PR-5 (v0.2.11): OrchestratorRootView type mirror (from orchestrator_root.rs).
  interface OrchestratorRootView {
    id: string | null;
    name: string;
    version: string;
    folder_path: string;
    is_registered: boolean;
    is_present: boolean;
  }

  let { projectId }: { projectId: string } = $props();

  let identity = $state<ProjectIdentity | null>(null);
  let loading = $state(true);
  let saving = $state(false);
  let redetecting = $state(false);

  // PR-5 (v0.2.11): fetch the orchestrator root view to display the
  // shared KG collection name for non-root projects.
  let orchestratorRootView = $state<OrchestratorRootView | null>(null);

  /**
   * Client-side mirror of Rust's `sanitize_kg_collection`.
   * Converts a display name to the PascalCase prefix Weaviate uses.
   * "VibeCoded Orchestrator" → "VibeCodedOrchestrator"
   */
  function sanitizeKgCollection(name: string): string {
    let out = '';
    let nextUpper = true;
    for (const ch of name) {
      if (/[a-zA-Z0-9]/.test(ch)) {
        out += nextUpper ? ch.toUpperCase() : ch;
        nextUpper = false;
      } else {
        nextUpper = true;
      }
    }
    if (!out) return 'Project';
    if (/[0-9]/.test(out[0])) out = 'P' + out;
    return out;
  }

  // Derived: the shared KG collection name. When the orchestratorRootView
  // is available, compute it from the root's name. Otherwise falls back
  // to the canonical default ("VibeCodedOrchestrator_KnowledgeGraph").
  //
  // v0.2.23 B1 (2026-05-21): casing flipped from lowercase-c "Vibecoded"
  // (the v0.2.12–v0.2.22 default) back to capital-C "VibeCoded" to match
  // the brand spelling. Existing installs with the lowercase-c class are
  // adopted in place via case-insensitive lookup in
  // `install.py::_ensure_collections`; the launcher's Shared KG picker
  // surfaces the live class name regardless of casing. The "VibeCodedTools"
  // pre-v0.2.12 alias remains a legacy-detection path; users migrate via
  // the picker.
  const sharedKgName = $derived(
    orchestratorRootView
      ? `${sanitizeKgCollection(orchestratorRootView.name)}_KnowledgeGraph`
      : 'VibeCodedOrchestrator_KnowledgeGraph'
  );

  // PR-8: manual entry point for the legacy-collections cleanup, gated to
  // the orchestrator-root tab only (per the brief's C.3 — "separate
  // 'Clean up legacy collections' button in the orchestrator-root's
  // Identity tab"). The launcher's `+layout.svelte` handles the
  // auto-show on first startup; this button is the explicit re-entry
  // point for users who dismissed the auto-notice.
  let showLegacyModal = $state(false);

  // PR-26 / Group E (v0.2.12 / 2026-05-16): orchestrator-shaped KG
  // collections detected on Weaviate. Loaded on mount via the new
  // `list_orchestrator_kg_collections` Tauri command (which wraps the
  // existing `hub::cli_api::detect_orchestrator_kg_collections` probe of
  // `/v1/schema`). Used to surface a picker when multiple candidates
  // exist on the user's Weaviate and the derived canonical name doesn't
  // match exactly one of them — e.g. a user with an old
  // `VibeCodedTools_KnowledgeGraph` class alongside the new canonical
  // `VibeCodedOrchestrator_KnowledgeGraph` (or the v0.2.12–v0.2.22
  // lowercase-c variant `VibecodedOrchestrator_KnowledgeGraph`) can
  // choose which one is authoritative without hand-editing env files.
  // Soft-fail: if the detect call fails (Weaviate unreachable, GUI
  // offline, etc.) the picker just doesn't show — `sharedKgName`
  // continues to render.
  let detectedKgClasses = $state<string[]>([]);
  let showKgPicker = $state(false);
  let savingSharedKgPick = $state(false);

  async function loadDetectedKgClasses() {
    try {
      detectedKgClasses = await invoke<string[]>('list_orchestrator_kg_collections');
    } catch {
      detectedKgClasses = [];
    }
  }

  // Show the picker affordance when:
  //   - multiple orchestrator-shaped classes exist on Weaviate, AND
  //   - the derived canonical name isn't one of them (user almost
  //     certainly has stale data they want to point at OR a custom
  //     branded name they want to register as canonical).
  const showPickerButton = $derived(
    detectedKgClasses.length > 1 && !detectedKgClasses.includes(sharedKgName),
  );

  async function persistSharedKgChoice(picked: string) {
    savingSharedKgPick = true;
    try {
      await invoke('set_shared_kg_collection_name', { name: picked });
      toast.success(`Shared KG canonical name set to ${picked}`);
      // Refresh the projects store so any project-scoped env-derived
      // values that depend on the shared KG name update.
      await projectsStore.load();
    } catch (e) {
      toast.error(e);
    } finally {
      savingSharedKgPick = false;
      showKgPicker = false;
    }
  }

  // Edit-buffer state. Bound to the inputs; flushed to the backend on Save.
  let editKg = $state('');
  let editCg = $state('');

  // Track the snapshot we loaded from the backend so we can offer "Discard"
  // and know whether changes need saving.
  let loadedKg = $state('');
  let loadedCg = $state('');

  // v0.2.46 Decision B — per-project SHARED_KG_READ_DISABLED toggle.
  // Symmetric mirror of the write-disable toggle (which has the same
  // setter wired through `projectsStore.setSharedKgWriteDisabled` but
  // no UI yet). When `true`, this project's hybrid_search /
  // semantic_graph_search stop searching the shared KG. Default false
  // (reads on, asymmetric-by-default).
  let sharedKgReadDisabled = $state(false);
  let savingReadDisabled = $state(false);

  async function loadSharedKgReadDisabled() {
    try {
      sharedKgReadDisabled = await invoke<boolean>('get_shared_kg_read_disabled_cmd', { projectId });
    } catch {
      // Soft-fail: getter unreachable (test contexts, partial install).
      // Default off — matches the canonical default on a missing row.
      sharedKgReadDisabled = false;
    }
  }

  async function toggleSharedKgReadDisabled(event: Event) {
    const target = event.currentTarget as HTMLInputElement;
    const newValue = target.checked;
    savingReadDisabled = true;
    try {
      await projectsStore.setSharedKgReadDisabled(projectId, newValue);
      sharedKgReadDisabled = newValue;
      toast.success(
        newValue
          ? 'Excluded from shared KG reads'
          : 'Re-enabled shared KG reads',
      );
    } catch (e) {
      toast.error(e);
      // Revert checkbox to actual state on failure.
      target.checked = sharedKgReadDisabled;
    } finally {
      savingReadDisabled = false;
    }
  }

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
    // PR-5 (v0.2.11): fetch the orchestrator root view to show shared KG
    // info. Non-fatal — if it fails we just show the canonical default.
    try {
      const view = await invoke<OrchestratorRootView | null>('get_orchestrator_root_view');
      orchestratorRootView = view ?? null;
    } catch {
      orchestratorRootView = null;
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
  onMount(loadDetectedKgClasses);
  onMount(loadSharedKgReadDisabled);
  $effect(() => {
    if (projectId) {
      void load();
      void loadSharedKgReadDisabled();
    }
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
          <span class="ps-meta-label">Display name</span>
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
          <span class="ps-meta-label">Slug</span>
          <p class="ps-meta-value"><code>{identity.slug}</code></p>
        </div>
        <div class="ps-span2">
          <span class="ps-meta-label">Folder</span>
          <p class="ps-meta-value"><code>{identity.folder_path}</code></p>
        </div>
        <div class="ps-span2">
          <span class="ps-meta-label">Identity source on disk</span>
          <p class="ps-meta-value"><code>{identity.identity_source}</code></p>
        </div>
        {#if identity.vct_module_version}
          <div class="ps-span2">
            <span class="ps-meta-label">vct-module.json version</span>
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

    <!-- PR-5 (v0.2.11): Shared KG section — readonly informational card. -->
    <div class="ps-section ps-section-shared-kg">
      <h4>Shared KG</h4>
      {#if identity.is_orchestrator_root}
        <p class="ps-form-hint ps-shared-kg-text">
          This project's primary KG binding is the <strong>shared KG</strong>
          for every other project on this machine. Other projects derive
          <code>SHARED_KG_COLLECTION</code> from this binding.
        </p>
        <p class="ps-form-hint ps-shared-kg-collection">
          Current shared KG collection:
          <code>{sharedKgName}</code>
        </p>
      {:else}
        <p class="ps-form-hint ps-shared-kg-text">
          Shared KG collection derived from the Orchestrator Project's
          primary KG binding:
        </p>
        <p class="ps-form-hint ps-shared-kg-collection">
          <code>{sharedKgName}</code>
        </p>
        {#if !orchestratorRootView?.is_registered}
          <p class="ps-form-hint ps-shared-kg-warn">
            No Orchestrator Project detected — shared KG name is the
            canonical default. Register an orchestrator clone to set a
            custom shared KG.
          </p>
        {/if}
      {/if}

      <!-- PR-26 / Group E (v0.2.12 / 2026-05-16): partial-match picker.
           When Weaviate has >1 orchestrator-shaped class AND none of
           them match the derived canonical name, surface the picker so
           the user can designate an existing class as canonical (common
           case: a pre-rename `VibeCodedTools_KnowledgeGraph` class
           alongside the new default). Soft-fail: button hidden when
           `list_orchestrator_kg_collections` returned <=1 candidate. -->
      {#if showPickerButton}
        <p class="ps-form-hint ps-shared-kg-picker-hint">
          <strong>{detectedKgClasses.length}</strong> orchestrator-shaped
          KG collections detected on Weaviate, none matching the canonical
          name. You can designate one as canonical — useful when migrating
          from a pre-v0.2.12 install or running a custom-branded orchestrator.
        </p>
        <button
          class="ps-btn-secondary"
          onclick={() => (showKgPicker = true)}
          disabled={savingSharedKgPick}
        >
          Manage shared KG collection ({detectedKgClasses.length} candidates)
        </button>
      {/if}

      <!-- v0.2.46 Decision B — symmetric READ gate. Hidden for the
           orchestrator-root project (which IS the shared KG; excluding
           it from its own reads makes no sense). For peer projects,
           offers an opt-out toggle so users can run a project in
           strict-isolation mode (no shared KG fan-out on
           hybrid_search / semantic_graph_search). Default off (reads
           on). The write-disable toggle has the same DB+env semantics
           but no UI yet; both share the same persistence path. -->
      {#if !identity.is_orchestrator_root}
        <label class="ps-shared-kg-read-toggle">
          <input
            type="checkbox"
            checked={sharedKgReadDisabled}
            disabled={savingReadDisabled}
            onchange={toggleSharedKgReadDisabled}
          />
          <span>
            Exclude this project from reading the shared KG
            <span class="ps-form-hint" style="display: block; margin-top: 2px;">
              When enabled, hybrid_search and semantic_graph_search stop
              fanning out into <code>{sharedKgName}</code> for this
              project. The project's own primary KG and any peer-grant
              access remain searchable. Default off.
            </span>
          </span>
        </label>
      {/if}
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

{#if showKgPicker}
  <SharedKgPicker
    candidates={detectedKgClasses}
    currentName={sharedKgName}
    onPick={persistSharedKgChoice}
    onClose={() => (showKgPicker = false)}
  />
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

  /* PR-5 (v0.2.11): Shared KG informational card. */
  .ps-section-shared-kg {
    border-left: 3px solid rgba(0,191,166,0.30);
  }
  .ps-section-shared-kg h4 {
    color: #0fc;
  }
  .ps-shared-kg-text {
    margin-bottom: 6px;
  }
  .ps-shared-kg-collection {
    margin-bottom: 0;
  }
  .ps-shared-kg-collection code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 11px;
    word-break: break-all;
    color: #0fc;
  }
  .ps-shared-kg-warn {
    margin-top: 6px;
    color: #f5b342;
    font-style: italic;
  }

  /* v0.2.46 Decision B — symmetric READ gate toggle. Same visual
     language as the existing form labels; the checkbox sits inline
     with a brief explainer so users can see the affordance without
     opening docs. */
  .ps-shared-kg-read-toggle {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    margin-top: 14px;
    padding: 8px 10px;
    background: rgba(255, 255, 255, 0.02);
    border-radius: 4px;
    font-size: 12px;
    cursor: pointer;
  }
  .ps-shared-kg-read-toggle input[type='checkbox'] {
    margin-top: 2px;
  }
  .ps-shared-kg-read-toggle code {
    font-family: ui-monospace, monospace;
    background: rgba(255, 255, 255, 0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
  }

  /* PR-26 / Group E (v0.2.12): picker affordance hint. */
  .ps-shared-kg-picker-hint {
    margin-top: 10px;
    padding: 6px 10px;
    background: rgba(245,179,66,0.06);
    border-left: 2px solid rgba(245,179,66,0.35);
    border-radius: 3px;
    line-height: 1.45;
  }
</style>
