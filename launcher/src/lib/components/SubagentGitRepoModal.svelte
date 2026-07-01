<script lang="ts">
  // SubagentGitRepoModal — v0.2.71 Track T-WT.
  //
  // ── What this is ────────────────────────────────────────────────────
  // The closed-source Claude Code harness runs `git worktree add` to give
  // each `isolation: worktree` subagent its own checkout. That command needs
  // the workspace root to be inside a git repo. When the root is NOT inside
  // any repo (the nested-only case — e.g. `Code/python/` is repo-less but
  // `Code/python/<app>/` has its own `.git`), the harness fails the subagent
  // spawn. VCO cannot intercept that spawn, so its only levers are:
  //
  //   1. Use an existing ENCLOSING repo (root or a parent has `.git`) — the
  //      harness already walks up to it; isolation works with zero config.
  //      This is auto-detected + auto-recorded; the modal is NOT shown.
  //   2. Create a LOCAL-ONLY repo at the workspace root (`git init`, no
  //      remote, nested repos gitignored) so the harness's worktree add
  //      succeeds with the agents' frontmatter unchanged.
  //   3. Opt out ("No .git repo") — subagents run in the shared cwd, no
  //      isolation. The persisted mode is honoured by VCO's SubagentStart
  //      isolation-check (warns on a shared-tree spawn) + SubagentStop
  //      reconcile (flags shared-tree writes post-hoc) — the runtime backstops
  //      that ship this cycle. (No agent-frontmatter rewrite is performed.)
  //
  // ── When it shows ───────────────────────────────────────────────────
  // ONLY when the workspace root is genuinely not inside any repo (detection
  // via `git rev-parse --show-toplevel` failed). If an enclosing repo was
  // detected, the parent silently records `use_existing` and never mounts
  // this modal — showing a choice there is pure friction (per the design
  // audit's auto-use-vs-show rule).
  //
  // ── Contract ────────────────────────────────────────────────────────
  //   props.open        — bindable; parent opens when root is repo-less.
  //   props.projectId   — persists the choice via set_worktree_repo_mode.
  //   props.projectPath — display in the header.
  //   props.onChoose    — called with the chosen mode after persistence
  //                       ('local_init' | 'no_repo'). For 'local_init' the
  //                       modal itself already ran the git-init side effect;
  //                       'no_repo' has no side effect (the persisted mode is
  //                       honoured at runtime by the SubagentStart/Stop hooks).
  //   props.onDismiss   — called on cancel/close; records NOTHING (current
  //                       behaviour preserved — never silently mutate).

  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import { invoke } from '$lib/tauri';

  type WorktreeRepoMode = 'use_existing' | 'local_init' | 'no_repo';

  let {
    open = $bindable(false),
    projectId,
    projectPath,
    onChoose,
    onDismiss,
  }: {
    open: boolean;
    projectId: string;
    projectPath: string;
    onChoose: (mode: WorktreeRepoMode) => void;
    onDismiss: () => void;
  } = $props();

  let busy = $state(false);
  let errorMsg = $state('');

  async function persistAndChoose(mode: WorktreeRepoMode) {
    if (busy) return;
    busy = true;
    errorMsg = '';
    try {
      await invoke('set_worktree_repo_mode', { projectId, mode });
      open = false;
      onChoose(mode);
    } catch (e) {
      // Persistence failed — keep the modal open so the user can retry or
      // dismiss. Never silently swallow (the choice drives a filesystem
      // side effect the parent runs next).
      errorMsg = `Could not save the choice: ${e}`;
    } finally {
      busy = false;
    }
  }

  async function handleCreateNew() {
    if (busy) return;
    busy = true;
    errorMsg = '';
    try {
      // Actually create the local-only repo (the enforcement side effect) —
      // the backend refuses if the root is already inside a repo + adds a
      // gitignore guard so nested repos aren't absorbed. THEN persist the
      // choice, so a git-init failure doesn't record a state we can't honor.
      await invoke('create_local_project_repo', { projectRoot: projectPath });
      await invoke('set_worktree_repo_mode', { projectId, mode: 'local_init' });
      open = false;
      onChoose('local_init');
    } catch (e) {
      errorMsg = `Could not create the local repo: ${e}`;
    } finally {
      busy = false;
    }
  }
  function handleNoRepo() {
    void persistAndChoose('no_repo');
  }
  function handleDismiss() {
    open = false;
    onDismiss();
  }
