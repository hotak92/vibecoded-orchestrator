<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  // v0.2.88 (DEFECT 2 / FIELD DEFECT): autostash-pop-conflict modal. The
  // orchestrator update MERGED SUCCESSFULLY, but restoring the user's local
  // uncommitted changes (git --autostash pop) conflicted — they edited a
  // tracked file this release also touched. This is NOT a merge failure (the
  // update's merge is done); labeling it as one was the field bug. The user's
  // changes are SAFE in the git stash.
  //
  // Per the whole conflicted set, the user picks one direction:
  //   * Keep the updated version (discard the stashed local change), or
  //   * Keep local (back up the updated version first, then restore local).
  // Backend: `resolve_autostash_pop_and_retry(path, files, keep_updated)` then
  // finishes the update (install.py + binary refresh + auto-restart).

  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { OrchestratorAutostashPopConflictPayload } from '$lib/stores/updater';

  let {
    payload,
    installPath,
    onClose,
  }: {
    payload: OrchestratorAutostashPopConflictPayload;
    installPath: string;
    onClose: () => void;
  } = $props();

  let resolving = $state(false);
  let resolved = $state(false);
  let mode = $state<'keep-updated' | 'keep-local' | null>(null);
  let error = $state<string | null>(null);

  async function resolve(keepUpdated: boolean) {
    if (resolving || resolved) return;
    resolving = true;
    mode = keepUpdated ? 'keep-updated' : 'keep-local';
    error = null;
    try {
      await invoke<unknown>('resolve_autostash_pop_and_retry', {
        path: installPath,
        files: payload.conflicted_files,
        keepUpdated,
      });
      resolved = true;
      toast.success(
        keepUpdated
          ? 'Kept the updated version — finishing the update.'
          : 'Kept your local version — finishing the update.'
      );
      setTimeout(onClose, 600);
    } catch (e) {
      error = `Resolve failed: ${e}`;
      mode = null;
    } finally {
      resolving = false;
    }
  }

  function cancel() {
    if (resolving) return;
    onClose();
  }
</script>

<div class="asp-backdrop" role="presentation" onclick={cancel}>
  <div
    class="asp-modal"
    role="dialog"
    aria-modal="true"
    aria-labelledby="asp-title"
    tabindex="-1"
    onclick={(e) => e.stopPropagation()}
    onkeydown={(e) => e.stopPropagation()}
  >
    <h3 id="asp-title">Update installed — restoring your local changes clashed</h3>
    <p>
      The update <strong>merged successfully</strong>. When it went to restore
      your uncommitted local changes, {payload.conflicted_files.length} file{payload
        .conflicted_files.length === 1
        ? ''
        : 's'} clashed because you had edited {payload.conflicted_files.length ===
      1
        ? 'it'
        : 'them'} too. Your changes are safe in the git stash — pick which
      version to keep.
    </p>

    <div class="asp-files">
      <div class="asp-files-title">
        {payload.conflicted_files.length} file(s):
      </div>
      <ul>
        {#each payload.conflicted_files as f (f)}
          <li><code>{f}</code></li>
        {/each}
      </ul>
    </div>

    <ul class="asp-options">
      <li>
        <strong>Keep the updated version</strong> — take the release's version
        for the whole set above, discarding your local edit. Your discarded edit
        is backed up to
        <code>.claude/state/update-collision-backups-…</code> first.
      </li>
      <li>
        <strong>Keep my local version</strong> — take your edit for the whole
        set; the updated version is backed up to
        <code>.claude/state/update-collision-backups-…</code> first.
      </li>
    </ul>
    <p class="asp-mixed-note">
      One choice applies to every file above. Want to keep some and discard
      others? Resolve those files in a terminal — the update wrote the exact
      steps to <code>.claude/context/UPDATE_DEFERRED.md</code>.
    </p>

    {#if error}
      <div class="asp-error">{error}</div>
    {/if}

    <details class="asp-stderr">
      <summary>Show raw git output</summary>
      <pre>{payload.git_stderr}</pre>
    </details>

    <div class="asp-actions">
      <button class="asp-btn" disabled={resolving} onclick={cancel}>Close</button>
      <button
        class="asp-btn asp-btn-primary"
        disabled={resolving || resolved}
        onclick={() => resolve(true)}
      >
        {resolving && mode === 'keep-updated'
          ? 'Keeping updated…'
          : 'Keep updated version'}
      </button>
      <button
        class="asp-btn asp-btn-primary"
        disabled={resolving || resolved}
        onclick={() => resolve(false)}
      >
        {resolving && mode === 'keep-local'
          ? 'Keeping local…'
          : 'Keep local (backup first)'}
      </button>
    </div>
  </div>
</div>

<style>
  .asp-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(5, 11, 31, 0.82);
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding: 16px;
    overflow-y: auto;
    z-index: 350;
  }
  .asp-modal {
    background: var(--color-bg2);
    border: 1px solid rgba(0, 191, 166, 0.4);
    border-radius: var(--radius-card, 12px);
    padding: 20px;
    max-width: 640px;
    width: 92%;
    margin-top: clamp(0px, 6vh, 64px);
    color: var(--color-text);
  }
  .asp-modal h3 {
    margin: 0 0 12px;
    font-size: 14px;
    color: var(--color-teal, #00bfa6);
  }
  .asp-modal p,
  .asp-modal li {
    font-size: 12px;
    line-height: 1.6;
    color: var(--color-mid);
  }
  .asp-modal p {
    margin: 0 0 10px;
  }
  .asp-modal code {
    background: var(--color-card);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 11px;
    color: var(--color-text);
  }
  .asp-files {
    background: rgba(0, 191, 166, 0.06);
    border: 1px solid rgba(0, 191, 166, 0.2);
    border-radius: 4px;
    padding: 10px 12px;
    margin: 12px 0;
  }
  .asp-files-title {
    font-size: 11px;
    text-transform: uppercase;
    color: var(--color-teal, #00bfa6);
    letter-spacing: 0.06em;
    margin-bottom: 6px;
  }
  .asp-files ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .asp-files li {
    padding: 3px 0;
    font-size: 11px;
  }
  .asp-options {
    margin: 0 0 12px;
    padding-left: 20px;
  }
  .asp-options li {
    margin-bottom: 4px;
  }
  .asp-mixed-note {
    font-size: 11px;
    color: var(--color-mid);
    font-style: italic;
    margin: 0 0 12px;
  }
  .asp-error {
    padding: 8px 10px;
    background: rgba(255, 79, 160, 0.12);
    border: 1px solid rgba(255, 79, 160, 0.3);
    border-radius: 4px;
    color: var(--color-pink);
    font-size: 11px;
    margin: 10px 0;
  }
  .asp-stderr {
    margin: 12px 0;
    font-size: 11px;
    color: var(--color-mid);
  }
  .asp-stderr summary {
    cursor: pointer;
    color: var(--color-mid);
    padding: 4px 0;
  }
  .asp-stderr pre {
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
  .asp-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 14px;
    flex-wrap: wrap;
  }
  .asp-btn {
    background: var(--color-card);
    border: 1px solid var(--color-border);
    color: var(--color-text);
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    font-family: inherit;
  }
  .asp-btn:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.08);
    border-color: rgba(255, 255, 255, 0.18);
  }
  .asp-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .asp-btn-primary {
    background: rgba(0, 191, 166, 0.18);
    border-color: rgba(0, 191, 166, 0.5);
    color: var(--color-text);
  }
  .asp-btn-primary:hover:not(:disabled) {
    background: rgba(0, 191, 166, 0.32);
    border-color: var(--color-teal, #00bfa6);
  }
</style>
