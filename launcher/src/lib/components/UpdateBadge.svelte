<script lang="ts">
  // Update notification badge for the MenuBar.
  //
  // Polls the orchestrator store on mount (which calls `check_for_updates`),
  // then shows a small dot + popover with an Update button when an update
  // is available. Persists "seen version" to localStorage so the toast
  // doesn't re-fire on every render — only when the underlying version
  // actually changes.
  //
  // v0.2.16 (W4 / 0.5): three-state banner. The Rust `check_for_updates`
  // command now returns a full UpdateStatus struct with three flags:
  //   - binary_stale  (highest priority) — newer launcher binary on disk
  //                   than the running process. Resolved by restart.
  //   - install_stale — source version > install manifest version
  //                     (user `git pull`-ed manually). Resolved by
  //                     install.py --update only (no git pull).
  //   - remote_ahead  — origin/main is ahead of local. Resolved by
  //                     full `update_orchestrator` (git pull + install).
  //
  // The banner renders ONE state at a time, in priority order.

  import { onMount } from 'svelte';
  import { orchestrator } from '$lib/stores/orchestrator';
  import { updater } from '$lib/stores/updater';
  import { ui } from '$lib/stores/ui';
  import OrchestratorUpdateDivergenceModal from './OrchestratorUpdateDivergenceModal.svelte';
  import OrchestratorUntrackedCollisionModal from './OrchestratorUntrackedCollisionModal.svelte';
  import OrchestratorAutostashPopModal from './OrchestratorAutostashPopModal.svelte';

  let popoverOpen = $state(false);
  let wrapperEl = $state<HTMLDivElement | null>(null);
  // v0.2.83 (WP-A2 / D3): the amber remote-check-failed badge has its own
  // popover + wrapper ref (it renders in a separate branch from the normal
  // update badge, and the two are mutually exclusive via `amberVisible`'s
  // `kind === null` guard).
  let amberPopoverOpen = $state(false);
  let amberWrapperEl = $state<HTMLDivElement | null>(null);

  const orchState = $derived($orchestrator);
  const upd = $derived($updater);

  // v0.2.16 (W4 / 0.5): copy + action per kind. Drives both popover
  // header text and primary button label/handler.
  // v0.2.51 (Bug A): added 'merge_resolved_incomplete' kind — highest
  // priority. Calls the new `resume_orchestrator_update` Tauri command
  // via `updater.resumeUpdate()`.
  const kindCopy = $derived.by(() => {
    const us = orchState.updateStatus;
    switch (upd.kind) {
      case 'merge_resolved_incomplete': {
        const op = us?.resume_operation || 'update';
        const branch = us?.resume_branch || 'main';
        // v0.2.88 (MINOR-9): the autostash-pop sentinel reuses this badge kind,
        // but its story is DIFFERENT — the merge SUCCEEDED and only restoring
        // local WIP clashed; it was NOT "resolved outside the launcher" (after a
        // restart the modal is simply gone). Branch the copy so it doesn't
        // misdescribe the state, and point at the deferral's per-file steps
        // (the resume marker-scan's generic `commit --amend` remediation is
        // wrong for a stash-pop conflict).
        if (op === 'autostash-pop') {
          return {
            title: 'Finish Update',
            desc:
              `A recent update on \`${branch}\` merged successfully, but ` +
              `restoring your uncommitted local changes (git \`--autostash\` ` +
              `pop) conflicted, so \`install.py --update\` and the binary ` +
              `refresh haven't run — last_installed_version is still ` +
              `v${us?.installed_version || '?'} while source is ` +
              `v${us?.source_version || '?'}. Resolve the conflicted file(s) ` +
              `(the update wrote the exact per-file steps to ` +
              `\`.claude/context/UPDATE_DEFERRED.md\`), then click Finish ` +
              `Update.`,
            buttonLabel: 'Finish Update',
            actionKey: 'resume' as const,
          };
        }
        return {
          title: 'Continue Update',
          desc:
            `A previous orchestrator ${op} on \`${branch}\` was halted at a ` +
            `conflict and resolved outside the launcher. The source is merged ` +
            `but \`install.py --update\` and the binary refresh never ran — ` +
            `last_installed_version is still v${us?.installed_version || '?'} ` +
            `while source is v${us?.source_version || '?'}. Click Continue ` +
            `Update to finish the install.`,
          buttonLabel: 'Continue Update',
          actionKey: 'resume' as const,
        };
      }
      case 'binary_stale':
        return {
          title: 'Restart Launcher',
          desc: us
            ? `A newer launcher binary is on disk (v${us.on_disk_binary_version}). The running launcher is v${us.running_version}. Restart to load it.`
            : 'A newer launcher binary is on disk. Restart to load it.',
          buttonLabel: 'Restart Launcher',
          actionKey: 'restart' as const,
        };
      case 'install_stale': {
        // v0.2.60: distinguish a fresh apply from RESUMING a half-finished
        // install. When a prior `install.py --update` already ran on this
        // tree (installed_version present) but didn't reach the on-disk
        // source version, the source is on disk yet the install is
        // incomplete — clicking again RESUMES it, it isn't a brand-new
        // install. Say so, so the user understands they're finishing a
        // previously-interrupted update (e.g. one that hit the launcher.db
        // lock and deferred), not starting over.
        const priorInstall = !!us?.installed_version;
        return {
          title: priorInstall ? 'Resume Update' : 'Install Update',
          desc: us
            ? priorInstall
              ? `A previous update to v${us.source_version} did not finish (last completed install: v${us.installed_version}). Click Resume Update to apply the rest.`
              : `v${us.source_version} is on disk. Click Install Update to apply.`
            : 'Source is newer than the last successful install. Click to apply.',
          buttonLabel: priorInstall ? 'Resume Update' : 'Install Update',
          actionKey: 'install' as const,
        };
      }
      case 'remote_ahead':
        return {
          title: 'Update available',
          desc: us
            ? `A new version of the orchestrator is available on the remote. Current: v${us.installed_version || us.source_version || orchState.version || 'unknown'}.`
            : 'A new version of the orchestrator is available.',
          buttonLabel: 'Fetch + Install',
          actionKey: 'fetch_install' as const,
        };
      default:
        return {
          title: 'Up to date',
          desc: 'No pending updates detected.',
          buttonLabel: '',
          actionKey: null as null | 'restart' | 'install' | 'fetch_install' | 'resume',
        };
    }
  });

  $effect(() => {
    // Re-evaluate when the orchestrator store reports a change.
    void orchState.updateAvailable;
    void orchState.updateStatus;
    void orchState.version;
    void orchState.status;
    updater.syncFromOrchestrator();
  });

  onMount(() => {
    // checkStatus() in the orchestrator store calls check_for_updates as
    // a side effect when an install is detected — we don't need to call
    // it ourselves. Just push current state into our store.
    updater.syncFromOrchestrator();
  });

  function handleClickOutside(e: MouseEvent) {
    if (popoverOpen && wrapperEl && !wrapperEl.contains(e.target as Node)) {
      popoverOpen = false;
    }
    if (
      amberPopoverOpen &&
      amberWrapperEl &&
      !amberWrapperEl.contains(e.target as Node)
    ) {
      amberPopoverOpen = false;
    }
  }

  async function handleAction() {
    popoverOpen = false;
    if (kindCopy.actionKey === null) return;
    // v0.2.40 (contributor): open the full-screen blocking progress overlay
    // BEFORE invoking the updater action. The overlay subscribes to
    // `$orchestrator.progress` (already populated by the install_progress
    // Tauri listener) and stays up across the entire flow:
    //   - `runUpdate`           — git pull + install.py --update + restart
    //   - `applyPendingInstall` — install.py --update only
    //   - `runRestart`          — re-exec the dist binary; the launcher
    //                             dies mid-call so the overlay blinks
    //                             once at restart, which is correct UX.
    // The overlay owns its own completion lifecycle (1.8 s hold at 100 %
    // + 400 ms fade-out), then calls ui.closeOrchestratorUpdateProgress()
    // itself — UpdateBadge no longer needs the rising/falling-edge
    // bookkeeping that lived here in the first draft of this branch.
    ui.openOrchestratorUpdateProgress();
    switch (kindCopy.actionKey) {
      case 'restart':
        await updater.runRestart();
        break;
      case 'install':
        await updater.applyPendingInstall();
        break;
      case 'fetch_install':
        await updater.runUpdate();
        break;
      case 'resume':
        // v0.2.51 Bug A: re-enter the post-merge tail of update_orchestrator
        // (install.py --update + binary refresh + auto-restart). The Rust
        // command audit-logs and refuses if conflict markers still present.
        await updater.resumeUpdate();
        break;
    }
  }

  function handleDismiss() {
    popoverOpen = false;
    updater.dismiss();
  }

  // v0.2.83 (WP-A2 / D3): manual "Retry now" from the amber remote-check-
  // failed popover. Delegates to the ONE real check entry point in the
  // updater store — the same flow RightSidebar's "Check Update" uses. We
  // don't branch on the result here: the store's derived state
  // (remoteCheckFailed / kind) re-renders the badge to whatever the check
  // resolved to (still amber, a real update badge, or gone).
  async function handleRetryNow() {
    await updater.manualCheck();
    amberPopoverOpen = false;
  }

  // Don't render anything if no update or already dismissed for this version.
  const visible = $derived(upd.available && !upd.dismissed && upd.kind !== null);

  // v0.2.83 (WP-A2 / D3): the amber "couldn't check for updates" state.
  // Shown ONLY when the remote check failed AND there is no real pending
  // update to render (the store's `remoteCheckFailed` already encodes the
  // `kind === null` guard, but we re-assert it here so a stale flag can
  // never paint amber over a genuine update badge). This is a distinct,
  // lower-urgency signal from the teal/pink/purple update kinds — the check
  // is unknown, not "up to date", and it retries on its own.
  const amberVisible = $derived(upd.remoteCheckFailed && upd.kind === null);
