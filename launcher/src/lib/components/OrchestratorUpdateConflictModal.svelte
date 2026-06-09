<script lang="ts">
  // v0.2.23 (B4 / D19): orchestrator-update merge/rebase conflict modal.
  // v0.2.52 (V52-A / V52-B): one-click "Keep local" + "Accept upstream"
  //   buttons added. The legacy "Resolve manually (close this dialog)"
  //   button is REMOVED — it was the silent-dismiss path that left the
  //   install in an inconsistent state. The modal now exposes exactly
  //   three options: Abort & restore, Keep local versions, Accept
  //   upstream versions. Window-X / Escape / backdrop click are mapped
  //   to Abort (with a confirmation) instead of silent dismissal so the
  //   user can never finish a session with a half-applied update.
  //
  // Surfaced when `merge_orchestrator_with_upstream` or
  // `rebase_orchestrator_onto_upstream` return a structured error with
  // `event: "orchestrator_update_conflict"` — the merge/rebase started
  // but produced unresolved conflicts in one or more files.
  //
  // Three resolution paths (V52-B 2026-06-09):
  //   1. **Keep local versions** — `git checkout --ours <files>` + commit +
  //      continue update. Tauri command: `keep_local_and_continue_update`.
  //      Tooltip: "Discards upstream changes for the conflicting files;
  //      keeps everything you've added locally. Good for: nodes you've
  //      heavily customized."
  //   2. **Accept upstream versions** — `git checkout --theirs <files>` +
  //      commit + continue update. Tauri command:
  //      `accept_upstream_and_continue_update`. Tooltip: "Discards your
  //      local changes for the conflicting files; takes the public
  //      release version. Good for: KG nodes you didn't really need."
  //   3. **Abort & restore** — `git {merge,rebase} --abort` via
  //      `abort_orchestrator_merge_or_rebase`. Working tree returns to
  //      its pre-merge state; the update is cancelled.
  //
  // Smart default (V52-B): when EVERY conflicted file lives under
  // `knowledge/` (KG nodes are user-curated state per V52-C's
  // architectural framing), the modal auto-highlights "Keep local" and
  // shows a notice explaining the recommendation. User can still
  // override by clicking either of the other buttons.
  //
  // v0.2.51 polling: while the modal is open we poll `check_for_updates`
  // every ~2 s. Once the result reports `merge_resolved_incomplete: true`
  // (sentinel present + .git/MERGE_HEAD gone) a "Continue Update" link
  // appears for users who chose to resolve manually via the CLI before
  // the v0.2.52 one-click buttons existed. The button is informational —
  // users CAN still resolve via CLI and click it, but the new defaults
  // make this path uncommon.

  import { onMount, onDestroy } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';

  export type OrchestratorConflictPayload = {
    event: 'orchestrator_update_conflict';
    operation: 'merge' | 'rebase';
    branch: string;
    conflicted_files: string[];
    git_stderr: string;
  };

  let {
    payload,
    installPath,
    onClose,
  }: {
    payload: OrchestratorConflictPayload;
    installPath: string;
    onClose: () => void;
  } = $props();

  let aborting = $state(false);
  let aborted = $state(false);
  let error = $state<string | null>(null);

  // v0.2.51 Bug A: poll for "merge resolved but install.py not run" state.
  // Source of truth: the Rust `check_for_updates` command's
  // `merge_resolved_incomplete` flag (sentinel present + no in-flight
  // merge state). 2s cadence balances responsiveness (user notices the
  // button activate within seconds of `git commit`) against load (10
  // git probes/minute is cheap).
  let resumeReady = $state(false);
  let resuming = $state(false);
  let resumed = $state(false);
  let pollHandle: ReturnType<typeof setInterval> | null = null;

  // v0.2.52 V52-B: one-click resolution state. Both buttons share a
  // single `resolving` boolean so a click on one disables the other
  // while the resolution is in flight (avoids racing two `git checkout`
  // sequences against the same working tree).
  let resolving = $state(false);
  let resolved = $state(false);
  let resolutionMode = $state<'keep-local' | 'accept-upstream' | null>(null);

  // V52-A: tracks whether the user has triggered a "close without
  // explicit choice" (Escape / X / backdrop click). Two-step
  // confirmation: first close attempt sets this true and shows an
  // inline prompt; second confirmation triggers `abort()`.
  let confirmingDismiss = $state(false);

  // V52-B smart default: when EVERY conflicted path lives under
  // `knowledge/` (or contains `/knowledge/` for nested layouts), the
  // modal auto-recommends "Keep local". The recommendation is purely a
  // UI hint — the Tauri commands themselves don't filter by path. We
  // compute it once at mount via the payload (which doesn't change for
  // the modal's lifetime), so a simple `$derived` is enough.
  const allConflictsAreKgNodes = $derived(
    payload.conflicted_files.length > 0 &&
      payload.conflicted_files.every((f) => {
        const lower = f.toLowerCase();
        return lower.startsWith('knowledge/') || lower.includes('/knowledge/');
      })
  );

  type CheckForUpdatesResult = {
    merge_resolved_incomplete?: boolean;
    resume_operation?: string;
    resume_branch?: string;
  };

  async function probeResumeReady() {
    try {
      const us = await invoke<CheckForUpdatesResult>('check_for_updates', {
        path: installPath,
      });
      // Only flip true; we never flip back to false because the user may
      // have toggled between resolved/unresolved repeatedly. Once the
      // tree is clean, give them the button.
      if (us && us.merge_resolved_incomplete) {
        resumeReady = true;
      }
    } catch {
      // Best-effort: stale check is fine, errors don't matter to UX here.
    }
  }

  onMount(() => {
    // Immediate probe so a refresh-of-an-already-resolved tree shows the
    // button without a 2-second wait.
    void probeResumeReady();
    pollHandle = setInterval(probeResumeReady, 2000);
    // V52-A: route Escape through `dismiss()` so the user can't
    // silently escape the modal. Document-level listener because the
    // modal-local onkeydown only fires when the modal has focus.
    if (typeof window !== 'undefined') {
      window.addEventListener('keydown', handleEscape);
    }
  });

  onDestroy(() => {
    if (pollHandle !== null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
    if (typeof window !== 'undefined') {
      window.removeEventListener('keydown', handleEscape);
    }
  });

  const operationLabel = $derived(
    payload.operation === 'merge' ? 'Merge' : 'Rebase'
  );

  // Pre-computed tooltip strings so we don't fight Svelte's parser
  // when interpolating `{payload.operation}` adjacent to literal
  // characters like `--abort` in attribute values.
  const abortBtnTitle = $derived(
    `Run \`git ${payload.operation} --abort\` to restore the working tree to its pre-${payload.operation} state.`
  );
  // V52-B tooltips, locked verbatim from the user 2026-06-09 (backlog
  // §V52-B). DO NOT paraphrase — the wording is the user's chosen
  // affordance for the choice between local-vs-upstream.
  const keepLocalBtnTitle =
    "Discards upstream changes for the conflicting files; keeps everything you've added locally. Good for: nodes you've heavily customized.";
  const acceptUpstreamBtnTitle =
    "Discards your local changes for the conflicting files; takes the public release version. Good for: KG nodes you didn't really need.";

  /**
   * Best-effort: classify each conflicted file so the inline hint
   * tells the user "keep local" vs "general merge" rather than a
   * generic message.
   */
  function guidanceFor(path: string): string {
    const lower = path.toLowerCase();
    if (
      lower === 'claude.md' ||
      lower.endsWith('/claude.md') ||
      lower === '.claude/context_state.md' ||
      lower.endsWith('context_state.md') ||
      lower.startsWith('knowledge/') ||
      lower.includes('/knowledge/')
    ) {
      return 'Keep your local version; manually merge upstream additions.';
    }
    return 'Resolve manually in your editor.';
  }

  async function abort() {
    if (aborting || aborted) return;
    aborting = true;
    confirmingDismiss = false;
    error = null;
    try {
      await invoke<void>('abort_orchestrator_merge_or_rebase', { path: installPath });
      aborted = true;
      toast.success(`${operationLabel} aborted — working tree restored.`);
      // Give the user a beat to see the toast before dismissing.
      setTimeout(onClose, 600);
    } catch (e) {
      error = `Abort failed: ${e}`;
    } finally {
      aborting = false;
    }
  }

  // V52-A: dismiss is NO LONGER a silent close. The legacy "Resolve
  // manually" button that called onClose() is removed. Window-X /
  // Escape / backdrop click ALL route here — per spec, they're
  // treated as Abort intent (NOT silent dismissal). To avoid
  // accidentally destroying the user's in-flight resolution, we
  // require a two-step confirmation: first click/keypress sets
  // `confirmingDismiss = true` and shows an inline notice; a second
  // confirmation within the modal triggers `abort()`.
  //
  // Pre-condition: don't trap them mid-action. If a resolution is
  // already in flight, ignore the close request entirely.
  function dismiss() {
    if (aborting || resolving || resuming) return;
    if (aborted || resumed || resolved) {
      // Action completed successfully — closing is safe.
      onClose();
      return;
    }
    // Don't silently close. Per V52-A: treat as Abort intent, but
    // gate behind a confirmation so a stray click on the backdrop
    // doesn't nuke the merge.
    if (!confirmingDismiss) {
      confirmingDismiss = true;
      return;
    }
    // User confirmed dismiss intent — abort the merge/rebase.
    confirmingDismiss = false;
    void abort();
  }

  // V52-A: cancel the confirm-dismiss state when the user picks any
  // of the explicit actions (Keep local, Accept upstream, Abort).
  function cancelConfirmDismiss() {
    confirmingDismiss = false;
  }

  // Listen for Escape at the document level (the modal-local
  // onkeydown only fires when the modal has focus, which Svelte
  // doesn't guarantee in every browser).
  function handleEscape(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      dismiss();
    }
  }

  // V52-B: one-click resolution handlers. Both invoke the Tauri command,
  // which performs the git checkout + commit/continue + delegates to
  // resume_orchestrator_update for install.py --update + binary refresh
  // + auto-restart. The auto-restart kills the launcher mid-call so we
  // rarely reach the `resolved = true` line — it's there for the
  // crash-recovery path where the restart hop fails.
  async function keepLocal() {
    if (resolving || resolved || aborting || aborted) return;
    resolving = true;
    confirmingDismiss = false;
    resolutionMode = 'keep-local';
    error = null;
    try {
      await invoke<unknown>('keep_local_and_continue_update', {
        path: installPath,
      });
      resolved = true;
      toast.success(
        `Resolved ${payload.conflicted_files.length} conflict(s) (kept local) — install.py is running.`
      );
      setTimeout(onClose, 600);
    } catch (e) {
      error = `Keep local failed: ${e}`;
      resolutionMode = null;
    } finally {
      resolving = false;
    }
  }

  async function acceptUpstream() {
    if (resolving || resolved || aborting || aborted) return;
    resolving = true;
    confirmingDismiss = false;
    resolutionMode = 'accept-upstream';
    error = null;
    try {
      await invoke<unknown>('accept_upstream_and_continue_update', {
        path: installPath,
      });
      resolved = true;
      toast.success(
        `Resolved ${payload.conflicted_files.length} conflict(s) (accepted upstream) — install.py is running.`
      );
      setTimeout(onClose, 600);
    } catch (e) {
      error = `Accept upstream failed: ${e}`;
      resolutionMode = null;
    } finally {
      resolving = false;
    }
  }

  async function continueUpdate() {
    if (resuming || resumed) return;
    if (!resumeReady) return;
    resuming = true;
    error = null;
    try {
      // resume_orchestrator_update audit-logs, refuses on stale/dirty
      // state, then re-enters install.py --update + binary refresh +
      // auto-restart. The auto-restart kills the launcher mid-call —
      // in practice we never reach the `resumed = true` line, but it's
      // there for crash-recovery paths where the restart hop fails.
      await invoke<unknown>('resume_orchestrator_update', { path: installPath });
      resumed = true;
      toast.success('Update resumed — install.py is running.');
      setTimeout(onClose, 600);
    } catch (e) {
      // The Rust command returns human-readable errors for the bad-state
      // cases (still mid-merge, leftover markers, no sentinel). Surface
      // verbatim — they're written FOR the user.
      error = `Continue Update failed: ${e}`;
    } finally {
      resuming = false;
    }
  }
