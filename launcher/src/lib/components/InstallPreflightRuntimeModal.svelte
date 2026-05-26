<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- Copyright (c) 2026 VibeCoded Tools -->
<script lang="ts">
  // InstallPreflightRuntimeModal — modal shown when the install-pipeline
  // preflight (`check_container_runtime_available`) determines that no
  // container runtime is on PATH at the moment of an Install click.
  //
  // v0.2.35 Agent M (2026-05-26):
  //
  // Distinct from the boot-time `NoContainerRuntimeDialog` modal:
  //   - That one fires once per launcher boot from the lifecycle hook.
  //   - This one fires on EVERY Install click, so a user who uninstalls
  //     their runtime mid-session gets the same actionable affordance
  //     (instead of a cryptic toast from
  //     `installer_engine::detect_container_runtime`).
  //
  // UX contract:
  //   - Three buttons: "Install Podman" (opens canonical install URL),
  //     "Detect again" (re-invokes the preflight; closes the modal +
  //     resolves to `true` if a runtime is now available), "Cancel"
  //     (closes + resolves to `false`).
  //   - The Install click handler in ModuleCatalog awaits the result of
  //     the preflight; if `available === false`, it shows this modal and
  //     waits for the user's choice. Only "Detect again with success"
  //     unblocks the install; the other two routes abort the install
  //     attempt.
  //   - Backdrop click / Escape are treated as Cancel (same dismissal
  //     semantics).

  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import { invoke } from '$lib/tauri';

  interface RuntimeAvailability {
    available: boolean;
    detected: string | null;
    platform: string; // 'linux' | 'macos' | 'windows' | 'unknown'
    install_url: string | null;
  }

  let {
    open = $bindable<boolean>(false),
    availability,
    onProceed,
    onCancel,
  }: {
    open?: boolean;
    availability: RuntimeAvailability | null;
    onProceed: () => void;
    onCancel: () => void;
  } = $props();

  let redetecting = $state(false);
  // Local copy of the availability so the "Detect again" button can
  // refresh it without forcing the parent to re-render. Initialised from
  // the prop on open.
  let current = $state<RuntimeAvailability | null>(null);
  // Status line shown below the buttons after a "Detect again" click.
  // Cleared on next open.
  let lastRedetectMessage = $state<string | null>(null);

  // Sync the local copy whenever the parent passes a new availability
  // (e.g. on a fresh open from a different Install click).
  $effect(() => {
    if (open) {
      current = availability;
      lastRedetectMessage = null;
    }
  });

  const platformLabel = $derived((() => {
    const p = current?.platform ?? availability?.platform ?? 'unknown';
    if (p === 'linux') return 'Linux';
    if (p === 'macos') return 'macOS';
    if (p === 'windows') return 'Windows';
    return 'your system';
  })());

  const installUrl = $derived(current?.install_url ?? availability?.install_url ?? null);

  async function openInstallPage() {
    // Always-true guard: the install_url comes from the Rust side which
    // resolves it against an OS-aware allowlist, and the
    // `runtime_open_install_url` opener re-checks against its own
    // allowlist. Belt-and-braces: don't even try to invoke if no URL.
    if (!installUrl) {
      lastRedetectMessage = 'No install URL available for this platform.';
      return;
    }
    try {
      await invoke('runtime_open_install_url', { url: installUrl });
    } catch (e) {
      lastRedetectMessage = `Could not open browser: ${e instanceof Error ? e.message : String(e)}`;
    }
  }

  async function detectAgain() {
    redetecting = true;
    lastRedetectMessage = null;
    try {
      const fresh = await invoke<RuntimeAvailability>('check_container_runtime_available');
      current = fresh;
      if (fresh.available) {
        // Resolved — close the modal and let the install proceed.
        lastRedetectMessage = `Detected ${fresh.detected ?? 'runtime'} — proceeding…`;
        // Tiny pause so the user sees the success message before the
        // modal disappears. Cosmetic only; the proceed handler is what
        // actually unblocks the install path.
        setTimeout(() => {
          open = false;
          onProceed();
        }, 350);
      } else {
        lastRedetectMessage =
          'Still no container runtime detected. After installing Podman, return here and click "Detect again".';
      }
    } catch (e) {
      lastRedetectMessage = `Detection failed: ${e instanceof Error ? e.message : String(e)}`;
    } finally {
      redetecting = false;
    }
  }

  function cancel() {
    open = false;
    onCancel();
  }
</script>

{#if open}
  <DialogRoot bind:open onClose={cancel} width="600px">
    {#snippet header()}
      <h2 id="install-preflight-runtime-title">No container runtime detected</h2>
    {/snippet}
    {#snippet body()}
      <p class="lead">
        VCO needs <strong>Podman</strong> or <strong>Docker</strong> to install this module.
        Neither is currently available on your PATH ({platformLabel}).
      </p>

      <p class="hint">
        Podman is the recommended runtime — it's daemonless, rootless by default, and
        matches the rest of VCO's container stack. Click "Install Podman" to open the
        canonical install page for {platformLabel} in your browser. Once installed,
        come back here and click "Detect again".
      </p>

      {#if lastRedetectMessage}
        <p
          class="redetect-status"
          class:success={current?.available}
          class:error={current?.available === false && lastRedetectMessage.toLowerCase().includes('still')}
        >
          {lastRedetectMessage}
        </p>
      {/if}
    {/snippet}
    {#snippet footer()}
      <div class="footer-actions">
        <button
          type="button"
          class="secondary"
          onclick={cancel}
          disabled={redetecting}
        >
          Cancel
        </button>
        <button
          type="button"
          class="secondary"
          onclick={detectAgain}
          disabled={redetecting}
        >
          {redetecting ? 'Detecting…' : 'Detect again'}
        </button>
        <button
          type="button"
          class="primary"
          onclick={openInstallPage}
          disabled={redetecting || !installUrl}
        >
          Install Podman
        </button>
      </div>
    {/snippet}
  </DialogRoot>
{/if}

<style>
  h2 {
    margin: 0;
    font-size: 1.2rem;
  }
  .lead {
    margin: 0 0 0.75rem 0;
    font-size: 0.95rem;
    line-height: 1.5;
  }
  .hint {
    margin: 0.5rem 0 1rem 0;
    font-size: 0.875rem;
    color: var(--text-color-muted, #a0a0a0);
    line-height: 1.5;
  }
  .redetect-status {
    margin: 0.5rem 0 0 0;
    padding: 0.5rem 0.75rem;
    font-size: 0.85rem;
    border-radius: 4px;
    background: var(--bg-color-elevated, rgba(255, 255, 255, 0.05));
    border-left: 3px solid var(--text-color-muted, #888);
  }
  .redetect-status.success {
    border-left-color: var(--accent-color, #00bfa6);
    color: var(--accent-color, #00bfa6);
  }
  .redetect-status.error {
    border-left-color: #ff6b6b;
    color: #ff6b6b;
  }
  .footer-actions {
    display: flex;
    gap: 0.5rem;
    justify-content: flex-end;
    flex-wrap: wrap;
  }
  button {
    padding: 0.5rem 1rem;
    border-radius: 4px;
    font-size: 0.9rem;
    cursor: pointer;
    border: 1px solid var(--border-color, #333);
  }
  button.primary {
    background: var(--accent-color, #00bfa6);
    color: #000;
    border-color: var(--accent-color, #00bfa6);
    font-weight: 600;
  }
  button.primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }
  button.secondary {
    background: var(--button-bg, transparent);
    color: inherit;
  }
  button.secondary:hover:not(:disabled) {
    background: var(--button-hover, rgba(255, 255, 255, 0.05));
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
