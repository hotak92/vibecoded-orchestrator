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

  let unlisten: (() => void) | null = null;

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
    const cached = await invoke<UpdateStatus>('get_cached_update_status');
    if (cached) status = cached;
    const paths = await invoke<string[]>('get_user_owned_paths');
    if (paths) userOwnedPaths = paths;
    const auto = await invoke<boolean>('get_auto_check_enabled');
    if (auto !== null) autoCheckEnabled = auto;
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
</script>

<div class="upd-page">
  <header class="upd-header">
    <button class="upd-back" onclick={() => goto('/preferences')}>← Back</button>
    <h1>Launcher updates</h1>
  </header>

  <main class="upd-main">
    <section class="upd-status">
      <h2>Status</h2>

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
    </section>
  </main>

  {#if confirmingApply}
    <div class="upd-modal-backdrop" onclick={() => (confirmingApply = false)}>
      <div class="upd-modal" onclick={(e) => e.stopPropagation()}>
        <h3>Update launcher?</h3>
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
      onclick={() => {
        if (!resyncing) nonFastForward = null;
      }}
    >
      <div class="upd-modal" onclick={(e) => e.stopPropagation()}>
        <h3>Local clone diverged from upstream</h3>
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
</style>
