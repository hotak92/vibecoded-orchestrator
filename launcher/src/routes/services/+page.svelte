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
  // v0.2.92 WP-12 — the model gateway. It is NOT a container, so it gets a
  // card of its own rather than a row in the table above: it has no image,
  // no adoption mode, nothing to "re-detect", and the container watchdog
  // deliberately does not supervise it. Every decision the card makes lives
  // in `$lib/api/model_gateway` so a vitest can reach it; what is left here
  // is markup and event wiring.
  import {
    DEFAULT_GATEWAY_MODEL,
    canStop,
    checkModelGateway,
    describeStatus,
    describeWriteResult,
    gatewayIsConfigured,
    getModelGatewayStatus,
    inspectVSCodeTarget,
    listVSCodeTargets,
    pointPanelAtGateway,
    pointPanelWarnings,
    projectHasRoutingGuidance,
    resetPanelToNative,
    setModelGatewayBoot,
    setProjectRoutingGuidance,
    startModelGateway,
    stopDisabledReason,
    stopModelGateway,
  } from '$lib/api/model_gateway';
  import type {
    ModelGatewayStatus,
    VSCodeInspection,
    VSCodeTarget,
    VSCodeWriteResult,
  } from '$lib/types/model-gateway';
  import { projects } from '$lib/stores/projects';

  interface ServiceRuntimeState {
    name: string;
    running: boolean;
    port: number;
    url: string;
    externally_managed: boolean;
    adoption_mode: 'unresolved' | 'adopt' | 'parallel' | 'refuse';
    container_name: string | null;
    // True when the pinned container exists per `podman ps` but its main
    // PID is dead (state-DB desync). Mirrors `ServiceRuntimeState.zombie`
    // in launcher/src-tauri/src/commands/lifecycle.rs.
    zombie?: boolean;
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
  //
  // v0.2.92 (review MAJOR-8): the `kind` arms are the hand-listed SERVICE
  // UNION for this page, and until now nothing pinned them to anything. They
  // are now diffed against the hub's canonical service table
  // (`vct-hub/src/lifecycle_api.rs::canonical_service_skeletons`) by
  // `services-union.test.ts`, which fails if a service joins that table and no
  // arm follows — or if an arm names something the hub does not serve.
  //
  // The pin has ONE declared exclusion, and it is structural rather than
  // drift: `model_gateway` is a PROCESS, not a `vco_*` container. It has no
  // image, no adoption mode, nothing to "re-detect", so a fullness probe for
  // it would have no candidates to describe — the page gives it a card of its
  // own instead (see the `$lib/api/model_gateway` import above). The test
  // states that exclusion explicitly, mirroring
  // `infra_watchdog::watchdog_never_supervises_the_model_gateway_process` on
  // the Rust side, so "sync the two lists" cannot quietly undo it.
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

  // ─── Model gateway card ────────────────────────────────────────────────
  let gw = $state<ModelGatewayStatus | null>(null);
  let gwError = $state<string | null>(null);
  let gwBusy = $state(false);
  let gwCheck = $state<string | null>(null);

  let vsTargets = $state<VSCodeTarget[]>([]);
  let vsSelected = $state<string | null>(null);
  let vsInspection = $state<VSCodeInspection | null>(null);
  let vsResult = $state<VSCodeWriteResult | null>(null);
  // Default model: OFF. VCO adds the gateway's catalogue to the picker; the
  // model is the user's choice, made visibly, in the picker or here.
  let setDefaultModel = $state(false);
  let modelChoice = $state(DEFAULT_GATEWAY_MODEL);
  let removeSlots = $state(false);

  // Per-project CLAUDE.md routing-guidance flags, keyed by project id.
  let guidance = $state<Record<string, boolean>>({});

  const gwLine = $derived(describeStatus(gw));
  const gwWarnings = $derived(pointPanelWarnings(vsInspection));
  const gwConfigured = $derived(gatewayIsConfigured(gw));

  async function refreshGateway() {
    try {
      gw = await getModelGatewayStatus();
      gwError = null;
    } catch (e) {
      gwError = String(e);
    }
  }

  async function refreshVSCodeTargets() {
    try {
      vsTargets = await listVSCodeTargets();
      if (!vsSelected && vsTargets.length > 0) {
        vsSelected = vsTargets[0].path;
      }
      await refreshInspection();
    } catch (e) {
      gwError = String(e);
    }
  }

  async function refreshInspection() {
    if (!vsSelected) {
      vsInspection = null;
      return;
    }
    try {
      vsInspection = await inspectVSCodeTarget(vsSelected);
    } catch (e) {
      vsInspection = null;
      gwError = String(e);
    }
  }

  async function gwAction(fn: () => Promise<unknown>) {
    gwBusy = true;
    gwError = null;
    try {
      await fn();
    } catch (e) {
      gwError = String(e);
    } finally {
      gwBusy = false;
      await refreshGateway();
    }
  }

  async function doPointPanel() {
    if (!vsSelected) return;
    await gwAction(async () => {
      vsResult = await pointPanelAtGateway({
        path: vsSelected!,
        model: setDefaultModel ? modelChoice : null,
        removeSlotOverrides: removeSlots,
      });
      await refreshInspection();
    });
  }

  async function doResetNative() {
    if (!vsSelected) return;
    await gwAction(async () => {
      vsResult = await resetPanelToNative(vsSelected!);
      await refreshInspection();
    });
  }

  async function loadGuidanceFlags() {
    const next: Record<string, boolean> = {};
    for (const p of $projects.projects) {
      try {
        next[p.id] = await projectHasRoutingGuidance(p.id);
      } catch {
        // A project whose row cannot be read is shown as off rather than
        // guessed as on — the section it gates is advice about models, and
        // showing it where it may not apply is the failure mode to avoid.
        next[p.id] = false;
      }
    }
    guidance = next;
  }

  async function toggleGuidance(projectId: string, enabled: boolean) {
    await gwAction(async () => {
      await setProjectRoutingGuidance(projectId, enabled);
      guidance = { ...guidance, [projectId]: enabled };
    });
  }

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

  // Force-recover a zombie container (podman state-DB desync: `podman ps`
  // says "Up" but the main PID is dead). Wired to `recover_zombie`, which
  // force-removes the stale record and re-brings-up the stack.
  async function recoverZombie(containerName: string) {
    loading = true;
    error = null;
    try {
      await invoke('recover_zombie', { containerName });
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
    // Model gateway: its own probes, on the same 5 s cadence as the table.
    await refreshGateway();
    await refreshVSCodeTargets();
    // The per-project guidance list needs the project rows; load them if
    // this page was the entry point.
    await projects.load();
    await loadGuidanceFlags();
    pollerHandle = setInterval(() => {
      refresh();
      refreshGateway();
    }, 5000);
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
              {#if svc.zombie}
                <span
                  class="tag tag-zombie"
                  title="The container exists but its main process is dead (state-DB desync). Use Recover to force-remove and restart it."
                >stuck</span>
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
              {#if svc.zombie && svc.container_name}
                <button
                  onclick={() => svc.container_name && recoverZombie(svc.container_name)}
                  disabled={loading}
                  class="recover"
                  title="Force-remove the stuck container and restart it"
                >
                  Recover
                </button>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>

    <ServicesSchemaSection />
  {/if}

  <!-- v0.2.92 WP-12 — model gateway. Rendered OUTSIDE the snapshot guard
       above: it is a process, not a container, so it must still be
       manageable on a machine with no container runtime at all. -->
  <section class="gateway-card">
    <header class="gw-head">
      <h2>Model gateway</h2>
      <span class="status {gwLine.tone}">{gwLine.label}</span>
    </header>
    <p class="muted">
      A local, loopback-only endpoint that serves your Claude subscription and
      any configured vendor subscription in ONE Claude Code model picker. Each
      entry names the model that answers: vendor models appear under a
      <code>claude-gw/</code> prefix and are forwarded under their real id.
    </p>
    <p class="gw-detail">{gwLine.detail}</p>

    {#if gwError}
      <div class="banner error">{gwError}</div>
    {/if}

    <div class="bulk-actions">
      <button
        onclick={() => gwAction(() => startModelGateway())}
        disabled={gwBusy || gw?.reachable === true}
      >
        Start
      </button>
      <button
        onclick={() => gwAction(async () => {
          const out = await stopModelGateway();
          if (!out.stopped) gwError = out.message;
        })}
        disabled={gwBusy || !canStop(gw)}
        title={stopDisabledReason(gw)}
      >
        Stop
      </button>
      <label class="gw-toggle">
        <input
          type="checkbox"
          checked={gw?.boot === 'enabled'}
          disabled={gwBusy || gw?.boot === 'unsupported'}
          onchange={(e) =>
            gwAction(() => setModelGatewayBoot(e.currentTarget.checked))}
        />
        Start at login
      </label>
      <button
        class="secondary"
        onclick={() =>
          gwAction(async () => {
            try {
              gwCheck = await checkModelGateway();
            } catch (e) {
              gwCheck = String(e);
            }
          })}
        disabled={gwBusy}
        title="Run the gateway's configuration self-test"
      >
        Diagnose
      </button>
    </div>

    {#if gw?.boot === 'unsupported'}
      <p class="muted small">
        This machine's init system could not be inspected, so login autostart
        is unavailable here — not off. Nothing was changed.
      </p>
    {:else}
      <p class="muted small">
        Turning login autostart off also stops a running gateway on Linux and
        macOS. On Windows it removes the scheduled task but leaves an
        already-running gateway running.
      </p>
    {/if}

    {#if !canStop(gw) && gw?.process === 'running'}
      <div class="banner warn">{stopDisabledReason(gw)}</div>
    {/if}

    {#if gw && !gw.python}
      <div class="banner error">
        No Python interpreter could be resolved for the gateway. Re-run
        <code>install.py</code> to rebuild the orchestrator venv — Start,
        Diagnose and the panel actions all need one.
      </div>
    {:else if gw?.python && gwCheck}
      <p class="muted small">
        Interpreter: <code>{gw.python}</code>
      </p>
    {/if}

    {#if gwCheck}
      <pre class="gw-check">{gwCheck}</pre>
    {/if}

    {#if gw?.health}
      <dl class="gw-meta">
        <dt>port</dt>
        <dd>{gw.port} <span class="muted">(loopback only)</span></dd>
        <dt>catalog</dt>
        <dd>
          {#each Object.entries(gw.health.catalog_source) as [family, source]}
            <code class="src {source}">{family}: {source}</code>
          {/each}
          {#if Object.values(gw.health.catalog_source).includes('static')}
            <span class="muted"
              >— a family reading <code>static</code> is being served from the
              shipped fallback list, so newly released models are missing from
              the picker until the live fetch succeeds.</span
            >
          {/if}
        </dd>
        <dt>context table</dt>
        <dd>
          {gw.health.context_table_source}
          {#if gw.health.context_table_path}
            <code>{gw.health.context_table_path}</code>
          {/if}
        </dd>
        <dt>Claude login</dt>
        <dd>
          {gw.health.oauth_state}
          {#if gw.health.oauth_state !== 'present'}
            <span class="muted"
              >— run <code>claude</code> once in a terminal and log in;
              the gateway reads (never writes) the CLI's credentials file, and
              Claude-family models will 401 until it is valid.</span
            >
          {/if}
        </dd>
        <dt>token file</dt>
        <dd>
          {gw.health.token_file_permissions}
          {#if gw.health.token_file_permissions !== 'owner_only'}
            <span class="muted"
              >— the gateway's token authorises proxying under your Claude
              login; anything but <code>owner_only</code> means another local
              account may be able to read it.</span
            >
          {/if}
        </dd>
      </dl>
    {/if}

    <!-- ── VS Code panel ─────────────────────────────────────────────── -->
    <h3>VS Code panel</h3>
    <p class="muted small">
      The Claude Code extension does not read <code>.claude/settings.json</code>
      for routing — it reads VS Code's own global
      <code>settings.json</code>. These buttons write exactly two keys there
      (<code>claudeCode.environmentVariables</code> and
      <code>claudeCode.disableLoginPrompt</code>), back the file up first, and
      restrict it to your account afterwards.
      <strong>VS Code must be fully quit and reopened</strong> for a change to
      take effect.
    </p>

    {#if vsTargets.length === 0}
      <p class="muted">
        No VS Code-family <code>settings.json</code> found for this user
        account. VCO only offers files that already exist — it will not create
        a configuration for an editor you do not have. Set
        <code>VCT_VSCODE_SETTINGS_FILES</code> to point at a portable or
        custom-profile install.
      </p>
    {:else}
      <label class="gw-field">
        Settings file
        <select
          bind:value={vsSelected}
          onchange={refreshInspection}
          disabled={gwBusy}
        >
          {#each vsTargets as t}
            <option value={t.path}>{t.display_name} — {t.path}</option>
          {/each}
        </select>
      </label>

      {#if vsInspection}
        <p class="gw-detail">
          {#if vsInspection.points_at_vco_gateway}
            Pointed at this gateway ({vsInspection.base_url}){#if vsInspection.model}, default model
              <code>{vsInspection.model}</code>{/if}.
          {:else if vsInspection.base_url}
            Pointed at <code>{vsInspection.base_url}</code>, which is not a
            gateway on this machine. Uninstalling VCO will leave it alone;
            "Reset to stock Claude Code" below still clears it, because that
            is you asking.
          {:else if vsInspection.parseable === false}
            Cannot be edited automatically (see the warning below).
          {:else}
            Stock Claude Code — no routing keys set.
          {/if}
        </p>
      {/if}

      {#each gwWarnings as w}
        <div class="banner warn">{w}</div>
      {/each}

      <div class="gw-options">
        <label class="gw-toggle">
          <input type="checkbox" bind:checked={setDefaultModel} disabled={gwBusy} />
          Also set the picker's Default entry
        </label>
        {#if setDefaultModel}
          <input
            class="gw-model"
            type="text"
            bind:value={modelChoice}
            disabled={gwBusy}
            aria-label="Default model id"
          />
        {/if}
        {#if vsInspection && vsInspection.slot_overrides.length > 0}
          <label class="gw-toggle">
            <input type="checkbox" bind:checked={removeSlots} disabled={gwBusy} />
            Also remove the tier/subagent overrides already in this file
          </label>
        {/if}
      </div>
      <p class="muted small">
        Leaving the Default entry unset keeps whatever you already chose. VCO
        never sets the Opus / Sonnet / Haiku / Fable tier slots or the subagent
        slot: the name you pick in the picker has to be the model that answers.
        That is safe here precisely because this gateway forwards real Claude
        ids to Anthropic — pointed straight at a third-party endpoint instead,
        those same names come back answered by the vendor's own small model
        with HTTP 200 and no error
        (<a
          href="https://docs.z.ai/scenario-example/develop-tools/claude"
          target="_blank"
          rel="noreferrer">documented vendor behaviour</a
        >).
      </p>

      <div class="bulk-actions">
        <button
          onclick={doPointPanel}
          disabled={gwBusy || !vsSelected || !gwConfigured}
          title={gwConfigured
            ? 'Write the routing keys into this settings file'
            : 'Start the gateway once first — it creates the host token this action writes.'}
        >
          Point panel at gateway
        </button>
        <button onclick={doResetNative} disabled={gwBusy || !vsSelected}>
          Reset to stock Claude Code
        </button>
      </div>

      {#if vsResult}
        <div class="banner {vsResult.ok ? 'info' : 'error'}">
          {describeWriteResult(vsResult)}
          {#if vsResult.backup_path}
            <br /><span class="muted">Backup: <code>{vsResult.backup_path}</code></span>
          {/if}
          {#if vsResult.paste_block}
            <p class="muted small">
              VCO did not touch the file. Paste these keys into it by hand
              (the token value is in the gateway's token file — VCO does not
              print credentials):
            </p>
            <pre class="gw-check">{vsResult.paste_block}</pre>
          {/if}
        </div>
      {/if}
    {/if}

    <!-- ── CLAUDE.md routing guidance ────────────────────────────────── -->
    {#if gwConfigured && $projects.projects.length > 0}
      <h3>Model-routing guidance in project CLAUDE.md</h3>
      <p class="muted small">
        Adds a model-routing section to a project's <code>CLAUDE.md</code> —
        which task classes to route to which model, and the rule that a model
        name must never be silently re-pointed. Off by default and offered only
        here, because a project on a machine with no gateway must not read
        advice about models it cannot reach. Toggling re-renders only the
        VCO-managed region of that file; anything you wrote around it is
        untouched.
      </p>
      <ul class="gw-projects">
        {#each $projects.projects as p}
          <li>
            <label class="gw-toggle">
              <input
                type="checkbox"
                checked={guidance[p.id] ?? false}
                disabled={gwBusy}
                onchange={(e) => toggleGuidance(p.id, e.currentTarget.checked)}
              />
              {p.name}
            </label>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

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
        tabindex="-1"
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
    /* Centre the content column. Without `margin: 0 auto` the 900px
       block pinned to the left edge, leaving a large empty gutter on the
       right (the table read as "stuck against the sidebar"). Mirrors the
       /codegraph and /audit content centring. */
    margin: 0 auto;
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
  /* v0.2.92 WP-12: the model gateway's status has FOUR tones, not two.
     `warn` is "alive but not answering" / "stale pid file"; `unknown` is
     "could not determine", which must not look like either up or down. */
  .status.warn {
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
  }
  .status.unknown {
    background: rgba(123, 95, 255, 0.18);
    color: #b7a6ff;
  }
  .gateway-card {
    margin-top: 2rem;
    padding: 1rem 1.1rem 1.2rem;
    border: 1px solid var(--border, #333);
    border-radius: 6px;
  }
  .gw-head {
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }
  .gw-head h2 {
    margin: 0;
    font-size: 1.1rem;
  }
  .gateway-card h3 {
    margin: 1.2rem 0 0.3rem;
    font-size: 0.95rem;
  }
  .gw-detail {
    margin: 0 0 0.6rem;
    font-size: 0.9rem;
  }
  .muted.small,
  .gateway-card p.small {
    font-size: 0.82rem;
  }
  .gw-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-size: 0.88rem;
  }
  .gw-field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    font-size: 0.85rem;
    margin: 0.5rem 0;
  }
  .gw-field select,
  .gw-model {
    padding: 0.35rem 0.5rem;
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    background: var(--button-bg, #2a2a2a);
    color: inherit;
    font-size: 0.85rem;
  }
  .gw-options {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.75rem;
    margin: 0.5rem 0;
  }
  .gw-meta {
    display: grid;
    grid-template-columns: 9rem 1fr;
    gap: 0.25rem 0.75rem;
    font-size: 0.85rem;
    margin: 0.6rem 0;
  }
  .gw-meta dt {
    color: var(--text-muted, #aaa);
  }
  .gw-meta dd {
    margin: 0;
  }
  .src {
    margin-right: 0.4rem;
  }
  .src.static {
    color: #fbbf24;
  }
  .src.unavailable {
    color: #f87171;
  }
  .gw-check {
    background: rgba(0, 0, 0, 0.25);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 0.6rem;
    font-size: 0.78rem;
    overflow-x: auto;
    white-space: pre-wrap;
  }
  .gw-projects {
    list-style: none;
    padding: 0;
    margin: 0.3rem 0 0;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .tag {
    margin-left: 0.4rem;
    font-size: 0.7rem;
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
  }
  .tag-zombie {
    background: rgba(255, 79, 160, 0.18);
    color: var(--color-pink, #ff4fa0);
    font-weight: 600;
  }
  button.recover {
    border-color: rgba(255, 79, 160, 0.5);
    color: var(--color-pink, #ff4fa0);
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
