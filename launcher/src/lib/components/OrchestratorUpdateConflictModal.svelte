<script lang="ts">
  // v0.2.23 (B4 / D19): orchestrator-update merge/rebase conflict modal.
  //
  // Surfaced when `merge_orchestrator_with_upstream` or
  // `rebase_orchestrator_onto_upstream` return a structured error with
  // `event: "orchestrator_update_conflict"` — the merge/rebase started
  // but produced unresolved conflicts in one or more files.
  //
  // The working tree is left in the conflicted state on purpose so the
  // user can:
  //   1. Resolve in their editor (CLAUDE.md, knowledge/*.md typically
  //      "keep local + manually merge upstream additions").
  //   2. Or click "Abort & restore" to bail cleanly via
  //      `abort_orchestrator_merge_or_rebase` (runs `git merge --abort`
  //      or `git rebase --abort` as appropriate).
  //
  // We do NOT auto-resolve. The user has legitimate local changes
  // (KG nodes, CLAUDE.md tweaks) that an "ours" / "theirs" auto-pick
  // would silently destroy.

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

  const operationLabel = $derived(
    payload.operation === 'merge' ? 'Merge' : 'Rebase'
  );

  // Pre-computed tooltip strings so we don't fight Svelte's parser
  // when interpolating `{payload.operation}` adjacent to literal
  // characters like `--abort` in attribute values.
  const abortBtnTitle = $derived(
    `Run \`git ${payload.operation} --abort\` to restore the working tree to its pre-${payload.operation} state.`
  );
  const dismissBtnTitle = $derived(
    'Close this dialog without aborting. Your working tree stays conflicted — resolve manually in your editor.'
  );

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

  function dismiss() {
    if (aborting) return;
    // Closing without aborting leaves the working tree conflicted —
    // surface a hint so the user knows what state they're in.
    onClose();
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
      You have two options:
    </p>
    <ol class="cfl-hint">
      <li>
        Open the files below in your editor, resolve the conflicts manually,
        then run <code>git add &lt;file&gt;</code> +
        <code>git {payload.operation} --continue</code> in the orchestrator
        directory (<code>{installPath}</code>).
      </li>
      <li>
        Click <strong>Abort &amp; restore</strong> to bail out. This runs
        <code>git {payload.operation} --abort</code> and brings your working
        tree back to the state it was in before the {payload.operation}
        started.
      </li>
    </ol>

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

    <details class="cfl-stderr">
      <summary>Show raw git output</summary>
      <pre>{payload.git_stderr}</pre>
    </details>

    <div class="cfl-actions">
      <button
        class="cfl-btn"
        disabled={aborting}
        onclick={dismiss}
        title={dismissBtnTitle}
      >
        Resolve manually
      </button>
      <button
        class="cfl-btn cfl-btn-warn"
        disabled={aborting || aborted}
        onclick={abort}
        title={abortBtnTitle}
      >
        {aborting ? 'Aborting…' : 'Abort & restore'}
      </button>
    </div>
  </div>
</div>

<style>
  /* v0.2.24.1 cosmetic pass: VCT color tokens (matches divergence modal). */
  .cfl-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(5, 11, 31, 0.82); /* --color-bg at 82% */
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 350;
  }
  .cfl-modal {
    background: var(--color-bg2);
    border: 1px solid rgba(255, 79, 160, 0.4); /* keep pink accent for conflict severity */
    border-radius: var(--radius-card, 12px);
    padding: 20px;
    max-width: 640px;
    width: 92%;
    max-height: 90vh;
    overflow-y: auto;
    color: var(--color-text);
  }
  .cfl-modal h3 {
    margin: 0 0 12px;
    font-size: 14px;
    color: var(--color-pink);
  }
  .cfl-modal p, .cfl-modal ol, .cfl-modal li {
    font-size: 12px;
    line-height: 1.6;
    color: var(--color-mid);
  }
  .cfl-modal p { margin: 0 0 10px; }
  .cfl-modal ol { margin: 0 0 14px; padding-left: 20px; }
  .cfl-modal ol li { margin-bottom: 6px; }
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
</style>
