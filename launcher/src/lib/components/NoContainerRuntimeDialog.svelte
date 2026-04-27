<script lang="ts">
  // NoContainerRuntimeDialog — blocking modal that shows when neither
  // Podman nor Docker is detected at launcher boot.
  //
  // Triggered by the `vct-no-container-runtime` event emitted by
  // `commands::lifecycle::auto_start_on_boot` (Rust side) when
  // `services::runtime::detect_runtime` returns None. Subscribed from
  // +layout.svelte so any route can display it.
  //
  // OS-specific UX:
  //   - Linux:   "Install Podman" button → calls
  //              `runtime_install_podman_linux` which runs
  //              `pkexec apt/dnf/pacman install podman`, streaming
  //              stdout/stderr via `vct-runtime-install-progress`.
  //   - macOS / Windows: "Open install instructions" → opens the
  //              canonical URL via `runtime_open_install_url`. After
  //              the user installs manually they click "Re-check"
  //              which calls `runtime_recheck` to invalidate cache and
  //              re-probe.
  //
  // Modal stays open until detection succeeds or the user cancels (the
  // launcher then runs without container services — same as
  // `--no-containers` install mode).
  import { onMount } from 'svelte';
  import { invoke, listen } from '$lib/tauri';

  type Os = 'linux' | 'macos' | 'windows' | 'unknown';
  interface NoRuntimePayload {
    os: Os;
  }
  interface InstallProgress {
    phase: 'starting' | 'output' | 'completed' | 'failed' | 'cancelled';
    message: string;
  }

  let open = $state(false);
  let os = $state<Os>('unknown');
  let installing = $state(false);
  let installLines = $state<string[]>([]);
  let error = $state<string | null>(null);
  let recheckedRuntime = $state<string | null>(null);

  // Canonical install URLs the modal is allowed to ask the backend to
  // open. The Rust side enforces an allowlist with these same prefixes —
  // mismatches here would just produce an "URL not in allowlist" error.
  const INSTALL_URLS = {
    podman: 'https://podman.io/getting-started/installation',
    podmanDesktop: 'https://podman-desktop.io/docs/installation/macos-install',
    podmanDesktopWin: 'https://podman-desktop.io/docs/installation/windows-install',
    dockerDesktop: 'https://www.docker.com/products/docker-desktop',
    wsl: 'https://learn.microsoft.com/en-us/windows/wsl/install',
  };

  async function installPodmanLinux() {
    installing = true;
    error = null;
    installLines = [];
    try {
      // Fire-and-forget on the Rust side: progress comes via the
      // `vct-runtime-install-progress` event subscribed below. The
      // command itself only returns when pkexec exits (success or
      // failure).
      await invoke('runtime_install_podman_linux');
      // Success: the `completed` event already updated state; force a
      // re-detect so the dialog can dismiss itself if pkexec succeeded.
      await recheck();
    } catch (e) {
      error = String(e);
    } finally {
      installing = false;
    }
  }

  async function openUrl(url: string) {
    try {
      await invoke('runtime_open_install_url', { url });
    } catch (e) {
      error = `Could not open browser: ${e}`;
    }
  }

  async function recheck() {
    error = null;
    try {
      const runtime = await invoke<string | null>('runtime_recheck');
      recheckedRuntime = runtime;
      if (runtime) {
        // Detected — close the modal. The launcher's auto-start hook
        // does NOT re-fire automatically; user can use the tray's
        // "Start services" or restart the launcher to actually bring
        // services up. We just dismiss here so the UI isn't blocked.
        open = false;
      } else {
        error = 'Still no runtime detected. Verify the install completed and try again.';
      }
    } catch (e) {
      error = `Re-check failed: ${e}`;
    }
  }

  function dismiss() {
    // User chose to proceed without a runtime. The orchestrator's
    // services won't start, but the launcher itself remains usable
    // (project list, settings, etc.). Re-opens on next boot until a
    // runtime appears.
    open = false;
  }

  onMount(() => {
    let unlistenNoRuntime: (() => void) | null = null;
    let unlistenProgress: (() => void) | null = null;

    listen<NoRuntimePayload>('vct-no-container-runtime', (e) => {
      os = e.payload?.os ?? 'unknown';
      open = true;
      installLines = [];
      error = null;
      recheckedRuntime = null;
    }).then((u) => {
      unlistenNoRuntime = u;
    });

    listen<InstallProgress>('vct-runtime-install-progress', (e) => {
      const p = e.payload;
      if (!p) return;
      if (p.phase === 'output') {
        // Stream stdout/stderr lines into the visible feed. Keep last
        // 200 lines to avoid unbounded DOM growth on a long install.
        installLines = [...installLines, p.message].slice(-200);
      } else if (p.phase === 'starting') {
        installLines = [...installLines, `→ ${p.message}`];
      } else if (p.phase === 'completed') {
        installLines = [...installLines, `✓ ${p.message}`];
      } else if (p.phase === 'failed' || p.phase === 'cancelled') {
        installLines = [...installLines, `✗ ${p.message}`];
        error = p.message;
      }
    }).then((u) => {
      unlistenProgress = u;
    });

    return () => {
      if (unlistenNoRuntime) unlistenNoRuntime();
      if (unlistenProgress) unlistenProgress();
    };
  });
