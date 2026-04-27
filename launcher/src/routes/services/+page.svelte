<script lang="ts">
  // Services preferences page — Start All / Stop All / Restart All +
  // per-service controls + Re-detect adoption for externally-managed
  // services. Polls `services_status` on a 5s timer to keep state fresh
  // (matches the tray pill's cadence).

  import { onMount, onDestroy } from 'svelte';
  import { invoke, listen } from '$lib/tauri';

  interface ServiceRuntimeState {
    name: string;
    running: boolean;
    port: number;
    url: string;
    externally_managed: boolean;
    adoption_mode: 'unresolved' | 'adopt' | 'parallel' | 'refuse';
  }
  interface ServicesRuntimeSnapshot {
    services: ServiceRuntimeState[];
    runtime: string | null;
    needs_podman_machine_start: boolean;
    has_unresolved_external: boolean;
  }
  interface LifecycleProgress {
    phase: string;
    message: string;
  }

  // services.toml-backed adoption config (read-only mirror of `services_get_adoption`).
  // Useful for "why is this service routed externally?" diagnostics; the per-row
  // `adoption_mode` on the snapshot is the runtime-classified value used for UI.
  interface ServiceAdoptionConfig {
    name: string;
    mode: string;
    external_url?: string | null;
    parallel_port?: number | null;
  }
  interface AdoptionState {
    services: ServiceAdoptionConfig[];
  }

  let snapshot = $state<ServicesRuntimeSnapshot | null>(null);
  let adoptionConfig = $state<AdoptionState | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let progress = $state<LifecycleProgress | null>(null);
  let pollerHandle: ReturnType<typeof setInterval> | null = null;
  let unlistenProgress: (() => void) | null = null;

  async function refresh() {
    try {
      snapshot = await invoke<ServicesRuntimeSnapshot>('services_status');
      error = null;
    } catch (e) {
      error = String(e);
    }
  }

  async function startAll() {
    loading = true;
    error = null;
    try {
      await invoke('services_start_all');
      await refresh();
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  async function stopAll() {
    loading = true;
    error = null;
    try {
      await invoke('services_stop_all');
      await refresh();
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  async function restartAll() {
    loading = true;
    error = null;
    try {
      await invoke('services_restart_all');
      await refresh();
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  async function startOne(name: string) {
    loading = true;
    error = null;
    try {
      await invoke('service_start', { name });
      await refresh();
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  async function stopOne(name: string) {
    loading = true;
    error = null;
    try {
      await invoke('service_stop', { name });
      await refresh();
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  async function restartOne(name: string) {
    loading = true;
    error = null;
    try {
      await invoke('service_restart', { name });
      await refresh();
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  async function resetAdoption() {
    loading = true;
    error = null;
    try {
      await invoke('services_reset_adoption');
      await refresh();
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  onMount(async () => {
    await refresh();
    // Mirror the on-disk adoption config for diagnostics. Failures are
    // non-fatal — the snapshot already drives the UI.
    try {
      adoptionConfig = await invoke<AdoptionState>('services_get_adoption');
    } catch (e) {
      // Soft-fail: log to console only; this is a diagnostics fetch.
      console.warn('services_get_adoption failed:', e);
    }
    pollerHandle = setInterval(refresh, 5000);
    unlistenProgress = await listen<LifecycleProgress>(
      'vct-services-lifecycle',
      (e) => {
        progress = e.payload;
        // Refresh status on terminal phases so the UI reflects the
        // post-action state without waiting for the next poll tick.
        if (
          ['started', 'stopped', 'start_failed', 'runtime_missing'].includes(
            e.payload.phase,
          )
        ) {
          refresh();
        }
      },
    );
  });

  onDestroy(() => {
    if (pollerHandle) clearInterval(pollerHandle);
    if (unlistenProgress) unlistenProgress();
  });
</script>

<section class="services-page">
  <header>
    <h1>Services</h1>
    <p class="muted">
      Manage the shared Weaviate / Ollama / code_embed containers used by
      every project.
    </p>
  </header>

  {#if !snapshot}
    <p>Loading…</p>
  {:else}
    {#if !snapshot.runtime}
      <div class="banner warn">
        <strong>No container runtime found.</strong>
        Install <a href="https://podman.io">Podman</a> or
        <a href="https://docker.com">Docker</a> to run VCT services.
      </div>
    {:else}
      <p class="runtime-line">
        Runtime: <strong>{snapshot.runtime}</strong>
      </p>
    {/if}

    {#if snapshot.needs_podman_machine_start}
      <div class="banner warn">
        Podman is installed but no machine is running. Run
        <code>podman machine start</code> and click Re-detect.
      </div>
    {/if}

    <div class="bulk-actions">
      <button onclick={startAll} disabled={loading || !snapshot.runtime}>
        Start All
      </button>
      <button onclick={stopAll} disabled={loading || !snapshot.runtime}>
        Stop All
      </button>
      <button onclick={restartAll} disabled={loading || !snapshot.runtime}>
        Restart All
      </button>
      <button onclick={resetAdoption} disabled={loading} class="secondary">
        Re-detect
      </button>
    </div>

    {#if error}
      <div class="banner error">{error}</div>
    {/if}
    {#if progress}
      <div class="banner info">
        <strong>{progress.phase}</strong>: {progress.message}
      </div>
    {/if}

    <table class="service-table">
      <thead>
        <tr>
          <th>Service</th>
          <th>Status</th>
          <th>Port</th>
          <th>Mode</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {#each snapshot.services as svc}
          <tr>
            <td><strong>{svc.name}</strong></td>
            <td>
              <span class="status {svc.running ? 'up' : 'down'}">
                {svc.running ? 'running' : 'stopped'}
              </span>
              {#if svc.externally_managed}
                <span class="tag">external</span>
              {/if}
            </td>
            <td>{svc.port}</td>
            <td
              class="mode-cell"
              title={
                adoptionConfig?.services.find((a) => a.name === svc.name)
                  ?.external_url ?? ''
              }
            >{svc.adoption_mode}</td>
            <td class="actions-cell">
              <button
                onclick={() => startOne(svc.name)}
                disabled={loading || svc.externally_managed}
                title={svc.externally_managed
                  ? 'Externally managed — launcher does not control this service'
                  : ''}
              >
                Start
              </button>
              <button
                onclick={() => stopOne(svc.name)}
                disabled={loading || svc.externally_managed}
              >
                Stop
              </button>
              <button
                onclick={() => restartOne(svc.name)}
                disabled={loading || svc.externally_managed}
              >
                Restart
              </button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</section>

<style>
  .services-page {
    max-width: 900px;
    padding: 1.5rem;
  }
  header h1 {
    margin: 0 0 0.25rem 0;
  }
  .muted {
    color: var(--text-muted, #aaa);
    margin: 0 0 1rem 0;
  }
  .runtime-line {
    margin: 0 0 1rem 0;
    color: var(--text-muted, #aaa);
  }
  .banner {
    padding: 0.6rem 0.8rem;
    border-radius: 4px;
    margin: 0.5rem 0;
  }
  .banner.warn {
    background: rgba(245, 158, 11, 0.15);
    border: 1px solid rgba(245, 158, 11, 0.4);
  }
  .banner.error {
    background: rgba(239, 68, 68, 0.15);
    border: 1px solid rgba(239, 68, 68, 0.4);
  }
  .banner.info {
    background: rgba(59, 130, 246, 0.15);
    border: 1px solid rgba(59, 130, 246, 0.4);
  }
  .bulk-actions {
    display: flex;
    gap: 0.5rem;
    margin: 0.75rem 0;
  }
  button {
    padding: 0.4rem 0.9rem;
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    background: var(--button-bg, #2a2a2a);
    color: inherit;
    cursor: pointer;
    font-size: 0.9rem;
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  button.secondary {
    margin-left: auto;
  }
  .service-table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 0.75rem;
  }
  .service-table th,
  .service-table td {
    text-align: left;
    padding: 0.5rem 0.6rem;
    border-bottom: 1px solid var(--border, #333);
    font-size: 0.9rem;
  }
  .status {
    text-transform: uppercase;
    font-size: 0.75rem;
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    font-weight: 600;
  }
  .status.up {
    background: rgba(34, 197, 94, 0.2);
    color: #4ade80;
  }
  .status.down {
    background: rgba(107, 114, 128, 0.2);
    color: #9ca3af;
  }
  .tag {
    margin-left: 0.4rem;
    font-size: 0.7rem;
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
  }
  .mode-cell {
    font-family: monospace;
    color: var(--text-muted, #aaa);
  }
  .actions-cell {
    display: flex;
    gap: 0.25rem;
  }
</style>