</script>

<div class="cfl-backdrop" role="presentation" onclick={dismiss}>
  <div class="cfl-modal" role="dialog" aria-modal="true" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
    <h3>{operationLabel} produced conflicts</h3>
    <p>
      <code>git {payload.operation}</code> started but couldn't combine your
      local changes with upstream automatically. Your working tree is in a
      conflicted state — the files below contain conflict markers
      (<code>&lt;&lt;&lt;&lt;&lt;&lt;&lt;</code>, <code>=======</code>,
      <code>&gt;&gt;&gt;&gt;&gt;&gt;&gt;</code>) and need to be resolved
      before you can continue.
    </p>

    <p class="cfl-hint">
      Pick how to resolve. Each option finishes the update automatically
      (runs <code>install.py --update</code>, refreshes the launcher
      binary, and restarts):
    </p>
    <ul class="cfl-options-list">
      <li>
        <strong>Keep local versions</strong> — replaces the conflicting
        files with your local content, discarding the upstream changes
        for those paths.
      </li>
      <li>
        <strong>Accept upstream versions</strong> — replaces the
        conflicting files with the public release content, discarding
        your local edits for those paths.
      </li>
      <li>
        <strong>Abort &amp; restore</strong> — runs
        <code>git {payload.operation} --abort</code>; the update is
        cancelled and your working tree returns to its pre-{payload.operation}
        state.
      </li>
    </ul>

    <!-- V52-B smart default: KG-only conflict steers the user toward
         "Keep local". KG nodes are user-curated state and a fresh
         install is unlikely to want the upstream version. -->
    {#if allConflictsAreKgNodes}
      <div class="cfl-smart-default">
        <strong>Recommended:</strong> all conflicting files are under
        <code>knowledge/</code> (your KG nodes). The default suggestion
        is <strong>Keep local versions</strong> — your KG content is
        user-curated and the upstream release rarely needs to overwrite
        it. You can still pick a different option below.
      </div>
    {/if}

    {#if resumeReady}
      <div class="cfl-ready">
        Working tree is clean — ready to continue the update.
      </div>
    {/if}

    <div class="cfl-files">
      <div class="cfl-files-title">
        {payload.conflicted_files.length} conflicted file(s):
      </div>
      {#if payload.conflicted_files.length === 0}
        <p class="cfl-empty">
          (git didn't report any conflicted paths — check
          <code>git status</code> in <code>{installPath}</code> manually.)
        </p>
      {:else}
        <ul>
          {#each payload.conflicted_files as f}
            <li>
              <code>{f}</code>
              <span class="cfl-guidance">{guidanceFor(f)}</span>
            </li>
          {/each}
        </ul>
      {/if}
    </div>

    {#if error}
      <div class="cfl-error">{error}</div>
    {/if}
    {#if aborted}
      <div class="cfl-ok">
        Working tree restored. You can try the update again or dismiss this dialog.
      </div>
    {/if}

    <!-- V52-A: two-step confirmation for Escape / X / backdrop click.
         First close attempt sets `confirmingDismiss`; we explain that
         closing without a choice will abort, and the user has to either
         confirm (click "Yes, abort") or pick one of the proper actions
         below. This prevents a stray click on the backdrop from
         destroying the merge but ALSO prevents silent dismiss. -->
    {#if confirmingDismiss && !aborting && !aborted && !resolving && !resolved}
      <div class="cfl-confirm-dismiss">
        <p>
          Closing this dialog without finishing the update will
          <strong>abort the {payload.operation}</strong> and restore
          your working tree. Continue?
        </p>
        <div class="cfl-confirm-actions">
          <button
            class="cfl-btn cfl-btn-warn cfl-btn-small"
            disabled={aborting}
            onclick={() => {
              confirmingDismiss = false;
              void abort();
            }}
          >
            Yes, abort &amp; close
          </button>
          <button
            class="cfl-btn cfl-btn-small"
            onclick={cancelConfirmDismiss}
          >
            No, keep the dialog open
          </button>
        </div>
      </div>
    {/if}

    <details class="cfl-stderr">
      <summary>Show raw git output</summary>
      <pre>{payload.git_stderr}</pre>
    </details>

    <!-- V52-A / V52-B (2026-06-09): exactly three primary actions — Abort,
         Keep local, Accept upstream. The legacy "Resolve manually (close
         this dialog)" button is REMOVED; it was the silent-dismiss path
         that left half-applied updates. Continue Update appears below the
         primary row, but only as a secondary affordance for users who
         resolved via CLI before clicking. -->
    <div class="cfl-actions">
      <button
        class="cfl-btn cfl-btn-warn"
        disabled={aborting || aborted || resolving || resolved || resuming}
        onclick={abort}
        title={abortBtnTitle}
      >
        {aborting ? 'Aborting…' : 'Abort & restore'}
      </button>
      <button
        class="cfl-btn cfl-btn-primary"
        class:cfl-btn-recommended={allConflictsAreKgNodes}
        disabled={aborting || aborted || resolving || resolved || resuming}
        onclick={keepLocal}
        title={keepLocalBtnTitle}
      >
        {resolving && resolutionMode === 'keep-local'
          ? 'Keeping local…'
          : resolved && resolutionMode === 'keep-local'
            ? 'Done'
            : 'Keep local versions'}
      </button>
      <button
        class="cfl-btn cfl-btn-primary"
        disabled={aborting || aborted || resolving || resolved || resuming}
        onclick={acceptUpstream}
        title={acceptUpstreamBtnTitle}
      >
        {resolving && resolutionMode === 'accept-upstream'
          ? 'Accepting upstream…'
          : resolved && resolutionMode === 'accept-upstream'
            ? 'Done'
            : 'Accept upstream versions'}
      </button>
    </div>

    <!-- v0.2.51 Bug A: Continue Update is the CLI-finished resolution
         path. Hidden by default; only shown when polling detects the
         working tree is clean (sentinel present + no .git/MERGE_HEAD)
         which happens when a user resolved via CLI then came back to
         the modal. Most users will use the primary buttons above and
         never see this. -->
    {#if resumeReady && !resolved && !aborted}
      <div class="cfl-resume-row">
        <button
          class="cfl-btn cfl-btn-link"
          disabled={aborting || resolving || resuming || resumed}
          onclick={continueUpdate}
          title="Run install.py --update + binary refresh + auto-restart."
        >
          {resuming
            ? 'Continuing…'
            : resumed
              ? 'Resumed'
              : 'Already resolved via CLI? Continue Update →'}
        </button>
      </div>
    {/if}
  </div>
</div>

<style>
  /* v0.2.24.1 cosmetic pass: VCT color tokens (matches divergence modal).
   * v0.2.51 fix: anchor to viewport top + safe margin + outer scroll so
   * the modal header never clips above the window in short viewports.
   * The backdrop itself becomes the scroll container (align-items:
   * flex-start), so tall modals push the bottom out of view but the
   * header stays reachable. */
  .cfl-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(5, 11, 31, 0.82); /* --color-bg at 82% */
    display: flex;
    align-items: flex-start;       /* anchor to top, not vertical center */
    justify-content: center;
    padding: 16px;                  /* safe margin from window edges */
    overflow-y: auto;               /* scroll when modal taller than viewport */
    z-index: 350;
  }
  .cfl-modal {
    background: var(--color-bg2);
    border: 1px solid rgba(255, 79, 160, 0.4); /* keep pink accent for conflict severity */
    border-radius: var(--radius-card, 12px);
    padding: 20px;
    max-width: 640px;
    width: 92%;
    /* Moderate top offset scales with viewport, never zero. */
    margin-top: clamp(0px, 6vh, 64px);
    color: var(--color-text);
  }
  .cfl-modal h3 {
    margin: 0 0 12px;
    font-size: 14px;
    color: var(--color-pink);
  }
  .cfl-modal p, .cfl-modal li {
    font-size: 12px;
    line-height: 1.6;
    color: var(--color-mid);
  }
  .cfl-modal p { margin: 0 0 10px; }
  /* v0.2.52: the legacy ordered-list selectors were removed alongside the
     ordered-list of two manual-resolution options in the template. The
     new V52-B template uses .cfl-options-list (defined below) for its
     bullet list of three options. */
  .cfl-modal code {
    background: var(--color-card);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 11px;
    color: var(--color-text);
  }
  .cfl-hint { color: var(--color-mid); }

  .cfl-files {
    background: rgba(255, 79, 160, 0.06);
    border: 1px solid rgba(255, 79, 160, 0.2);
    border-radius: 4px;
    padding: 10px 12px;
    margin: 12px 0;
  }
  .cfl-files-title {
    font-size: 11px;
    text-transform: uppercase;
    color: var(--color-pink);
    letter-spacing: 0.06em;
    margin-bottom: 6px;
  }
  .cfl-files ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .cfl-files li {
    padding: 4px 0;
    border-bottom: 1px solid var(--color-border);
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
  }
  .cfl-files li:last-child { border-bottom: none; }
  .cfl-files code {
    color: var(--color-text);
    font-size: 11px;
  }
  .cfl-empty { margin: 0; color: var(--color-mid); font-size: 11px; }
  .cfl-guidance {
    font-size: 10px;
    color: var(--color-mid);
    font-style: italic;
  }

  .cfl-error {
    padding: 8px 10px;
    background: rgba(255, 79, 160, 0.12);
    border: 1px solid rgba(255, 79, 160, 0.3);
    border-radius: 4px;
    color: var(--color-pink);
    font-size: 11px;
    margin: 10px 0;
  }
  .cfl-ok {
    padding: 8px 10px;
    background: rgba(0, 191, 166, 0.1);  /* --color-teal at 10% */
    border: 1px solid rgba(0, 191, 166, 0.3);
    border-radius: 4px;
    color: var(--color-teal);
    font-size: 11px;
    margin: 10px 0;
  }

  .cfl-stderr {
    margin: 12px 0;
    font-size: 11px;
    color: var(--color-mid);
  }
  .cfl-stderr summary {
    cursor: pointer;
    color: var(--color-mid);
    padding: 4px 0;
  }
  .cfl-stderr summary:hover { color: var(--color-text); }
  .cfl-stderr pre {
    background: var(--color-bg);
    padding: 8px;
    border-radius: 4px;
    font-size: 10px;
    color: var(--color-mid);
    overflow-x: auto;
    max-height: 140px;
    margin: 4px 0 0;
    border: 1px solid var(--color-border);
  }

  .cfl-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 14px;
  }
  .cfl-btn {
    background: var(--color-card);
    border: 1px solid var(--color-border);
    color: var(--color-text);
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    font-family: inherit;
  }
  .cfl-btn:hover:not(:disabled) { background: rgba(255, 255, 255, 0.08); border-color: rgba(255, 255, 255, 0.18); }
  .cfl-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .cfl-btn-warn {
    background: rgba(255, 79, 160, 0.18);
    border-color: rgba(255, 79, 160, 0.45);
    color: var(--color-text);
  }
  .cfl-btn-warn:hover:not(:disabled) {
    background: rgba(255, 79, 160, 0.3);
    border-color: var(--color-pink);
  }
  /* v0.2.51 Bug A: primary action for the resume path. Teal (success-y)
     to differentiate from the warning-pink abort button. */
  .cfl-btn-primary {
    background: rgba(0, 191, 166, 0.18);
    border-color: rgba(0, 191, 166, 0.5);
    color: var(--color-text);
  }
  .cfl-btn-primary:hover:not(:disabled) {
    background: rgba(0, 191, 166, 0.32);
    border-color: var(--color-teal, #00bfa6);
  }

  /* v0.2.51 Bug A: "tree is clean — ready to continue" hint shown above
     the actions row once polling detects the resume sentinel. */
  .cfl-ready {
    padding: 8px 10px;
    background: rgba(0, 191, 166, 0.1);
    border: 1px solid rgba(0, 191, 166, 0.3);
    border-radius: 4px;
    color: var(--color-teal, #00bfa6);
    font-size: 11px;
    margin: 10px 0;
  }

  /* v0.2.52 V52-B: bullet list explaining the three primary options.
     Tighter spacing than the previous ordered list since each item is
     also documented in its button's tooltip. */
  .cfl-options-list {
    margin: 0 0 14px;
    padding-left: 20px;
    color: var(--color-mid);
    font-size: 12px;
    line-height: 1.6;
  }
  .cfl-options-list li {
    margin-bottom: 4px;
  }

  /* v0.2.52 V52-B: smart-default notice — purple-tinted (VCO accent) to
     differentiate from the conflict-pink and teal-success colour cues.
     Shown when EVERY conflicting file is under knowledge/, hinting the
     user toward "Keep local". */
  .cfl-smart-default {
    padding: 8px 10px;
    background: rgba(123, 95, 255, 0.1);      /* --color-purple at 10% */
    border: 1px solid rgba(123, 95, 255, 0.3);
    border-radius: 4px;
    color: var(--color-text);
    font-size: 11px;
    line-height: 1.5;
    margin: 10px 0 14px;
  }
  .cfl-smart-default strong {
    color: var(--color-purple, #7b5fff);
  }
  .cfl-smart-default code {
    background: rgba(123, 95, 255, 0.18);
    border-color: rgba(123, 95, 255, 0.3);
  }

  /* v0.2.52 V52-B: pulse-style border on the recommended button when
     the smart-default kicks in. Subtle — doesn't override the user's
     ability to pick a different button. */
  .cfl-btn-recommended {
    box-shadow: 0 0 0 1px rgba(123, 95, 255, 0.55);
    border-color: rgba(123, 95, 255, 0.7);
  }
  .cfl-btn-recommended:hover:not(:disabled) {
    box-shadow: 0 0 0 1px rgba(123, 95, 255, 0.85);
  }

  /* v0.2.52 V52-A: inline two-step confirmation block surfaced when the
     user tries to close the modal via Escape / X / backdrop click. The
     pink accent matches conflict severity — closing destroys the
     merge. */
  .cfl-confirm-dismiss {
    padding: 10px 12px;
    background: rgba(255, 79, 160, 0.12);
    border: 1px solid rgba(255, 79, 160, 0.4);
    border-radius: 4px;
    margin: 10px 0;
    color: var(--color-text);
    font-size: 12px;
  }
  .cfl-confirm-dismiss p {
    margin: 0 0 8px;
    color: var(--color-text);
    font-size: 12px;
  }
  .cfl-confirm-actions {
    display: flex;
    gap: 6px;
    justify-content: flex-end;
  }
  .cfl-btn-small {
    padding: 4px 10px;
    font-size: 11px;
  }

  /* v0.2.52: secondary row holding the "Already resolved via CLI"
     fallback Continue Update button. Right-aligned + lighter weight so
     it reads as advanced/optional. */
  .cfl-resume-row {
    display: flex;
    justify-content: flex-end;
    margin-top: 8px;
  }
  .cfl-btn-link {
    background: transparent;
    border: 1px solid transparent;
    color: var(--color-teal, #00bfa6);
    text-decoration: underline;
    font-size: 11px;
    padding: 4px 8px;
  }
  .cfl-btn-link:hover:not(:disabled) {
    background: rgba(0, 191, 166, 0.08);
    border-color: rgba(0, 191, 166, 0.2);
  }
</style>
