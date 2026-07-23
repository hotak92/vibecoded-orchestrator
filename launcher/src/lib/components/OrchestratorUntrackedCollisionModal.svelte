<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  // v0.2.88 (DEFECT 1 / FIELD DEFECT): untracked-collision "Resolve & retry"
  // modal. Surfaced when `update_orchestrator`'s inline pull aborts because a
  // local UNTRACKED file sits at a path this release ADDS ("untracked working
  // tree files would be overwritten by merge"). Pre-fix, this routed to the
  // generic conflict modal with an EMPTY file list — a dead-end. Now the parsed
  // collision set is shown, split into byte-identical (safe delete) + divergent
  // (backed up before delete), and one button resolves them all + retries.
  //
  // Backend command: `resolve_untracked_collision_and_retry(path, files)`.
  //   - byte-identical files → deleted (content is exactly what upstream ships)
  //   - divergent files → copied to .claude/state/update-collision-backups-<ts>/
  //     then deleted, so nothing is lost.
  // then re-enters update_orchestrator (which now proceeds past the collision).

  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { OrchestratorUntrackedCollisionResolvablePayload } from '$lib/stores/updater';

  let {
    payload,
    installPath,
    onClose,
  }: {
    payload: OrchestratorUntrackedCollisionResolvablePayload;
    installPath: string;
    onClose: () => void;
  } = $props();

  let resolving = $state(false);
  let resolved = $state(false);
  let error = $state<string | null>(null);

  const identical = $derived(payload.identical_files ?? []);
  const divergent = $derived(payload.divergent_files ?? []);
  const allFiles = $derived([...identical, ...divergent]);
  // v0.2.88 (NIT-11): only the enriched POST-pull variant carries
  // `resolvable: true` and the identical/divergent split the "Resolve & retry"
  // command needs. The v0.2.78 pre-pull leave-alone variant omits `resolvable`
  // — for that shape the modal degrades to an informational view (no live
  // button) instead of offering a resolve it can't safely perform. Today only
  // the resolvable variant reaches this modal (the pre-pull variant is parsed
  // inside OrchestratorUpdateDivergenceModal), but this guard keeps the claim
  // honest and the button latent-safe.
  const canResolve = $derived(payload.resolvable === true && allFiles.length > 0);

  async function resolveAndRetry() {
    if (resolving || resolved) return;
    resolving = true;
    error = null;
    try {
      // The backend auto-restarts on a successful retry, so we usually don't
      // return here. The `resolved` flag covers the crash-recovery path.
      await invoke<unknown>('resolve_untracked_collision_and_retry', {
        path: installPath,
        files: allFiles,
      });
      resolved = true;
      toast.success(
        `Resolved ${allFiles.length} colliding file(s) — the update is retrying.`
      );
      setTimeout(onClose, 600);
    } catch (e) {
      error = `Resolve & retry failed: ${e}`;
    } finally {
      resolving = false;
    }
  }

  function cancel() {
    if (resolving) return;
    onClose();
  }
</script>