</script>

<svelte:window onclick={handleClickOutside} />

<!-- v0.2.23 (B4 / D19): when update_orchestrator fails with a non-FF
     divergence error, surface the merge/rebase/cancel modal. This runs
     OUTSIDE the {#if visible} block so the modal stays usable even when
     the user has dismissed the badge. -->
{#if upd.nonFf}
  <OrchestratorUpdateDivergenceModal
    payload={upd.nonFf}
    installPath={orchState.installPath}
    onClose={() => updater.dismissNonFf()}
  />
{/if}

<!-- v0.2.88 (DEFECT 1 / FIELD DEFECT): untracked-file collision with a new-in-
     release path. The parsed collision set + a "Resolve & retry" button (the
     pre-fix dead-end empty conflict modal is what this replaces). -->
{#if upd.untrackedCollision}
  <OrchestratorUntrackedCollisionModal
    payload={upd.untrackedCollision}
    installPath={orchState.installPath}
    onClose={() => updater.dismissUntrackedCollision()}
  />
{/if}

<!-- v0.2.88 (DEFECT 2 / FIELD DEFECT): merge succeeded but the --autostash pop
     of local WIP conflicted. Distinct from a merge failure; keep-updated /
     keep-local per file. -->
{#if upd.autostashPop}
  <OrchestratorAutostashPopModal
    payload={upd.autostashPop}
    installPath={orchState.installPath}
    onClose={() => updater.dismissAutostashPop()}
  />
{/if}

{#if visible}
  <div class="update-wrapper" bind:this={wrapperEl}>
    <button
      class="update-trigger"
      class:updating={upd.updating}
      class:kind-resume={upd.kind === 'merge_resolved_incomplete'}
      class:kind-binary={upd.kind === 'binary_stale'}
      class:kind-install={upd.kind === 'install_stale'}
      class:kind-remote={upd.kind === 'remote_ahead'}
      onclick={(e) => { e.stopPropagation(); popoverOpen = !popoverOpen; }}
      title={kindCopy.title}
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="23 4 23 10 17 10"/>
        <polyline points="1 20 1 14 7 14"/>
        <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
      </svg>
      <span class="update-dot"></span>
    </button>

    {#if popoverOpen}
      <div class="update-popover">
        <div class="popover-header">
          <span class="popover-title">{kindCopy.title}</span>
        </div>
        <p class="popover-desc">{kindCopy.desc}</p>
        {#if upd.error}
          <div class="popover-error">{upd.error}</div>
        {/if}
        <div class="popover-actions">
          <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={handleDismiss} disabled={upd.updating}>
            Later
          </button>
          <button
            class="btn-3d btn-3d-primary btn-3d-sm"
            onclick={handleAction}
            disabled={upd.updating || kindCopy.actionKey === null}
          >
            {#if upd.updating}
              Working…
            {:else}
              {kindCopy.buttonLabel}
            {/if}
          </button>
        </div>
      </div>
    {/if}
  </div>
{/if}

<!-- v0.2.83 (WP-A2 / D3): amber "couldn't check for updates" badge. Renders
     only when the remote check failed AND no real update kind is active
     (mutually exclusive with the block above). Distinct amber color so it
     reads as "unknown, retrying" — NOT "up to date" and NOT a pending
     update. The check retries automatically in the background; "Retry now"
     lets the user force it. -->
{#if amberVisible}
  <div class="update-wrapper" bind:this={amberWrapperEl}>
    <button
      class="update-trigger kind-check-failed"
      onclick={(e) => { e.stopPropagation(); amberPopoverOpen = !amberPopoverOpen; }}
      title="Couldn't check for updates — retrying automatically"
    >
      <!-- alert-triangle: reads as "attention, but not an error" -->
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
        <line x1="12" y1="9" x2="12" y2="13"/>
        <line x1="12" y1="17" x2="12.01" y2="17"/>
      </svg>
      <span class="update-dot dot-amber"></span>
    </button>

    {#if amberPopoverOpen}
      <div class="update-popover">
        <div class="popover-header">
          <span class="popover-title">Couldn't check for updates</span>
        </div>
        <p class="popover-desc">
          {#if upd.remoteCheckError}
            Couldn't check for updates — {upd.remoteCheckError}. Retrying automatically.
          {:else}
            Couldn't check for updates. Retrying automatically.
          {/if}
        </p>
        <div class="popover-actions">
          <button
            class="btn-3d btn-3d-primary btn-3d-sm"
            onclick={handleRetryNow}
            disabled={upd.checking}
          >
            {#if upd.checking}
              Checking…
            {:else}
              Retry now
            {/if}
          </button>
        </div>
      </div>
    {/if}
  </div>
{/if}

<style>
  .update-wrapper {
    position: relative;
  }

  .update-trigger {
    position: relative;
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(0, 191, 166, 0.3);
    border-radius: 10px;
    color: var(--color-teal);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .update-trigger:hover {
    background: rgba(0, 191, 166, 0.08);
    border-color: rgba(0, 191, 166, 0.5);
  }

  .update-trigger.updating svg {
    animation: spin 1.2s linear infinite;
  }

  /* v0.2.16 (W4 / 0.5): visual tint per kind. Binary-stale uses an
     attention-grabbing pink (matches the green-banner pattern from
     v0.2.15's LauncherRestartBanner — same urgency); install-stale
     and remote-ahead share the default teal. */
  .update-trigger.kind-binary {
    border-color: rgba(255, 79, 160, 0.5);
    color: var(--color-pink, #ff4fa0);
  }
  .update-trigger.kind-binary:hover {
    background: rgba(255, 79, 160, 0.08);
    border-color: rgba(255, 79, 160, 0.7);
  }
  /* v0.2.51 Bug A: merge_resolved_incomplete uses purple — distinct from
     binary_stale's pink (= "restart") and the default teal (= "available")
     so the user can tell at a glance "this is the conflict-recovery one,
     not the routine update one". */
  .update-trigger.kind-resume {
    border-color: rgba(123, 95, 255, 0.55);
    color: var(--color-purple, #7b5fff);
    /* Subtle pulse animation to draw the eye — this state is rarely
       expected and easy to miss otherwise. */
    animation: resume-pulse 2.4s ease-in-out infinite;
  }
  .update-trigger.kind-resume:hover {
    background: rgba(123, 95, 255, 0.1);
    border-color: rgba(123, 95, 255, 0.75);
  }
  @keyframes resume-pulse {
    0%, 100% {
      box-shadow: 0 0 0 0 rgba(123, 95, 255, 0.35);
    }
    50% {
      box-shadow: 0 0 0 6px rgba(123, 95, 255, 0);
    }
  }

  /* v0.2.83 (WP-A2 / D3): amber "couldn't check for updates" state. A
     warning-amber (#f1c40f) distinct from the teal (available), pink
     (restart) and purple (resume) update kinds — signals "unknown, not up
     to date" without the urgency of a real pending update. Matches the
     amber `sidebar-info-status-warn` used elsewhere in the launcher. */
  .update-trigger.kind-check-failed {
    border-color: rgba(241, 196, 15, 0.5);
    color: #f1c40f;
  }
  .update-trigger.kind-check-failed:hover {
    background: rgba(241, 196, 15, 0.08);
    border-color: rgba(241, 196, 15, 0.7);
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .update-dot.dot-amber {
    background: #f1c40f;
  }

  .update-dot {
    position: absolute;
    top: 6px;
    right: 6px;
    width: 8px;
    height: 8px;
    background: var(--color-pink);
    border: 2px solid rgba(8, 15, 40, 1);
    border-radius: 50%;
  }

  .update-popover {
    position: fixed;
    top: 52px;
    right: 16px;
    width: 320px;
    background: rgba(13, 23, 53, 0.97);
    backdrop-filter: blur(24px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 14px;
    padding: 14px;
    z-index: 250;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
    animation: pop-in 0.15s ease-out;
  }

  @keyframes pop-in {
    from { opacity: 0; transform: translateY(-8px) scale(0.96); }
    to { opacity: 1; transform: translateY(0) scale(1); }
  }

  .popover-header {
    margin-bottom: 6px;
  }

  .popover-title {
    font-size: 13px;
    font-weight: 700;
    color: var(--color-text);
  }

  .popover-desc {
    font-size: 12px;
    color: var(--color-mid);
    line-height: 1.5;
    margin-bottom: 12px;
  }

  .popover-error {
    padding: 8px 10px;
    background: rgba(255, 79, 160, 0.1);
    border: 1px solid rgba(255, 79, 160, 0.25);
    border-radius: 8px;
    color: var(--color-pink);
    font-size: 11px;
    margin-bottom: 10px;
  }

  .popover-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
</style>
