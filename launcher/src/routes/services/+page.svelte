<script lang="ts">
  // Services preferences page — Start All / Stop All / Restart All +
  // per-service controls. Polls `services_status` on a 5s timer to keep
  // state fresh (matches the tray pill's cadence).
  //
  // v0.2.97 (service endpoints SSOT): each row shows WHERE the service runs
  // — its launcher.db `service_endpoints` row: VCO-managed, "Using your
  // container <name>" or "Using <url>" — with its port, container and data
  // mount. "Change…" re-detects candidates (the Python detector) and opens
  // the adoption dialog; "Let VCO manage it" is the opt-in ownership
  // transfer of an adopted Weaviate/Ollama container, behind a confirmation
  // that names the data mount. There is no "Reset adoption" and no
  // "refuse": a service always has an endpoint. Every decision lives in
  // `$lib/api/service_endpoints` (vitest-pinned); this file is markup.

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
    defaultModelError,
    describeStatus,
    pointPanelPort,
    describeWriteResult,
    describeDogfood,
    describeHubCondition,
    describeOAuthExpiry,
    describeRegistration,
    describeSecretScope,
    describeSupervision,
    describeUsageLedger,
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
  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import ExternalServicesDialog from '$lib/components/ExternalServicesDialog.svelte';
  import {
    ACTION_LABELS,
    buildCandidateReport,
    describeDataMount,
    getEndpointCandidates,
    getServicesStatus,
    isCoreService,
    modeBadge,
    parseDataMount,
    pendingFromSnapshot,
    rowsFromSnapshot,
    runEndpointAction,
    serviceActions,
    serviceLabel,
    type CandidateReport,
    type CoreServiceName,
    type ServiceActionId,
    type ServiceRuntimeState,
    type ServicesRuntimeSnapshot,
  } from '$lib/api/service_endpoints';
  // v0.2.97 (R7b F22): "Move to another port" for VCO's own services. The
  // decisions live in `$lib/api/service-endpoint-move` (vitest-pinned).
  import {
    checkGrpcPort,
    checkMovePort,
    moveOffer,
    moveRequest,
    moveService,
    resultLines,
    type MovableService,
  } from '$lib/api/service-endpoint-move';

  interface LifecycleProgress {
    phase: string;
    message: string;
  }

  let snapshot = $state<ServicesRuntimeSnapshot | null>(null);
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
  //
  // v0.2.94: the entry is validated first-party-only. ANTHROPIC_MODEL is
  // what a RESTARTED Claude Code panel resumes on, and this control's
  // pre-selected GLM id is how one reached a real settings.json on
  // 2026-09-08. The Python writer refuses a vendor id independently; this is
  // the inline error that stops the user finding out after the write.
  let setDefaultModel = $state(false);
  let modelChoice = $state(DEFAULT_GATEWAY_MODEL);
  let removeSlots = $state(false);
  const modelError = $derived(setDefaultModel ? defaultModelError(modelChoice) : '');

  // Per-project CLAUDE.md routing-guidance flags, keyed by project id.
  let guidance = $state<Record<string, boolean>>({});

  const gwLine = $derived(describeStatus(gw));
  // Two facts the status line deliberately does not fold in: who (if anyone)
  // supervises the process, and how long the Claude login it proxies has
  // left. Both are `null` when there is nothing worth saying.
  const gwSupervision = $derived(describeSupervision(gw));
  const gwOAuth = $derived(describeOAuthExpiry(gw));
  // v0.2.95 (R5c): the login registration's THIRD state — present but unable
  // to run — and what the detached hub supervisor concluded when it stopped
  // retrying. Both `null` when there is nothing to say; neither is ever
  // folded into the "Start at login" checkbox, which answers a different
  // (binary) question.
  const gwRegistration = $derived(describeRegistration(gw));
  const gwHubCondition = $derived(describeHubCondition(gw));
  // Why a gateway that is UP may still serve nothing: its secret scope.
  const gwSecretScope = $derived(describeSecretScope(gw));
  const gwUsageLedger = $derived(describeUsageLedger(gw));
  // Only ever set by a START, and only shown when the proof REFUSED.
  const gwDogfood = $derived(describeDogfood(gw));
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
    if (modelError) return;
    await gwAction(async () => {
      vsResult = await pointPanelAtGateway({
        path: vsSelected!,
        model: setDefaultModel ? modelChoice : null,
        removeSlotOverrides: removeSlots,
        // The port THIS card is describing, when it has proof one is live.
        // Without it the Rust side re-resolves and can write a base URL
        // naming a different process (R1-1, same class); null when nothing
        // is running keeps the pre-v0.2.94 resolution.
        port: pointPanelPort(gw),
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

  // ─── Where a service runs (v0.2.97) ──────────────────────────────────
  let endpointDialogOpen = $state(false);
  let endpointReport = $state<CandidateReport | null>(null);
  let endpointOnly = $state<CoreServiceName | null>(null);
  // "Let VCO manage it" confirmation: the service it is about, or null.
  let handTarget = $state<ServiceRuntimeState | null>(null);
  let handOpen = $state(false);
  let handBusy = $state(false);

  /** Re-detect and open the dialog — for one service ("Change…") or all. */
  async function openEndpointDialog(service: CoreServiceName | null) {
    endpointOnly = service;
    endpointReport = null;
    endpointDialogOpen = true;
    error = null;
    try {
      const detection = await getEndpointCandidates(service ?? undefined);
      endpointReport = buildCandidateReport(detection, rowsFromSnapshot(snapshot), pendingFromSnapshot(snapshot));
    } catch (e) {
      endpointDialogOpen = false;
      error = `Detecting services failed: ${String(e)}`;
    }
  }

  function askHandToVco(svc: ServiceRuntimeState) {
    handTarget = svc;
    handOpen = true;
  }

  async function confirmHandToVco() {
    if (!handTarget || !isCoreService(handTarget.name)) return;
    handBusy = true;
    error = null;
    try {
      await runEndpointAction({ action: 'hand_to_vco', service: handTarget.name });
      handOpen = false;
      await refresh();
    } catch (e) {
      error = String(e);
    } finally {
      handBusy = false;
    }
  }

  // ─── Move to another port (v0.2.97, R7b F22) ─────────────────────────
  let moveOpen = $state(false);
  let moveTarget = $state<{ service: MovableService; label: string; port: number } | null>(null);
  let movePort = $state('');
  let moveGrpc = $state('');
  let moveBusy = $state(false);
  let moveLines = $state<string[]>([]);
  let moveFailed = $state(false);
  const movePortError = $derived(moveTarget ? checkMovePort(movePort, moveTarget.port) : null);
  const moveGrpcError = $derived(moveTarget?.service === 'weaviate' ? checkGrpcPort(moveGrpc, movePort) : null);

  function openMove(svc: ServiceRuntimeState, service: MovableService) {
    moveTarget = { service, label: serviceLabel(svc.name), port: svc.port };
    movePort = '';
    moveGrpc = '';
    moveLines = [];
    moveFailed = false;
    moveOpen = true;
  }

  async function confirmMove() {
    if (!moveTarget || movePortError || moveGrpcError) return;
    moveBusy = true;
    moveLines = [];
    moveFailed = false;
    try {
      const out = await moveService(moveRequest(moveTarget.service, movePort, moveGrpc));
      moveLines = resultLines(out.output);
    } catch (e) {
      moveFailed = true;
      moveLines = resultLines(String(e));
    } finally {
      moveBusy = false;
      await refresh();
    }
  }

  async function onRowAction(svc: ServiceRuntimeState, action: ServiceActionId) {
    switch (action) {
      case 'start':
        return runServiceAction(svc.name, 'service_start');
      case 'stop':
        return runServiceAction(svc.name, 'service_stop');
      case 'restart':
        return runServiceAction(svc.name, 'service_restart');
      case 'recover':
        return recoverZombie(svc.name);
      case 'change':
        return isCoreService(svc.name) ? openEndpointDialog(svc.name) : undefined;
      case 'hand_to_vco':
        return askHandToVco(svc);
    }
  }

  async function refresh() {
    try {
      snapshot = await getServicesStatus();
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

  // Per-service action wrapper. A `container_missing: …` error (the row's
  // container is gone) opens the adoption dialog for that service; a
  // `no_lifecycle: …` error (an external endpoint) is shown as it is. The
  // prefixes are the ERR_KIND_* constants in
  // launcher/src-tauri/src/commands/lifecycle.rs.
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
      error = msg;
      if (msg.startsWith('container_missing:') && isCoreService(name)) {
        await openEndpointDialog(name);
      }
    } finally {
      loading = false;
    }
  }

  // Recover a stuck (zombie) service — podman state-DB desync: `podman ps`
  // says "Up" but the main PID is dead. `recover_zombie` is row-gated: VCO's
  // own container is removed and brought back up; an adopted container is
  // only restarted by name, never removed.
  async function recoverZombie(name: string) {
    loading = true;
    error = null;
    try {
      await invoke('recover_zombie', { name });
      await refresh();
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
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
      <button onclick={() => openEndpointDialog(null)} disabled={loading} class="secondary">
        Detect services
      </button>
    </div>

    {#if snapshot.endpoints_missing}
      <div class="banner warn" data-testid="endpoints-missing">
        <strong>Where some services run is not recorded yet.</strong>
        A finished install or update records it. You can choose now:
        <button class="linklike" onclick={() => openEndpointDialog(null)} disabled={loading}>
          Detect services
        </button>
      </div>
    {/if}
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
          <th>Where it runs</th>
          <th>Port</th>
          <th>Container &amp; data</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {#each snapshot.services as svc (svc.name)}
          {@const badge = modeBadge(svc)}
          {@const mount = parseDataMount(svc.endpoint?.data_mount_json)}
          {@const move = moveOffer(svc)}
          <tr data-testid="service-row-{svc.name}">
            <td><strong>{serviceLabel(svc.name)}</strong></td>
            <td>
              <span class="status {svc.running ? 'up' : 'down'}">
                {svc.running ? 'running' : 'stopped'}
              </span>
              {#if svc.zombie}
                <span
                  class="tag tag-zombie"
                  title="The container exists but its main process is dead (state-DB desync). Use Recover."
                >stuck</span>
              {/if}
            </td>
            <td class="mode-cell">
              <span class="mode-badge {badge.tone}" title={badge.title}>{badge.label}</span>
            </td>
            <td>
              {svc.port}
              {#if svc.endpoint?.grpc_port}
                <span class="muted small">gRPC {svc.endpoint.grpc_port}</span>
              {/if}
            </td>
            <td class="container-cell">
              {#if svc.container_name}
                <code>{svc.container_name}</code>
              {:else}
                <span class="muted">no container</span>
              {/if}
              {#if svc.endpoint}
                <span class="mount" title="Where this service keeps its data">{describeDataMount(mount)}</span>
              {/if}
            </td>
            <td class="actions-cell">
              {#each serviceActions(svc) as action (action)}
                <button
                  onclick={() => onRowAction(svc, action)}
                  disabled={loading}
                  class={action === 'change' || action === 'hand_to_vco' ? 'secondary' : action === 'recover' ? 'recover' : ''}
                >
                  {action === 'change' && svc.pending_choice ? 'Choose…' : ACTION_LABELS[action]}
                </button>
              {/each}
              {#if move.kind === 'move'}
                <button class="secondary" onclick={() => openMove(svc, move.service)} disabled={loading}>
                  Move to another port…
                </button>
              {:else if move.kind === 'follows_owner'}
                <span class="muted small follows-owner">{move.note}</span>
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

    {#if gwSupervision}
      <p class="gw-detail {gwSupervision.tone}">
        <strong>{gwSupervision.label}.</strong>
        {gwSupervision.detail}
      </p>
    {/if}
    {#if gwOAuth}
      <p class="gw-detail {gwOAuth.tone}">
        <strong>{gwOAuth.label}.</strong>
        {gwOAuth.detail}
      </p>
    {/if}
    {#if gwRegistration}
      <p class="gw-detail {gwRegistration.tone}">
        <strong>{gwRegistration.label}.</strong>
        {gwRegistration.detail}
        {#if gwRegistration.tone === 'down'}
          Re-run <code>python install.py --update</code> from the orchestrator
          root — it re-renders this registration from the install venv and
          verifies it before writing.
        {/if}
      </p>
    {/if}
    {#if gwHubCondition}
      <div class="banner error">
        <strong>{gwHubCondition.label}.</strong>
        {gwHubCondition.detail}
      </div>
    {/if}
    {#if gwSecretScope}
      <p class="gw-detail {gwSecretScope.tone}">
        <strong>{gwSecretScope.label}.</strong>
        {gwSecretScope.detail}
      </p>
    {/if}
    {#if gwDogfood}
      <div class="banner error">
        <strong>{gwDogfood.label}.</strong>
        {gwDogfood.detail}
      </div>
    {/if}

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
        {#if gw.health.secret_scope}
          <dt>vendor key scope</dt>
          <dd>
            <code>{gw.health.secret_scope.project}</code>
            {gw.health.secret_scope.resolvable === true
              ? '— resolves'
              : gw.health.secret_scope.resolvable === false
                ? '— does NOT resolve'
                : '— not probed yet'}
          </dd>
        {/if}
        {#if gwUsageLedger}
          <dt>usage ledger</dt>
          <dd>{gwUsageLedger}</dd>
        {/if}
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
            class:invalid={!!modelError}
            type="text"
            bind:value={modelChoice}
            disabled={gwBusy}
            aria-label="Default model id"
            aria-invalid={!!modelError}
            aria-describedby={modelError ? 'gw-model-error' : undefined}
          />
          {#if modelError}
            <span class="gw-model-error" id="gw-model-error" role="alert">{modelError}</span>
          {/if}
        {/if}
        {#if vsInspection && vsInspection.slot_overrides.length > 0}
          <label class="gw-toggle">
            <input type="checkbox" bind:checked={removeSlots} disabled={gwBusy} />
            Also remove the tier/subagent overrides already in this file
          </label>
        {/if}
      </div>
      <p class="muted small">
        Leaving the Default entry unset keeps whatever you already chose. The
        Default must be a Claude id: it is what a RESTARTED panel falls back
        to, so a vendor model there answers sessions you believe are on
        Claude — pick vendor models in the /model picker, which is what the
        gateway's catalogue is for. VCO
        never sets the Opus / Sonnet / Haiku / Fable tier slots or the subagent
        slot either: the name you pick in the picker has to be the model that answers.
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
          disabled={gwBusy || !vsSelected || !gwConfigured || !!modelError}
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

  <ExternalServicesDialog
    bind:open={endpointDialogOpen}
    bind:report={endpointReport}
    only={endpointOnly}
    listenForBoot={false}
    onchanged={refresh}
  />

  <DialogRoot bind:open={handOpen} ariaLabelledBy="hand-to-vco-title" width="560px">
    {#snippet header()}
      <h2 id="hand-to-vco-title">Let VCO manage {handTarget ? serviceLabel(handTarget.name) : ''}?</h2>
    {/snippet}
    {#snippet body()}
      {#if handTarget}
        {@const handMount = parseDataMount(handTarget.endpoint?.data_mount_json)}
        <p>
          VCO will take over your container <code>{handTarget.container_name}</code>: stop it,
          remove the container (never its data), and recreate it from VCO’s compose file with
          VCO’s settings — keeping the SAME data:
        </p>
        <p class="hand-mount"><code>{describeDataMount(handMount)}</code></p>
        <p class="muted small">
          VCO checks the new container mounts exactly that data before it starts, and again
          after; on any mismatch it puts your original container back. From then on VCO
          starts, stops and heals it.
        </p>
      {/if}
    {/snippet}
    {#snippet footer()}
      <div class="bulk-actions">
        <button class="secondary" onclick={() => (handOpen = false)} disabled={handBusy}>Cancel</button>
        <button onclick={confirmHandToVco} disabled={handBusy || !handTarget?.endpoint?.data_mount_json}>
          {handBusy ? 'Handing over…' : 'Let VCO manage it'}
        </button>
      </div>
    {/snippet}
  </DialogRoot>

  <DialogRoot bind:open={moveOpen} ariaLabelledBy="move-service-title" width="560px">
    {#snippet header()}
      <h2 id="move-service-title">Move {moveTarget?.label ?? ''} to another port</h2>
    {/snippet}
    {#snippet body()}
      {#if moveTarget}
        <p>
          VCO re-creates its {moveTarget.label} container on the new port with the SAME data, checks
          that it answers there, and puts it back on port {moveTarget.port} if it does not. Every
          project's settings and the MCP registration follow.
        </p>
        <label class="move-field">
          New port
          <input
            class="gw-model"
            class:invalid={movePort !== '' && movePortError !== null}
            inputmode="numeric"
            bind:value={movePort}
            placeholder={String(moveTarget.port + 1)}
            disabled={moveBusy}
          />
        </label>
        {#if movePort !== '' && movePortError}
          <p class="gw-model-error">{movePortError}</p>
        {/if}
        {#if moveTarget.service === 'weaviate'}
          <label class="move-field">
            gRPC port (optional — empty keeps its distance from the HTTP port)
            <input
              class="gw-model"
              class:invalid={moveGrpcError !== null}
              inputmode="numeric"
              bind:value={moveGrpc}
              disabled={moveBusy}
            />
          </label>
          {#if moveGrpcError}
            <p class="gw-model-error">{moveGrpcError}</p>
          {/if}
        {/if}
        {#if moveLines.length > 0}
          <div class="banner {moveFailed ? 'error' : 'info'} move-result">
            {#each moveLines as line, i (i)}
              <div>{line}</div>
            {/each}
          </div>
        {/if}
      {/if}
    {/snippet}
    {#snippet footer()}
      <div class="bulk-actions">
        <button class="secondary" onclick={() => (moveOpen = false)} disabled={moveBusy}>Close</button>
        <button onclick={confirmMove} disabled={moveBusy || movePortError !== null || moveGrpcError !== null}>
          {moveBusy ? 'Moving…' : 'Move'}
        </button>
      </div>
    {/snippet}
  </DialogRoot>
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
  .gw-model.invalid {
    border-color: var(--color-pink, #ff4fa0);
  }
  .gw-model-error {
    flex-basis: 100%;
    color: var(--color-pink, #ff4fa0);
    font-size: 0.8rem;
  }
  /* R7b F22: the move dialog + the adopted row's "VCO follows" note. */
  .move-field {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    margin: 0.6rem 0 0.2rem;
    color: var(--color-mid, #94a3b8);
    font-size: 0.85rem;
  }
  .move-field input {
    max-width: 10rem;
  }
  .move-result {
    font-family: ui-monospace, monospace;
    font-size: 0.8rem;
    margin-top: 0.8rem;
  }
  .follows-owner {
    display: block;
    max-width: 16rem;
    margin-top: 0.3rem;
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

  /* v0.2.97: where a service runs. */
  .mode-badge {
    display: inline-block;
    font-size: 0.78rem;
    padding: 0.15rem 0.55rem;
    border-radius: 999px;
    border: 1px solid var(--color-border);
    white-space: nowrap;
  }
  .mode-badge.managed {
    color: var(--color-teal);
    border-color: rgba(0, 191, 166, 0.4);
  }
  .mode-badge.adopted {
    color: var(--color-purple);
    border-color: rgba(123, 95, 255, 0.4);
  }
  .mode-badge.external {
    color: var(--color-mid);
  }
  .mode-badge.pending {
    color: var(--color-pink);
    border-color: rgba(255, 79, 160, 0.4);
  }
  .container-cell .mount {
    display: block;
    font-size: 0.75rem;
    color: var(--color-mid);
    overflow-wrap: anywhere;
  }
  .hand-mount {
    margin: 0.25rem 0 0.75rem;
  }
  .linklike {
    background: none;
    border: none;
    color: var(--color-teal);
    text-decoration: underline;
    cursor: pointer;
    padding: 0;
    font: inherit;
  }
</style>
