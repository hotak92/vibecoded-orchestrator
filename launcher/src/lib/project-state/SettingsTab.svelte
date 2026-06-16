<script lang="ts">
  // v0.2.22 — Item #14. Settings as a project tab, inlined.
  //
  // Previously the project page's "Settings" tab rendered only a link
  // ("Open project settings →") that navigated to
  // /project/[id]/settings — a separate page with the actual form.
  // Two clicks for what should be one. This component is the extracted
  // body of that route, parameterised by `projectId`, mounted directly
  // inside the project page's tab content. The /project/[id]/settings
  // route still exists and now also delegates to this component so
  // direct URLs / external links remain valid.
  //
  // Behaviour parity with the prior settings route: rename, update
  // bundle, env-vars-notes, danger-zone unregister. All the same Tauri
  // commands.

  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import { projects } from '$lib/stores/projects';
  import type { ProjectView, RenameProjectResult } from '$lib/types/launcher';
  import RegenerateOrDeferModal, {
    type StaleDerivedArtifact,
  } from '$lib/components/RegenerateOrDeferModal.svelte';

  let { projectId }: { projectId: string } = $props();

  // v0.2.60 Piece 4: after a bundle update we probe for DERIVED collections
  // that are stale + schema-changed + have NO data-preserving migration
  // (POLICY STEP 3). If any, render the regenerate-or-defer modal. The probe
  // is read-only (migrate-schema --check) and soft-fails to "no modal".
  let staleDerived = $state<StaleDerivedArtifact[]>([]);
  let showRegenerateModal = $state(false);

  let project = $state<ProjectView | null>(null);
  let newName = $state('');
  let saving = $state(false);
  // PR 5 (2026-05-01): "Update bundle" button — re-runs the per-project
  // bundle install in update mode. Subprocess can take 5-15s (Python
  // startup + file copies + Weaviate probe), so we lock the button while
  // it's in flight and toast the one-line summary on completion.
  let updating = $state(false);

  // Env vars: stored in module install rows? We don't have a single-project
  // env API yet — expose via the project_state secret_refs path for guidance
  // and link to the secrets panel for actual values.
  let envEntries = $state<Array<{ key: string; value: string }>>([]);
  let newEnvKey = $state('');
  let newEnvValue = $state('');

  // 2026-05-06: Danger zone — non-destructive unregister UX.
  // Two checkboxes + type-to-confirm gate the action. Both checkboxes
  // map to fields on the new `UnregisterOptions` Tauri command shape:
  //   - purgeLauncherFiles: ON by default; surgically removes hooks/
  //     scripts/compose/canonical-env-keys, preserves user content.
  //   - purgeCollections: OFF by default (opt-in); drops the project's
  //     own Weaviate collections. Shared never touched. Tooltip
  //     surfaces the rebuild path so users don't fear the choice.
  let purgeLauncherFiles = $state(true);
  let purgeCollections = $state(false);
  let unregisterConfirmText = $state('');
  let unregistering = $state(false);
  // The Unregister button is enabled only when the user has typed the
  // exact project name. Case-sensitive — matches the muscle memory of
  // GitHub's "delete repo" gate.
  let unregisterReady = $derived(
    !!project && unregisterConfirmText === project.name && !unregistering,
  );

  async function load() {
    try {
      project = await invoke<ProjectView>('get_project_v2', { id: projectId });
      newName = project?.name ?? '';
    } catch (e) {
      toast.error(e);
    }
  }

  async function rename() {
    if (!newName.trim() || !project) return;
    saving = true;
    try {
      // HIGH-7: rename_project_v2 returns RenameProjectResult { project, warnings }.
      const result = await invoke<RenameProjectResult>('rename_project_v2', {
        id: project.id,
        newName: newName.trim(),
      });
      project = result.project;
      for (const w of result.warnings) toast.error(w);
      toast.success('Renamed');
    } catch (e) {
      toast.error(e);
    } finally {
      saving = false;
    }
  }

  /**
   * PR 5 (2026-05-01): re-run the per-project bundle install in update
   * mode. Picks up new orchestrator-shipped files (hooks, scripts,
   * agents, skills, settings, infrastructure) without overwriting user
   * customizations. The store handles toasts for the per-action summary
   * + every deferral / file error.
   */
  async function updateBundle() {
    if (!project || updating) return;
    updating = true;
    try {
      // The store's `update` toasts the summary + warnings; we just
      // refresh the local copy after it returns. Errors thrown by the
      // invoke (project not in DB, folder gone) are caught here and
      // surfaced as their own error toast — soft-fail conditions never
      // throw; they flow through `result.warnings`.
      const result = await projects.update(project.id);
      project = result.project;

      // v0.2.60 Piece 4: probe for stale derived collections that hit POLICY
      // STEP 3 (no data-preserving migration). If any, surface the modal so
      // the user explicitly chooses Regenerate-now vs Defer per collection.
      // Read-only probe; soft-fail (a failed probe just means no modal — the
      // bundle update already succeeded).
      try {
        const pending = await invoke<StaleDerivedArtifact[]>(
          'probe_stale_derived_collections',
          { projectId: project.id },
        );
        if (pending && pending.length > 0) {
          staleDerived = pending;
          showRegenerateModal = true;
        }
      } catch (probeErr) {
        // Non-fatal: the update succeeded; the modal is an optional follow-up.
        console.warn('probe_stale_derived_collections failed:', probeErr);
      }
    } catch (e) {
      toast.error(`Update bundle failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      updating = false;
    }
  }

  function closeRegenerateModal() {
    showRegenerateModal = false;
    staleDerived = [];
  }

  function addEnv() {
    if (!newEnvKey.trim()) return;
    envEntries = [...envEntries, { key: newEnvKey.trim().toUpperCase(), value: newEnvValue }];
    newEnvKey = '';
    newEnvValue = '';
  }
  function removeEnv(idx: number) {
    envEntries = envEntries.filter((_, i) => i !== idx);
  }

  /**
   * 2026-05-06: invoke the new non-destructive unregister flow.
   *
   * The store's `delete()` returns the `UnregisterReport` so we can
   * surface a one-line summary toast with the actual counts of what
   * was removed. Soft-fail warnings come back via `report.warnings[]`
   * and get their own toasts (matches the bundle-install pattern).
   *
   * On success we navigate back to /projects — the deleted project
   * is no longer in the store, so /project/<id> would 404.
   */
  async function unregister() {
    if (!project || !unregisterReady) return;
    unregistering = true;
    try {
      const report = await projects.delete(project.id, {
        purgeLauncherFiles,
        purgeCollections,
      });

      // Surface every soft-fail warning as its own error toast (each is
      // distinct enough that batching would lose information).
      for (const w of report.warnings) toast.error(w);

      // One-line summary toast — counts give the user a confirmation
      // anchor without forcing them to read the full warning stream.
      const parts: string[] = [];
      if (report.filesPurged.length > 0) {
        parts.push(`${report.filesPurged.length} files removed`);
      }
      if (report.keysPurgedFromEnv.length > 0) {
        parts.push(`${report.keysPurgedFromEnv.length} env keys cleaned`);
      }
      if (report.collectionsDropped.length > 0) {
        parts.push(`${report.collectionsDropped.length} collections dropped`);
      }
      const summary = parts.length > 0
        ? `Unregistered "${report.projectName}" — ${parts.join(', ')}`
        : `Unregistered "${report.projectName}"`;
      toast.success(summary);

      goto('/projects');
    } catch (e) {
      toast.error(e);
    } finally {
      unregistering = false;
    }
  }

  onMount(load);
  // Re-load when the embedding page swaps projectId (rare — the project
  // page already remounts the tab, but keep the effect for safety).
  $effect(() => {
    if (projectId) void load();
  });
</script>

{#if !project}
  <p class="ps-empty">Loading…</p>
{:else}
  <div class="ps-main">
    <section class="ps-section">
      <h2>Metadata</h2>
      <div class="ps-grid">
        <label><span>Name</span><input bind:value={newName} /></label>
        <div class="ps-meta">
          <p><span>Folder:</span> <code>{project.folder_path}</code></p>
          <p><span>Host:</span> <code>{project.host}</code></p>
          <p><span>Modules:</span> {project.module_count}</p>
          <p><span>Created:</span> {new Date(project.created_at).toLocaleString()}</p>
        </div>
      </div>
      <button
        class="ps-btn-primary"
        onclick={rename}
        disabled={saving || newName === project.name}
        title="Rename the project (updates KG_COLLECTION + CODE_GRAPH_PROJECT env keys)"
      >
        {saving ? 'Saving…' : 'Save name'}
      </button>
    </section>

    <section class="ps-section">
      <h2>Bundle</h2>
      <p class="ps-hint">
        Re-run the per-project bundle install to pick up newly-shipped orchestrator files
        (hooks, scripts, agents, skills, infrastructure) WITHOUT overwriting your
        customizations. Files you've edited are preserved and listed in
        <code>.claude/context/UPDATE_DEFERRED.md</code> with a
        <code>bundle_user_modified_preserved</code> entry. The bundle install can take
        5–15 seconds.
      </p>
      <button
        class="ps-btn-primary"
        onclick={updateBundle}
        disabled={updating}
        title="Re-run install-bundle --update for this project"
      >
        {updating ? 'Updating bundle…' : 'Update bundle'}
      </button>
    </section>

    <section class="ps-section">
      <h2>Project env vars (notes only)</h2>
      <p class="ps-hint">
        Values are stored in <code>~/.vct-secrets/</code> or the OS keychain — not here. This list is a
        reminder of what your agents expect. Use the Secrets panel to set actual values.
      </p>
      <table class="ps-table">
        <thead><tr><th>KEY</th><th>Notes / placeholder</th><th></th></tr></thead>
        <tbody>
          {#each envEntries as e, i (e.key)}
            <tr>
              <td><code>{e.key}</code></td>
              <td><input bind:value={envEntries[i].value} /></td>
              <td><button class="ps-btn-link" onclick={() => removeEnv(i)} title="Remove this env-var note">Remove</button></td>
            </tr>
          {/each}
          <tr>
            <td><input bind:value={newEnvKey} placeholder="MY_VAR" /></td>
            <td><input bind:value={newEnvValue} placeholder="optional notes" /></td>
            <td><button class="ps-btn-primary" onclick={addEnv} title="Add a placeholder note for this env var (no value stored here)">Add</button></td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="ps-section ps-danger">
      <h2>Danger zone</h2>

      <div class="ps-danger-block">
        <h3>Unregister project</h3>
        <p class="ps-danger-lead">Remove this project from the launcher.</p>

        <label class="ps-danger-check">
          <input type="checkbox" bind:checked={purgeLauncherFiles} />
          <span>
            <strong>Remove launcher-managed files</strong>
            <small>
              Removes <code>.claude/hooks/</code>, <code>.claude/scripts/</code>,
              infra compose YAMLs, and the canonical keys from your
              <code>.env</code> / <code>.claude/env</code> /
              <code>.claude/settings.json</code> /
              <code>.vscode/settings.json</code>.
              Your agents, skills, <code>CONTEXT_STATE.md</code>,
              <code>CLAUDE.md</code>, source code, and user-added
              <code>.env</code> values are preserved.
            </small>
          </span>
        </label>

        <label class="ps-danger-check">
          <input type="checkbox" bind:checked={purgeCollections} />
          <span>
            <strong>Drop Weaviate collections</strong>
            <small>
              Drops <code>{project.name}_KnowledgeGraph</code> and
              <code>{project.name}_Development</code>. Shared collections
              are not touched.
              <em>Tip: collections can always be rebuilt from
              <code>/knowledge</code> + source code via
              <code>install-bundle --update</code>.</em>
            </small>
          </span>
        </label>

        <label class="ps-danger-confirm">
          <span>Type <code>{project.name}</code> to confirm:</span>
          <input
            type="text"
            bind:value={unregisterConfirmText}
            placeholder={project.name}
            autocomplete="off"
            spellcheck="false"
          />
        </label>

        <button
          class="ps-btn-danger"
          onclick={unregister}
          disabled={!unregisterReady}
          title="Unregister this project; gated by name-match confirmation above"
        >
          {unregistering ? 'Unregistering…' : 'Unregister'}
        </button>
      </div>
    </section>
  </div>
{/if}

<!-- v0.2.60 Piece 4: regenerate-or-defer modal, shown after a bundle update
     when ≥1 derived collection is stale with no data-preserving migration
     (POLICY STEP 3). Closing == Defer (safe default — never drops). -->
{#if showRegenerateModal && project}
  <RegenerateOrDeferModal
    projectId={project.id}
    artifacts={staleDerived}
    onClose={closeRegenerateModal}
  />
{/if}

<style>
  .ps-empty { padding: 40px; text-align: center; color: #888; }
  .ps-main { max-width: 720px; margin: 0 auto; padding: 16px; }
  .ps-section { background: rgba(255,255,255,0.03); padding: 14px; border-radius: 6px; margin-bottom: 14px; }
  .ps-section h2 { font-size: 13px; margin: 0 0 8px; color: #c4b3ff; }
  .ps-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; align-items: start; }
  .ps-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: #888; }
  .ps-grid input {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); color: inherit;
    padding: 5px 8px; border-radius: 4px; font-size: 13px;
  }
  .ps-meta { font-size: 12px; line-height: 1.7; color: #ccc; }
  .ps-meta p { margin: 0; }
  .ps-meta span { color: #888; display: inline-block; min-width: 90px; }
  .ps-meta code { background: rgba(0,0,0,0.3); padding: 1px 6px; border-radius: 3px; font-family: ui-monospace, monospace; }
  .ps-hint { font-size: 11px; color: #888; margin: 0 0 10px; line-height: 1.5; }
  .ps-hint code { background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; }
  .ps-btn-primary { background: rgb(0,191,166); border: none; color: #000; padding: 5px 14px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600; }
  .ps-btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .ps-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .ps-table th { text-align: left; padding: 4px 8px; color: #888; font-weight: 500; border-bottom: 1px solid rgba(255,255,255,0.08); }
  .ps-table td { padding: 4px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .ps-table code { font-family: ui-monospace, monospace; }
  .ps-table input {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); color: inherit;
    padding: 3px 6px; border-radius: 3px; font-size: 12px; width: 100%;
  }
  .ps-btn-link { background: none; border: none; color: #f99; cursor: pointer; font-size: 11px; padding: 0; }
  .ps-btn-link:hover { text-decoration: underline; }

  /* Danger zone — distinct red border + dark accent so it doesn't look
     like just another section. Mirrors the GitHub "Danger zone" pattern. */
  .ps-section.ps-danger {
    border: 1px solid rgba(239, 83, 80, 0.45);
    background: rgba(239, 83, 80, 0.04);
  }
  .ps-danger h2 { color: #ef9a9a; }
  .ps-danger-block { display: flex; flex-direction: column; gap: 12px; }
  .ps-danger-block h3 {
    margin: 0; font-size: 13px; color: #ef9a9a;
    border-bottom: 1px solid rgba(239, 83, 80, 0.2); padding-bottom: 4px;
  }
  .ps-danger-lead { margin: 0; font-size: 12px; color: #ddd; }
  .ps-danger-check {
    display: flex; gap: 10px; align-items: flex-start;
    padding: 8px 10px; background: rgba(0,0,0,0.2); border-radius: 4px;
    cursor: pointer; user-select: none;
  }
  .ps-danger-check input[type='checkbox'] { margin-top: 2px; flex-shrink: 0; }
  .ps-danger-check span { display: flex; flex-direction: column; gap: 4px; flex: 1; }
  .ps-danger-check strong { font-size: 12px; color: #f5f5f5; font-weight: 600; }
  .ps-danger-check small { font-size: 11px; color: #aaa; line-height: 1.5; }
  .ps-danger-check em { color: #c4b3ff; font-style: normal; }
  .ps-danger-check code, .ps-danger-block code {
    background: rgba(0,0,0,0.3); padding: 1px 4px; border-radius: 3px;
    font-family: ui-monospace, monospace; font-size: 10px;
  }
  .ps-danger-confirm {
    display: flex; flex-direction: column; gap: 4px;
    margin-top: 4px; font-size: 12px; color: #ddd;
  }
  .ps-danger-confirm input {
    background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.12); color: inherit;
    padding: 5px 8px; border-radius: 4px; font-size: 13px; font-family: ui-monospace, monospace;
  }
  .ps-btn-danger {
    background: #d32f2f; border: none; color: #fff;
    padding: 6px 16px; border-radius: 4px; cursor: pointer;
    font-size: 12px; font-weight: 600; align-self: flex-start;
  }
  .ps-btn-danger:hover:not(:disabled) { background: #e53935; }
  .ps-btn-danger:disabled {
    background: rgba(211, 47, 47, 0.4); color: rgba(255,255,255,0.5);
    cursor: not-allowed;
  }
</style>
