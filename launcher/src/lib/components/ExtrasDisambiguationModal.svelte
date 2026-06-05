<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // v0.2.47: disambiguation modal for "add codegraph extra path" when the
  // chosen path turns out to be the root of an existing launcher project.
  //
  // Per spec §13.1 + §14.3: the user gets three choices:
  //   1. "Add as project (grant access matrix)" — calls
  //      `codegraph_grant_access` with the existing project as grantor and
  //      the current project as grantee, so this project's codegraph
  //      reads see entries from the OTHER project's already-maintained
  //      collections. No row is added to project_codegraph_extra_paths.
  //   2. "Add as path anyway" — re-calls add_project_codegraph_extra_path
  //      with `force: true` so the launcher-project detection is
  //      bypassed and the path is persisted as a regular extra. The
  //      panel's auto-sync flow proceeds normally afterwards.
  //   3. "Cancel" — no-op.
  //
  // NOT minimisable per spec §14.4. The user must pick.

  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import type { ProjectMeta } from '$lib/types/codegraph-extras';

  let {
    open = $bindable<boolean>(true),
    path,
    existingProject,
    currentProjectName,
    onAddAsProject,
    onAddAsPathAnyway,
    onCancel,
  }: {
    open?: boolean;
    /** Canonicalised path the user selected. */
    path: string;
    /** Launcher project whose `folder_path` matches `path`. */
    existingProject: ProjectMeta;
    /** Display name of the project we're configuring (for prose). */
    currentProjectName: string;
    /** Invoked when the user picks "Add as project". The handler must
     *  call codegraph_grant_access; the modal closes itself first. */
    onAddAsProject: () => Promise<void> | void;
    /** Invoked when the user picks "Add as path anyway". The handler
     *  must call addExtraPath with force=true and then proceed to
     *  auto-sync. The modal closes itself first. */
    onAddAsPathAnyway: () => Promise<void> | void;
    /** Invoked when the user picks Cancel or Escape. */
    onCancel?: () => void;
  } = $props();

  // Track whether either action button is in flight so we can disable
  // both + show a "Working..." label. Either await chain can take a
  // few hundred ms (Tauri command + db round-trip).
  let working = $state(false);
  let workingLabel = $state('');

  async function pickAddAsProject() {
    if (working) return;
    working = true;
    workingLabel = 'Granting access...';
    try {
      await onAddAsProject();
      open = false;
    } finally {
      working = false;
      workingLabel = '';
    }
  }

  async function pickAddAsPath() {
    if (working) return;
    working = true;
    workingLabel = 'Adding path...';
    try {
      await onAddAsPathAnyway();
      open = false;
    } finally {
      working = false;
      workingLabel = '';
    }
  }

  function pickCancel() {
    if (working) return;
    open = false;
    onCancel?.();
  }
</script>

<DialogRoot
  bind:open
  closeOnBackdrop={false}
  closeOnEscape={!working}
  width="640px"
  ariaLabelledBy="extras-disambig-heading"
  onClose={pickCancel}
>
  {#snippet header()}
    <h2 id="extras-disambig-heading" class="extras-disambig-title">
      Path is a launcher project
    </h2>
  {/snippet}

  {#snippet body()}
    <div class="extras-disambig-body">
      <p>
        The path <code class="extras-disambig-path">{path}</code> is the
        root of an existing launcher project
        <strong>{existingProject.name}</strong>.
      </p>
      <p>
        Instead of indexing it as a read-only path, you can grant
        <strong>{currentProjectName}</strong> codegraph read-access to
        <strong>{existingProject.name}</strong> via the access matrix.
        That way you benefit from
        <strong>{existingProject.name}</strong>'s own continuously-
        maintained codegraph instead of re-analyzing its files
        separately.
      </p>
      <p class="extras-disambig-hint">
        Picking "Add as path anyway" indexes the folder into this
        project's own codegraph collection. Both choices show results
        in this project's <code>search_code_graph</code> queries; the
        access-matrix grant avoids duplicating analyze work.
      </p>
    </div>
  {/snippet}

  {#snippet footer()}
    <div class="extras-disambig-actions">
      <button
        type="button"
        class="ps-btn-primary"
        onclick={pickAddAsProject}
        disabled={working}
        aria-label="Grant access matrix instead of indexing path"
      >
        {working && workingLabel === 'Granting access...'
          ? workingLabel
          : 'Add as project (grant access matrix)'}
      </button>
      <button
        type="button"
        class="ps-btn-secondary"
        onclick={pickAddAsPath}
        disabled={working}
        aria-label="Index this folder anyway"
      >
        {working && workingLabel === 'Adding path...'
          ? workingLabel
          : 'Add as path anyway'}
      </button>
      <button
        type="button"
        class="ps-btn-secondary extras-disambig-cancel"
        onclick={pickCancel}
        disabled={working}
      >
        Cancel
      </button>
    </div>
  {/snippet}
</DialogRoot>

<style>
  .extras-disambig-title {
    margin: 0;
    font-size: 15px;
    color: #c4b3ff;
  }
  .extras-disambig-body {
    font-size: 13px;
    line-height: 1.5;
    color: #ddd;
  }
  .extras-disambig-body p {
    margin: 0 0 10px;
  }
  .extras-disambig-body p:last-child {
    margin-bottom: 0;
  }
  .extras-disambig-path {
    font-family: ui-monospace, monospace;
    background: rgba(255, 255, 255, 0.05);
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 11px;
    word-break: break-all;
  }
  .extras-disambig-hint {
    font-size: 12px;
    color: #999;
  }
  .extras-disambig-hint code {
    font-family: ui-monospace, monospace;
    background: rgba(255, 255, 255, 0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
  }
  .extras-disambig-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }
  .extras-disambig-cancel {
    margin-left: auto;
  }

  /* Re-declare the project-state button styles locally so the modal
     renders consistently even when the DialogRoot's snippet body is
     outside the scope where IdentityTab's styles live. Same palette
     as the IdentityTab's --ps-* equivalents (the launcher does not
     expose those as CSS custom properties yet). */
  .ps-btn-primary {
    background: rgb(0, 191, 166);
    border: none;
    color: #000;
    padding: 8px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
  }
  .ps-btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .ps-btn-secondary {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: inherit;
    padding: 8px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
  }
  .ps-btn-secondary:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.08);
  }
  .ps-btn-secondary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
