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
  // v0.2.91 WP-I (decision #6) — the GLOBAL deferral ledger lives here, on the
  // page that already owns install-wide state (self-update, binary lag,
  // protected paths). Per-project entries deliberately do NOT appear here;
  // each project's ledger renders on its own Settings tab.
  import DeferralLedgerPanel from '$lib/components/DeferralLedgerPanel.svelte';
  import {
    renderCheck,
    checkError,
    type CheckState,
    type InstallProgress,
  } from '$lib/stores/orchestrator';
  // v0.2.93 (F): the card re-checks itself when an orchestrator update op
  // finishes (falling edge of `$updater.updating`), when an
  // `install_progress` "done" stage arrives, and when the window regains
  // focus after one of those — the cached status is stale the moment an
  // update lands, and the old page kept showing "Update available".
  import { updater } from '$lib/stores/updater';
  import { parseTaggedErrorPayload } from '$lib/tauri-error-payload';

  /** Mirror of Rust `self_update::UpdateStatus`. */
  type UpdateStatus = {
    /** Only meaningful when `remote_check.state === 'ok'`. */
    available: boolean;
    current_sha: string | null;
    remote_sha: string | null;
    /** Only meaningful when `remote_check.state === 'ok'`. */
    commit_count: number;
    /** Normalised — never the literal `"HEAD"`. See `head_detached`. */
    branch: string;
    /** v0.2.92 (WP-13). */
    head_detached: boolean;
    /** v0.2.92 (WP-13): what the remote-currency probe established. */
    remote_check: CheckState;
    /** v0.2.92 (WP-13): what the release-tag probe established. */
    latest_source_release_check: CheckState;
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
  // v0.2.92 (WP-13): whether the release-tag lookup SUCCEEDED, which is a
  // different question from whether it returned a tag. Pre-fix the two were
  // conflated into `latestSourceTag = null`, so a failed lookup and a
  // tagless remote both silently hid the line.
  let latestTagLookupFailed = $state(false);
  // v0.2.92 (WP-13): reattach affordance state.
  let reattaching = $state(false);

  let unlisten: (() => void) | null = null;
  let unlistenProgress: (() => void) | null = null;
  // v0.2.93 (F): true once the cached-status load has resolved. Before
  // that the "Current" cell shows a loading placeholder, so "—" is
  // reserved for the case where the backend TRULY returned null.
  let loaded = $state(false);
  // v0.2.93 (F): an update-class op ended while this page was open but the
  // window was unfocused; consumed by the next window focus.
  let recheckPending = false;
  let prevUpdating = false;

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
    // v0.2.93: the shared tolerant parser — no `startsWith('{')` brittleness.
    return parseTaggedErrorPayload<NonFastForwardError>(raw, 'kind', 'non_fast_forward');
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
      // v0.2.93 (review R1 #7): the repo-aware variant fills current_sha /
      // branch and retracts "N behind" when HEAD already contains the cached
      // remote SHA; it runs its git reads off-thread. The tray keeps the
      // pure-cache `get_cached_update_status`.
      const cached = await invoke<UpdateStatus>('get_cached_update_status_refreshed');
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
      latestTagLookupFailed = false;
    } catch (e) {
      // v0.2.92 (WP-13): an ERROR here is not "no tags". The Rust command
      // now distinguishes `Ok(None)` (the remote genuinely has no tags) from
      // `Err` (we could not ask), and this page must too — otherwise a
      // failed lookup renders identically to a healthy repo with nothing to
      // report, which is the shape of the whole incident.
      console.warn('[updates] get_latest_source_release_tag failed:', e);
      latestSourceTag = null;
      latestTagLookupFailed = true;
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

  async function checkNow(opts: { silent?: boolean } = {}) {
    if (checking) return;
    checking = true;
    try {
      const result = await invoke<UpdateStatus>('check_for_launcher_update');
      if (result) {
        status = result;
        loaded = true;
        // v0.2.93 (F): automatic re-checks (after an update op / on focus)
        // refresh the card silently — toasts are for the user's own click.
        if (opts.silent) return;
        // v0.2.92 (WP-13): the toast follows the tri-state, in this order.
        // "Launcher is up to date" is now reachable ONLY from a check that
        // actually completed. Pre-fix it was the else-branch of
        // `result.available`, so every failed check congratulated the user.
        const check = renderCheck(result.remote_check);
        if (result.error) {
          toast.error(result.error);
        } else if (check === 'unknown') {
          const why = checkError(result.remote_check);
          toast.error(
            why
              ? `Couldn't check for updates — ${why}`
              : "Couldn't check for updates",
          );
        } else if (check === 'not_applicable') {
          toast.success('No git remote to check on this install');
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

  /**
   * v0.2.92 (WP-13): return HEAD to its branch.
   *
   * The one GUI path out of a detached HEAD. Guarded entirely in Rust
   * (`reattach_orchestrator_branch`: detached + clean tree + the commit is an
   * upstream ancestor); this handler only relays the refusal, verbatim,
   * because the refusal text names what the user has to do.
   */
  async function reattachBranch() {
    reattaching = true;
    try {
      const branch = await invoke<string>('reattach_orchestrator_branch');
      toast.success(`Reattached to ${branch}`);
      await checkNow();
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
    loaded = true;
    unlisten = await listen<UpdateStatus>('vct-launcher-update-available', (e) => {
      status = e.payload;
    });
    // v0.2.93 (F): an `install_progress` "done" stage means an orchestrator
    // update / install just landed — the cached status is stale; re-check.
    unlistenProgress = await listen<InstallProgress>('install_progress', (e) => {
      if (e.payload?.stage === 'done') void recheckAfterUpdate();
    });
  });

  onDestroy(() => {
    if (unlisten) unlisten();
    if (unlistenProgress) unlistenProgress();
  });

  // v0.2.93 (F): falling edge of an update-class op (`$updater.updating`
  // true → false) — the card is stale. Re-check now if the window has
  // focus; otherwise remember, and re-check on the next focus.
  $effect(() => {
    const isUpdating = $updater.updating;
    if (prevUpdating && !isUpdating) {
      recheckPending = true;
      if (typeof document === 'undefined' || document.hasFocus()) {
        void recheckAfterUpdate();
      }
    }
    prevUpdating = isUpdating;
  });

  async function recheckAfterUpdate() {
    recheckPending = false;
    await checkNow({ silent: true });
  }

  function handleWindowFocus() {
    if (recheckPending) void recheckAfterUpdate();
  }

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

<!-- v0.2.93 (F): re-check when the window regains focus after an update op
     ended while it was unfocused (see `handleWindowFocus`). -->
<svelte:window onfocus={handleWindowFocus} />

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
          <!-- v0.2.92 (WP-13): three renderings, because there are three
               answers. This line is where the incident hid: the tag came from
               `git describe` (closest tag reachable FROM HEAD), so an install
               detached on its own release tag read
               `Running: v0.2.88 | Latest source release: v0.2.88` — a
               truthful answer to a question nobody asked, and the only place
               the five-week gap could have been seen. The tag now comes from
               the REMOTE, and a failed lookup says so instead of vanishing. -->
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

      <!-- v0.2.92 (WP-13): "✓ Up to date" is reachable ONLY from a check
           that completed. Pre-fix it was the final `else` — so a repo whose
           behind-count could not be computed at all landed here, in green,
           with a tick. -->
      {#if status?.error}
        <div class="upd-error">
          <strong>Check failed:</strong> {status.error}
        </div>
      {:else if status && !status.last_checked}
        <!-- Nothing has been checked yet on this install (the cached status
             reports `unknown` for exactly this reason). Show the neutral
             prompt rather than the amber "couldn't check" — the check has not
             failed, it has not run. Both are honest; this one is also
             actionable, and it is what a first launch should say. -->
        <p class="upd-empty">No check has run yet — click "Check now" to query the remote.</p>
      {:else if status && renderCheck(status.remote_check) === 'unknown'}
        <div class="upd-banner upd-banner-unknown">
          <strong>⚠ Couldn't check for updates</strong>
          <span>
            This is <em>not</em> "up to date" — the launcher could not
            determine whether new commits exist.
            {#if checkError(status.remote_check)}
              <br />git said: <code>{checkError(status.remote_check)}</code>
            {/if}
          </span>
        </div>
      {:else if status && renderCheck(status.remote_check) === 'not_applicable'}
        <div class="upd-banner upd-banner-unknown">
          <strong>No remote to check</strong>
          <span>
            This install is not a git checkout, so there is no upstream to
            compare against.
          </span>
        </div>
      {:else if status?.available && status.commit_count > 0}
        <!-- v0.2.93 (F): a cached `available: true` with `commit_count: 0`
             is a stale/contradictory record (the count is the verdict) —
             it falls through to "Up to date" below, never to this banner. -->
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

      <!-- v0.2.92 (WP-13): detached HEAD, named and actionable.
           Before this, the page printed `Branch: HEAD` (self-update surface)
           or `Branch: main` (installer surface) and there was no in-GUI way
           back to a branch at all — every other `git checkout` in the
           launcher is path-scoped and cannot move HEAD, so a GUI-first user
           was told to open a terminal. -->
      {#if status?.head_detached}
        <div class="upd-banner upd-banner-detached">
          <div class="upd-detached-text">
            <strong>Detached HEAD — this clone is not on a branch</strong>
            <span>
              Update checks compare against
              <code>vco_upstream/{status.branch || 'main'}</code> and updates
              still apply, but the clone stays detached afterwards. Reattaching
              is safe when the working tree is clean and your current commit is
              already contained in the upstream branch; the button refuses (and
              says why) otherwise.
            </span>
          </div>
          <button
            class="upd-btn"
            disabled={reattaching || checking || applying}
            onclick={reattachBranch}
          >
            {reattaching ? 'Reattaching…' : `Reattach to ${status.branch || 'main'}`}
          </button>
        </div>
      {/if}

      <dl class="upd-meta">
        <dt>Current</dt>
        <!-- v0.2.93 (F): "—" ONLY when the backend truly returned null;
             before the cached load resolves, a loading placeholder. -->
        <dd><code>{loaded ? shortSha(status?.current_sha ?? null) : '…'}</code></dd>
        <dt>Remote</dt>
        <dd><code>{shortSha(status?.remote_sha ?? null)}</code></dd>
        <dt>Branch</dt>
        <dd>
          <code>{status?.branch || '—'}</code>
          {#if status?.head_detached}
            <span class="upd-unknown">(detached HEAD)</span>
          {/if}
        </dd>
        <dt>Commits behind</dt>
        <dd>
          <!-- v0.2.92 (WP-13): the count is a VERDICT and only exists when
               the probe completed. Rendering the sentinel `0` for a failed
               probe is precisely how a crashed `rev-list` came to read as
               "you are current". -->
          {#if status && renderCheck(status.remote_check) === 'ok'}
            {status.commit_count}
          {:else if status}
            <span class="upd-unknown">unknown</span>
          {:else}
            —
          {/if}
        </dd>
        <dt>Last checked</dt>
        <dd>{formatTime(status?.last_checked ?? null)}</dd>
      </dl>

      <div class="upd-actions">
        <button class="upd-btn" disabled={checking || applying} onclick={() => checkNow()}>
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

    <!-- v0.2.91 WP-I: the orchestrator-ROOT deferral ledger. Clearly global:
         it sits between the install's own update state and its protected
         paths, and its own header + folder line name the scope. -->
    <DeferralLedgerPanel scope="orchestrator_root" />

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
