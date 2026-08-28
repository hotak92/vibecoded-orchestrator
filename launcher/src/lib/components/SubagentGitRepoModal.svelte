<script lang="ts">
  // SubagentGitRepoModal — v0.2.71 Track T-WT; v0.2.91 (#30) adds the
  // "Connect an existing repo" choice.
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
  //   3. Connect an EXISTING repo (v0.2.91 #30) — either a remote URL
  //      (`git init` + `git remote add origin` + `git fetch` ONLY; never
  //      checkout/merge/pull over existing content — the user reconciles
  //      manually) or a local folder / detected nested repo (record-only,
  //      nothing on disk is mutated; the root stays repo-less, so worktree
  //      isolation remains unavailable — the record is documentation, not
  //      enforcement, and the copy + toast say so — M7).
  //   4. Opt out ("No .git repo") — subagents run in the shared cwd, no
  //      isolation. The persisted mode is honoured by VCO's SubagentStart
  //      isolation-check (warns on a shared-tree spawn) + SubagentStop
  //      reconcile (flags shared-tree writes post-hoc). (No agent-frontmatter
  //      rewrite is performed.)
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
  //   props.projectPath — display in the header + connect-arm probes.
  //   props.onChoose    — called with the chosen mode after persistence
  //                       ('local_init' | 'no_repo' | 'use_existing_remote'
  //                       | 'use_existing_at'). For 'local_init' and the two
  //                       connect modes the modal itself already ran the
  //                       side effect; 'no_repo' has no side effect (the
  //                       persisted mode is honoured at runtime by the
  //                       SubagentStart/Stop hooks).
  //   props.onDismiss   — called on cancel/close; records NOTHING (current
  //                       behaviour preserved — never silently mutate).

  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import Dropdown from '$lib/components/Dropdown.svelte';
  import { invoke } from '$lib/tauri';
  import { pickDirectory } from '$lib/dialog';
  import { toast } from '$lib/stores/toast';
  import {
    isValidGitRemoteUrl,
    resolveConnectSelection,
  } from '$lib/components/subagent-git-repo-logic';

  type WorktreeRepoMode =
    | 'use_existing'
    | 'local_init'
    | 'no_repo'
    | 'use_existing_at'
    | 'use_existing_remote';

  type NestedRepoCandidate = { rel_path: string; abs_path: string };

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

  // ── v0.2.91 (#30) connect-existing state ──────────────────────────
  let connectOpen = $state(false);
  let remoteUrl = $state('');
  let localRepoPath = $state('');
  let candidateChoice = $state<string | undefined>(undefined);
  let candidates = $state<NestedRepoCandidate[]>([]);
  let scaffoldOnly = $state(false);
  // Remote-arm retry safety: if the attach succeeded but persisting the
  // mode failed, a retry must NOT re-run the attach (the root is now a
  // repo, so the re-check would refuse and mask the real error). Keyed by
  // URL so editing the URL after a half-failure still re-attaches (and
  // gets the honest "already inside a repo" refusal).
  let attachedRemoteUrl = $state<string | null>(null);
  let attachedOutcome = $state<{ fetched: boolean; message: string } | null>(null);

  // Best-effort probes for the connect arm, once per mount (the modal is
  // only mounted while offered). Soft-fail: no candidates dropdown / no
  // scaffold copy — never an error state.
  let probesLoaded = false;
  $effect(() => {
    if (!open) return;
    void loadConnectProbes();
  });
  async function loadConnectProbes() {
    if (probesLoaded) return;
    probesLoaded = true;
    try {
      candidates = await invoke<NestedRepoCandidate[]>('list_nested_repo_candidates', {
        projectRoot: projectPath,
      });
    } catch {
      candidates = [];
    }
    try {
      scaffoldOnly = await invoke<boolean>('detect_scaffold_only_root', {
        projectRoot: projectPath,
      });
    } catch {
      scaffoldOnly = false;
    }
  }

  const candidateOptions = $derived(
    candidates.map((c) => ({ value: c.abs_path, label: c.rel_path })),
  );
  const urlLooksInvalid = $derived(
    remoteUrl.trim() !== '' && !isValidGitRemoteUrl(remoteUrl),
  );
  const connectResolution = $derived(resolveConnectSelection(remoteUrl, localRepoPath));

  // Mutual-clear so the form always describes ONE source (the logic fn
  // still guards the 'both' case explicitly).
  function onRemoteUrlInput() {
    if (remoteUrl.trim() !== '') {
      localRepoPath = '';
      candidateChoice = undefined;
    }
  }
  function onCandidatePicked(abs: string) {
    localRepoPath = abs;
    remoteUrl = '';
  }
  async function browseLocalRepo() {
    const picked = await pickDirectory({
      title: 'Select the folder that contains your existing repo',
    });
    if (picked) {
      localRepoPath = picked;
      candidateChoice = undefined;
      remoteUrl = '';
    }
  }

  async function handleConnect() {
    if (busy) return;
    const sel = resolveConnectSelection(remoteUrl, localRepoPath);
    if (!sel.ok) return;
    busy = true;
    errorMsg = '';
    try {
      if (sel.kind === 'remote') {
        // Attach (init + remote add + fetch ONLY — the backend never
        // checks out or merges over existing content), THEN persist, so
        // an attach failure doesn't record a state we can't honor.
        if (attachedRemoteUrl !== sel.url) {
          attachedOutcome = await invoke<{ fetched: boolean; message: string }>(
            'attach_existing_repo_remote',
            { projectRoot: projectPath, remoteUrl: sel.url },
          );
          attachedRemoteUrl = sel.url;
        }
        await invoke('set_worktree_repo_mode', {
          projectId,
          mode: 'use_existing_remote',
          source: sel.url,
        });
        if (attachedOutcome) {
          // Honest status either way: fetched, or recorded-but-fetch-failed.
          if (attachedOutcome.fetched) toast.success(attachedOutcome.message);
          else toast.info(attachedOutcome.message);
        }
        open = false;
        onChoose('use_existing_remote');
      } else {
        // Record-only: validate + resolve the repo's toplevel; nothing on
        // disk is mutated.
        const toplevel = await invoke<string>('attach_existing_repo_local', {
          projectRoot: projectPath,
          repoPath: sel.path,
        });
        await invoke('set_worktree_repo_mode', {
          projectId,
          mode: 'use_existing_at',
          source: toplevel,
        });
        // M7: honest by construction — this arm is a durable RECORD, not an
        // enforcement. The root stays repo-less, so say so (info, not
        // "success" framing) instead of letting the closed prompt imply
        // isolation now works.
        toast.info(
          `Recorded ${toplevel} as this project's existing repo. Nothing on disk was changed — ` +
            `the workspace root is still not inside a git repo, so subagent worktree isolation ` +
            `remains unavailable here.`,
        );
        open = false;
        onChoose('use_existing_at');
      }
    } catch (e) {
      errorMsg = `Could not connect the repo: ${e}`;
    } finally {
      busy = false;
    }
  }

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

      {#if scaffoldOnly}
        <!-- v0.2.91 (#30): honest empty-scaffold callout. `scaffoldOnly`
             means the root holds ONLY VCO scaffolding (detect_scaffold_only_root)
             — the classic field case is a New-Project path mishap that
             scaffolded a fresh folder while the user's code lives elsewhere. -->
        <p class="swt-scaffold-note">
          This folder was created empty — if your code already lives in a
          repo, connect it here.
        </p>
      {/if}

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
            onclick={() => (connectOpen = !connectOpen)}
            disabled={busy}
          >
            Connect existing
          </button>
          <div class="swt-connect">
            <p class="swt-option-text">
              Use a repo you already have: paste a remote URL, pick a local
              folder, or choose a repo detected inside this project. A remote
              URL is connected <strong>non-destructively</strong> —
              <code>git init</code> + <code>git remote add</code> +
              <code>git fetch</code> only; your files are never checked out
              over or merged automatically. Picking a local folder only
              <strong>records</strong> where the repo lives — the workspace
              root itself stays outside a git repo, so worktree isolation
              still won't work here.
            </p>
            {#if connectOpen}
              <div class="swt-connect-form">
                <label class="swt-connect-label" for="swt-remote-url">Remote URL</label>
                <input
                  id="swt-remote-url"
                  class="swt-connect-input"
                  type="text"
                  bind:value={remoteUrl}
                  oninput={onRemoteUrlInput}
                  placeholder="https://github.com/example/example-repo.git"
                  disabled={busy}
                />
                {#if urlLooksInvalid}
                  <p class="swt-connect-hint swt-connect-hint-warn">
                    Enter a URL like <code>https://host/path</code> or
                    <code>git@host:path</code>.
                  </p>
                {/if}

                <div class="swt-connect-row">
                  <span class="swt-connect-label">Local folder</span>
                  <button
                    class="btn-3d btn-3d-ghost btn-3d-sm"
                    onclick={browseLocalRepo}
                    disabled={busy}
                  >
                    Browse…
                  </button>
                  {#if localRepoPath}
                    <code class="swt-connect-picked">{localRepoPath}</code>
                  {/if}
                </div>

                {#if candidateOptions.length > 0}
                  <div class="swt-connect-row">
                    <span class="swt-connect-label">Detected in this folder</span>
                    <Dropdown
                      id="swt-candidate"
                      options={candidateOptions}
                      bind:value={candidateChoice}
                      placeholder="Pick a detected repo…"
                      disabled={busy}
                      onChange={onCandidatePicked}
                    />
                  </div>
                {/if}

                <div class="swt-connect-actions">
                  <button
                    class="btn-3d btn-3d-primary btn-3d-sm"
                    onclick={handleConnect}
                    disabled={busy || !connectResolution.ok}
                  >
                    Connect
                  </button>
                </div>
              </div>
            {/if}
          </div>
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
  /* v0.2.91 (#30): empty-scaffold callout — info-blue, matches the
     launcher's informational banner tone (not a warning). */
  .swt-scaffold-note {
    margin: 0 0 14px;
    padding: 8px 10px;
    font-size: 12px;
    line-height: 1.5;
    color: #bcd6f5;
    background: rgba(70, 140, 220, 0.08);
    border: 1px solid rgba(70, 140, 220, 0.35);
    border-radius: 6px;
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
  /* v0.2.91 (#30): connect-existing sub-form. */
  .swt-connect {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .swt-connect-form {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.02);
  }
  .swt-connect-label {
    font-size: 11px;
    font-weight: 600;
    color: #999;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .swt-connect-input {
    width: 100%;
    padding: 7px 10px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 6px;
    color: #e8e8ee;
    font-family: ui-monospace, monospace;
    font-size: 12px;
    outline: none;
  }
  .swt-connect-input:focus {
    border-color: rgba(0, 191, 166, 0.5);
  }
  .swt-connect-hint {
    margin: 0;
    font-size: 11px;
    line-height: 1.4;
  }
  .swt-connect-hint-warn {
    color: #f5b342;
  }
  .swt-connect-hint code {
    font-family: ui-monospace, monospace;
    font-size: 10px;
  }
  .swt-connect-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .swt-connect-picked {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    color: #9ad;
    background: rgba(255, 255, 255, 0.04);
    padding: 2px 6px;
    border-radius: 3px;
    word-break: break-all;
  }
  .swt-connect-actions {
    display: flex;
    justify-content: flex-end;
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
