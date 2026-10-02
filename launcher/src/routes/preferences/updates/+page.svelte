<script lang="ts">
  // Orchestrator updates page (Preferences → Updates).
  //
  // v0.2.100 (WP-08, AD-1 + L3-F01/F05/F10): this page no longer runs its own
  // update pipeline. It renders the SAME state the update badge renders
  // (`$orchestrator.updateStatus` + `$updater`, one wording table
  // `badgeCopyFor`) and its buttons call the SAME store actions:
  //   - "Check now"   → `updater.manualCheck()` (`check_for_updates`)
  //   - primary button → `updater.perform(actionForKind(kind))`, i.e.
  //     `updater.run('PullFf' | 'ApplyOnly' | 'Resume')` / `runRestart()` —
  //     which opens the one progress overlay; a diverged clone opens the one
  //     divergence modal (`OrchestratorUpdateDivergenceModal`, hoisted in
  //     `+layout.svelte`). The page-local resync modal, the second check
  //     command and the page-local state they fed are gone.
  //   - the binary-lag banner asks the backend (`check_running_version_lags_tag`)
  //     instead of mirroring the Rust rule in TypeScript.
  //
  // The "Update now" button shows a confirmation first: checks are automatic,
  // applying an update is always an explicit user action.

  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';
  // v0.2.91 WP-I (decision #6) — the GLOBAL deferral ledger lives here, on the
  // page that already owns install-wide state (update state, binary lag,
  // protected paths). Per-project entries deliberately do NOT appear here;
  // each project's ledger renders on its own Settings tab.
  import DeferralLedgerPanel from '$lib/components/DeferralLedgerPanel.svelte';
  import { orchestrator, renderCheck, checkError } from '$lib/stores/orchestrator';
  import { updater, badgeCopyFor } from '$lib/stores/updater';

  let confirmingApply = $state(false);
  let userOwnedPaths = $state<string[]>([]);
  let autoCheckEnabled = $state(true);
  // Auto-retry failed module installs on orchestrator update. Backend
  // default is true; this toggle exposes the opt-out.
  let autoRetryFailedInstalls = $state(true);

  // v0.2.35 Agent K — running-version display + binary-lag warning.
  // `runningVersion` is the launcher's compile-time CARGO_PKG_VERSION.
  // `latestSourceTag` is the most recent release tag on the remote, e.g.
  // `v0.2.34` — or null when there are none. `binaryLags` is the BACKEND's
  // answer (`check_running_version_lags_tag`, v0.2.100 L3-F10: the TS mirror
  // of that rule is deleted — one rule, one home).
  let runningVersion = $state<string | null>(null);
  let latestSourceTag = $state<string | null>(null);
  let binaryLags = $state(false);
  let binaryLagDismissed = $state(false);
  // v0.2.92 (WP-13): whether the release-tag lookup SUCCEEDED, which is a
  // different question from whether it returned a tag.
  let latestTagLookupFailed = $state(false);
  // v0.2.92 (WP-13): reattach affordance state.
  let reattaching = $state(false);

  const orch = $derived($orchestrator);
  const upd = $derived($updater);
  const us = $derived(orch.updateStatus);
  // The SAME copy + action the badge shows for the same state.
  const copy = $derived(badgeCopyFor(upd.kind, us, orch.version));
  const busy = $derived(upd.updating || upd.checking);

  let showBinaryLagBanner = $derived(!binaryLagDismissed && binaryLags);

  /**
   * localStorage key under which we record the LATEST tag the user has
   * dismissed the banner for. Per-version so a future mismatch with a
   * different tag re-shows the warning.
   */
  const DISMISS_KEY = 'vct.updates.binary-lag-dismissed-tag';

  async function loadSettings() {
    // Every read soft-fails with a console warning so browser-mode (vite dev)
    // lands on defaults instead of an unhandled rejection.
    try {
      const paths = await invoke<string[]>('get_user_owned_paths');
      if (paths) userOwnedPaths = paths;
      const auto = await invoke<boolean>('get_auto_check_enabled');
      if (auto !== null) autoCheckEnabled = auto;
      const retry = await invoke<boolean>('get_auto_retry_failed_installs_setting');
      if (retry !== null) autoRetryFailedInstalls = retry;
    } catch (e) {
      console.warn('[updates] settings load skipped:', e);
    }

    try {
      const rv = await invoke<string>('get_launcher_running_version');
      if (rv) runningVersion = rv;
    } catch (e) {
      console.warn('[updates] get_launcher_running_version skipped:', e);
    }
    try {
      const tag = await invoke<string | null>('get_latest_source_release_tag');
      latestSourceTag = tag ?? null;
      latestTagLookupFailed = false;
    } catch (e) {
      // v0.2.92 (WP-13): an ERROR here is not "no tags" — say so.
      console.warn('[updates] get_latest_source_release_tag failed:', e);
      latestSourceTag = null;
      latestTagLookupFailed = true;
    }

    binaryLags = false;
    if (runningVersion && latestSourceTag) {
      try {
        binaryLags = await invoke<boolean>('check_running_version_lags_tag', {
          running: runningVersion,
          latestTag: latestSourceTag,
        });
      } catch (e) {
        // Unknown is not "lagging": the banner makes a claim, so it needs an
        // answer. The version line above still shows both values.
        console.warn('[updates] check_running_version_lags_tag failed:', e);
      }
    }

    // Per-tag dismissal: hidden only for the SAME tag the user dismissed.
    try {
      const dismissedFor = localStorage.getItem(DISMISS_KEY);
      binaryLagDismissed =
        dismissedFor !== null &&
        latestSourceTag !== null &&
        dismissedFor === latestSourceTag;
    } catch {
      binaryLagDismissed = false;
    }
  }

  function dismissBinaryLagBanner() {
    binaryLagDismissed = true;
    try {
      if (latestSourceTag) localStorage.setItem(DISMISS_KEY, latestSourceTag);
    } catch {
      // localStorage unavailable — the in-memory flag still hides it.
    }
  }

  /** "Check now" — the ONE check (`updater.manualCheck`), same as the
   *  RightSidebar button, the badge's Retry and the tray item. */
  async function checkNow() {
    const result = await updater.manualCheck();
    // v0.2.92 (WP-13): "up to date" only from a check that completed.
    if (result === 'available') {
      toast.success('Update available');
    } else if (result === 'up_to_date') {
      toast.success('The orchestrator is up to date');
    } else {
      const why = checkError($orchestrator.updateStatus?.remote_check);
      toast.error(why ? `Couldn't check for updates — ${why}` : "Couldn't check for updates");
    }
  }

  function requestApply() {
    if (!copy.action) return;
    // Applying an update is confirmed; a restart / conflict reopen is not an
    // update run and needs no second click.
    if (copy.action.type === 'run') {
      confirmingApply = true;
    } else {
      void updater.perform(copy.action);
    }
  }

  async function confirmApply() {
    confirmingApply = false;
    // The one action: overlay, error routing and restart all live in the store.
    await updater.perform(copy.action);
  }

  /**
   * v0.2.92 (WP-13): return HEAD to its branch — the one GUI path out of a
   * detached HEAD. Guarded entirely in Rust (`reattach_orchestrator_branch`);
   * this handler relays the refusal verbatim because it names the fix.
   */
  async function reattachBranch() {
    reattaching = true;
    try {
      const branch = await invoke<string>('reattach_orchestrator_branch');
      toast.success(`Reattached to ${branch}`);
      await updater.manualCheck();
    } catch (e) {
      toast.error(String(e));
    } finally {
      reattaching = false;
    }
  }

  async function toggleAutoCheck(enabled: boolean) {
    autoCheckEnabled = enabled;
    try {
      await invoke('set_auto_check_enabled', { enabled });
      toast.success(enabled ? 'Automatic update checks enabled' : 'Automatic update checks disabled');
    } catch (e) {
      toast.error(String(e));
      autoCheckEnabled = !enabled; // revert on failure
    }
  }

  async function toggleAutoRetryFailedInstalls(enabled: boolean) {
    autoRetryFailedInstalls = enabled;
    try {
      await invoke('set_auto_retry_failed_installs_setting', { enabled });
      toast.success(
        enabled
          ? 'Auto-retry of failed module installs enabled'
          : 'Auto-retry of failed module installs disabled',
      );
    } catch (e) {
      toast.error(String(e));
      autoRetryFailedInstalls = !enabled; // revert on failure
    }
  }

  onMount(() => {
    void loadSettings();
  });

  function v(s: string | null | undefined): string {
    return s ? `v${s}` : '—';
  }

  // v0.2.35 (a11y): keyboard support for the hand-rolled confirm modal —
  // Escape closes it, focus lands on its first button when it opens.
  function onConfirmApplyKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      confirmingApply = false;
    }
    e.stopPropagation();
  }
  function autofocusFirstButton(el: HTMLDivElement) {
    queueMicrotask(() => {
      const btn = el.querySelector<HTMLButtonElement>('button');
      btn?.focus();
    });
  }
