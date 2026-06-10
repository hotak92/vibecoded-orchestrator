<script lang="ts">
  // InstallHealthGate — blocking modal that fires when the launcher binary
  // is running from inside an orchestrator install root, but the install
  // never actually completed (no .venv/, no state/, no .env with
  // KG_COLLECTION, or no claude_mcp_servers/.venv/).
  //
  // Concern this guards: a user downloads the launcher .exe directly from
  // a GitHub Release asset and skips first-install.{bat,sh,command}. The
  // .exe alone has no Python, no Docker stack, no MCP registration — the
  // app would silently fail. This modal explains the situation up front
  // and points the user at the install script.
  //
  // Source of truth for detection: `check_install_health` in
  // src-tauri/src/commands/installer.rs. This component is purely
  // presentational — all probes happen in Rust. Developer mode (no install
  // root found by walking up from current_exe()) returns `all_ok: true`,
  // so the modal never fires for `cargo run` / `pnpm tauri dev`.
  //
  // Persistence: the secondary "let me through" button writes
  // `vct.install_check_dismissed=true` to localStorage so the user is not
  // re-prompted on every subsequent launch.

  import { onMount, onDestroy } from 'svelte';
  import { invoke, tauriAvailable } from '$lib/tauri';
  import type { InstallHealth } from '$lib/types/launcher';
  import {
    getInstallScopedFlag,
    setInstallScopedFlag,
  } from '$lib/stores/install-state-store';

  // M-P1-5: dismiss flag is scoped by install_root so two clones on
  // the same machine don't share dismissals. Pre-v0.2.53 unscoped
  // copies are migrated by `getInstallScopedFlag()` on first read.
  const DISMISSED_KEY = 'vct.install_check_dismissed';
  const README_URL =
    'https://github.com/hotak92/vibecoded-orchestrator/blob/main/README.md#tldr--install--launch';

  let health = $state<InstallHealth | null>(null);
  let visible = $state(false);
  let opening = $state(false);
  // M-P0-8: track an in-flight probe so the Re-check button + focus
  // listener can guard against rapid re-entry while still letting the
  // user click "Re-check" from the same render.
  let rechecking = $state(false);
  // M-P1-5: current install_root for scoped localStorage operations.
  // Updated on every check_install_health response (defensive — the
  // walk-up resolver could in principle return a different root after
  // a launcher relocates, though in practice it does not).
  let installRoot = $state<string | null>(null);
  // M-P1-6: state for the "Run installer now" button. `launchError` is
  // surfaced inline so the user gets immediate feedback if no terminal
  // emulator is available (e.g. headless Linux box).
  let launchingInstaller = $state(false);
  let launchError = $state<string | null>(null);

  function userDismissedPreviously(): boolean {
    return getInstallScopedFlag(DISMISSED_KEY, installRoot) === 'true';
  }

  async function openReadme() {
    opening = true;
    try {
      const { openUrl } = await import('@tauri-apps/plugin-opener');
      await openUrl(README_URL);
    } catch (err) {
      // Fallback: try a plain window.open. In a Tauri webview this is
      // usually intercepted by the runtime; harmless if it fails.
      try {
        window.open(README_URL, '_blank');
      } catch {}
      console.error('[install-health-gate] openUrl failed:', err);
    } finally {
      opening = false;
    }
  }

  function letMeThrough() {
    // M-P1-5: persist the dismissal against the active install_root so
    // a sibling clone retains its own decision.
    setInstallScopedFlag(DISMISSED_KEY, installRoot, 'true');
    visible = false;
  }

  /// M-P0-8: run the backend health probe. Used at mount, by the
  /// explicit "Re-check" button, and by the window-focus listener.
  /// Updates `health` and `visible` as a side effect.
  ///
  /// When `opts.fromFocus` is true (focus listener OR manual Re-check),
  /// the dismissal flag is IGNORED for an unhealthy install — the user
  /// explicitly came back to the launcher, so they want to know if
  /// their install is still broken. At mount-time (no `fromFocus`) a
  /// prior dismissal still suppresses the gate so the user is not
  /// re-prompted on every relaunch.
  async function runHealthCheck(opts?: { fromFocus?: boolean }) {
    if (!tauriAvailable()) return;
    if (rechecking) return;
    rechecking = true;
    try {
      const result = await invoke<InstallHealth>('check_install_health');
      health = result;
      installRoot = result.install_root ?? null;
      if (result.all_ok) {
        // Self-heal: the install completed in a side terminal while
        // the launcher was open. Drop the gate without requiring a
        // launcher restart.
        visible = false;
        return;
      }
      if (!opts?.fromFocus && userDismissedPreviously()) {
        visible = false;
        return;
      }
      visible = true;
    } catch (err) {
      // Probe failure is non-fatal: don't gate the app on a backend bug.
      console.error('[install-health-gate] check_install_health failed:', err);
    } finally {
      rechecking = false;
    }
  }

  async function manualRecheck() {
    await runHealthCheck({ fromFocus: true });
  }

  /// M-P1-6: open a terminal at the install_root with the right
  /// `python install.py` command pre-loaded so the user does NOT have
  /// to copy/paste the path. The Rust side handles OS detection +
  /// terminal selection (Terminal.app on macOS, gnome-terminal /
  /// konsole / xfce4-terminal / xterm fallback chain on Linux,
  /// cmd.exe on Windows).
  async function runInstallerNow() {
    if (!installRoot) {
      launchError = 'Install root not detected — please open a terminal manually.';
      return;
    }
    launchError = null;
    launchingInstaller = true;
    try {
      await invoke('launch_installer_terminal', { installRoot });
    } catch (err) {
      console.error('[install-health-gate] launch_installer_terminal failed:', err);
      launchError =
        typeof err === 'string'
          ? err
          : 'Could not launch a terminal automatically. Open one and run `python3 install.py` from the install root.';
    } finally {
      launchingInstaller = false;
    }
  }

  function handleFocus() {
    // Window-regains-focus signal: the user just came back from a
    // side terminal (often after running install.py). Re-probe so the
    // gate either auto-clears or refreshes its details.
    void runHealthCheck({ fromFocus: true });
  }

  onMount(async () => {
    // Browser-mode dev preview: nothing to gate.
    if (!tauriAvailable()) return;
    await runHealthCheck();
    // M-P0-8: install-state changes during a launcher session (user
    // runs install.py in a side terminal). Re-probe whenever the
    // launcher window regains focus so the gate clears without a
    // launcher restart. Idempotent — runHealthCheck guards re-entry.
    if (typeof window !== 'undefined') {
      window.addEventListener('focus', handleFocus);
    }
  });

  onDestroy(() => {
    if (typeof window !== 'undefined') {
      window.removeEventListener('focus', handleFocus);
    }
  });
