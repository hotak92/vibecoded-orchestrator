<script lang="ts">
  // v0.2.27: orchestrator-update divergence modal — rewrite.
  //
  // Surfaced when `update_orchestrator` returns a structured error with
  // `event: "orchestrator_update_non_ff"` — the user's local clone has
  // diverged from upstream and `git pull --ff-only` failed.
  //
  // Design goals for this rewrite (vs the v0.2.23 original):
  //
  // 1. Separate two file categories the user is told about, so the count
  //    and the rendering match user-intuition:
  //      - "Files only on your clone" (local_only_files): paths that the
  //        local clone tracks but upstream doesn't — typical user fork
  //        additions like `other_projects_knowledge/*`. These are NOT
  //        merge blockers. Collapsed by default, count badged.
  //      - "Files where both sides diverge" (diverged_files): paths
  //        where the user AND upstream have committed changes — actual
  //        merge work. First 5 shown inline; the rest behind a
  //        collapsed <details>.
  //    If either list is empty, that section is hidden entirely.
  //
  // 2. Git stderr lives in its own collapsible block so it never bleeds
  //    into the file list as a misleading "filename" (see screenshots:
  //    "Merge with strategy ort failed." was being rendered as a path).
  //
  // 3. Retry state: if Merge fails, the same "primary recommended"
  //    button shouldn't keep telling the user to retry the failing
  //    action. We track attempt history and demote the failed action
  //    while promoting the alternative. After both fail we surface the
  //    manual-recovery affordance more prominently.
  //
  // 4. Sticky bottom action row: when the file <details> blocks expand,
  //    the action buttons stay visible above the fold (`position:
  //    sticky` inside the scrollable modal body).
  //
  // 5. Accessibility: backdrop is a real <button>, modal has `role=
  //    "dialog"` + `aria-modal` + `aria-labelledby`, Escape closes,
  //    focus is parked on a sensible primary action on mount.

  import { invoke } from '$lib/tauri';
  import { orchestrator } from '$lib/stores/orchestrator';
  import type { OrchestratorNonFfPayload } from '$lib/stores/updater';
  import OrchestratorUpdateConflictModal from './OrchestratorUpdateConflictModal.svelte';

  // Conflict payload (re-declared here for self-containment; the
  // conflict modal also declares its own copy).
  type OrchestratorConflictPayload = {
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
    payload: OrchestratorNonFfPayload;
    installPath: string;
    onClose: () => void;
  } = $props();

  let busy = $state(false);
  let busyOp = $state<'merge' | 'rebase' | null>(null);
  // v0.2.27: retry state. Tracks which operations the user has tried
  // AND seen fail in this modal session. Drives button priority.
  let mergeFailed = $state(false);
  let rebaseFailed = $state(false);
  let lastError = $state<string | null>(null);
  let conflict = $state<OrchestratorConflictPayload | null>(null);
  let copyHint = $state<string | null>(null);
  // Ref to each action button so we can focus the "currently primary"
  // one. Svelte 5 doesn't allow conditional bind:this, so we bind each
  // separately and choose at focus time.
  let mergeBtnEl = $state<HTMLButtonElement | null>(null);
  let rebaseBtnEl = $state<HTMLButtonElement | null>(null);

  // Derived view-model. Falls back to treating the whole `diverged_files`
  // list as truly-diverging when the Rust side hasn't yet split it.
  const localOnlyFiles = $derived(payload.local_only_files ?? []);
  const divergingFiles = $derived(payload.diverged_files ?? []);
  const divergingPreview = $derived(divergingFiles.slice(0, 5));
  const divergingRest = $derived(divergingFiles.slice(5));
  const stderrTrimmed = $derived((payload.git_stderr ?? '').trim());
  const stderrSummary = $derived(extractStderrSummary(stderrTrimmed));

  // Which action is "primary" right now? Defaults to merge; if merge
  // has failed once, rebase becomes primary; if both have failed, both
  // are demoted to secondary and we elevate the manual-recovery link.
  const mergeIsPrimary = $derived(!mergeFailed && !(rebaseFailed && mergeFailed));
  const rebaseIsPrimary = $derived(mergeFailed && !rebaseFailed);
  const bothFailed = $derived(mergeFailed && rebaseFailed);

  function shortSha(sha: string | null): string {
    return sha ? sha.slice(0, 7) : '—';
  }

  /**
   * Extract the most informative single-line summary from a git stderr
   * blob. We prefer lines starting with `error:`, `fatal:`, or
   * `CONFLICT`; fallback is the last non-empty line.
   */
  function extractStderrSummary(raw: string): string | null {
    if (!raw) return null;
    const lines = raw.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    if (lines.length === 0) return null;
    const priority = lines.find((l) => /^(error:|fatal:|CONFLICT)/i.test(l));
    return priority ?? lines[lines.length - 1];
  }

  /**
   * Try to parse a Tauri error as a conflict payload. The merge/rebase
   * commands return JSON-encoded errors when they hit conflicts. Any
   * other shape stays a raw error string.
   */
  function parseConflictError(raw: unknown): OrchestratorConflictPayload | null {
    if (typeof raw !== 'string') return null;
    if (!raw.startsWith('{')) return null;
    try {
      const parsed = JSON.parse(raw);
      if (parsed && parsed.event === 'orchestrator_update_conflict') {
        return parsed as OrchestratorConflictPayload;
      }
    } catch {
      // not JSON
    }
    return null;
  }

  async function runMerge() {
    busy = true;
    busyOp = 'merge';
    lastError = null;
    try {
      // The Rust command auto-restarts on success — we typically don't
      // return here. If we DO, refresh the store state.
      await invoke<void>('merge_orchestrator_with_upstream', { path: installPath });
      await orchestrator.checkStatus();
      onClose();
    } catch (e) {
      const conf = parseConflictError(e);
      if (conf) {
        conflict = conf;
      } else {
        lastError = `Merge failed: ${e}`;
        mergeFailed = true;
      }
    } finally {
      busy = false;
      busyOp = null;
    }
  }

  async function runRebase() {
    busy = true;
    busyOp = 'rebase';
    lastError = null;
    try {
      await invoke<void>('rebase_orchestrator_onto_upstream', { path: installPath });
      await orchestrator.checkStatus();
      onClose();
    } catch (e) {
      const conf = parseConflictError(e);
      if (conf) {
        conflict = conf;
      } else {
        lastError = `Rebase failed: ${e}`;
        rebaseFailed = true;
      }
    } finally {
      busy = false;
      busyOp = null;
    }
  }

  /**
   * Open the clone folder in the OS file manager (best-effort; no
   * dedicated terminal-open Tauri command exists today). If that fails,
   * fall back to copying the path to the clipboard so the user can
   * paste it into a terminal manually.
   */
  async function openClone() {
    if (busy) return;
    copyHint = null;
    try {
      const { openPath } = await import('@tauri-apps/plugin-opener');
      await openPath(installPath);
      copyHint = 'Opened in file manager.';
    } catch (err) {
      console.error('[divergence-modal] openPath failed:', err);
      try {
        if (typeof navigator !== 'undefined' && navigator.clipboard) {
          await navigator.clipboard.writeText(installPath);
          copyHint = 'Path copied to clipboard.';
        } else {
          copyHint = `Clone path: ${installPath}`;
        }
      } catch {
        copyHint = `Clone path: ${installPath}`;
      }
    }
  }

  function cancel() {
    if (busy) return;
    onClose();
  }

  function dismissConflict() {
    conflict = null;
    // Conflict modal handled its own abort/manual flow; closing the
    // parent divergence modal too keeps the user out of an
    // indeterminate state.
    onClose();
  }

  function onBackdropKey(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      cancel();
    }
  }

  function onModalKey(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      cancel();
    }
  }

  // Focus the recommended action when the modal mounts (or when the
  // recommendation changes after a failed attempt).
  $effect(() => {
    // Read both reactive flags so this re-fires when either flips.
    const target = rebaseIsPrimary ? rebaseBtnEl : mergeBtnEl;
    if (target && !busy) {
      // Defer to next microtask so Svelte has finished patching the
      // disabled attribute.
      queueMicrotask(() => target.focus());
    }
  });
