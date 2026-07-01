<script lang="ts">
  // v0.2.60 Piece 4: the regenerate-or-defer modal.
  //
  // SPEC-v0260-migration-runner.md §6.2. Surfaced when
  // `probe_stale_derived_collections` (called after a project bundle update)
  // returns ≥1 DERIVED collection that is STALE + schema-changed + has NO
  // data-preserving migration script (POLICY STEP 3). The runner did NOT drop
  // anything — the user must explicitly CHOOSE per collection:
  //
  //   • "Regenerate now"  → drop + recreate + re-sync from disk
  //       (knowledge/** / docs/** / the source walk). Safe because the source
  //       is on disk, but takes time (re-embed). For the shared KG this routes
  //       through the guarded migrate-shared-kg-schema body (GUARD 1/2), so a
  //       cross-project-unrecoverable case still REFUSES (shown inline) rather
  //       than dropping. Tauri: apply_stale_derived_choice(choice="regenerate").
  //   • "Defer to Claude" → write the migration need to UPDATE_DEFERRED.md; a
  //       future Claude session handles it. Tauri:
  //       apply_stale_derived_choice(choice="defer").
  //
  // SAFE DEFAULT: closing the modal (Escape / X / backdrop) == Defer for any
  // still-undecided collection. Nothing destructive ever happens without an
  // explicit "Regenerate now" click. The same modal + commands back v0.3.0's
  // clean-reset (clean-reset just pre-selects "Regenerate now").
  //
  // Brand: navy bg + teal (#00BFA6) primary / purple (#7B5FFF) accent / pink
  // (#FF4FA0) warning, Inter, glass card — per .claude/references/
  // VCO_BRAND_REFERENCE.md.

  import { onMount, onDestroy } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import {
    pickKeepCandidate,
    keepCandidateDisplayCount,
    type SlotPopulatedCount,
    type ModelSwitchContext,
  } from './regenerate-modal-logic';

  export type StaleDerivedArtifact = {
    artifact_type: string;
    artifact_name: string;
    stored_version: number | null;
    canonical_version: number | null;
    changed_fields: string[];
    regenerate_est_seconds: number | null;
    has_cross_project_shared_nodes: boolean | null;
  };

  type MigrationChoiceResult = {
    artifact_type: string;
    artifact_name: string;
    choice: string;
    ok: boolean;
    refused: boolean;
    dropped: boolean;
    registered: boolean;
    deferred: boolean;
    detail: string;
    error: string | null;
  };

  // v0.2.71 Track T-C-modal: the model-switch types + smart-default logic
  // live in `./regenerate-modal-logic.ts` (a dependency-free module) so they
  // are unit-testable under the node/vitest setup. Re-export for parents that
  // construct a `ModelSwitchContext` to pass in.
  export type { SlotPopulatedCount, ModelSwitchContext };

  let {
    projectId,
    artifacts,
    onClose,
    modelSwitch = null,
  }: {
    projectId: string;
    artifacts: StaleDerivedArtifact[];
    onClose: () => void;
    /**
     * v0.2.71 Track T-C-modal — OPTIONAL. When non-null, a model SWITCH was
     * detected: the modal renders a model-switch section with three options
     * (Regenerate now / Keep previous model / Defer). When null, the modal is
     * the classic v0.2.60 per-artifact regenerate-or-defer form.
     */
    modelSwitch?: ModelSwitchContext | null;
  } = $props();

  // Per-artifact UI state keyed by artifact_name (unique per project).
  type RowState = {
    busy: boolean;
    decided: 'regenerated' | 'deferred' | null;
    refused: boolean;
    detail: string;
    error: string | null;
  };
  // Seed the per-artifact UI state once. `artifacts` is a fixed prop for this
  // modal instance's lifetime (the parent re-mounts the modal via {#if} each
  // time it surfaces), so capturing the initial value here is intentional.
  function seedRows(list: StaleDerivedArtifact[]): Record<string, RowState> {
    return Object.fromEntries(
      list.map((a) => [
        a.artifact_name,
        { busy: false, decided: null, refused: false, detail: '', error: null },
      ]),
    );
  }
  // svelte-ignore state_referenced_locally — intentional one-time seed; the
  // modal is re-mounted with fresh `artifacts` by the parent {#if} each time.
  let rows = $state<Record<string, RowState>>(seedRows(artifacts));

  // True once every artifact has a final decision (regenerated or deferred).
  const allDecided = $derived(
    artifacts.every((a) => rows[a.artifact_name]?.decided !== null),
  );

  // ── v0.2.71 Track T-C-modal: model-switch "keep previous model" state ────
  type ModelSwitchState = {
    busy: boolean;
    decided: 'kept' | null;
    detail: string;
    error: string | null;
  };
  let switchState = $state<ModelSwitchState>({
    busy: false,
    decided: null,
    detail: '',
    error: null,
  });

  const anyBusy = $derived(
    artifacts.some((a) => rows[a.artifact_name]?.busy) || switchState.busy,
  );

  // The slot row to PROPOSE for "keep previous model": the most-populated
  // profile per the count probe, falling back to the highest-populated slot.
  // Logic lives in the testable `regenerate-modal-logic` module.
  const keepCandidate = $derived(pickKeepCandidate(modelSwitch));
  // v0.2.71 (L2 fix): the number shown to the user is the profile's TOTAL
  // populated objects across ALL its slots (a profile like `arctic` can span
  // `ollama_embed` + `arctic2_embed`), not just the single candidate slot's
  // count. The invoke still targets `keepCandidate.profile` (correct).
  const keepCount = $derived(keepCandidateDisplayCount(modelSwitch));

  /**
   * "Keep previous model" — revert the active-embedding choice to the
   * most-populated slot/model and re-sync from there (cheap: skip-by-hash +
   * backfill gaps, NOT a full re-embed). Writes the choice through T-B-emb's
   * sticky user path (`set_project_active_embedding`, source=user) so the kept
   * model survives future update passes.
   */
  async function keepPreviousModel() {
    if (!modelSwitch || !keepCandidate) return;
    if (switchState.busy || switchState.decided !== null) return;
    switchState = { ...switchState, busy: true, error: null };
    const profile = keepCandidate.profile;
    try {
      // T-B-emb command: records a deliberate user pick (source=user, sticky).
      await invoke('set_project_active_embedding', { projectId, profile });
      switchState = {
        busy: false,
        decided: 'kept',
        detail: `Kept ${profile} (${keepCount} already embedded). Re-syncing fills any gaps.`,
        error: null,
      };
      toast.success(`Kept previous model: ${profile}.`);
      // Pure model-switch (no derived-schema artifacts to decide) → close.
      // When artifacts remain, leave the modal up so the user decides those
      // (the footer's "Defer the rest & close" handles them safely).
      if (artifacts.length === 0) {
        onClose();
      }
    } catch (e) {
      switchState = {
        busy: false,
        decided: null,
        detail: '',
        error: e instanceof Error ? e.message : String(e),
      };
      toast.error(
        `Keep previous model failed: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
  }

  function humanType(t: string): string {
    switch (t) {
      case 'shared_kg_collection':
        return 'Shared Knowledge Graph';
      case 'kg_collection':
        return 'Knowledge Graph';
      case 'development_collection':
        return 'Development docs collection';
      case 'diagrams_collection':
        return 'Diagrams collection';
      case 'codegraph_collection':
        return 'Code graph collections';
      default:
        return t;
    }
  }

  function estLabel(secs: number | null): string {
    if (secs == null) return 'a few minutes (re-embed from disk)';
    if (secs < 60) return `~${secs}s`;
    return `~${Math.round(secs / 60)} min`;
  }

  async function choose(a: StaleDerivedArtifact, choice: 'regenerate' | 'defer') {
    const key = a.artifact_name;
    const r = rows[key];
    if (!r || r.busy || r.decided !== null) return;
    rows[key] = { ...r, busy: true, error: null };
    try {
      const res = await invoke<MigrationChoiceResult>('apply_stale_derived_choice', {
        projectId,
        artifactType: a.artifact_type,
        artifactName: a.artifact_name,
        choice,
      });
      if (choice === 'regenerate') {
        if (res.refused) {
          // GUARD 1/2 blocked the drop — NOTHING was dropped. Surface the
          // reason + the escalation; leave the row undecided so the user can
          // still Defer (or set consent + retry).
          rows[key] = {
            busy: false,
            decided: null,
            refused: true,
            detail: res.detail,
            error: null,
          };
          toast.error(
            `Regenerate refused for ${a.artifact_name}: a data-safety guard ` +
              `blocked the drop. Nothing was dropped.`,
          );
        } else if (res.ok) {
          rows[key] = {
            busy: false,
            decided: 'regenerated',
            refused: false,
            detail: res.detail || 'Regenerated and re-synced from disk.',
            error: null,
          };
          toast.success(`Regenerated ${a.artifact_name}.`);
        } else {
          rows[key] = {
            busy: false,
            decided: null,
            refused: false,
            detail: '',
            error: res.error ?? 'Regenerate did not complete.',
          };
          toast.error(`Regenerate failed for ${a.artifact_name}: ${res.error ?? '?'}`);
        }
      } else {
        // defer
        rows[key] = {
          busy: false,
          decided: 'deferred',
          refused: false,
          detail: res.detail || 'Deferred to Claude (UPDATE_DEFERRED.md).',
          error: null,
        };
        toast.success(`Deferred ${a.artifact_name} to Claude.`);
      }
    } catch (e) {
      rows[key] = {
        busy: false,
        decided: null,
        refused: false,
        detail: '',
        error: e instanceof Error ? e.message : String(e),
      };
      toast.error(`Action failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  /**
   * Dismiss == Defer for every undecided artifact (the safe default — closing
   * must never drop). We fire defer for each still-undecided row, then close.
   * Decided rows are left as-is. If a row is mid-flight we wait it out (the
   * close is a no-op while busy).
   */
  async function dismissAsDefer() {
    if (anyBusy) return;
    const undecided = artifacts.filter((a) => rows[a.artifact_name]?.decided === null);
    for (const a of undecided) {
      // Defer is non-destructive; fire sequentially so deferral writes don't
      // race the same UPDATE_DEFERRED.md file.
      await choose(a, 'defer');
    }
    onClose();
  }

  /**
   * v0.2.71 Track T-C-modal: "Regenerate now" from the model-switch panel.
   * Re-embeds every still-undecided derived artifact (drop + recreate +
   * re-sync into the new model's slot). Fires sequentially; if every artifact
   * ends up decided (none refused) the modal closes. A GUARD refusal leaves
   * that artifact's card visible below so the user can still Defer it.
   */
  async function regenerateAll() {
    if (anyBusy) return;
    const undecided = artifacts.filter((a) => rows[a.artifact_name]?.decided === null);
    for (const a of undecided) {
      await choose(a, 'regenerate');
    }
    // Close only when nothing is left undecided (a refusal keeps the modal up).
    if (artifacts.every((a) => rows[a.artifact_name]?.decided !== null)) {
      onClose();
    }
  }

  function handleEscape(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      void dismissAsDefer();
    }
  }

  onMount(() => {
    if (typeof window !== 'undefined') {
      window.addEventListener('keydown', handleEscape);
    }
  });
  onDestroy(() => {
    if (typeof window !== 'undefined') {
      window.removeEventListener('keydown', handleEscape);
    }
  });
</script>

<div class="rgd-backdrop" role="presentation" onclick={() => void dismissAsDefer()}>
  <div
    class="rgd-modal"
    role="dialog"
    aria-modal="true"
    tabindex="-1"
    onclick={(e) => e.stopPropagation()}
    onkeydown={(e) => e.stopPropagation()}
  >
    {#if modelSwitch}
      <h3>Embedding model switched</h3>
      <p>
        The active embedding model changed to <code>{modelSwitch.newProfile}</code>,
        whose vector slot is empty. Nothing was dropped. Choose how to proceed:
      </p>
      <!-- v0.2.71 Track T-C-modal: THREE-option model-switch panel. -->
      <div class="rgd-switch" class:rgd-done={switchState.decided !== null}>
        <div class="rgd-switch-head">
          <span class="rgd-type">Active embedding model</span>
          {#if switchState.decided === 'kept'}
            <span class="rgd-badge rgd-badge-ok">Kept previous</span>
          {/if}
        </div>

        {#if keepCandidate}
          <p class="rgd-switch-note">
            <strong>{keepCandidate.profile}</strong> already has
            <strong>{keepCount}</strong>{#if modelSwitch.total > 0}
              of {modelSwitch.total}{/if} entries embedded — keeping it re-syncs
            cheaply (skip already-embedded, backfill gaps) instead of re-embedding
            everything into <code>{modelSwitch.newProfile}</code>'s empty slot.
          </p>
        {:else}
          <p class="rgd-switch-note">
            No previously-populated model slot was found, so re-embedding into
            <code>{modelSwitch.newProfile}</code> is the only option.
          </p>
        {/if}

        {#if switchState.error}
          <div class="rgd-error">{switchState.error}</div>
        {/if}
        {#if switchState.decided === 'kept' && switchState.detail}
          <div class="rgd-ok">{switchState.detail}</div>
        {/if}

        {#if switchState.decided === null}
          <div class="rgd-card-actions">
            <button
              class="rgd-btn rgd-btn-primary"
              disabled={anyBusy}
              onclick={() => void regenerateAll()}
              title="Re-embed every node into the new model's slot. Takes time."
            >
              Regenerate now (re-embed into {modelSwitch.newProfile})
            </button>
            {#if keepCandidate}
              <button
                class="rgd-btn rgd-btn-keep"
                disabled={anyBusy}
                onclick={() => void keepPreviousModel()}
                title="Revert the active model to the most-populated slot and re-sync cheaply."
              >
                {switchState.busy
                  ? 'Keeping…'
                  : `Keep previous model (${keepCandidate.profile})`}
              </button>
            {/if}
            <button
              class="rgd-btn rgd-btn-defer"
              disabled={anyBusy}
              onclick={() => void dismissAsDefer()}
              title="Record the migration need in UPDATE_DEFERRED.md; decide later."
            >
              Defer to Claude
            </button>
          </div>
        {/if}
      </div>
    {/if}

    {#if artifacts.length > 0}
      <h3>Derived collection{artifacts.length > 1 ? 's' : ''} need regenerating</h3>
      <p>
        {artifacts.length === 1 ? 'A derived collection has' : `${artifacts.length} derived collections have`}
        a schema change with no data-preserving migration available. Nothing was
        dropped. For each one, choose <strong>Regenerate now</strong> (drop +
        recreate + re-sync from disk — your source files on disk are the source of
        truth, so this is safe but takes time) or <strong>Defer to Claude</strong>
        (record it in <code>UPDATE_DEFERRED.md</code> for a later session).
      </p>
    {/if}

    <div class="rgd-list">
      {#each artifacts as a (a.artifact_name)}
        {@const r = rows[a.artifact_name]}
        <div class="rgd-card" class:rgd-done={r?.decided !== null}>
          <div class="rgd-card-head">
            <div class="rgd-card-title">
              <span class="rgd-type">{humanType(a.artifact_type)}</span>
              <code class="rgd-name">{a.artifact_name}</code>
            </div>
            {#if r?.decided === 'regenerated'}
              <span class="rgd-badge rgd-badge-ok">Regenerated</span>
            {:else if r?.decided === 'deferred'}
              <span class="rgd-badge rgd-badge-defer">Deferred</span>
            {/if}
          </div>

          <div class="rgd-changed">
            Changed:
            {#if a.changed_fields.length > 0}
              {#each a.changed_fields as f}<code>{f}</code>{/each}
            {:else}
              <code>schema fingerprint</code>
            {/if}
          </div>

          {#if a.has_cross_project_shared_nodes}
            <div class="rgd-warn">
              <strong>Heads up:</strong> other projects contributed nodes to this
              shared collection. Re-syncing from <em>this</em> clone may not
              restore their nodes — re-run each contributing project's
              <code>kg-sync --all</code> after regenerating.
            </div>
          {/if}

          {#if r?.refused}
            <div class="rgd-warn">
              <strong>Regenerate refused.</strong> A data-safety guard blocked the
              drop — nothing was dropped. {r.detail}
            </div>
          {/if}
          {#if r?.error}
            <div class="rgd-error">{r.error}</div>
          {/if}
          {#if r?.decided !== null && r?.detail}
            <div class="rgd-ok">{r.detail}</div>
          {/if}

          {#if r?.decided === null}
            <div class="rgd-card-actions">
              <button
                class="rgd-btn rgd-btn-primary"
                disabled={r?.busy}
                onclick={() => void choose(a, 'regenerate')}
                title="Drop + recreate + re-sync from disk. Takes ~{estLabel(a.regenerate_est_seconds)}."
              >
                {r?.busy ? 'Regenerating…' : `Regenerate now (${estLabel(a.regenerate_est_seconds)})`}
              </button>
              <button
                class="rgd-btn rgd-btn-defer"
                disabled={r?.busy}
                onclick={() => void choose(a, 'defer')}
                title="Write the migration need to UPDATE_DEFERRED.md; a later Claude session handles it. Nothing is dropped."
              >
                Defer to Claude
              </button>
            </div>
          {/if}
        </div>
      {/each}
    </div>

    <div class="rgd-footer">
      <p class="rgd-hint">
        Closing this dialog defers every remaining collection — nothing is ever
        dropped without an explicit <strong>Regenerate now</strong> click.
      </p>
      <button
        class="rgd-btn"
        disabled={anyBusy}
        onclick={() => void dismissAsDefer()}
      >
        {allDecided ? 'Close' : 'Defer the rest & close'}
      </button>
    </div>
  </div>
</div>

<style>
  /* Brand: navy backdrop, glass card, teal/purple/pink accents (VCO ref). */
  .rgd-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(5, 11, 31, 0.82); /* --color-bg @82% */
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding: 16px;
    overflow-y: auto;
    z-index: 350;
  }
  .rgd-modal {
    background: var(--color-bg2);
    border: 1px solid rgba(123, 95, 255, 0.4); /* purple accent — schema work */
    border-radius: var(--radius-card, 16px);
    padding: 22px;
    max-width: 680px;
    width: 92%;
    margin-top: clamp(0px, 6vh, 64px);
    color: var(--color-text);
    box-shadow: 0 20px 60px rgba(5, 11, 31, 0.6);
  }
  .rgd-modal h3 {
    margin: 0 0 12px;
    font-size: 15px;
    color: var(--color-purple, #7b5fff);
  }
  .rgd-modal p {
    font-size: 12px;
    line-height: 1.6;
    color: var(--color-mid);
    margin: 0 0 14px;
  }
  .rgd-modal code {
    background: var(--color-card);
    padding: 1px 5px;
    border-radius: 4px;
    font-size: 11px;
    color: var(--color-text);
  }

  .rgd-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .rgd-card {
    background: var(--color-card);
    border: 1px solid var(--color-border);
    border-radius: 10px;
    padding: 12px 14px;
  }
  .rgd-card.rgd-done {
    opacity: 0.78;
  }
  .rgd-card-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .rgd-card-title {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .rgd-type {
    font-size: 12px;
    font-weight: 600;
    color: var(--color-text);
  }
  .rgd-name {
    font-size: 11px;
    color: var(--color-mid);
  }
  .rgd-badge {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 2px 8px;
    border-radius: 999px;
  }
  .rgd-badge-ok {
    background: rgba(0, 191, 166, 0.16);
    color: var(--color-teal, #00bfa6);
    border: 1px solid rgba(0, 191, 166, 0.4);
  }
  .rgd-badge-defer {
    background: rgba(123, 95, 255, 0.16);
    color: var(--color-purple, #7b5fff);
    border: 1px solid rgba(123, 95, 255, 0.4);
  }

  .rgd-changed {
    font-size: 11px;
    color: var(--color-mid);
    margin: 8px 0 4px;
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    align-items: center;
  }

  .rgd-warn {
    margin: 8px 0;
    padding: 8px 10px;
    background: rgba(255, 79, 160, 0.1); /* pink @10% */
    border: 1px solid rgba(255, 79, 160, 0.3);
    border-radius: 6px;
    color: var(--color-text);
    font-size: 11px;
    line-height: 1.5;
  }
  .rgd-warn strong {
    color: var(--color-pink, #ff4fa0);
  }
  .rgd-error {
    margin: 8px 0;
    padding: 8px 10px;
    background: rgba(255, 79, 160, 0.12);
    border: 1px solid rgba(255, 79, 160, 0.3);
    border-radius: 6px;
    color: var(--color-pink, #ff4fa0);
    font-size: 11px;
  }
  .rgd-ok {
    margin: 8px 0 0;
    padding: 8px 10px;
    background: rgba(0, 191, 166, 0.1);
    border: 1px solid rgba(0, 191, 166, 0.3);
    border-radius: 6px;
    color: var(--color-teal, #00bfa6);
    font-size: 11px;
  }

  .rgd-card-actions {
    display: flex;
    gap: 8px;
    margin-top: 12px;
    flex-wrap: wrap;
  }

  /* 3D-ish buttons (brand): teal primary, purple defer, neutral close. */
  .rgd-btn {
    background: var(--color-card);
    border: 1px solid var(--color-border);
    color: var(--color-text);
    padding: 7px 14px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 12px;
    font-family: inherit;
    transition: background 0.15s ease, border-color 0.15s ease, transform 0.05s ease;
  }
  .rgd-btn:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.08);
    border-color: rgba(255, 255, 255, 0.18);
  }
  .rgd-btn:active:not(:disabled) {
    transform: translateY(1px);
  }
  .rgd-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .rgd-btn-primary {
    background: rgba(0, 191, 166, 0.18);
    border-color: rgba(0, 191, 166, 0.5);
    color: var(--color-text);
  }
  .rgd-btn-primary:hover:not(:disabled) {
    background: rgba(0, 191, 166, 0.32);
    border-color: var(--color-teal, #00bfa6);
  }
  .rgd-btn-defer {
    background: rgba(123, 95, 255, 0.16);
    border-color: rgba(123, 95, 255, 0.45);
    color: var(--color-text);
  }
  .rgd-btn-defer:hover:not(:disabled) {
    background: rgba(123, 95, 255, 0.3);
    border-color: var(--color-purple, #7b5fff);
  }
  /* v0.2.71 T-C-modal: "keep previous model" — the smart-default accent
     (brand pink #FF4FA0), distinct from teal regenerate + purple defer. */
  .rgd-btn-keep {
    background: rgba(255, 79, 160, 0.16);
    border-color: rgba(255, 79, 160, 0.5);
    color: var(--color-text);
  }
  .rgd-btn-keep:hover:not(:disabled) {
    background: rgba(255, 79, 160, 0.3);
    border-color: var(--color-pink, #ff4fa0);
  }

  /* v0.2.71 T-C-modal: model-switch panel (rendered above the artifact list
     when a model SWITCH is detected). Same glass-card treatment as rgd-card. */
  .rgd-switch {
    background: var(--color-card);
    border: 1px solid rgba(255, 79, 160, 0.3); /* pink — embedding-model work */
    border-radius: 10px;
    padding: 12px 14px;
    margin-bottom: 14px;
  }
  .rgd-switch.rgd-done {
    opacity: 0.78;
  }
  .rgd-switch-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .rgd-switch-note {
    font-size: 11px;
    line-height: 1.6;
    color: var(--color-mid);
    margin: 8px 0 0;
  }
  .rgd-switch-note strong {
    color: var(--color-text);
  }

  .rgd-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-top: 18px;
    flex-wrap: wrap;
  }
  .rgd-hint {
    margin: 0;
    flex: 1 1 240px;
    font-size: 10px;
    color: var(--color-mid);
    font-style: italic;
  }
</style>