</script>

{#if visible && health}
  <!-- Backdrop: full-viewport, click-through disabled so this is truly
       blocking. The Z-index is intentionally above every other modal so
       the gate cannot be dismissed by stacking. -->
  <div
    class="install-gate-backdrop"
    role="dialog"
    aria-modal="true"
    aria-labelledby="install-gate-title"
  >
    <div class="install-gate-card">
      <h2 id="install-gate-title">Installation incomplete</h2>
      <p class="lead">
        It looks like you launched the orchestrator binary directly without
        running the install script first. The launcher .exe alone is not a
        complete install — it does not include Python dependencies, the
        Docker/Podman service stack, or the MCP server registration that
        the orchestrator needs to function.
      </p>

      <p class="lead">
        Please run the bundled installer for your platform from the
        repository root, then restart the launcher:
      </p>
      <ul class="cmds">
        <li><code>first-install.bat</code> &nbsp;<span class="dim">(Windows)</span></li>
        <li><code>./first-install.sh</code> &nbsp;<span class="dim">(Linux)</span></li>
        <li><code>./first-install.command</code> &nbsp;<span class="dim">(macOS)</span></li>
      </ul>

      <details class="diag">
        <summary>What is missing</summary>
        <ul>
          <li>Python venv (<code>.venv/</code>): {health.has_venv ? 'OK' : 'missing'}</li>
          <li>State directory (<code>state/</code>): {health.has_state_dir ? 'OK' : 'missing'}</li>
          <li>
            <code>.env</code> with <code>KG_COLLECTION</code>: {health.has_env_with_kg ? 'OK' : 'missing'}
          </li>
          <li>
            MCP servers (<code>claude_mcp_servers/.venv</code>): {health.mcp_servers_ok ? 'OK' : 'missing'}
          </li>
          {#if health.install_root}
            <li class="dim">Detected install root: <code>{health.install_root}</code></li>
          {/if}
        </ul>
      </details>

      {#if launchError}
        <p class="error" role="alert" data-testid="install-gate-launch-error">
          {launchError}
        </p>
      {/if}

      <div class="actions">
        <button
          class="primary"
          onclick={runInstallerNow}
          disabled={launchingInstaller || !installRoot}
          data-testid="install-gate-run-installer"
        >
          {launchingInstaller ? 'Opening terminal…' : 'Run installer now'}
        </button>
        <button class="secondary" onclick={openReadme} disabled={opening}>
          {opening ? 'Opening…' : 'Open install instructions'}
        </button>
        <button
          class="secondary"
          onclick={manualRecheck}
          disabled={rechecking}
          data-testid="install-gate-recheck"
        >
          {rechecking ? 'Re-checking…' : 'Re-check'}
        </button>
        <button class="secondary" onclick={letMeThrough}>
          I know what I'm doing — let me through anyway
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .install-gate-backdrop {
    position: fixed;
    inset: 0;
    /* Above every other modal in the layout (OnboardingWizard, etc).
       z-index ladder in this app tops out around 1000; we deliberately
       outrank it so the gate cannot be obscured. */
    z-index: 9999;
    background: rgba(0, 0, 0, 0.72);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }

  .install-gate-card {
    width: 100%;
    max-width: 560px;
    background: var(--color-bg, #0e1116);
    border: 1px solid var(--color-border, rgba(255, 255, 255, 0.12));
    border-radius: 12px;
    padding: 28px 28px 24px;
    color: var(--color-fg, #e6e6e6);
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.5);
  }

  h2 {
    margin: 0 0 12px;
    font-size: 20px;
    font-weight: 700;
  }

  .lead {
    margin: 0 0 12px;
    font-size: 14px;
    line-height: 1.5;
    color: var(--color-fg, #e6e6e6);
  }

  ul.cmds {
    list-style: none;
    padding: 0;
    margin: 8px 0 16px;
    font-size: 14px;
  }

  ul.cmds li {
    margin: 4px 0;
  }

  code {
    background: rgba(255, 255, 255, 0.06);
    padding: 2px 6px;
    border-radius: 4px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 13px;
  }

  .dim {
    color: var(--color-mid, #8a93a3);
    font-size: 12px;
  }

  .diag {
    margin: 4px 0 18px;
    font-size: 13px;
  }

  .diag summary {
    cursor: pointer;
    color: var(--color-mid, #8a93a3);
    margin-bottom: 8px;
  }

  .diag ul {
    margin: 6px 0 0 8px;
    padding-left: 16px;
  }

  .actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    flex-wrap: wrap;
  }

  .error {
    margin: 4px 0 12px;
    padding: 8px 12px;
    background: rgba(255, 79, 160, 0.08);
    border: 1px solid rgba(255, 79, 160, 0.32);
    border-radius: 6px;
    color: var(--color-pink, #ff4fa0);
    font-size: 13px;
  }

  button {
    border: none;
    padding: 9px 16px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    font-family: inherit;
  }

  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  button.primary {
    background: linear-gradient(135deg, var(--color-teal, #00bfa6), var(--color-purple, #7c5cff));
    color: #fff;
  }

  button.primary:hover:not(:disabled) {
    filter: brightness(1.08);
  }

  button.secondary {
    background: transparent;
    border: 1px solid var(--color-border, rgba(255, 255, 255, 0.18));
    color: var(--color-fg, #e6e6e6);
  }

  button.secondary:hover {
    background: rgba(255, 255, 255, 0.05);
  }
</style>
