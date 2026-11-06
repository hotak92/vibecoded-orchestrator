<script module lang="ts">
  /** What the flow needs from a project row — ProjectView satisfies it. */
  export type RemoveTarget = { id: string; name: string };
</script>

<script lang="ts">
  // The ONE shared "Remove project" flow (v0.2.106, owner ruling).
  //
  // Both GUI surfaces that remove a project — the Projects page's per-card
  // Remove button and the top-bar project selector's delete row action —
  // render THIS component; there is no second copy of the confirm dialog or
  // of the invoke. The pure half (`removeUnregister` in
  // `$lib/unregister-escape`) sends the owner's removal definition
  // explicitly: launcher files purged AND the project's own collections
  // dropped, `leaveUnremovable: false` first.
  //
  // Confirm step (owner asked for yes/no): two explicit buttons — "Yes,
  // remove" and "No". No typed confirmation here; No is the default focus
  // (first button in DOM, so the native dialog's showModal() focuses it) and
  // Esc/backdrop behave as No. The unregister STOP keeps its existing dialog
  // and "Unregister anyway" escape (owner ruling, v0.2.97 review R5 F39).
  // Per-option control stays on the project's Settings → Danger zone.

  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import UnregisterStoppedDialog from '$lib/components/UnregisterStoppedDialog.svelte';
  import { removeUnregister } from '$lib/unregister-escape';
  import { projects } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';

  let {
    project = $bindable<RemoveTarget | null>(null),
  }: {
    /** The project to remove. Non-null opens the confirm dialog; the flow
     *  clears it on No, Esc, backdrop and completion. */
    project?: RemoveTarget | null;
  } = $props();

  let removing = $state(false);

  // Owner ruling (review R5 F39): the unregister STOP opens a dialog whose
  // second action is "Unregister anyway — leave these values".
  let stopMessage = $state('');
  let stopOpen = $state(false);
  let resolveStop: ((leaveAnyway: boolean) => void) | null = null;
  function askLeaveAnyway(message: string): Promise<boolean> {
    stopMessage = message;
    stopOpen = true;
    return new Promise((resolve) => (resolveStop = resolve));
  }
  function onStopDecided(leaveAnyway: boolean) {
    resolveStop?.(leaveAnyway);
    resolveStop = null;
  }

  /** No / Esc / backdrop: close the confirm dialog, invoke nothing. */
  function decline() {
    project = null;
  }

  async function confirmRemove() {
    const target = project;
    if (!target || removing) return;
    removing = true;
    try {
      const outcome = await removeUnregister(
        (options) => projects.delete(target.id, options),
        askLeaveAnyway,
      );
      if (outcome.kind === 'kept') {
        toast.info('Unregister stopped — the project is still registered.');
        return;
      }
      const report = outcome.report;

      // Surface every soft-fail warning as its own error toast (each is
      // distinct enough that batching would lose information).
      for (const w of report.warnings) toast.error(w);

      // One-line summary toast — counts give the user a confirmation
      // anchor (same shape as the settings page's Danger zone).
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
        ? `Removed "${report.projectName}" — ${parts.join(', ')}`
        : `Removed "${report.projectName}"`;
      toast.success(summary);

      project = null;
    } catch (e) {
      // Non-stop failures keep the dialog open so the error toast has
      // context; the user can retry or No out.
      console.error('remove failed', e);
      toast.error(e);
    } finally {
      removing = false;
    }
  }
</script>

{#if project}
  <!-- Snippets are type-checked at declaration, not call-site, so the
       {#if} narrowing does not reach them: pin the name once here. -->
  {@const name = project.name}
  <DialogRoot
    open={true}
    onClose={decline}
    width="560px"
    ariaLabelledBy="project-remove-title"
  >
    {#snippet header()}
      <h2 id="project-remove-title">Remove project</h2>
      <p class="stage-tag">"{name}" — are you sure?</p>
    {/snippet}
    {#snippet body()}
      <p class="goes">
        This stops tracking the project in the launcher and removes what VCO
        put there: its hooks, scripts and bundled files (in
        <code>.claude/hooks/</code>, <code>.claude/scripts/</code> and the
        infra compose files), the lines VCO wrote into your env files, and
        this project's own Weaviate collections — its knowledge graph, its
        docs collection and its code graph.
      </p>
      <p class="stays">
        The folder and everything in it that is yours stays: your source
        code, <code>.env</code> and other values that are yours, secrets in
        the keychain, your <code>knowledge/</code> notes,
        <code>CLAUDE.md</code>, <code>CONTEXT_STATE.md</code>, and anything
        you changed yourself.
      </p>
      <p class="hint">
        For finer control (keep the collections, keep the bundled files),
        use the project's Settings → Danger zone.
      </p>
    {/snippet}
    {#snippet footer()}
      <div class="actions">
        <!-- No first: the native dialog's showModal() focuses the first
             focusable element, so "No" is the default focus. -->
        <button
          type="button"
          class="btn-3d btn-3d-primary btn-3d-sm"
          data-testid="project-remove-no"
          onclick={decline}
          disabled={removing}
        >
          No
        </button>
        <button
          type="button"
          class="btn-3d btn-3d-accent btn-3d-sm"
          data-testid="project-remove-yes"
          onclick={confirmRemove}
          disabled={removing}
        >
          {#if removing}
            <span class="spinner-sm"></span>
          {:else}
            Yes, remove
          {/if}
        </button>
      </div>
    {/snippet}
  </DialogRoot>
{/if}

<UnregisterStoppedDialog
  bind:open={stopOpen}
  message={stopMessage}
  projectName={project?.name ?? ''}
  onDecide={onStopDecided}
/>

<style>
  h2 {
    margin: 0;
    font-size: 18px;
    font-weight: 700;
  }
  .stage-tag {
    margin: 0.35rem 0 0;
    font-size: 11px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--color-teal, #00bfa6);
  }
  .goes {
    margin: 0 0 0.75rem;
    font-size: 0.85rem;
    line-height: 1.55;
    color: var(--color-text, #f1f5f9);
  }
  .stays {
    margin: 0 0 0.75rem;
    font-size: 0.85rem;
    line-height: 1.55;
    color: var(--color-mid, #94a3b8);
  }
  .goes code,
  .stays code {
    font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
    font-size: 0.78rem;
    background: rgba(255, 255, 255, 0.05);
    padding: 1px 4px;
    border-radius: 3px;
  }
  .hint {
    margin: 0;
    font-size: 0.8rem;
    font-style: italic;
    color: var(--color-muted, #475569);
  }
  .actions {
    display: flex;
    gap: 0.75rem;
    justify-content: flex-end;
    flex-wrap: wrap;
  }
  .spinner-sm {
    display: inline-block;
    width: 12px;
    height: 12px;
    border: 2px solid rgba(0, 0, 0, 0.2);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
</style>
