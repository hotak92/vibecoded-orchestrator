<script lang="ts">
  // Launcher self-update page.
  //
  // Triggers `check_for_launcher_update` and `apply_launcher_update`
  // commands. Subscribes to the `vct-launcher-update-available` event
  // emitted by the daily background check so a check that runs while
  // this page is open updates the UI live.
  //
  // The "Update now" button shows a confirmation modal — per the spec,
  // the daily check is a notification only; install requires explicit
  // user action.

  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { invoke, listen } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';

  type UpdateStatus = {
    available: boolean;
    current_sha: string | null;
    remote_sha: string | null;
    commit_count: number;
    branch: string;
    last_checked: string | null;
    error: string | null;
  };

  // Structured payload returned by `apply_launcher_update` when the local
  // clone has diverged from upstream (post-2026-05-06 history rewrite).
  // Backend serializes this as a JSON string; we parse below.
  type NonFastForwardError = {
    kind: 'non_fast_forward';
    branch: string;
    local_sha: string | null;
    remote_sha: string | null;
    git_stderr: string;
  };

  let status = $state<UpdateStatus | null>(null);
  let checking = $state(false);
  let applying = $state(false);
  let confirmingApply = $state(false);
  let resyncing = $state(false);
  // When set, the resync modal is shown.
  let nonFastForward = $state<NonFastForwardError | null>(null);
  let userOwnedPaths = $state<string[]>([]);
  let autoCheckEnabled = $state(true);
  // Auto-retry failed module installs on orchestrator update. Backend
  // default is true; this toggle exposes the opt-out.
  let autoRetryFailedInstalls = $state(true);

  // v0.2.35 Agent K — running-version display + binary-lag warning.
  // `runningVersion` is the launcher's compile-time CARGO_PKG_VERSION
  // (always populated when running in Tauri). `latestSourceTag` is the
  // most recent release tag from `vco_upstream`, e.g. `v0.2.34` — or
  // null when no tags exist / git failed. `binaryLagDismissed` tracks
  // per-tag-version dismissal so the banner doesn't nag forever once
  // the user has acknowledged it for a given mismatch.
  let runningVersion = $state<string | null>(null);
  let latestSourceTag = $state<string | null>(null);
  let binaryLagDismissed = $state(false);

  let unlisten: (() => void) | null = null;

  /**
   * v0.2.35 Agent K — true iff the running binary's version differs
   * from the latest source release tag (after normalising the tag's
   * `v` prefix). Mirrors `running_version_lags_tag` in self_update.rs;
   * keeping a Svelte-side clone so we can render the banner without an
   * extra IPC round-trip.
   */
  function versionLagsTag(running: string | null, tag: string | null): boolean {
    if (!running || !tag) return false;
    const r = running.trim();
    const t = tag.trim().replace(/^v/, '');
    if (!r || !t) return false;
    return r !== t;
  }

  // Reactive: should we show the post-update lag banner?
  let showBinaryLagBanner = $derived(
    !binaryLagDismissed &&
      versionLagsTag(runningVersion, latestSourceTag)
  );

  /**
   * localStorage key under which we record the LATEST tag the user has
   * dismissed the banner for. Per-version so a future mismatch with a
   * different tag re-shows the warning.
   */
  const DISMISS_KEY = 'vct.updates.binary-lag-dismissed-tag';

  /**
   * Parse the error returned by `apply_launcher_update`. The backend
   * either returns a plain string (legacy / unrecognized errors) OR a
   * JSON-encoded `NonFastForwardError` for the post-rewrite divergence
   * case. We try to parse JSON first and only treat it as structured
   * when `kind === 'non_fast_forward'` — every other shape stays a
   * plain error string.
   */
  function parseUpdateError(raw: unknown): NonFastForwardError | null {
    if (typeof raw !== 'string') return null;
    if (!raw.startsWith('{')) return null;
    try {
      const parsed = JSON.parse(raw);
      if (parsed && parsed.kind === 'non_fast_forward') {
        return parsed as NonFastForwardError;
      }
    } catch {
      // not JSON — fall through to legacy string handling
    }
    return null;
  }

  async function loadCached() {
    // get_cached_update_status is non-blocking — pulls from
    // ~/.vct/launcher-update-state.json without making a network call.
    //
    // v0.2.32 E2 (2026-05-23): wrap in try/catch with console.warn so
    // browser-mode (vite dev) lands on a soft warning instead of an
    // unhandled-rejection console.error. Matches the convention used by
    // other Tauri-missing surfaces (e.g. preferences/+page.svelte's
    // get_default_embedding_models handler).
    try {
      const cached = await invoke<UpdateStatus>('get_cached_update_status');
      if (cached) status = cached;
      const paths = await invoke<string[]>('get_user_owned_paths');
      if (paths) userOwnedPaths = paths;
      const auto = await invoke<boolean>('get_auto_check_enabled');
      if (auto !== null) autoCheckEnabled = auto;
      const retry = await invoke<boolean>('get_auto_retry_failed_installs_setting');
      if (retry !== null) autoRetryFailedInstalls = retry;
    } catch (e) {
      console.warn('[updates] loadCached skipped:', e);
    }

    // v0.2.35 Agent K — load the running launcher version (always
    // available — compile-time CARGO_PKG_VERSION) and the latest
    // source release tag (network/git op, soft-fails to null). These
    // are independent of the cached-status block above so a stale-cache
    // case still renders the version line correctly.
    try {
      const rv = await invoke<string>('get_launcher_running_version');
      if (rv) runningVersion = rv;
    } catch (e) {
      console.warn('[updates] get_launcher_running_version skipped:', e);
    }
    try {
      const tag = await invoke<string | null>('get_latest_source_release_tag');
      latestSourceTag = tag ?? null;
    } catch (e) {
      // No-git or no-tags is the common path; warn quietly so the
      // banner just stays hidden rather than producing an error toast
      // the user has no action for.
      console.warn('[updates] get_latest_source_release_tag skipped:', e);
      latestSourceTag = null;
    }

    // Apply per-tag dismissal. If the user previously dismissed the
    // banner for the SAME tag we're showing now, keep it hidden;
    // otherwise reset so a freshly-detected lag pops back up.
    try {
      const dismissedFor = localStorage.getItem(DISMISS_KEY);
      binaryLagDismissed =
        dismissedFor !== null &&
        latestSourceTag !== null &&
        dismissedFor === latestSourceTag;
    } catch {
      // localStorage can throw in restricted browsers; just default
      // to "not dismissed" — the user can dismiss again if needed.
      binaryLagDismissed = false;
    }
  }

  /**
   * v0.2.35 Agent K — record per-tag dismissal so the banner stays
   * hidden until the next mismatch arises (typically a new release
   * tag, possibly with the same CI-lag situation).
   */
  function dismissBinaryLagBanner() {
    binaryLagDismissed = true;
    try {
      if (latestSourceTag) {
        localStorage.setItem(DISMISS_KEY, latestSourceTag);
      }
    } catch {
      // localStorage unavailable — fine, the in-memory flag still
      // hides it for this session.
    }
  }

  async function checkNow() {
    checking = true;
    try {
      const result = await invoke<UpdateStatus>('check_for_launcher_update');
      if (result) {
        status = result;
        if (result.error) {
          toast.error(result.error);
        } else if (result.available) {
          toast.success(`Update available: ${result.commit_count} commit${result.commit_count === 1 ? '' : 's'} behind`);
        } else {
          toast.success('Launcher is up to date');
        }
      }
    } catch (e) {
      toast.error(String(e));
    } finally {
      checking = false;
    }
  }

  async function applyUpdate() {
    confirmingApply = false;
    applying = true;
    try {
      // This call doesn't return on success — the launcher restarts.
      // If we DO get back here, something failed before the restart.
      await invoke<void>('apply_launcher_update');
      toast.success('Launcher will restart…');
    } catch (e) {
      // Detect the post-history-rewrite divergence case. If the backend
      // returned a structured non-FF error we open the resync modal
      // instead of just toasting an opaque message.
      const nff = parseUpdateError(e);
      if (nff) {
        nonFastForward = nff;
      } else {
        toast.error(`Update failed: ${e}`);
      }
    } finally {
      applying = false;
    }
  }

  async function resyncNow() {
    if (!nonFastForward) return;
    resyncing = true;
    try {
      // Like apply_launcher_update, this command doesn't return on
      // success — the launcher restarts after rebuild.
      await invoke<void>('force_resync_launcher');
      toast.success('Launcher will restart…');
      nonFastForward = null;
    } catch (e) {
      toast.error(`Resync failed: ${e}`);
    } finally {
      resyncing = false;
    }
  }

  async function toggleAutoCheck(enabled: boolean) {
    autoCheckEnabled = enabled;
    try {
      await invoke('set_auto_check_enabled', { enabled });
      toast.success(enabled ? 'Auto-check enabled' : 'Auto-check disabled');
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

  onMount(async () => {
    await loadCached();
    unlisten = await listen<UpdateStatus>('vct-launcher-update-available', (e) => {
      status = e.payload;
    });
  });

  onDestroy(() => {
    if (unlisten) unlisten();
  });

  function shortSha(sha: string | null): string {
    return sha ? sha.slice(0, 7) : '—';
  }

  function formatTime(iso: string | null): string {
    if (!iso) return 'never';
    try {
      const d = new Date(iso);
      return d.toLocaleString();
    } catch {
      return iso;
    }
  }

  // v0.2.35 (a11y, Agent O): keyboard support for the custom modals on
  // this page. The two confirm/resync modals were rolled by hand (not
  // via DialogRoot's native <dialog>), so they lacked native Escape
  // handling. Wire Escape → close on the modal containers. Also handle
  // focus restoration: when a modal opens we autofocus its first
  // actionable button so the keyboard user can act immediately and so
  // SR users land inside the dialog.
  function onConfirmApplyKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape' && !applying) {
      e.preventDefault();
      confirmingApply = false;
    }
    e.stopPropagation();
  }
  function onResyncKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape' && !resyncing) {
      e.preventDefault();
      nonFastForward = null;
    }
    e.stopPropagation();
  }
  function autofocusFirstButton(el: HTMLDivElement) {
    // After the modal mounts, move keyboard focus into the dialog so
    // it lands on the first interactive control (typically the Cancel
    // button — same position the native <dialog> would default to via
    // showModal()'s focus trap).
    queueMicrotask(() => {
      const btn = el.querySelector<HTMLButtonElement>('button');
      btn?.focus();
    });
  }
