<script lang="ts">
  // Services preferences page — Start All / Stop All / Restart All +
  // per-service controls + Re-detect adoption for externally-managed
  // services. Polls `services_status` on a 5s timer to keep state fresh
  // (matches the tray pill's cadence).
  //
  // v0.2.7 (Bug E1+E2): each row shows the pinned container name (the
  // container the launcher is configured to manage) and exposes a
  // "Re-detect" button that enumerates candidates and opens a picker
  // modal. The picker modal also surfaces "fullness" probes per
  // candidate (collection / model counts, etc.) so the user can tell a
  // working container from a stale one.

  import { onMount, onDestroy } from 'svelte';
  import { invoke, listen } from '$lib/tauri';
  // PR-37 (v0.2.12 / 2026-05-16): schema-health card surfaces the two
  // schema migrations introduced in PR-24 (Development temporal props
  // + shared KG indexNullState). Soft-fails to a "Weaviate not
  // reachable" hint when /v1/schema is unreachable.
  import ServicesSchemaSection from '$lib/components/ServicesSchemaSection.svelte';
  // v0.2.22 — Item #12: first-class banner when neither Podman nor
  // Docker is detected. Renders nothing when at least one runtime is
  // present. Mounted here in addition to the home page so users who
  // navigate straight to Services (e.g. troubleshooting why nothing
  // starts) still see the install affordance front-and-centre.
  import RuntimeMissingBanner from '$lib/components/RuntimeMissingBanner.svelte';
  import { orchestrator } from '$lib/stores/orchestrator';

  interface ServiceRuntimeState {
    name: string;
    running: boolean;
    port: number;
    url: string;
    externally_managed: boolean;
    adoption_mode: 'unresolved' | 'adopt' | 'parallel' | 'refuse';
    container_name: string | null;
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

  // Discriminated union mirroring `ContainerFullness` in
  // launcher/src-tauri/src/services/picker.rs. Serde emits `kind` as the
  // discriminator (snake_case).
  type ContainerFullness =
    | {
        kind: 'weaviate';
        collection_count: number;
        canonical_collections_present: string[];
        weaviate_version: string | null;
      }
    | {
        kind: 'ollama';
        model_count: number;
        canonical_models_present: string[];
      }
    | {
        kind: 'code_embed';
        backend: string | null;
        model: string | null;
        dim: number | null;
      };

  interface ContainerCandidate {
    container_name: string;
    compose_project: string | null;
    image: string;
    status: string;
    health: string | null;
    port_published: number | null;
    restart_count: number;
    fullness: ContainerFullness | null;
  }

  // services.toml-backed adoption config (read-only mirror of `services_get_adoption`).
  // Useful for "why is this service routed externally?" diagnostics; the per-row
  // `adoption_mode` on the snapshot is the runtime-classified value used for UI.
  interface ServiceAdoptionConfig {
    name: string;
    mode: string;
    external_url?: string | null;
    parallel_port?: number | null;
    container_name?: string | null;
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

  // Picker-modal state. Open when `pickerService != null`.
  let pickerService = $state<string | null>(null);
  let pickerCandidates = $state<ContainerCandidate[]>([]);
  let pickerLoading = $state(false);
  let pickerError = $state<string | null>(null);

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

  // Per-service action wrapper. v0.2.7: if the backend returns a
  // structured error (`multiple_candidates: …` / `container_missing: …`),
  // auto-open the picker for the offending service. We match by the
  // colon-prefix to keep parsing trivial — the kinds are pinned by the
  // ERR_KIND_* constants in launcher/src-tauri/src/commands/lifecycle.rs.
  async function runServiceAction(
    name: string,
    cmd: 'service_start' | 'service_stop' | 'service_restart',
  ) {
    loading = true;
    error = null;
    try {
      await invoke(cmd, { name });
      await refresh();
    } catch (e) {
      const msg = String(e);
      if (msg.startsWith('multiple_candidates:') || msg.startsWith('container_missing:') || msg.startsWith('no_candidates:')) {
        // Surface the kind to the user in the modal — they need to
        // know whether to pick, re-detect, or install something.
        error = msg;
        await openPicker(name);
      } else {
        error = msg;
      }
    } finally {
      loading = false;
    }
  }

  async function startOne(name: string) {
    await runServiceAction(name, 'service_start');
  }
  async function stopOne(name: string) {
    await runServiceAction(name, 'service_stop');
  }
  async function restartOne(name: string) {
    await runServiceAction(name, 'service_restart');
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

  // ---------------------------------------------------------------------
  // Picker modal
  // ---------------------------------------------------------------------

  async function openPicker(service: string) {
    pickerService = service;
    pickerCandidates = [];
    pickerError = null;
    pickerLoading = true;
    try {
      pickerCandidates = await invoke<ContainerCandidate[]>(
        'services_enumerate_candidates',
        { service },
      );
    } catch (e) {
      pickerError = String(e);
    } finally {
      pickerLoading = false;
    }
  }

  function closePicker() {
    pickerService = null;
    pickerCandidates = [];
    pickerError = null;
  }

  async function pickCandidate(candidate: ContainerCandidate) {
    if (!pickerService) return;
    pickerLoading = true;
    pickerError = null;
    try {
      await invoke('services_pick_container', {
        service: pickerService,
        containerName: candidate.container_name,
      });
      closePicker();
      // Refresh both snapshot + adoption config so the row reflects the
      // new pin immediately.
      await refresh();
      try {
        adoptionConfig = await invoke<AdoptionState>('services_get_adoption');
      } catch (e) {
        console.warn('services_get_adoption (post-pick) failed:', e);
      }
    } catch (e) {
      pickerError = String(e);
    } finally {
      pickerLoading = false;
    }
  }

  function fullnessSummary(c: ContainerCandidate): string {
    if (!c.fullness) {
      return c.status === 'running' ? 'probe failed' : '—';
    }
    switch (c.fullness.kind) {
      case 'weaviate': {
        const f = c.fullness;
        const canon = f.canonical_collections_present.length;
        const ver = f.weaviate_version ? `, v${f.weaviate_version}` : '';
        return `${f.collection_count} collections (${canon} canonical${ver})`;
      }
      case 'ollama': {
        const f = c.fullness;
        const canon = f.canonical_models_present.length;
        return `${f.model_count} models (${canon} canonical)`;
      }
      case 'code_embed': {
        const f = c.fullness;
        const bits = [
          f.backend ?? 'unknown backend',
          f.model ?? 'unknown model',
          f.dim ? `${f.dim}d` : '',
        ].filter(Boolean);
        return bits.join(' · ');
      }
    }
  }

  function fullnessDetails(c: ContainerCandidate): string[] {
    if (!c.fullness) return [];
    switch (c.fullness.kind) {
      case 'weaviate':
        return c.fullness.canonical_collections_present.slice(0, 5);
      case 'ollama':
        return c.fullness.canonical_models_present.slice(0, 5);
      case 'code_embed':
        return [];
    }
  }

  onMount(async () => {
    // v0.2.22 Item #12: kick a system detect so RuntimeMissingBanner has
    // fresh has_podman / has_docker data when the user lands on Services.
    // Fire-and-forget — the banner self-triggers detection as a fallback
    // and won't render before the probe completes (visible derives on
    // system !== null).
    void orchestrator.detectSystem();
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

<svelte:head>
  <title>Services — VCT Launcher</title>
</svelte:head>

<section class="services-page">
  <!-- v0.2.22 Item #12: runtime-missing banner. Self-mounts/unmounts
       based on `system.has_podman` / `system.has_docker` in the
       orchestrator store. No-op when at least one runtime exists, so
       the existing `!snapshot.runtime` warn-banner below covers the
       complementary case (runtime present but no container running). -->
  <RuntimeMissingBanner />
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
        Reset adoption
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
          <th>Managing</th>
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
            <td class="container-cell">
              {#if svc.container_name}
                <code>{svc.container_name}</code>
              {:else}
                <span class="muted">unpinned</span>
              {/if}
            </td>
            <td class="actions-cell">
              <button
                onclick={() => startOne(svc.name)}
                disabled={loading}
              >
                Start
              </button>
              <button
                onclick={() => stopOne(svc.name)}
                disabled={loading}
              >
                Stop
              </button>
              <button
                onclick={() => restartOne(svc.name)}
                disabled={loading}
              >
                Restart
              </button>
              <button
                onclick={() => openPicker(svc.name)}
                disabled={loading}
                class="secondary"
                title="Enumerate candidate containers for this service"
              >
                Re-detect
              </button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>

    <ServicesSchemaSection />
  {/if}

  {#if pickerService}
    <div
      class="modal-backdrop"
      onclick={closePicker}
      onkeydown={(e) => { if (e.key === 'Escape') closePicker(); }}
      role="presentation"
    >
      <div
        class="modal"
        onclick={(e) => e.stopPropagation()}
        onkeydown={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Pick a container for {pickerService}"
      >
        <header class="modal-header">
          <h2>Pick a container for <code>{pickerService}</code></h2>
          <button class="close" onclick={closePicker} aria-label="Close">×</button>
        </header>

        {#if pickerLoading}
          <p>Enumerating containers…</p>
        {:else if pickerError}
          <div class="banner error">{pickerError}</div>
        {:else if pickerCandidates.length === 0}
          <p class="muted">
            No candidate containers found for <code>{pickerService}</code>.
            Either nothing is running yet, or the launcher's container
            runtime can't see your existing stack. Click "Start All" to
            create fresh containers, or check your runtime config.
          </p>
        {:else}
          <p class="muted">
            {pickerCandidates.length} candidate{pickerCandidates.length === 1 ? '' : 's'} found.
            Pick the one the launcher should manage.
          </p>
          <div class="candidates">
            {#each pickerCandidates as c}
              <article class="candidate {c.status === 'running' ? 'running' : 'stopped'}">
                <header>
                  <code class="cname">{c.container_name}</code>
                  <span class="status {c.status === 'running' ? 'up' : 'down'}">
                    {c.status}
                  </span>
                  {#if c.health}
                    <span class="health {c.health}">{c.health}</span>
                  {/if}
                </header>
                <dl class="meta">
                  {#if c.compose_project}
                    <dt>project</dt><dd><code>{c.compose_project}</code></dd>
                  {/if}
                  <dt>image</dt><dd><code>{c.image}</code></dd>
                  <dt>port</dt><dd>
                    {#if c.port_published}{c.port_published}{:else}—{/if}
                  </dd>
                  <dt>restarts</dt><dd>{c.restart_count}</dd>
                  <dt>fullness</dt><dd>{fullnessSummary(c)}</dd>
                </dl>
                {#if fullnessDetails(c).length > 0}
                  <ul class="fullness-list">
                    {#each fullnessDetails(c) as d}
                      <li><code>{d}</code></li>
                    {/each}
                  </ul>
                {/if}
                <footer>
                  <button
                    onclick={() => pickCandidate(c)}
                    disabled={pickerLoading}
                  >
                    Pick this one
                  </button>
                </footer>
              </article>
            {/each}
          </div>
        {/if}
      </div>
    </div>
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
  .container-cell {
    font-family: monospace;
    font-size: 0.85rem;
    color: var(--text-muted, #aaa);
  }
  .container-cell .muted {
    margin: 0;
  }
  .actions-cell {
    display: flex;
    gap: 0.25rem;
    flex-wrap: wrap;
  }

  /* ---------- Picker modal ---------- */
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }
  .modal {
    background: var(--modal-bg, #1c1c1c);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 1.25rem;
    max-width: 720px;
    width: 90vw;
    max-height: 85vh;
    overflow: auto;
    box-shadow: 0 10px 40px rgba(0, 0, 0, 0.5);
  }
  .modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.75rem;
  }
  .modal-header h2 {
    margin: 0;
    font-size: 1.15rem;
  }
  .close {
    background: transparent;
    border: none;
    color: inherit;
    font-size: 1.5rem;
    cursor: pointer;
    padding: 0 0.4rem;
  }
  .candidates {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }
  .candidate {
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 0.6rem 0.8rem;
    background: rgba(255, 255, 255, 0.02);
  }
  .candidate.stopped {
    opacity: 0.75;
  }
  .candidate header {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin-bottom: 0.4rem;
  }
  .candidate .cname {
    font-weight: 600;
  }
  .health {
    text-transform: uppercase;
    font-size: 0.7rem;
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
  }
  .health.healthy {
    background: rgba(34, 197, 94, 0.2);
    color: #4ade80;
  }
  .health.unhealthy {
    background: rgba(239, 68, 68, 0.2);
    color: #fca5a5;
  }
  .health.starting {
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
  }
  .candidate .meta {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.15rem 0.6rem;
    font-size: 0.85rem;
    margin: 0.2rem 0;
  }
  .candidate .meta dt {
    color: var(--text-muted, #aaa);
  }
  .candidate .meta dd {
    margin: 0;
  }
  .fullness-list {
    margin: 0.3rem 0 0.5rem 0;
    padding-left: 1.2rem;
    font-size: 0.8rem;
    color: var(--text-muted, #aaa);
  }
  .candidate footer {
    margin-top: 0.4rem;
    display: flex;
    justify-content: flex-end;
  }
</style>