</script>

{#if open}
  <div class="dialog-backdrop" role="dialog" aria-modal="true" aria-labelledby="no-runtime-title">
    <div class="dialog-card">
      <h2 id="no-runtime-title">No container runtime found</h2>
      <p class="lead">
        VCT services (Weaviate, Ollama, code embeddings) need either Podman or
        Docker to run. We checked your PATH and found neither.
      </p>

      {#if os === 'linux'}
        <p>
          On Linux we can install Podman for you using your system package
          manager (apt / dnf / pacman). You'll see your desktop's auth dialog
          asking for your password.
        </p>
        <div class="actions">
          <button class="primary" disabled={installing} onclick={installPodmanLinux}>
            {installing ? 'Installing…' : 'Install Podman'}
          </button>
          <button disabled={installing} onclick={() => openUrl(INSTALL_URLS.podman)}>
            Open install page instead
          </button>
        </div>

        {#if installLines.length > 0}
          <pre class="install-log">{installLines.join('\n')}</pre>
        {/if}

        <p class="hint">
          Or, if you'd rather use Docker, install it manually then click Re-check:
          <a href="#docker" onclick={(e) => { e.preventDefault(); openUrl(INSTALL_URLS.dockerDesktop); }}>
            docker.com/products/docker-desktop
          </a>
        </p>
      {:else if os === 'macos'}
        <p>
          On macOS, Podman runs in a small VM. We can't fully automate the
          install (it needs <code>podman machine init</code> + a manual
          first-run step), so please install it from the official page:
        </p>
        <ul class="manual-steps">
          <li>Open <button class="link" onclick={() => openUrl(INSTALL_URLS.podman)}>podman.io install page</button> and download the <code>.pkg</code> installer.</li>
          <li>Or open <button class="link" onclick={() => openUrl(INSTALL_URLS.podmanDesktop)}>Podman Desktop for macOS</button> for a GUI install.</li>
          <li>Or, if you prefer Docker, install <button class="link" onclick={() => openUrl(INSTALL_URLS.dockerDesktop)}>Docker Desktop</button>.</li>
          <li>After install, run <code>podman machine init &amp;&amp; podman machine start</code> in Terminal (Podman only).</li>
        </ul>
        <div class="actions">
          <button class="primary" disabled={installing} onclick={recheck}>I've installed it — Re-check</button>
        </div>
      {:else if os === 'windows'}
        <p>
          On Windows, Podman requires WSL2 (Microsoft's Linux subsystem).
          The full setup needs admin elevation + a reboot, so we can't do
          it from inside the launcher. Please follow the canonical guide:
        </p>
        <ul class="manual-steps">
          <li>Run in PowerShell as Administrator: <code>wsl --install</code> (see <button class="link" onclick={() => openUrl(INSTALL_URLS.wsl)}>WSL install docs</button>). Reboot after.</li>
          <li>Then: <code>winget install RedHat.Podman</code> (or use <button class="link" onclick={() => openUrl(INSTALL_URLS.podmanDesktopWin)}>Podman Desktop for Windows</button>).</li>
          <li>Then: <code>podman machine init &amp;&amp; podman machine start</code>.</li>
          <li>Or, if you prefer Docker: <button class="link" onclick={() => openUrl(INSTALL_URLS.dockerDesktop)}>Docker Desktop</button>.</li>
        </ul>
        <div class="actions">
          <button class="primary" disabled={installing} onclick={recheck}>I've installed it — Re-check</button>
        </div>
      {:else}
        <p>
          We couldn't detect your operating system clearly. Install Podman or
          Docker manually:
        </p>
        <ul class="manual-steps">
          <li><button class="link" onclick={() => openUrl(INSTALL_URLS.podman)}>Podman install</button></li>
          <li><button class="link" onclick={() => openUrl(INSTALL_URLS.dockerDesktop)}>Docker Desktop</button></li>
        </ul>
        <div class="actions">
          <button class="primary" onclick={recheck}>Re-check</button>
        </div>
      {/if}

      {#if error}
        <p class="error">{error}</p>
      {/if}
      {#if recheckedRuntime}
        <p class="ok">Detected runtime: <code>{recheckedRuntime}</code></p>
      {/if}

      <div class="actions secondary-actions">
        <button onclick={dismiss} disabled={installing}>
          Continue without container services
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .dialog-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1100;
  }
  .dialog-card {
    background: var(--surface, #1a1a1a);
    color: var(--text, #f0f0f0);
    border-radius: 8px;
    padding: 1.5rem;
    width: min(640px, 92vw);
    max-height: 88vh;
    overflow-y: auto;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
  }
  h2 {
    margin: 0 0 0.5rem 0;
    font-size: 1.25rem;
  }
  .lead {
    margin: 0 0 0.75rem 0;
    color: var(--text-muted, #aaa);
    font-size: 0.9rem;
  }
  p {
    margin: 0.5rem 0;
    font-size: 0.9rem;
  }
  code {
    font-family: monospace;
    background: var(--input-bg, #0a0a0a);
    padding: 0.05rem 0.3rem;
    border-radius: 3px;
    font-size: 0.85rem;
  }
  .manual-steps {
    margin: 0.5rem 0 1rem 1.25rem;
    padding: 0;
    font-size: 0.9rem;
  }
  .manual-steps li {
    margin: 0.25rem 0;
  }
  .actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-top: 0.75rem;
  }
  .actions.secondary-actions {
    margin-top: 1.5rem;
    padding-top: 0.75rem;
    border-top: 1px solid var(--border, #333);
  }
  button {
    background: var(--button-bg, #2a2a2a);
    color: inherit;
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 0.45rem 0.9rem;
    font-size: 0.9rem;
    cursor: pointer;
  }
  button:hover:not(:disabled) {
    background: var(--button-hover, #3a3a3a);
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  button.primary {
    background: var(--accent, #00bfa6);
    color: #000;
    border-color: var(--accent, #00bfa6);
    font-weight: 600;
  }
  button.link {
    background: transparent;
    border: none;
    padding: 0;
    color: var(--accent, #00bfa6);
    text-decoration: underline;
    font-size: inherit;
    cursor: pointer;
  }
  .install-log {
    background: var(--input-bg, #0a0a0a);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 0.5rem;
    margin: 0.5rem 0;
    max-height: 220px;
    overflow-y: auto;
    font-family: monospace;
    font-size: 0.78rem;
    line-height: 1.4;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .error {
    color: #ff6b6b;
    font-size: 0.85rem;
    margin-top: 0.5rem;
  }
  .ok {
    color: var(--accent, #00bfa6);
    font-size: 0.85rem;
    margin-top: 0.5rem;
  }
  .hint {
    color: var(--text-muted, #aaa);
    font-size: 0.85rem;
    margin-top: 0.75rem;
  }
  .hint a {
    color: var(--accent, #00bfa6);
  }
</style>