</script>

<svelte:head>
  <title>Launcher updates — VCT Launcher</title>
</svelte:head>

<div class="upd-page">
  <header class="upd-header">
    <button class="upd-back" onclick={() => goto('/preferences')}>← Back</button>
    <h1>Launcher updates</h1>
  </header>

  <main class="upd-main">
    <section class="upd-status">
      <h2>Status</h2>

      <!--
        v0.2.35 Agent K — running-version display.
        Always visible (when we have data) so the user can spot a
        binary-lag situation at a glance without clicking anything.
        Sits above the "available / up to date / error" banner so the
        eye lands on it first when the page opens.
      -->
      {#if runningVersion}
        <p class="upd-version-line">
          <span class="upd-version-label">Running:</span>
          <code>v{runningVersion}</code>
          {#if latestSourceTag}
            <span class="upd-version-sep">|</span>
            <span class="upd-version-label">Latest source release:</span>
            <code>{latestSourceTag}</code>
          {/if}
        </p>
      {/if}

      <!--
        v0.2.35 Agent K — post-update binary-lag banner.
        Lights up when `running_version` (compile-time CARGO_PKG_VERSION
        of the launcher we're inside) doesn't match the latest source
        release tag. Almost always means: user clicked "Update orchestrator"
        right after a tag pushed but BEFORE CI's `chore(binary):` commit
        landed, so the binary on disk is the previous release's.
        Dismissible per-tag via localStorage so it doesn't nag once
        acknowledged for a given mismatch.
      -->
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
              binary. Click <strong>Update now</strong> again in
              5-10 minutes to pick up the matching <code>{latestSourceTag}</code>
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

      {#if status?.error}
        <div class="upd-error">
          <strong>Check failed:</strong> {status.error}
        </div>
      {:else if status?.available}
        <div class="upd-banner upd-banner-warn">
          <strong>⚠ Update available</strong>
          <span>
            {status.commit_count} commit{status.commit_count === 1 ? '' : 's'} behind on
            <code>{status.branch || 'main'}</code>
          </span>
        </div>
      {:else if status}
        <div class="upd-banner upd-banner-ok">
          <strong>✓ Up to date</strong>
        </div>
      {:else}
        <p class="upd-empty">Click "Check now" to query the remote.</p>
      {/if}

      <dl class="upd-meta">
        <dt>Current</dt>
        <dd><code>{shortSha(status?.current_sha ?? null)}</code></dd>
        <dt>Remote</dt>
        <dd><code>{shortSha(status?.remote_sha ?? null)}</code></dd>
        <dt>Branch</dt>
        <dd><code>{status?.branch || '—'}</code></dd>
        <dt>Commits behind</dt>
        <dd>{status?.commit_count ?? 0}</dd>
        <dt>Last checked</dt>
        <dd>{formatTime(status?.last_checked ?? null)}</dd>
      </dl>

      <div class="upd-actions">
        <button class="upd-btn" disabled={checking || applying} onclick={checkNow}>
          {checking ? 'Checking…' : 'Check now'}
        </button>
        <button
          class="upd-btn upd-btn-primary"
          disabled={!status?.available || applying || checking}
          onclick={() => (confirmingApply = true)}
        >
          {applying ? 'Updating…' : 'Update now'}
        </button>
      </div>
    </section>

    <section class="upd-protected">
      <h2>Protected paths</h2>
      <p class="upd-hint">
        These paths are <strong>never</strong> overwritten by the launcher self-update.
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
        <span>Check for updates automatically once per day</span>
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

  <!-- v0.2.35 (a11y, Agent O): the two custom modals below were rolled
       by hand (not via DialogRoot's native <dialog>), so they lacked:
       (1) aria-labelledby pointing at the heading,
       (2) keyboard Escape handling,
       (3) initial focus management when opening.
       Each is fixed surgically below — see autofocusFirstButton + the
       per-modal keydown handlers added in the script block. -->
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
        <h3 id="upd-confirm-apply-heading">Update launcher?</h3>
        <p>
          This will pull the latest changes from <code>{status?.branch || 'main'}</code>,
          rebuild the launcher, and restart it. Any unsaved work in the launcher window
          will be lost.
        </p>
        <p class="upd-modal-hint">
          Your <code>.claude/CONTEXT_STATE.md</code>, logs, and runtime state are protected
          and will not be touched.
        </p>
        <div class="upd-modal-actions">
          <button class="upd-btn" onclick={() => (confirmingApply = false)}>Cancel</button>
          <button class="upd-btn upd-btn-primary" onclick={applyUpdate}>Continue</button>
        </div>
      </div>
    </div>
  {/if}

  {#if nonFastForward}
    <div
      class="upd-modal-backdrop"
      role="presentation"
      onclick={() => {
        if (!resyncing) nonFastForward = null;
      }}
    >
      <div
        class="upd-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="upd-resync-heading"
        tabindex="-1"
        onclick={(e) => e.stopPropagation()}
        onkeydown={onResyncKeydown}
        use:autofocusFirstButton
      >
        <h3 id="upd-resync-heading">Local clone diverged from upstream</h3>
        <p>
          Your local copy can't fast-forward to the latest version because history
          has diverged (likely because we rewrote git history on 2026-05-06 to remove
          internal docs from older commits).
        </p>
        <p>
          <strong>Resyncing will discard any tracked-file changes you've made locally.</strong>
          Untracked files (your projects, <code>.env</code>, <code>state/</code>, <code>~/.vct/</code>)
          are safe.
        </p>
        <p class="upd-modal-hint">
          Want to back up first? See <code>docs/RECOVERY-2026-05-06.md</code>.
        </p>
        <dl class="upd-meta">
          <dt>Branch</dt>
          <dd><code>{nonFastForward.branch}</code></dd>
          <dt>Local</dt>
          <dd><code>{shortSha(nonFastForward.local_sha)}</code></dd>
          <dt>Remote</dt>
          <dd><code>{shortSha(nonFastForward.remote_sha)}</code></dd>
        </dl>
        <div class="upd-modal-actions">
          <button
            class="upd-btn"
            disabled={resyncing}
            onclick={() => (nonFastForward = null)}
          >
            Cancel
          </button>
          <button
            class="upd-btn upd-btn-primary"
            disabled={resyncing}
            onclick={resyncNow}
          >
            {resyncing ? 'Resyncing…' : 'Resync now'}
          </button>
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
  .upd-btn-primary { background: rgba(80, 140, 240, 0.2); border-color: rgba(80, 140, 240, 0.5); }
  .upd-btn-primary:hover:not(:disabled) { background: rgba(80, 140, 240, 0.3); }

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
