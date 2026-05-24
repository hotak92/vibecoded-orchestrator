<script lang="ts">
  // Blocking CDI-drift modal (2026-05-08).
  //
  // Surfaces NVIDIA driver vs CDI spec mismatch at launcher startup.
  // Background + forensics: see launcher/src-tauri/src/commands/gpu.rs
  // and knowledge/tools/podman-cdi-gpu-passthrough.md.
  //
  // Behaviour:
  //   - Calls check_cdi_drift() once on mount.
  //   - On Ok / NotApplicable → silent. Modal never opens.
  //   - On Drift → opens a blocking dialog with the host driver version,
  //     CDI versions, and the exact recovery command (`sudo rm
  //     /etc/cdi/nvidia.yaml` for the most-common shadowing case;
  //     `systemctl restart nvidia-cdi-refresh.service` otherwise).
  //
  // The modal has a single "Got it" close button — we explicitly do NOT
  // try to run the sudo command from the launcher (would need polkit
  // wiring + per-distro packaging). Showing the command is the right
  // ergonomic balance: user sees the exact fix and copy-pastes it.

  import { onMount } from 'svelte';
  // v0.2.32 E1 (2026-05-23): use the wrapper from $lib/tauri instead of
  // importing `invoke` from '@tauri-apps/api/core' directly. The direct
  // import resolves to `undefined` under `vite dev` (browser mode), and
  // calling it throws `TypeError: Cannot read properties of undefined`
  // before the existing try/catch in onMount has a chance to fire. The
  // wrapper's browser-mode guard surfaces a clear "Tauri not available"
  // error that the catch handles gracefully.
  import { invoke } from '$lib/tauri';
  import DialogRoot from '$lib/components/DialogRoot.svelte';

  type CdiDriftReport =
    | { kind: 'ok'; host_driver: string }
    | {
        kind: 'drift';
        host_driver: string;
        cdi_etc_driver: string;
        cdi_run_driver: string;
        message: string;
        stale_etc_cdi_present: boolean;
      }
    | { kind: 'not_applicable'; reason: string };

  let report = $state<CdiDriftReport | null>(null);
  let open = $state(false);

  onMount(async () => {
    try {
      const r = await invoke<CdiDriftReport>('check_cdi_drift');
      report = r;
      // Only auto-open the modal on actual drift. Ok / NotApplicable
      // pass silently — no need to interrupt the user.
      if (r.kind === 'drift') {
        open = true;
      }
    } catch (e) {
      // The Rust side never returns Err in practice (it folds failures
      // into NotApplicable), but be defensive — if invoke() throws for
      // any other reason (Tauri infra, etc.), don't block the UI.
      console.warn('check_cdi_drift failed silently', e);
    }
  });

  function close() {
    open = false;
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') close();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open && report?.kind === 'drift'}
  {@const drift = report}
  <DialogRoot bind:open onClose={close} width="720px">
    {#snippet header()}
      <h2 id="cdi-drift-title">⚠️ NVIDIA / CDI version mismatch</h2>
    {/snippet}
    {#snippet body()}
      <pre class="cdi-message">{drift.message}</pre>
      <p class="cdi-footnote">
        The launcher detected this once at startup. After you run the fix and
        restart the GPU containers, this dialog will not reappear unless the
        mismatch returns (e.g. after another driver upgrade).
      </p>
    {/snippet}
    {#snippet footer()}
      <button type="button" class="primary" onclick={close}>
        Got it — I'll run the fix
      </button>
    {/snippet}
  </DialogRoot>
{/if}

<style>
  .cdi-drift-modal {
    max-width: 720px;
    padding: 1.5rem;
    color: var(--text-color, #e0e0e0);
  }

  h2 {
    margin: 0 0 1rem;
    font-size: 1.25rem;
  }

  .cdi-message {
    background: var(--bg-color-elevated, rgba(255, 255, 255, 0.05));
    border-radius: 0.375rem;
    padding: 1rem;
    font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
    font-size: 0.85rem;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 50vh;
    overflow-y: auto;
    margin: 0 0 1.25rem;
  }

  .cdi-actions {
    display: flex;
    justify-content: flex-end;
    margin-bottom: 1rem;
  }

  button.primary {
    background: var(--accent-color, #4a9eff);
    color: white;
    border: none;
    padding: 0.5rem 1.25rem;
    border-radius: 0.375rem;
    cursor: pointer;
    font-size: 0.9rem;
  }

  button.primary:hover {
    filter: brightness(1.1);
  }

  .cdi-footnote {
    font-size: 0.8rem;
    color: var(--text-color-muted, #a0a0a0);
    margin: 0;
  }
</style>
