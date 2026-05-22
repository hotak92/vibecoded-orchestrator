<script lang="ts">
  // v0.2.23 (B4 / D19): orchestrator-update divergence modal.
  //
  // Surfaced when `update_orchestrator` returns a structured error with
  // `event: "orchestrator_update_non_ff"` — the user's local clone has
  // diverged from upstream (typical: local edits to CLAUDE.md /
  // CONTEXT_STATE.md / knowledge/*.md) and `git pull --ff-only` failed.
  //
  // Offers three resolutions:
  //   - Merge (default, recommended): `git pull --no-rebase` → produces
  //     a merge commit when histories overlap cleanly, OR surfaces a
  //     conflict modal if files touch on both sides.
  //   - Rebase: `git rebase vco_upstream/<branch>` → replays local
  //     commits on top of upstream. Cleaner history, but conflicts
  //     surface per-commit instead of as a single merge state.
  //   - Cancel: dismiss; user resolves manually in a terminal.
  //
  // Why we don't auto-recover: the user has legitimate local changes
  // they want preserved. Hard-resetting to upstream (the launcher
  // self-update pattern) would wipe their CLAUDE.md tweaks, KG nodes,
  // and CONTEXT_STATE.md edits. The user has to choose the merge
  // strategy explicitly.

  import { invoke } from '$lib/tauri';
  import { orchestrator } from '$lib/stores/orchestrator';
  import OrchestratorUpdateConflictModal from './OrchestratorUpdateConflictModal.svelte';

  // Payload shape mirrors Rust `serialize_orchestrator_non_ff_error`.
  export type OrchestratorNonFfPayload = {
    event: 'orchestrator_update_non_ff';
    branch: string;
    local_sha: string | null;
    remote_sha: string | null;
    diverged_files: string[];
    git_stderr: string;
  };

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
  let error = $state<string | null>(null);
  let conflict = $state<OrchestratorConflictPayload | null>(null);

  function shortSha(sha: string | null): string {
    return sha ? sha.slice(0, 7) : '—';
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
    error = null;
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
        error = `Merge failed: ${e}`;
      }
    } finally {
      busy = false;
      busyOp = null;
    }
  }

  async function runRebase() {
    busy = true;
    busyOp = 'rebase';
    error = null;
    try {
      await invoke<void>('rebase_orchestrator_onto_upstream', { path: installPath });
      await orchestrator.checkStatus();
      onClose();
    } catch (e) {
      const conf = parseConflictError(e);
      if (conf) {
        conflict = conf;
      } else {
        error = `Rebase failed: ${e}`;
      }
    } finally {
      busy = false;
      busyOp = null;
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
</script>

{#if conflict}
  <OrchestratorUpdateConflictModal
    payload={conflict}
    installPath={installPath}
    onClose={dismissConflict}
  />
{:else}
  <div class="dvg-backdrop" onclick={cancel}>
    <div class="dvg-modal" onclick={(e) => e.stopPropagation()}>
      <h3>Local clone has diverged from upstream</h3>
      <p>
        Your local copy of the orchestrator has commits that aren't on upstream
        (typically: edits to <code>CLAUDE.md</code>, <code>CONTEXT_STATE.md</code>,
        or <code>knowledge/*.md</code>). A fast-forward pull is not possible —
        you need to choose how to combine your changes with the new ones.
      </p>

      <dl class="dvg-meta">
        <dt>Branch</dt>
        <dd><code>{payload.branch}</code></dd>
        <dt>Local</dt>
        <dd><code>{shortSha(payload.local_sha)}</code></dd>
        <dt>Upstream</dt>
        <dd><code>{shortSha(payload.remote_sha)}</code></dd>
      </dl>

      {#if payload.diverged_files.length > 0}
        <details class="dvg-files">
          <summary>
            {payload.diverged_files.length} file(s) differ between local and upstream
          </summary>
          <ul>
            {#each payload.diverged_files as f}
              <li><code>{f}</code></li>
            {/each}
          </ul>
        </details>
      {/if}

      {#if error}
        <div class="dvg-error">{error}</div>
      {/if}

      <div class="dvg-actions">
        <button
          class="dvg-btn"
          disabled={busy}
          onclick={cancel}
          title="Dismiss this dialog. You can resolve manually with `git pull` or `git rebase` in a terminal."
        >
          Cancel
        </button>
        <button
          class="dvg-btn"
          disabled={busy}
          onclick={runRebase}
          title="Replay your local commits on top of upstream. Produces a cleaner history but conflicts surface per-commit."
        >
          {busyOp === 'rebase' ? 'Rebasing…' : 'Rebase onto upstream'}
        </button>
        <button
          class="dvg-btn dvg-btn-primary"
          disabled={busy}
          onclick={runMerge}
          title="Creates a merge commit. Safest for most users — preserves local history as-is."
        >
          {busyOp === 'merge' ? 'Merging…' : 'Merge with upstream (recommended)'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  /* v0.2.24.1 cosmetic pass: VCT color tokens + max-height to fix the
   * header-truncation symptom (modal could overflow viewport when the
   * <details> diverged-files list was expanded). All static colors now
   * resolve to `--color-*` tokens defined in app.css. */
  .dvg-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(5, 11, 31, 0.72); /* var(--color-bg) at 72% */
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 300;
  }
  .dvg-modal {
    background: var(--color-bg2);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-card, 12px);
    padding: 20px;
    max-width: 560px;
    width: 92%;
    /* Fix header-truncation: cap modal height + scroll inside the modal
     * when the expanded diverged-files list pushes content past the
     * viewport. */
    max-height: 90vh;
    overflow-y: auto;
    color: var(--color-text);
  }
  .dvg-modal h3 { margin: 0 0 12px; font-size: 14px; color: var(--color-teal); }
  .dvg-modal p { font-size: 12px; line-height: 1.6; margin: 0 0 10px; color: var(--color-mid); }
  .dvg-modal code {
    background: var(--color-card);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 11px;
    color: var(--color-text);
  }

  .dvg-meta {
    display: grid;
    grid-template-columns: 100px 1fr;
    gap: 4px 16px;
    font-size: 12px;
    margin: 12px 0;
  }
  .dvg-meta dt { color: var(--color-mid); }
  .dvg-meta dd { margin: 0; color: var(--color-text); }

  .dvg-files {
    margin: 12px 0;
    font-size: 12px;
    color: var(--color-mid);
  }
  .dvg-files summary {
    cursor: pointer;
    color: var(--color-mid);
    padding: 4px 0;
  }
  .dvg-files summary:hover { color: var(--color-text); }
  .dvg-files ul {
    list-style: none;
    padding: 4px 0 4px 12px;
    margin: 0;
    max-height: 160px;
    overflow-y: auto;
    border-left: 1px solid var(--color-border);
  }
  .dvg-files li { padding: 2px 0; color: var(--color-text); }

  .dvg-error {
    margin: 12px 0;
    padding: 8px 10px;
    background: rgba(255, 79, 160, 0.1);  /* --color-pink at 10% */
    border: 1px solid rgba(255, 79, 160, 0.3);
    border-radius: 4px;
    color: var(--color-pink);
    font-size: 11px;
    line-height: 1.5;
  }

  .dvg-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 18px;
    flex-wrap: wrap;
  }
  .dvg-btn {
    background: var(--color-card);
    border: 1px solid var(--color-border);
    color: var(--color-text);
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    font-family: inherit;
  }
  .dvg-btn:hover:not(:disabled) { background: rgba(255, 255, 255, 0.08); border-color: rgba(255, 255, 255, 0.18); }
  .dvg-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .dvg-btn-primary {
    background: rgba(0, 191, 166, 0.2);  /* --color-teal at 20% */
    border-color: rgba(0, 191, 166, 0.5);
    color: var(--color-text);
  }
  .dvg-btn-primary:hover:not(:disabled) {
    background: rgba(0, 191, 166, 0.32);
    border-color: var(--color-teal);
  }
</style>