</script>

{#if conflict}
  <OrchestratorUpdateConflictModal
    payload={conflict}
    installPath={installPath}
    onClose={dismissConflict}
  />
{:else}
  <!-- Backdrop is a real button so click+keyboard dismissal are both
       accessible. The modal itself stops propagation so clicks inside
       don't bubble up to the backdrop. -->
  <button
    type="button"
    class="dvg-backdrop"
    aria-label="Close dialog"
    onclick={cancel}
    onkeydown={onBackdropKey}
  ></button>
  <div
    class="dvg-modal"
    role="dialog"
    aria-modal="true"
    aria-labelledby="dvg-title"
    tabindex="-1"
    onkeydown={onModalKey}
  >
    <header class="dvg-header">
      <h3 id="dvg-title">Local clone has diverged from upstream</h3>
      <p class="dvg-summary">
        Your clone has changes upstream doesn't, and upstream has changes you
        don't — so a fast-forward pull isn't possible. Pick how to combine
        them, or open the clone and resolve it by hand.
      </p>
    </header>

    <div class="dvg-body">
      <dl class="dvg-meta">
        <dt>Branch</dt>
        <dd><code>{payload.branch}</code></dd>
        <dt>Local</dt>
        <dd><code>{shortSha(payload.local_sha)}</code></dd>
        <dt>Upstream</dt>
        <dd><code>{shortSha(payload.remote_sha)}</code></dd>
      </dl>

      {#if divergingFiles.length > 0}
        <section class="dvg-section" aria-labelledby="dvg-diverging-title">
          <div class="dvg-section-head">
            <span id="dvg-diverging-title" class="dvg-section-title">
              Files where both sides have diverging history
            </span>
            <span class="dvg-badge dvg-badge-warn">{divergingFiles.length}</span>
          </div>
          <p class="dvg-section-help">
            These are the merge-blocker candidates — both your clone and upstream
            modified them.
          </p>
          {#if divergingPreview.length > 0}
            <ul class="dvg-files-preview">
              {#each divergingPreview as f (f)}
                <li><code>{f}</code></li>
              {/each}
            </ul>
          {/if}
          {#if divergingRest.length > 0}
            <details class="dvg-details">
              <summary>Show {divergingRest.length} more</summary>
              <ul class="dvg-files-list">
                {#each divergingRest as f (f)}
                  <li><code>{f}</code></li>
                {/each}
              </ul>
            </details>
          {/if}
        </section>
      {/if}

      {#if localOnlyFiles.length > 0}
        <section class="dvg-section" aria-labelledby="dvg-local-title">
          <div class="dvg-section-head">
            <span id="dvg-local-title" class="dvg-section-title">
              Files only on your clone
            </span>
            <span class="dvg-badge">{localOnlyFiles.length}</span>
          </div>
          <p class="dvg-section-help">
            Paths that exist locally but not on upstream — typically additions
            from your fork. Not merge blockers; merge and rebase preserve them.
          </p>
          <details class="dvg-details">
            <summary>Show files</summary>
            <ul class="dvg-files-list">
              {#each localOnlyFiles as f (f)}
                <li><code>{f}</code></li>
              {/each}
            </ul>
          </details>
        </section>
      {/if}

      {#if stderrTrimmed}
        <section class="dvg-section" aria-labelledby="dvg-stderr-title">
          <div class="dvg-section-head">
            <span id="dvg-stderr-title" class="dvg-section-title">
              Git error output (raw)
            </span>
          </div>
          <details class="dvg-details">
            <summary>Show git stderr</summary>
            <pre class="dvg-stderr">{stderrTrimmed}</pre>
          </details>
        </section>
      {/if}
    </div>

    <footer class="dvg-footer">
      {#if lastError}
        <div class="dvg-error" role="alert">
          {#if stderrSummary}
            <strong>{lastError}</strong>
            <span class="dvg-error-detail">{stderrSummary}</span>
          {:else}
            {lastError}
          {/if}
        </div>
      {/if}

      {#if bothFailed}
        <div class="dvg-manual-prompt" role="status">
          Both Merge and Rebase have failed. Recommended:
          <button
            type="button"
            class="dvg-link dvg-link-strong"
            disabled={busy}
            onclick={openClone}
          >
            open the clone folder
          </button>
          and resolve manually in a terminal.
        </div>
      {/if}

      <div class="dvg-actions">
        <button
          type="button"
          class="dvg-btn"
          disabled={busy}
          onclick={cancel}
          title="Dismiss this dialog. You can resolve manually with `git pull` or `git rebase` in a terminal."
        >
          Cancel
        </button>

        <button
          type="button"
          class="dvg-btn dvg-btn-with-sub"
          class:dvg-btn-primary={rebaseIsPrimary}
          disabled={busy}
          onclick={runRebase}
          bind:this={rebaseBtnEl}
          title="Replay your local commits on top of upstream. Conflicts surface per-commit."
        >
          <span class="dvg-btn-label">
            {#if busyOp === 'rebase'}
              Rebasing…
            {:else if rebaseFailed}
              Try Rebase again
            {:else if mergeFailed}
              Try Rebase instead
            {:else}
              Rebase onto upstream
            {/if}
          </span>
          <span class="dvg-btn-sub">
            Replays your local commits on top of upstream — requires clean working tree
          </span>
        </button>

        <button
          type="button"
          class="dvg-btn dvg-btn-with-sub"
          class:dvg-btn-primary={mergeIsPrimary}
          disabled={busy}
          onclick={runMerge}
          bind:this={mergeBtnEl}
          title="Creates a merge commit. Safest for most users — preserves local history as-is."
        >
          <span class="dvg-btn-label">
            {#if busyOp === 'merge'}
              Merging…
            {:else if mergeFailed}
              Try Merge again
            {:else if mergeIsPrimary}
              Merge upstream changes (recommended)
            {:else}
              Merge upstream changes
            {/if}
          </span>
          <span class="dvg-btn-sub">
            Auto-merges where possible; preserves your local changes on conflict
          </span>
        </button>
      </div>

      {#if !bothFailed}
        <div class="dvg-manual-link">
          <button
            type="button"
            class="dvg-link"
            disabled={busy}
            onclick={openClone}
          >
            Open clone folder
          </button>
          {#if copyHint}
            <span class="dvg-copy-hint" role="status">{copyHint}</span>
          {/if}
        </div>
      {:else if copyHint}
        <div class="dvg-manual-link">
          <span class="dvg-copy-hint" role="status">{copyHint}</span>
        </div>
      {/if}
    </footer>
  </div>
{/if}

<style>
  /* v0.2.27 rewrite: sticky bottom action row + separated file
   * categories + own stderr block + retry-aware button states.
   * v0.2.51 fix: anchor modal to viewport top with safe margin so the
   * header never clips above the window in short viewports. The body
   * still scrolls internally via .dvg-body's overflow-y: auto.
   * All colors resolve to --color-* tokens defined in app.css. */
  .dvg-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(5, 11, 31, 0.72); /* var(--color-bg) at 72% */
    display: block;
    border: 0;
    padding: 0;
    margin: 0;
    cursor: default;
    z-index: 300;
  }
  .dvg-backdrop:focus-visible {
    outline: none;
  }

  .dvg-modal {
    position: fixed;
    top: 0;
    left: 50%;
    transform: translateX(-50%);
    /* Moderate top offset, scales with viewport but never less than
     * 16px and never more than 80px — keeps header well below the
     * window chrome on tall windows, and flush near the top with a
     * safe margin on short ones. */
    margin-top: clamp(16px, 8vh, 80px);
    /* Never exceed viewport minus the top offset + a bottom safe
     * margin (16px). When the modal is shorter than this, it stays at
     * its natural height; when it's taller, internal body scrolls. */
    max-height: calc(100vh - clamp(32px, 8vh, 96px) - 16px);
    background: var(--color-bg2);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-card, 12px);
    width: min(640px, 92vw);
    display: flex;
    flex-direction: column;
    color: var(--color-text);
    z-index: 301;
    outline: none;
  }

  .dvg-header {
    padding: 20px 20px 8px;
    border-bottom: 1px solid var(--color-border);
  }
  .dvg-header h3 {
    margin: 0 0 8px;
    font-size: 14px;
    color: var(--color-teal);
  }
  .dvg-summary {
    font-size: 12px;
    line-height: 1.55;
    margin: 0 0 12px;
    color: var(--color-mid);
  }

  .dvg-body {
    padding: 12px 20px 4px;
    overflow-y: auto;
    flex: 1 1 auto;
    min-height: 0;
  }

  code {
    background: var(--color-card);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 11px;
    color: var(--color-text);
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  }

  .dvg-meta {
    display: grid;
    grid-template-columns: 92px 1fr;
    gap: 4px 16px;
    font-size: 12px;
    margin: 0 0 16px;
  }
  .dvg-meta dt { color: var(--color-mid); }
  .dvg-meta dd { margin: 0; color: var(--color-text); }

  .dvg-section {
    margin: 0 0 14px;
    padding: 10px 12px;
    background: var(--color-card);
    border: 1px solid var(--color-border);
    border-radius: 8px;
  }
  .dvg-section-head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
  }
  .dvg-section-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--color-text);
  }
  .dvg-section-help {
    margin: 0 0 8px;
    font-size: 11px;
    line-height: 1.5;
    color: var(--color-mid);
  }
  .dvg-badge {
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 10px;
    background: var(--color-bg);
    color: var(--color-mid);
    border: 1px solid var(--color-border);
    line-height: 1.5;
  }
  .dvg-badge-warn {
    background: rgba(255, 79, 160, 0.14); /* --color-pink at 14% */
    color: var(--color-pink);
    border-color: rgba(255, 79, 160, 0.35);
  }

  .dvg-files-preview {
    list-style: none;
    margin: 0 0 6px;
    padding: 0;
  }
  .dvg-files-preview li {
    padding: 2px 0;
    font-size: 11px;
  }

  .dvg-details {
    margin-top: 4px;
    font-size: 11px;
  }
  .dvg-details summary {
    cursor: pointer;
    color: var(--color-mid);
    padding: 4px 0;
    user-select: none;
  }
  .dvg-details summary:hover { color: var(--color-text); }
  .dvg-details summary:focus-visible {
    outline: 1px solid var(--color-teal);
    outline-offset: 2px;
    border-radius: 2px;
  }
  .dvg-files-list {
    list-style: none;
    padding: 4px 0 4px 12px;
    margin: 0;
    max-height: 200px;
    overflow-y: auto;
    border-left: 1px solid var(--color-border);
  }
  .dvg-files-list li {
    padding: 2px 0;
    color: var(--color-text);
  }

  .dvg-stderr {
    margin: 6px 0 0;
    padding: 8px 10px;
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 4px;
    font-size: 11px;
    line-height: 1.5;
    color: var(--color-text);
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    max-height: 180px;
    overflow: auto;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .dvg-footer {
    /* Sticky-bottom semantics: the footer is the bottom of the flex
     * container, not scrollable. Body scrolls behind it because body is
     * `flex: 1; overflow-y: auto`. Net effect: actions remain reachable
     * even when file lists are expanded. */
    padding: 10px 20px 16px;
    border-top: 1px solid var(--color-border);
    background: var(--color-bg2);
    border-bottom-left-radius: var(--radius-card, 12px);
    border-bottom-right-radius: var(--radius-card, 12px);
    flex-shrink: 0;
  }

  .dvg-error {
    margin: 0 0 10px;
    padding: 8px 10px;
    background: rgba(255, 79, 160, 0.1); /* --color-pink at 10% */
    border: 1px solid rgba(255, 79, 160, 0.3);
    border-radius: 4px;
    color: var(--color-pink);
    font-size: 11px;
    line-height: 1.5;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .dvg-error-detail {
    color: var(--color-text);
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 10.5px;
    word-break: break-word;
  }

  .dvg-manual-prompt {
    margin: 0 0 10px;
    padding: 8px 10px;
    background: rgba(0, 191, 166, 0.08); /* --color-teal at 8% */
    border: 1px solid rgba(0, 191, 166, 0.28);
    border-radius: 4px;
    color: var(--color-text);
    font-size: 11.5px;
    line-height: 1.55;
  }

  .dvg-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    flex-wrap: wrap;
    align-items: stretch;
  }
  .dvg-btn {
    background: var(--color-card);
    border: 1px solid var(--color-border);
    color: var(--color-text);
    padding: 8px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 12px;
    font-family: inherit;
    min-height: 36px;
    text-align: left;
  }
  .dvg-btn:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.08);
    border-color: rgba(255, 255, 255, 0.18);
  }
  .dvg-btn:focus-visible {
    outline: 2px solid var(--color-teal);
    outline-offset: 2px;
  }
  .dvg-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
  .dvg-btn-with-sub {
    display: flex;
    flex-direction: column;
    gap: 2px;
    align-items: flex-start;
    max-width: 280px;
  }
  .dvg-btn-label {
    font-weight: 600;
    font-size: 12px;
  }
  .dvg-btn-sub {
    font-size: 10.5px;
    color: var(--color-mid);
    line-height: 1.4;
    font-weight: 400;
  }
  .dvg-btn-primary {
    background: rgba(0, 191, 166, 0.2); /* --color-teal at 20% */
    border-color: rgba(0, 191, 166, 0.5);
    color: var(--color-text);
  }
  .dvg-btn-primary:hover:not(:disabled) {
    background: rgba(0, 191, 166, 0.32);
    border-color: var(--color-teal);
  }
  .dvg-btn-primary .dvg-btn-sub {
    color: var(--color-text);
    opacity: 0.85;
  }

  .dvg-manual-link {
    margin-top: 10px;
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }
  .dvg-link {
    background: transparent;
    border: none;
    padding: 4px 0;
    color: var(--color-teal);
    cursor: pointer;
    font: inherit;
    font-size: 11px;
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  .dvg-link:hover:not(:disabled) {
    color: var(--color-teal-hover, var(--color-teal));
  }
  .dvg-link:focus-visible {
    outline: 2px solid var(--color-teal);
    outline-offset: 2px;
    border-radius: 2px;
  }
  .dvg-link:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
  .dvg-link-strong {
    font-weight: 600;
    font-size: 11.5px;
  }
  .dvg-copy-hint {
    font-size: 11px;
    color: var(--color-mid);
  }
</style>
