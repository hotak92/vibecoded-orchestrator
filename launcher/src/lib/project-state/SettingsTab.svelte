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
  import type {
    ProjectView,
    RenameCollectionsPreview,
    RenameCollectionsResult,
  } from '$lib/types/launcher';
  import RegenerateOrDeferModal, {
    type StaleDerivedArtifact,
  } from '$lib/components/RegenerateOrDeferModal.svelte';
  // The model-switch types originate in the dependency-free logic module;
  // import them from there directly (the svelte language server does not
  // surface a `.svelte` file's `export type` re-exports as named members).
  import type {
    ModelSwitchContext,
    SlotPopulatedCount,
  } from '$lib/components/regenerate-modal-logic';
  // v0.2.71 T-B-emb — per-project ACTIVE_EMBEDDING profile picker.
  import ActiveEmbeddingPicker from '$lib/project-state/ActiveEmbeddingPicker.svelte';
  // v0.2.71 T-B-flags — per-project dual-write + dual-log toggles.
  import DualWriteFlagsPanel from '$lib/project-state/DualWriteFlagsPanel.svelte';
  // v0.2.91 WP-I (decision #6) — THIS project's deferral ledger. Scope-locked:
  // the panel reads only this project's folder, and the orchestrator-root
  // ledger renders on its own global surface (Preferences → Updates), never
  // here. The Bundle section below already tells users their preserved-file
  // entries land in UPDATE_DEFERRED.md; this panel is where they read and
  // clear them without leaving the launcher.
  import DeferralLedgerPanel from '$lib/components/DeferralLedgerPanel.svelte';
  // v0.2.92 WP-17 (W3) — change the project's folder. The picker helper is
  // the shared one (browse-cancel is a silent no-op there, which is the
  // behaviour this flow wants too).
  import { pickDirectory } from '$lib/dialog';

  // Move types are declared HERE rather than in `$lib/types/launcher.ts`
  // because this is their only consumer today. The plan itself is produced by
  // `MovePlan.to_json` in `vco_lib/project_move.py` and carried through Rust
  // as an opaque JSON value on purpose — a mirrored Rust struct would be a
  // third copy of a shape with one owner.
  //
  // W3 wrote "when W14's rename flow becomes a second consumer, these move to
  // `$lib/types/launcher.ts` unchanged". W14 ARRIVED and did NOT become one:
  // a collection rename carries CLASSES, not files, so it needs its own shape
  // (`RenameCollectionsPreview`, imported above) rather than this one. Those
  // types live in `$lib/types/launcher.ts` because they sit beside the
  // existing `RenameProjectResult` there. These two stay local: still one
  // consumer, so the trigger has not fired. Recorded rather than left as an
  // open expectation, so the next reader does not go looking for a move that
  // was correctly not made.
  interface MovePlanConflict {
    rel: string;
    /** `identical` — the destination already has these exact bytes.
     *  `divergent` — it has different bytes; the source's version lands as a
     *  `.vco-moved` sibling and NOTHING is overwritten. */
    kind: 'identical' | 'divergent';
  }
  interface MovePlanPreview {
    src: string;
    dst: string;
    counts: {
      bundle_clean: number;
      user_modified: number;
      user_adjacent: number;
      to_copy: number;
      conflicts_identical: number;
      conflicts_divergent: number;
    };
    conflicts: MovePlanConflict[];
    stays_in_old_folder: string[];
    warnings: string[];
  }
  interface ChangeProjectPathResult {
    ok: boolean;
    /** Distinct machine key for a pre-commit refusal (`dst_not_empty`,
     *  `dst_inside_registered_project`, …) so the UI can offer the right
     *  next action rather than a generic error. */
    refused: string | null;
    error: string | null;
    plan: MovePlanPreview | null;
    pre_flip: unknown | null;
    post_flip: unknown | null;
    commit: unknown | null;
    /** TRUE once the flip transaction landed. After this the UI must NOT say
     *  "nothing changed" — the project HAS moved. */
    committed: boolean;
    warnings: string[];
  }

  let { projectId }: { projectId: string } = $props();

  // v0.2.60 Piece 4: after a bundle update we probe for DERIVED collections
  // that are stale + schema-changed + have NO data-preserving migration
  // (POLICY STEP 3). If any, render the regenerate-or-defer modal. The probe
  // is read-only (migrate-schema --check) and soft-fails to "no modal".
  let staleDerived = $state<StaleDerivedArtifact[]>([]);
  let showRegenerateModal = $state(false);
  // v0.2.71 Track T-C-modal: when the ActiveEmbeddingPicker reports a genuine
  // model SWITCH, we build a ModelSwitchContext (per-slot populated counts +
  // smart default) and open the SAME modal with a `modelSwitch` prop so the
  // user gets the three-option Regenerate / Keep-previous / Defer panel. Null
  // when the modal was opened by the bundle-update stale-derived probe instead.
  let modelSwitchCtx = $state<ModelSwitchContext | null>(null);
  // v0.2.71 (R1 MEDIUM fix): the ActiveEmbeddingPicker's dropdown caches the
  // effective profile at load. "Keep previous model" reverts the DB profile
  // AFTER the picker already saved the new one — so the picker would keep
  // showing the new profile while the DB is on the old. Bumping this nonce
  // re-mounts the picker (via {#key}) after the model-switch modal closes, so
  // its dropdown re-reads the effective value from the DB.
  let pickerReloadNonce = $state(0);

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
      // v0.2.91 WP-F3: delegate to the store instead of a second, direct
      // rename invoke. The duplicate call-site updated only
      // this component's local `project`, so a rename from Settings left the
      // top-left selector AND the project-page header stale until reload.
      // `projects.rename` patches the store row (which the selector and the
      // page header both derive from) and owns the warning toasts, including
      // their severity typing (WP-F4) — no toast logic here.
      project = await projects.rename(project.id, newName.trim());
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
    // If this was a model-switch modal, the effective profile in the DB may
    // have changed while it was open (Keep-previous reverts it) — re-mount the
    // picker so its dropdown re-reads the DB. Cheap: only when a switch modal
    // was actually shown.
    const wasModelSwitch = modelSwitchCtx !== null;
    showRegenerateModal = false;
    staleDerived = [];
    modelSwitchCtx = null;
    if (wasModelSwitch) pickerReloadNonce += 1;
  }

  /**
   * v0.2.71 Track T-C-modal: the ActiveEmbeddingPicker fired `onModelSwitch`
   * after the user saved a NEW active-embedding profile. Build the
   * ModelSwitchContext (per-slot populated counts → smart default) via the
   * `project_embedding_slot_counts` command, then open the RegenerateOrDeferModal
   * with `modelSwitch` set so the three-option panel renders.
   *
   * Best-effort: slot-count probe soft-fails to an empty result (the command
   * itself never throws on a probe failure — only on project-not-found), in
   * which case the modal still opens but degrades to Regenerate/Defer (no
   * keep-previous smart default). We open the modal regardless of the probe so
   * a switch always surfaces the choice.
   */
  async function handleModelSwitch(newProfile: string) {
    if (!project) return;
    let slotCounts: SlotPopulatedCount[] = [];
    let mostPopulatedProfile: string | null = null;
    let total = 0;
    let collection: string | null = null;
    let targetSlot: string | null = null;
    try {
      // Pass `forProfile` so the backend also returns `target_slot` — the slot
      // the new profile embeds into — for the modal's "Regenerate now".
      const counts = await invoke<{
        collection: string;
        total: number;
        slots: SlotPopulatedCount[];
        most_populated_profile: string | null;
        target_slot: string | null;
      }>('project_embedding_slot_counts', {
        projectId: project.id,
        forProfile: newProfile,
      });
      slotCounts = counts.slots ?? [];
      mostPopulatedProfile = counts.most_populated_profile;
      total = counts.total ?? 0;
      collection = counts.collection ?? null;
      targetSlot = counts.target_slot ?? null;
    } catch (e) {
      // Probe faulted (project vanished, etc.) — still surface the switch
      // choice with a degraded (2-option) modal.
      console.warn('project_embedding_slot_counts failed:', e);
    }
    modelSwitchCtx = {
      newProfile,
      slotCounts,
      mostPopulatedProfile,
      total,
      collection,
      targetSlot,
    };
    // No stale-derived artifacts in the pure model-switch path — the modal
    // renders just the model-switch panel.
    staleDerived = [];
    showRegenerateModal = true;
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


  // ── v0.2.92 WP-17 (W3): change the project's folder ────────────────────
  //
  // Two steps, never one. `preview_project_path_change` is read-only and
  // ALWAYS runs first, because the one thing a move cannot do is ask
  // afterwards: once files have landed at the destination and the row has
  // flipped, "did you mean this?" is too late. The preview is where the user
  // sees the conflict list, the copy counts, and the fact that the old folder
  // is kept.
  let moveTarget = $state('');

  // ── Rename collections (v0.2.92 W14) ────────────────────────────────────
  //
  // The OTHER rename. `rename_project_v2` (the name field above) is
  // identity-preserving by design: it changes the display name and the slug
  // and every collection keeps its creation-time name. This panel is the
  // explicitly consented operation that carries the DATA too.
  //
  // Preview → confirm, the same shape as the move panel, because the same
  // property makes it safe: the user sees exactly which classes move and how
  // many objects each holds BEFORE anything is written.
  let renameTarget = $state('');
  let renameBusy = $state(false);
  let renamePreview = $state<RenameCollectionsPreview | null>(null);
  let renameRefusal = $state<{ reason: string; error: string } | null>(null);
  let renameResult = $state<RenameCollectionsResult | null>(null);

  async function previewRenameCollections() {
    if (!project || !renameTarget.trim()) return;
    renameBusy = true;
    renameRefusal = null;
    renamePreview = null;
    renameResult = null;
    try {
      const res = await invoke<RenameCollectionsResult>(
        'rename_collections_v2',
        {
          id: project.id,
          newName: renameTarget.trim(),
          dryRun: true,
        },
      );
      if (!res.ok) {
        renameRefusal = {
          reason: res.refused ?? 'unknown',
          error: res.error ?? '',
        };
        return;
      }
      renamePreview = res.preview ?? null;
    } catch (e) {
      toast.error(e);
    } finally {
      renameBusy = false;
    }
  }

  async function confirmRenameCollections() {
    if (!project || !renamePreview) return;
    renameBusy = true;
    try {
      const res = await invoke<RenameCollectionsResult>(
        'rename_collections_v2',
        {
          id: project.id,
          newName: renameTarget.trim(),
          dryRun: false,
        },
      );
      renameResult = res;
      if (res.ok) {
        toast.success('Collections renamed — the previous classes were kept');
        renamePreview = null;
        await load();
      } else {
        // A refusal here means NOTHING was changed: every precondition is
        // checked before the first object is copied, and the copy is verified
        // before a single binding moves.
        renameRefusal = {
          reason: res.refused ?? 'unknown',
          error: res.error ?? '',
        };
      }
    } catch (e) {
      toast.error(e);
    } finally {
      renameBusy = false;
    }
  }

  let movePreview = $state<MovePlanPreview | null>(null);
  let moveRefusal = $state<{ reason: string; error: string } | null>(null);
  let moveIntoExisting = $state(false);
  let moveFromMissing = $state(false);
  let moveBusy = $state(false);
  let moveResult = $state<ChangeProjectPathResult | null>(null);

  async function browseMoveTarget() {
    const picked = await pickDirectory({
      title: 'Choose the project’s new folder',
      defaultPath: project?.folder_path,
    });
    // Browse-cancel is a silent no-op — clearing a path the user typed
    // because they changed their mind about browsing would be hostile.
    if (picked) {
      moveTarget = picked;
      movePreview = null;
      moveRefusal = null;
      moveResult = null;
    }
  }

  async function previewMove() {
    if (!project || !moveTarget.trim()) return;
    moveBusy = true;
    moveRefusal = null;
    movePreview = null;
    moveResult = null;
    try {
      const res = await invoke<ChangeProjectPathResult>(
        'preview_project_path_change',
        {
          projectId: project.id,
          newPath: moveTarget.trim(),
          intoExisting: moveIntoExisting,
          fromMissing: moveFromMissing,
        },
      );
      if (!res.ok) {
        moveRefusal = {
          reason: res.refused ?? 'unknown',
          error: res.error ?? '',
        };
        // `dst_not_empty` and `src_registered_path_missing` are the two
        // refusals with a legitimate opt-in. Surfacing the checkbox only
        // when its refusal actually fired keeps the default path free of
        // switches whose consequences the user has no reason to think about.
        return;
      }
      movePreview = res.plan ?? null;
    } catch (e) {
      toast.error(e);
    } finally {
      moveBusy = false;
    }
  }

  async function confirmMove() {
    if (!project || !movePreview) return;
    moveBusy = true;
    try {
      const res = await invoke<ChangeProjectPathResult>(
        'change_project_path_v2',
        {
          projectId: project.id,
          newPath: moveTarget.trim(),
          intoExisting: moveIntoExisting,
          fromMissing: moveFromMissing,
          safeAdd: false,
        },
      );
      moveResult = res;
      if (res.ok) {
        toast.success('Project folder changed');
        movePreview = null;
        await load();
      } else if (res.committed) {
        // The distinction the user needs most: the move DID happen and the
        // follow-up did not. Saying "nothing changed" here would be the most
        // expensive kind of wrong.
        toast.error(
          'The project moved, but the follow-up reconciliation did not ' +
            'finish. Run: vco project move --verify --folder ' +
            moveTarget.trim(),
        );
      } else {
        toast.error(
          (res.error ?? 'The move was refused.') +
            ' Nothing changed — the project is still at its current folder.',
        );
      }
    } catch (e) {
      toast.error(e);
    } finally {
      moveBusy = false;
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
      <p class="ps-hint">
        Renaming changes the display name only; the project's collections keep
        their original names.
      </p>
      <button
        class="ps-btn-primary"
        onclick={rename}
        disabled={saving || newName === project.name}
        title="Rename the project (display name + URL slug only — collection names are immutable post-creation)"
      >
        {saving ? 'Saving…' : 'Save name'}
      </button>
    </section>

    <section class="ps-section">
      <h2>Project folder</h2>
      <p class="ps-hint">
        Move this project's registration to a different folder. VCO copies what
        it manages plus your VCO-adjacent files (<code>knowledge/</code>,
        <code>.claude/context/</code>, disabled agents and skills, files you
        edited) and re-creates the rest there.
        <strong>Nothing at the destination is ever overwritten</strong> — a file
        that differs lands beside it as a <code>.vco-moved</code> sibling with a
        ledger entry — and
        <strong>the old folder is never deleted</strong>. Your project's own
        source, <code>.git</code> and <code>.env</code> stay where they are;
        move those yourself.
      </p>
      <p class="ps-hint">
        The project keeps its identity: collection names, the code-graph prefix
        and the slug do not change. Only where it lives does.
      </p>
      <div class="ps-move-row">
        <input
          bind:value={moveTarget}
          placeholder="Absolute path of the new folder"
          aria-label="New project folder"
          onchange={() => {
            movePreview = null;
            moveRefusal = null;
          }}
        />
        <button class="ps-btn" onclick={browseMoveTarget} disabled={moveBusy}>
          Browse…
        </button>
        <button
          class="ps-btn"
          onclick={previewMove}
          disabled={moveBusy || !moveTarget.trim()}
        >
          {moveBusy ? 'Checking…' : 'Preview move'}
        </button>
      </div>

      {#if moveRefusal}
        <div class="ps-move-refusal">
          <p><strong>Cannot move there:</strong> {moveRefusal.error}</p>
          {#if moveRefusal.reason === 'dst_not_empty'}
            <label class="ps-move-opt">
              <input type="checkbox" bind:checked={moveIntoExisting} />
              That folder already has my project in it — move into it anyway
              (existing files are still never overwritten)
            </label>
          {/if}
          {#if moveRefusal.reason === 'src_registered_path_missing'}
            <label class="ps-move-opt">
              <input type="checkbox" bind:checked={moveFromMissing} />
              The current folder is gone — just re-point the registration
              (nothing can be copied)
            </label>
          {/if}
        </div>
      {/if}

      {#if movePreview}
        <div class="ps-move-preview">
          <p>
            <code>{movePreview.src}</code> → <code>{movePreview.dst}</code>
          </p>
          <ul>
            <li>{movePreview.counts.to_copy} file(s) copied</li>
            <li>
              {movePreview.counts.bundle_clean} unmodified bundle file(s) re-created
              at the destination instead of copied
            </li>
            <li>
              {movePreview.counts.conflicts_identical} already identical there
            </li>
            <li>
              <strong
                >{movePreview.counts.conflicts_divergent} differ there</strong
              >
              — those land beside the existing file as
              <code>.vco-moved</code> siblings, one ledger entry each
            </li>
          </ul>
          {#if movePreview.conflicts.some((c) => c.kind === 'divergent')}
            <details>
              <summary>Show the files that differ</summary>
              <ul class="ps-move-conflicts">
                {#each movePreview.conflicts.filter((c) => c.kind === 'divergent') as c (c.rel)}
                  <li><code>{c.rel}</code></li>
                {/each}
              </ul>
            </details>
          {/if}
          {#each movePreview.warnings as w (w)}
            <p class="ps-move-warn">{w}</p>
          {/each}
          <p class="ps-hint">
            Claude Code keeps its per-project memory and transcripts keyed to
            the OLD path. They do not follow a move; the summary afterwards
            gives you the copy command.
          </p>
          <button
            class="ps-btn-primary"
            onclick={confirmMove}
            disabled={moveBusy}
          >
            {moveBusy ? 'Moving…' : 'Move project'}
          </button>
        </div>
      {/if}

      {#if moveResult && moveResult.warnings.length > 0}
        <ul class="ps-move-conflicts">
          {#each moveResult.warnings as w (w)}
            <li>{w}</li>
          {/each}
        </ul>
      {/if}
    </section>
    <section class="ps-card">
      <h3>Rename collections</h3>
      <p class="ps-hint">
        Renaming a project above changes only its display name — its Weaviate
        collections keep the names they were created with, on purpose. This
        does the other half: it gives the project a new name
        <em>and carries every object</em>, with its vectors, to the class names
        that name derives. Nothing is re-embedded.
      </p>
      <p class="ps-hint">
        <strong>The previous classes are never dropped.</strong> They keep every
        object; afterwards the project's ledger records the one guarded command
        that retires them, which re-checks at that moment that nothing is still
        bound to them and that the replacement still holds the data.
      </p>
      <div class="ps-move-row">
        <input
          bind:value={renameTarget}
          placeholder="New project name"
          aria-label="New project name"
          onchange={() => {
            renamePreview = null;
            renameRefusal = null;
          }}
        />
        <button
          class="ps-btn"
          onclick={previewRenameCollections}
          disabled={renameBusy || !renameTarget.trim()}
        >
          {renameBusy ? 'Checking…' : 'Preview rename'}
        </button>
      </div>

      {#if renameRefusal}
        <div class="ps-move-refusal">
          <p><strong>Cannot rename:</strong> {renameRefusal.error}</p>
          <p class="ps-hint">
            Nothing was changed. Every precondition is checked before the first
            object is copied, and the copy is verified before a single binding
            moves.
          </p>
        </div>
      {/if}

      {#if renamePreview}
        <div class="ps-move-preview">
          <p>
            <code>{renamePreview.project_name}</code> →
            <code>{renamePreview.new_name}</code>
          </p>
          <ul class="ps-move-conflicts">
            {#each renamePreview.moves.filter((m) => m.action !== 'noop') as m (m.src)}
              <li>
                <code>{m.src}</code> → <code>{m.dst}</code>
                {#if m.action === 'copy'}
                  — {m.src_count} object(s), copied with their vectors
                {:else if m.action === 'source-absent'}
                  — <strong>this class does not exist</strong>; no data is
                  carried and the destination is created so the new binding
                  names something real
                {:else if m.action === 'resume'}
                  — resuming an interrupted rename
                {:else}
                  — empty
                {/if}
              </li>
            {/each}
          </ul>
          <p>
            <strong>{renamePreview.carried_objects} object(s)</strong> carried in
            total. Kept, not dropped:
            <code>{renamePreview.retired_classes.join(', ')}</code>
          </p>
          {#each renamePreview.warnings as w (w)}
            <p class="ps-move-warn">{w}</p>
          {/each}
          <button
            class="ps-btn-primary"
            onclick={confirmRenameCollections}
            disabled={renameBusy}
          >
            {renameBusy ? 'Carrying collections…' : 'Rename and carry the data'}
          </button>
        </div>
      {/if}

      {#if renameResult?.ok}
        <p class="ps-hint">
          Done. The previous classes are still there — see this project's
          ledger below for the guarded command that retires them.
        </p>
      {/if}
    </section>


    <section class="ps-section">
      <h2>Bundle</h2>
      <p class="ps-hint">
        Re-run the per-project bundle install to pick up newly-shipped orchestrator files
        (hooks, scripts, agents, skills, infrastructure). Your knowledge notes
        under <code>knowledge/</code> are never overwritten. For other files you
        have edited, your version is <strong>backed up</strong> to
        <code>.claude/backups/bundle-adoptions/&lt;timestamp&gt;/</code> and the
        shipped version is then written, so your edit stays recoverable but is
        not what runs. If a backup cannot be written, your file is left in place
        instead and listed in <code>.claude/context/UPDATE_DEFERRED.md</code>
        with a <code>bundle_user_modified_preserved</code> entry. If your
        <code>.claude</code> (or <code>.claude/agents</code>) is a symlink, new
        content is parked at <code>.vco-new</code> siblings and listed under a
        <code>symlink_preserved_under_install_path</code> entry instead. The
        bundle install can take 5–15 seconds.
      </p>
      <button
        class="ps-btn-primary"
        onclick={updateBundle}
        disabled={updating}
        title="Re-run install-bundle --update for this project"
      >
        {updating ? 'Updating bundle…' : 'Update bundle'}
      </button>
      <p class="ps-hint">
        Preserved files and every other deferred condition for THIS project are
        listed in the panel below — read them, run the exact command, or dismiss
        an entry that no longer applies.
      </p>
    </section>

    <!-- v0.2.91 WP-I: per-project ledger. Scope-locked to this project. -->
    <DeferralLedgerPanel scope="project" {projectId} />

    {#key pickerReloadNonce}
      <ActiveEmbeddingPicker {projectId} onModelSwitch={handleModelSwitch} />
    {/key}

    <DualWriteFlagsPanel {projectId} />

    <section class="ps-section">
      <h2>Project env vars (notes only)</h2>
      <p class="ps-hint">
        <!-- v0.2.91 F-1: `envEntries` is component-local state with no
             backend — `load()` never fetches it and no command persists it,
             so the rows a user adds vanish on the next tab switch. The
             heading's "(notes only)" disclaims the VALUES being stored; it
             does not tell the reader the NOTES are not stored either. Say so
             until there is a single-project env API to save them to. -->
        <strong>Not saved</strong> — this list is scratch space and resets when you
        leave the tab. Values are stored in <code>~/.vct-secrets/</code> or the OS
        keychain, never here; use the Secrets panel to set actual values.
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
    modelSwitch={modelSwitchCtx}
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

  /* v0.2.92 WP-17 — the move flow. Preview-then-confirm, so the preview
     block is visually distinct from the input row that produced it. */
  .ps-move-row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
  }
  .ps-move-row input {
    flex: 1 1 24rem;
    min-width: 16rem;
  }
  .ps-move-refusal {
    margin-top: 0.75rem;
    padding: 0.75rem;
    border: 1px solid var(--danger, #b23);
    border-radius: 6px;
  }
  .ps-move-opt {
    display: flex;
    gap: 0.5rem;
    align-items: flex-start;
    margin-top: 0.5rem;
    font-size: 0.9em;
  }
  .ps-move-preview {
    margin-top: 0.75rem;
    padding: 0.75rem;
    border: 1px solid var(--border, #444);
    border-radius: 6px;
  }
  .ps-move-warn {
    font-size: 0.9em;
    opacity: 0.85;
  }
  .ps-move-conflicts {
    max-height: 12rem;
    overflow-y: auto;
    font-size: 0.85em;
  }

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