</script>

<DialogRoot bind:open onClose={handleDismiss} ariaLabel="Subagent worktree isolation needs a git repo">
  {#snippet header()}
    <h2 class="swt-title">Subagent isolation needs a git repo</h2>
  {/snippet}
  {#snippet body()}
    <div class="swt-modal">
      <p class="swt-path">
        <code>{projectPath}</code> is not inside any git repository.
      </p>

      <p class="swt-explanation">
        Subagents that use <strong>worktree isolation</strong> need a git repo
        at (or above) the workspace root — Claude Code creates each agent its
        own isolated checkout via <code>git worktree add</code>, which requires
        a repo. Without one, those agents can't spawn in isolation.
      </p>

      <div class="swt-options">
        <div class="swt-option">
          <button
            class="btn-3d btn-3d-primary btn-3d-sm"
            onclick={handleCreateNew}
            disabled={busy}
          >
            Create new
          </button>
          <p class="swt-option-text">
            Initialise a <strong>local-only</strong> repo at the workspace root
            (<code>git init</code>, <code>main</code> branch). Never pushed to
            any remote, exists solely so subagents can use worktree isolation.
            Your nested repos are left untouched (gitignored).
          </p>
        </div>

        <div class="swt-option">
          <button
            class="btn-3d btn-3d-ghost btn-3d-sm"
            onclick={handleNoRepo}
            disabled={busy}
          >
            No .git repo
          </button>
          <p class="swt-option-text">
            Opt out for this project. Subagents run in the shared working
            directory (no worktree isolation). VCO's <code>SubagentStart</code>
            hook warns if an agent that requested isolation lands in the shared
            tree, and <code>SubagentStop</code> flags shared-tree writes after
            the fact. You won't be asked again.
          </p>
        </div>
      </div>

      {#if errorMsg}
        <p class="swt-error">{errorMsg}</p>
      {/if}

      <div class="swt-actions">
        <button
          class="btn-3d btn-3d-ghost btn-3d-sm"
          onclick={handleDismiss}
          disabled={busy}
        >
          Decide later
        </button>
      </div>
    </div>
  {/snippet}
</DialogRoot>

<style>
  .swt-title {
    font-size: 16px;
    margin: 0 0 12px;
    color: #fff;
  }
  .swt-modal {
    padding: 16px;
    max-width: 640px;
    color: #ccc;
  }
  .swt-path {
    font-size: 12px;
    color: #888;
    margin: 0 0 12px;
  }
  .swt-path code {
    background: rgba(255, 255, 255, 0.06);
    padding: 2px 6px;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
    font-size: 11px;
  }
  .swt-explanation {
    font-size: 13px;
    color: #bbb;
    margin: 0 0 16px;
    line-height: 1.5;
  }
  .swt-explanation code {
    background: rgba(255, 255, 255, 0.06);
    padding: 1px 4px;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
  }
  .swt-options {
    display: flex;
    flex-direction: column;
    gap: 16px;
    margin: 0 0 12px;
  }
  .swt-option {
    display: grid;
    grid-template-columns: 130px 1fr;
    gap: 12px;
    align-items: start;
  }
  .swt-option-text {
    margin: 0;
    font-size: 12px;
    color: #999;
    line-height: 1.5;
  }
  .swt-option-text code {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    color: #c4b3ff;
    background: rgba(255, 255, 255, 0.04);
    padding: 1px 4px;
    border-radius: 3px;
  }
  .swt-error {
    margin: 8px 0 0;
    font-size: 12px;
    color: #ff8aa8;
  }
  .swt-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 16px;
  }
</style>