</script>

<svelte:head>
  <title>Orchestrator updates — VCT Launcher</title>
</svelte:head>

<div class="upd-page">
  <header class="upd-header">
    <button class="upd-back" onclick={() => goto('/preferences')}>← Back</button>
    <h1>Orchestrator updates</h1>
  </header>

  <main class="upd-main">
    <section class="upd-status">
      <h2>Status</h2>

      <!-- v0.2.35 Agent K — running-version display. v0.2.92 (WP-13): the tag
           comes from the REMOTE, and a failed lookup says so. -->
      {#if runningVersion}
        <p class="upd-version-line">
          <span class="upd-version-label">Running:</span>
          <code>v{runningVersion}</code>
          {#if latestSourceTag}
            <span class="upd-version-sep">|</span>
            <span class="upd-version-label">Latest source release:</span>
            <code>{latestSourceTag}</code>
          {:else if latestTagLookupFailed}
            <span class="upd-version-sep">|</span>
            <span class="upd-version-label">Latest source release:</span>
            <span class="upd-unknown">couldn't check</span>
          {/if}
        </p>
      {/if}

      <!-- v0.2.35 Agent K — post-update binary-lag banner. v0.2.100: the
           verdict is the backend's (`check_running_version_lags_tag`). -->
      {#if showBinaryLagBanner}
        <div class="upd-banner upd-banner-binary-lag">
          <div class="upd-banner-binary-lag-text">
            <strong>⚠ Binary is older than the latest source release</strong>
            <span>
              You're running <code>v{runningVersion}</code>, but the latest
              source release is <code>{latestSourceTag}</code>. The
              orchestrator's release CI publishes the matching binary
              ~5-10 minutes after the source tag — if you updated during
              that window, the launcher pulled the previous release's
              binary. Check again in 5-10 minutes; when the update shows
              up, apply it to pick up the matching <code>{latestSourceTag}</code>
              binary.
            </span>
          </div>
          <button
            class="upd-banner-dismiss"
            onclick={dismissBinaryLagBanner}
            aria-label="Dismiss binary-lag warning"
            title="Dismiss for this version"
          >
            ×
          </button>
        </div>
      {/if}

      <!-- v0.2.100 (WP-08): the verdict is the update badge's — same store,
           same wording table. "✓ Up to date" only from a check that completed
           with nothing pending (v0.2.92 WP-13). -->
      {#if upd.kind !== null}
        <div class="upd-banner upd-banner-warn" data-testid="upd-pending">
          <strong>⚠ {copy.title}</strong>
          <span>{copy.desc}</span>
        </div>
        {#if copy.binaryAlsoStale}
          <p class="upd-hint">
            A newer launcher binary is also on disk; the update restarts into
            the version it installs.
          </p>
        {/if}
      {:else if orch.status !== 'installed' && orch.status !== 'updating'}
        <p class="upd-empty">
          {orch.status === 'not_installed'
            ? 'No orchestrator install was found.'
            : 'Checking the orchestrator install…'}
        </p>
      {:else if orch.lastCheckFailed === true}
        <div class="upd-banner upd-banner-unknown">
          <strong>⚠ Couldn't check for updates</strong>
          <span>
            This is <em>not</em> "up to date" — the launcher could not
            determine whether new commits exist. It retries on its own.
            {#if checkError(us?.remote_check)}
              <br />git said: <code>{checkError(us?.remote_check)}</code>
            {/if}
          </span>
        </div>
      {:else if us && renderCheck(us.remote_check) === 'not_applicable'}
        <div class="upd-banner upd-banner-unknown">
          <strong>No remote to check</strong>
          <span>
            This install is not a git checkout, so there is no upstream to
            compare against.
          </span>
        </div>
      {:else if us && renderCheck(us.remote_check) === 'ok'}
        <div class="upd-banner upd-banner-ok">
          <strong>✓ Up to date</strong>
        </div>
      {:else}
        <p class="upd-empty">Click "Check now" to query the remote.</p>
      {/if}

      {#if upd.failed && upd.error && !upd.updating}
        <div class="upd-error" data-testid="upd-last-error">
          <strong>Last update attempt failed:</strong> {upd.error}
        </div>
      {/if}

      <!-- v0.2.92 (WP-13): detached HEAD, named and actionable. -->
      {#if us?.head_detached}
        <div class="upd-banner upd-banner-detached">
          <div class="upd-detached-text">
            <strong>Detached HEAD — this clone is not on a branch</strong>
            <span>
              Update checks compare against the upstream branch and updates
              still apply, but the clone stays detached afterwards.
              Reattaching is safe when the working tree is clean and your
              current commit is already contained in the upstream branch; the
              button refuses (and says why) otherwise.
            </span>
          </div>
          <button
            class="upd-btn"
            disabled={reattaching || busy}
            onclick={reattachBranch}
          >
            {reattaching ? 'Reattaching…' : 'Reattach to its branch'}
          </button>
        </div>
      {/if}

      <dl class="upd-meta">
        <dt>Source</dt>
        <dd><code>{v(us?.source_version)}</code></dd>
        <dt>Installed</dt>
        <dd><code>{v(us?.installed_version)}</code></dd>
        <dt>Launcher running</dt>
        <dd><code>{v(us?.running_version)}</code></dd>
        <dt>Launcher on disk</dt>
        <dd><code>{v(us?.on_disk_binary_version)}</code></dd>
      </dl>

      <div class="upd-actions">
        <button class="upd-btn" disabled={busy} onclick={checkNow}>
          {upd.checking ? 'Checking…' : 'Check now'}
        </button>
        <button
          class="upd-btn upd-btn-primary"
          disabled={!copy.action || busy}
          onclick={requestApply}
        >
          {upd.updating ? 'Updating…' : copy.buttonLabel || 'Update now'}
        </button>
      </div>
    </section>

    <!-- v0.2.91 WP-I: the orchestrator-ROOT deferral ledger. -->
    <DeferralLedgerPanel scope="orchestrator_root" />

    <section class="upd-protected">
      <h2>Protected paths</h2>
      <p class="upd-hint">
        These paths are <strong>never</strong> overwritten by an orchestrator update.
        If you have local changes to other tracked files, the update will be blocked
        with a clear message.
      </p>
      <ul class="upd-paths">
        {#each userOwnedPaths as p}
          <li><code>{p}</code></li>
        {/each}
      </ul>
    </section>

    <section class="upd-settings">
      <h2>Settings</h2>
      <label class="upd-toggle">
        <input
          type="checkbox"
          checked={autoCheckEnabled}
          onchange={(e) => toggleAutoCheck((e.target as HTMLInputElement).checked)}
        />
        <!-- v0.2.100 (L3-F09): one preference, read by BOTH the hourly
             in-app poll and the daily background check. -->
        <span>
          Check for updates automatically (hourly while the launcher is open,
          plus a daily background check). The launcher still checks once when
          it starts.
        </span>
      </label>
      <label class="upd-toggle">
        <input
          type="checkbox"
          checked={autoRetryFailedInstalls}
          onchange={(e) => toggleAutoRetryFailedInstalls((e.target as HTMLInputElement).checked)}
        />
        <span>Automatically retry failed module installs after an orchestrator update</span>
      </label>
    </section>
  </main>

  {#if confirmingApply}
    <div class="upd-modal-backdrop" role="presentation" onclick={() => (confirmingApply = false)}>
      <div
        class="upd-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="upd-confirm-apply-heading"
        tabindex="-1"
        onclick={(e) => e.stopPropagation()}
        onkeydown={onConfirmApplyKeydown}
        use:autofocusFirstButton
      >
        <h3 id="upd-confirm-apply-heading">{copy.buttonLabel || 'Update'}?</h3>
        <p>
          This runs the orchestrator update (<code>install.py --update</code>
          and the launcher binary refresh) and restarts the launcher. Any
          unsaved work in the launcher window will be lost.
        </p>
        <p class="upd-modal-hint">
          Your <code>.claude/CONTEXT_STATE.md</code>, logs, and runtime state are protected
          and will not be touched.
        </p>
        <div class="upd-modal-actions">
          <button class="upd-btn" onclick={() => (confirmingApply = false)}>Cancel</button>
          <button class="upd-btn upd-btn-primary" onclick={confirmApply}>Continue</button>
        </div>
      </div>
    </div>
  {/if}
</div>

<Toast />

<style>
  .upd-page { min-height: 100vh; background: var(--color-bg, #0e0e16); color: var(--color-light, #e8e8ee); }
  .upd-header { display: flex; align-items: center; gap: 12px; padding: 10px 24px; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .upd-header h1 { font-size: 16px; margin: 0; }
  .upd-back { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); color: inherit; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }

  .upd-main { max-width: 720px; margin: 0 auto; padding: 16px; }
  .upd-main h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; color: #888; margin: 16px 0 8px; }
  .upd-main section { background: rgba(255,255,255,0.03); border-radius: 6px; padding: 14px; margin-bottom: 14px; }

  .upd-banner { padding: 10px 12px; border-radius: 4px; margin-bottom: 12px; display: flex; gap: 10px; align-items: center; font-size: 12px; }
  .upd-banner-ok { background: rgba(60, 180, 100, 0.1); border: 1px solid rgba(60, 180, 100, 0.3); }
  .upd-banner-warn { background: rgba(220, 170, 50, 0.12); border: 1px solid rgba(220, 170, 50, 0.4); }
  /* v0.2.92 (WP-13): "couldn't determine" gets its OWN colour. Amber, not
     green and not the red of a hard error — the check did not fail loudly,
     it failed to conclude, and the visual language has to say so. */
  .upd-banner-unknown { background: rgba(220, 140, 40, 0.12); border: 1px solid rgba(220, 140, 40, 0.45); align-items: flex-start; flex-direction: column; }
  /* Detached HEAD: purple, because it is a STATE rather than a problem —
     distinct from both "update available" (amber) and "couldn't check". */
  .upd-banner-detached { background: rgba(123, 95, 255, 0.10); border: 1px solid rgba(123, 95, 255, 0.40); align-items: flex-start; justify-content: space-between; }
  .upd-detached-text { display: flex; flex-direction: column; gap: 4px; }
  .upd-unknown { color: rgba(220, 140, 40, 0.95); font-style: italic; }
  .upd-error { padding: 10px 12px; border-radius: 4px; margin-bottom: 12px; background: rgba(220, 80, 80, 0.12); border: 1px solid rgba(220, 80, 80, 0.4); font-size: 12px; }
  .upd-empty { color: #888; font-size: 12px; margin: 0 0 12px; }

  .upd-meta { display: grid; grid-template-columns: 140px 1fr; gap: 4px 16px; font-size: 12px; margin: 0 0 12px; }
  .upd-meta dt { color: #888; }
  .upd-meta dd { margin: 0; color: #ccc; }
  .upd-meta code, .upd-banner code { background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; font-size: 11px; }

  .upd-actions { display: flex; gap: 8px; }
  .upd-btn { background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12); color: inherit; padding: 6px 14px; border-radius: 4px; cursor: pointer; font-size: 12px; }
  .upd-btn:hover:not(:disabled) { background: rgba(255,255,255,0.1); }
  .upd-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  /* P2-I9 (v0.2.91 wave 5): was an invented rgba(80,140,240,*) blue — this
     button is the CTA for "Update now" / the confirm modal's "Continue" /
     the destructive git-resync modal's "Resync now", the two most
     consequential actions on this page, in a product whose palette has
     no blue at all. Brand teal. */
  .upd-btn-primary { background: rgba(var(--color-teal-rgb), 0.2); border-color: var(--color-teal); }
  .upd-btn-primary:hover:not(:disabled) { background: rgba(var(--color-teal-rgb), 0.3); }

  .upd-hint { font-size: 11px; color: #888; margin: 0 0 8px; line-height: 1.5; }
  .upd-paths { list-style: none; padding: 0; margin: 0; font-size: 11px; }
  .upd-paths li { padding: 3px 0; color: #ccc; }
  .upd-paths code { background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; }

  .upd-toggle { display: flex; align-items: center; gap: 10px; font-size: 12px; cursor: pointer; }

  .upd-modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 100; }
  .upd-modal { background: #1a1a24; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 20px; max-width: 480px; }
  .upd-modal h3 { margin: 0 0 12px; font-size: 14px; }
  .upd-modal p { font-size: 12px; line-height: 1.6; margin: 0 0 10px; color: #ccc; }
  .upd-modal-hint { color: #888 !important; font-size: 11px !important; }
  .upd-modal code { background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; font-size: 11px; }
  .upd-modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }

  /* v0.2.35 Agent K — running-version display + binary-lag banner */
  .upd-version-line { font-size: 12px; color: #ccc; margin: 0 0 12px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .upd-version-label { color: #888; }
  .upd-version-sep { color: #555; }
  .upd-version-line code { background: rgba(255,255,255,0.06); padding: 1px 6px; border-radius: 3px; font-size: 11px; color: #e8e8ee; }

  .upd-banner-binary-lag { background: rgba(220, 130, 50, 0.12); border: 1px solid rgba(220, 130, 50, 0.45); align-items: flex-start; justify-content: space-between; flex-direction: row; padding: 10px 12px; }
  .upd-banner-binary-lag-text { display: flex; flex-direction: column; gap: 6px; line-height: 1.5; }
  .upd-banner-binary-lag-text code { background: rgba(255,255,255,0.08); padding: 1px 4px; border-radius: 3px; font-size: 11px; }
  .upd-banner-dismiss { background: transparent; border: none; color: #ccc; font-size: 16px; line-height: 1; padding: 2px 6px; cursor: pointer; border-radius: 3px; }
  .upd-banner-dismiss:hover { background: rgba(255,255,255,0.08); color: #fff; }
</style>