<div class="utc-backdrop" role="presentation" onclick={cancel}>
  <div
    class="utc-modal"
    role="dialog"
    aria-modal="true"
    aria-labelledby="utc-r-title"
    tabindex="-1"
    onclick={(e) => e.stopPropagation()}
    onkeydown={(e) => e.stopPropagation()}
  >
    <h3 id="utc-r-title">Local files clash with new files in this update</h3>
    <p>
      {allFiles.length} untracked file{allFiles.length === 1 ? '' : 's'} in your
      clone {allFiles.length === 1 ? 'sits' : 'sit'} at a path this update adds,
      so git refused to overwrite {allFiles.length === 1 ? 'it' : 'them'} and
      paused the update.
    </p>

    {#if identical.length > 0}
      <div class="utc-group">
        <div class="utc-group-title utc-safe">
          {identical.length} identical to the update — safe to remove
        </div>
        <ul>
          {#each identical as f (f)}
            <li><code>{f}</code></li>
          {/each}
        </ul>
        <p class="utc-note">
          These are byte-for-byte what the update ships, so removing your copy
          loses nothing.
        </p>
      </div>
    {/if}

    {#if divergent.length > 0}
      <div class="utc-group">
        <div class="utc-group-title utc-warn">
          {divergent.length} differ{divergent.length === 1 ? 's' : ''} from the
          update — backed up first
        </div>
        <ul>
          {#each divergent as f (f)}
            <li><code>{f}</code></li>
          {/each}
        </ul>
        <p class="utc-note">
          Your version is copied to
          <code>.claude/state/update-collision-backups-…</code> before it's
          replaced, so you can recover it.
        </p>
      </div>
    {/if}

    <p class="utc-hint">
      Prefer to handle it yourself? Your Claude agent can resolve this — the
      update wrote an entry to
      <code>.claude/context/UPDATE_DEFERRED.md</code> describing exactly what to
      do.
    </p>

    {#if !canResolve && allFiles.length > 0}
      <p class="utc-hint">
        This view is informational — resolve the file(s) above via the steps in
        <code>.claude/context/UPDATE_DEFERRED.md</code>, then re-run the update.
      </p>
    {/if}

    {#if error}
      <div class="utc-error">{error}</div>
    {/if}

    <div class="utc-actions">
      <button class="utc-btn" disabled={resolving} onclick={cancel}>
        Close
      </button>
      {#if canResolve}
        <button
          class="utc-btn utc-btn-primary"
          disabled={resolving || resolved}
          onclick={resolveAndRetry}
          title="Remove the identical files, back up + replace the differing ones, then retry the update."
        >
          {resolving ? 'Resolving…' : resolved ? 'Done' : 'Resolve & retry'}
        </button>
      {/if}
    </div>
  </div>
</div>

<style>
  .utc-backdrop {
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
  .utc-modal {
    background: var(--color-bg2);
    border: 1px solid rgba(123, 95, 255, 0.4);
    border-radius: var(--radius-card, 12px);
    padding: 20px;
    max-width: 640px;
    width: 92%;
    margin-top: clamp(0px, 6vh, 64px);
    color: var(--color-text);
  }
  .utc-modal h3 {
    margin: 0 0 12px;
    font-size: 14px;
    color: var(--color-purple, #7b5fff);
  }
  .utc-modal p {
    font-size: 12px;
    line-height: 1.6;
    color: var(--color-mid);
    margin: 0 0 10px;
  }
  .utc-modal code {
    background: var(--color-card);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 11px;
    color: var(--color-text);
  }
  .utc-group {
    background: rgba(123, 95, 255, 0.06);
    border: 1px solid rgba(123, 95, 255, 0.2);
    border-radius: 4px;
    padding: 10px 12px;
    margin: 12px 0;
  }
  .utc-group-title {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 6px;
  }
  .utc-safe {
    color: var(--color-teal, #00bfa6);
  }
  .utc-warn {
    color: var(--color-pink, #ff4fa0);
  }
  .utc-group ul {
    list-style: none;
    padding: 0;
    margin: 0 0 6px;
  }
  .utc-group li {
    padding: 3px 0;
    font-size: 11px;
  }
  .utc-note {
    font-size: 10px;
    color: var(--color-mid);
    font-style: italic;
    margin: 0;
  }
  .utc-hint {
    color: var(--color-mid);
    font-size: 11px;
  }
  .utc-error {
    padding: 8px 10px;
    background: rgba(255, 79, 160, 0.12);
    border: 1px solid rgba(255, 79, 160, 0.3);
    border-radius: 4px;
    color: var(--color-pink);
    font-size: 11px;
    margin: 10px 0;
  }
  .utc-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 14px;
  }
  .utc-btn {
    background: var(--color-card);
    border: 1px solid var(--color-border);
    color: var(--color-text);
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    font-family: inherit;
  }
  .utc-btn:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.08);
    border-color: rgba(255, 255, 255, 0.18);
  }
  .utc-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .utc-btn-primary {
    background: rgba(0, 191, 166, 0.18);
    border-color: rgba(0, 191, 166, 0.5);
    color: var(--color-text);
  }
  .utc-btn-primary:hover:not(:disabled) {
    background: rgba(0, 191, 166, 0.32);
    border-color: var(--color-teal, #00bfa6);
  }
</style>
