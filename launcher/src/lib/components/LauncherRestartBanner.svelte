<script lang="ts">
  // v0.2.15 (Agent D, 2026-05-17): launcher self-restart UX after binary swap.
  //
  // When the user clicks "Update orchestrator", install.py runs and (when the
  // launcher binary at launcher/dist/<arch>/vct-launcher[.exe] gets refreshed)
  // emits a `launcher_restart_required` deferral entry. On Linux/macOS the
  // running launcher is happily executing the OLD binary in memory; on
  // Windows install.py's rename-fallback handles the locked-file case OR
  // emits `launcher_binary_swap_failed_locked` when even rename fails.
  //
  // This banner reads `<install_root>/.claude/context/UPDATE_DEFERRED.md` on
  // mount and every 5s thereafter so it picks up entries written by
  // background install runs without page reload. Two rendering paths:
  //
  //   - `restart_required` (green banner): one-click "Restart now" button
  //     invokes `restart_launcher` Tauri command, which spawns the new
  //     binary detached + exits the current process. The same command also
  //     clears the entry from UPDATE_DEFERRED.md so the next launcher
  //     start doesn't re-render the banner.
  //
  //   - `swap_failed_locked` (red banner, Windows-only): inline recovery
  //     instructions ("fully quit launcher, re-run install.py from terminal,
  //     relaunch"). No auto-action — the launcher process holding the lock
  //     IS this one, so we can't fix it from within.
  //
  // Mounted globally in routes/+layout.svelte so it's visible regardless of
  // which page the user is on when install.py finishes.

  import { onDestroy, onMount } from 'svelte';
  import { invoke, safeInvoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import { orchestrator } from '$lib/stores/orchestrator';

  //   - `binary_stale` (amber banner, v0.2.91 WP-A/WI-1): detected AT REST by
  //     the boot / update-check freshness probe — the binary on disk is not
  //     the binary this process is executing (a frozen exe over advanced
  //     source; the field failure that produced a launcher stuck two releases
  //     behind with no code path able to repair it). Deliberately has NO
  //     action button: replacing a running executable is the user's call
  //     (standing no-auto-restart ruling), so the banner states the two
  //     versions and the one manual action instead of pretending to fix it.

  interface LauncherRestartStatus {
    restart_required: boolean;
    swap_failed_locked: boolean;
    new_version: string | null;
    new_binary_path: string | null;
    failure_detail: string | null;
    // v0.2.91 WI-1 surfacing.
    binary_stale: boolean;
    stale_running_version: string | null;
    stale_on_disk_version: string | null;
    stale_detail: string | null;
  }

  let status = $state<LauncherRestartStatus | null>(null);
  let restarting = $state(false);
  let dismissed = $state(false);
  let installPath = $state<string>('');
  let pollHandle: ReturnType<typeof setInterval> | null = null;

  // Track install path from the orchestrator store so we always poll the
  // correct UPDATE_DEFERRED.md when the user reconfigures their install.
  const unsubOrchestrator = orchestrator.subscribe((s) => {
    installPath = s.installPath || '';
  });

  async function poll() {
    if (!installPath) {
      // No known install path — banner not applicable. The orchestrator
      // store usually resolves this within the first 1-2 seconds of
      // app start; we poll lazily so first-render isn't blocked.
      return;
    }
    try {
      const next = await safeInvoke<LauncherRestartStatus | null>(
        'get_launcher_restart_status',
        { installRoot: installPath },
      );
      if (next) {
        // If status moved from "something" -> "nothing", auto-undismiss
        // for the next time an entry appears.
        if (!next.restart_required && !next.swap_failed_locked && !next.binary_stale) {
          status = next;
          dismissed = false;
        } else {
          status = next;
        }
      }
    } catch (e) {
      // Don't toast — banner polling failures should not interrupt the user.
      console.warn('[LauncherRestartBanner] poll failed:', e);
    }
  }

  async function restartNow() {
    if (restarting || !installPath) return;
    restarting = true;
    try {
      // This call doesn't return on success — the launcher exits after
      // spawning the new process. If we DO get back here, something failed
      // before the restart actually took effect.
      await invoke<void>('restart_launcher', { installRoot: installPath });
      toast.success('Restarting launcher...');
    } catch (e) {
      toast.error(`Restart failed: ${e}`);
      restarting = false;
    }
  }

  onMount(() => {
    void poll();
    pollHandle = setInterval(() => void poll(), 5_000);
  });

  onDestroy(() => {
    if (pollHandle !== null) clearInterval(pollHandle);
    unsubOrchestrator();
  });

  let visible = $derived.by(() => {
    if (!status || dismissed) return false;
    return status.restart_required || status.swap_failed_locked || status.binary_stale;
  });
</script>

{#if visible && status}
  {#if status.swap_failed_locked}
    <!-- Red banner: Windows-only path. Binary swap failed because the
         launcher .exe is held open. Cannot self-recover; user must
         fully quit + re-run install from terminal. -->
    <div class="lrb-banner lrb-locked" role="alert" aria-live="assertive">
      <div class="lrb-row">
        <span class="lrb-glyph" aria-hidden="true">!</span>
        <div class="lrb-text">
          <div class="lrb-label">
            Launcher binary update blocked — file locked
          </div>
          <div class="lrb-detail">
            Windows refused to overwrite the launcher .exe because this process
            holds it open. Manual recovery required:
          </div>
          <ol class="lrb-steps">
            <li>Fully quit the launcher (tray icon → Quit, NOT just close the window).</li>
            <li>
              From a terminal in <code>{installPath}</code>, run:
              <code class="lrb-cmd">python install.py --update</code>
            </li>
            <li>Relaunch the launcher via your usual entrypoint.</li>
          </ol>
          {#if status.failure_detail}
            <details class="lrb-details">
              <summary>Show diagnostic detail</summary>
              <pre class="lrb-pre">{status.failure_detail}</pre>
            </details>
          {/if}
        </div>
        <div class="lrb-actions">
          <button
            type="button"
            class="lrb-btn-x"
            aria-label="Dismiss banner"
            onclick={() => (dismissed = true)}
          >×</button>
        </div>
      </div>
    </div>
  {:else if status.restart_required}
    <!-- Green banner: happy path. New binary is on disk; one click to
         restart and load it. -->
    <div class="lrb-banner lrb-restart" role="status" aria-live="polite">
      <div class="lrb-row">
        <span class="lrb-glyph" aria-hidden="true">↻</span>
        <div class="lrb-text">
          <div class="lrb-label">
            {#if status.new_version}
              Launcher {status.new_version} ready — restart to apply
            {:else}
              New launcher binary ready — restart to apply
            {/if}
          </div>
          <div class="lrb-detail">
            The orchestrator update just refreshed the launcher binary. You're
            still running the previous version in memory until you restart.
            {#if status.new_binary_path}
              <span class="lrb-path">New binary: <code>{status.new_binary_path}</code></span>
            {/if}
          </div>
        </div>
        <div class="lrb-actions">
          <button
            type="button"
            class="lrb-btn-primary"
            onclick={restartNow}
            disabled={restarting || !installPath}
          >
            {restarting ? 'Restarting…' : 'Restart now'}
          </button>
          <button
            type="button"
            class="lrb-btn-secondary"
            onclick={() => (dismissed = true)}
            disabled={restarting}
          >Later</button>
        </div>
      </div>
    </div>
  {:else if status.binary_stale}
    <!-- Amber banner (v0.2.91 WI-1): the running process is behind the
         binary on disk. No button by design — the launcher does not replace
         its own running executable, and it never restarts or quits itself
         (standing ruling). The honest statement IS the fix path. -->
    <div class="lrb-banner lrb-stale" role="status" aria-live="polite">
      <div class="lrb-row">
        <span class="lrb-glyph" aria-hidden="true">⌛</span>
        <div class="lrb-text">
          <div class="lrb-label">
            {#if status.stale_running_version && status.stale_on_disk_version}
              Running launcher {status.stale_running_version} — {status.stale_on_disk_version}
              is on disk
            {:else}
              The launcher on disk is newer than the one running
            {/if}
          </div>
          <div class="lrb-detail">
            Quit the launcher completely (tray icon → Quit) and start it again to
            load it. Nothing restarts on its own, and closing the window to the
            tray is not enough — the process has to exit.
            {#if status.stale_detail}
              <details class="lrb-details">
                <summary>Show what was detected</summary>
                <pre class="lrb-pre">{status.stale_detail}</pre>
              </details>
            {/if}
          </div>
        </div>
        <div class="lrb-actions">
          <button
            type="button"
            class="lrb-btn-x"
            aria-label="Dismiss banner"
            onclick={() => (dismissed = true)}
          >×</button>
        </div>
      </div>
    </div>
  {/if}
{/if}

<style>
  /* Visually consistent with KgSyncBanner / CodeGraphBuildBanner — same
     row layout, same glyph, same button shapes. Color palette differs
     per condition (green for restart-ready, red for locked-file). */

  .lrb-banner {
    display: block;
    border-bottom: 1px solid transparent;
    font-size: 13px;
    line-height: 1.4;
  }

  .lrb-row {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 10px 24px;
  }

  .lrb-glyph {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    font-family: ui-monospace, monospace;
    font-size: 14px;
    font-weight: 700;
    flex-shrink: 0;
    margin-top: 1px;
  }

  .lrb-text { flex: 1; min-width: 0; }
  .lrb-label { font-weight: 600; }
  .lrb-detail {
    font-size: 12px;
    color: rgba(255,255,255,0.65);
    margin-top: 3px;
  }
  .lrb-path { display: block; margin-top: 2px; }
  .lrb-path code {
    background: rgba(0,0,0,0.18);
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 11px;
  }

  .lrb-actions {
    display: flex;
    gap: 8px;
    flex-shrink: 0;
  }

  .lrb-btn-primary, .lrb-btn-secondary {
    padding: 5px 14px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    border: 1px solid transparent;
    font-family: inherit;
  }
  .lrb-btn-primary {
    background: rgb(70, 200, 120);
    color: #002a13;
  }
  .lrb-btn-primary:hover:not(:disabled) { background: rgb(90, 220, 140); }
  .lrb-btn-primary:disabled { opacity: 0.5; cursor: default; }

  .lrb-btn-secondary {
    background: rgba(255,255,255,0.06);
    color: #ccc;
    border-color: rgba(255,255,255,0.12);
  }
  .lrb-btn-secondary:hover:not(:disabled) { background: rgba(255,255,255,0.1); }
  .lrb-btn-secondary:disabled { opacity: 0.5; cursor: default; }

  .lrb-btn-x {
    background: none; border: none; color: inherit;
    font-size: 18px; line-height: 1; cursor: pointer;
    padding: 0 8px; border-radius: 6px;
    opacity: 0.6;
  }
  .lrb-btn-x:hover { opacity: 1; background: rgba(255,255,255,0.06); }

  .lrb-steps {
    margin: 6px 0 0;
    padding-left: 20px;
    font-size: 12px;
    color: rgba(255,255,255,0.75);
  }
  .lrb-steps li { margin-bottom: 4px; }
  .lrb-steps code {
    background: rgba(0,0,0,0.20);
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 11px;
  }
  .lrb-cmd {
    display: inline-block;
    margin-left: 4px;
  }

  .lrb-details {
    margin-top: 6px;
    font-size: 11px;
  }
  .lrb-details summary {
    cursor: pointer;
    color: rgba(255,255,255,0.55);
  }
  .lrb-pre {
    margin: 4px 0 0;
    padding: 6px 8px;
    background: rgba(0,0,0,0.20);
    border-radius: 4px;
    font-family: ui-monospace, monospace;
    font-size: 11px;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 160px;
    overflow-y: auto;
  }

  .lrb-restart {
    background: rgba(70, 200, 120, 0.10);
    border-bottom-color: rgba(70, 200, 120, 0.35);
    color: rgb(140, 230, 175);
  }
  .lrb-restart .lrb-glyph {
    background: rgba(70, 200, 120, 0.25);
    color: rgb(140, 230, 175);
  }

  /* v0.2.91 WI-1: amber — informational-but-persistent. Distinct from the
     green "one click and you're done" and the red "recovery required". */
  .lrb-stale {
    background: rgba(240, 180, 60, 0.10);
    border-bottom-color: rgba(240, 180, 60, 0.35);
    color: rgb(240, 205, 130);
  }
  .lrb-stale .lrb-glyph {
    background: rgba(240, 180, 60, 0.22);
    color: rgb(245, 210, 140);
  }

  .lrb-locked {
    background: rgba(255, 79, 80, 0.10);
    border-bottom-color: rgba(255, 79, 80, 0.40);
    color: rgb(255, 150, 150);
  }
  .lrb-locked .lrb-glyph {
    background: rgba(255, 79, 80, 0.22);
    color: rgb(255, 160, 160);
  }
</style>
