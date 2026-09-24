<script lang="ts">
  // ExternalServicesDialog — "where should this service run?"
  //
  // v0.2.97 (service endpoints SSOT). Two ways in:
  //   * launcher boot: `commands::lifecycle::auto_start_on_boot` emits
  //     `vct-external-services-detected` with the Python detector's
  //     candidate report when a service's endpoint is undecided — above all
  //     a third-party Weaviate, which VCO never adopts without the user's
  //     explicit confirmation (owner ruling Q1). Mounted once in
  //     +layout.svelte, so any route shows it.
  //   * the Services page's "Change…" / "Detect services", which passes a
  //     `report` (and optionally the one service to show).
  //
  // Per service the user either picks a candidate ("Use this one") or runs
  // VCO's own copy on a free port. Both are `python -m
  // vco_lib.service_endpoints` verbs (the rows' one writer), which also
  // re-project every project, refresh the MCP registration and resolve the
  // UPDATE_DEFERRED entry that asked the question (for the third-party
  // Weaviate: `service_adoption_confirmation_required`, whose row is the
  // DISABLED `vco_managed` Weaviate — "waiting for your choice"; nothing
  // starts or heals it meanwhile). There is no "refuse" and
  // no "reset": "Decide later" leaves the question open, and the launcher
  // asks again next time.

  import { onMount } from 'svelte';
  import { listen } from '$lib/tauri';
  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import {
    adoptActionFor,
    adoptNeedsKgConfirm,
    buildCandidateReport,
    candidateFacts,
    CORE_SERVICES,
    isAdoptable,
    pendingServices,
    runEndpointAction,
    serviceLabel,
    vcoCopyAction,
    vcoCopyNeedsKgConfirm,
    vcoDataLine,
    type CandidateReport,
    type ChoiceEvent,
    type CoreServiceName,
    type EndpointAction,
    type EndpointCandidate,
  } from '$lib/api/service_endpoints';

  let {
    open = $bindable(false),
    report = $bindable<CandidateReport | null>(null),
    only = null,
    listenForBoot = true,
    onchanged,
  }: {
    open?: boolean;
    report?: CandidateReport | null;
    /** Show just this service (the Services page's "Change…"). */
    only?: CoreServiceName | null;
    /** The +layout instance listens for the boot event; page instances don't. */
    listenForBoot?: boolean;
    /** Called after a verb succeeded, so the caller re-reads the snapshot. */
    onchanged?: () => void;
  } = $props();

  let busy = $state<string | null>(null);
  let error = $state<string | null>(null);
  let done = $state<Record<string, string>>({});
  let kgConfirmed = $state<Record<string, boolean>>({});

  const shown = $derived.by((): CoreServiceName[] => {
    if (!report) return [];
    if (only) return report.services[only] ? [only] : [];
    const pending = pendingServices(report);
    return pending.length > 0 ? pending : CORE_SERVICES.filter((s) => s !== 'code_embed' && report?.services[s]);
  });

  function candidateTitle(c: EndpointCandidate): string {
    return c.container_name ?? c.url;
  }

  async function run(service: CoreServiceName, action: EndpointAction, key: string) {
    busy = key;
    error = null;
    try {
      await runEndpointAction(action);
      done = {
        ...done,
        [service]: action.action === 'use_vco_copy' ? "VCO's own copy" : 'the endpoint you picked',
      };
      onchanged?.();
      if (shown.every((s) => done[s])) {
        open = false;
      }
    } catch (e) {
      error = String(e);
    } finally {
      busy = null;
    }
  }

  function decideLater() {
    open = false;
  }

  function reset() {
    done = {};
    kgConfirmed = {};
    error = null;
  }

  onMount(() => {
    if (!listenForBoot) return;
    let unlisten: (() => void) | null = null;
    listen<ChoiceEvent>('vct-external-services-detected', (e) => {
      const p = e.payload;
      report = p ? buildCandidateReport(p.detection, p.rows ?? {}, p.pending ?? []) : null;
      reset();
      open = pendingServices(report).length > 0;
    }).then((u) => {
      unlisten = u;
    });
    return () => unlisten?.();
  });
</script>

<DialogRoot bind:open width="720px" ariaLabelledBy="endpoint-dialog-title" onClose={reset}>
  {#snippet header()}
    <h2 id="endpoint-dialog-title" class="title">
      {only ? `Where should ${serviceLabel(only)} run?` : 'Where should VCO’s services run?'}
    </h2>
    <p class="lead">
      VCO found services it did not start. Pick one to use as it is, or let VCO run
      its own copy next to it on a free port. VCO never starts a second copy of a
      service you already run without asking.
    </p>
  {/snippet}

  {#snippet body()}
    {#if !report}
      <p class="muted">Detecting services…</p>
    {:else if shown.length === 0}
      <p class="muted">Nothing to decide: every service has an endpoint.</p>
    {/if}
    {#if report?.error}
      <p class="error" role="alert">
        Detecting existing services failed ({report.error}). You can still run VCO’s own copy.
      </p>
    {/if}
    {#each shown as service (service)}
      {@const sc = report?.services[service]}
      <section class="svc glass-card" data-testid="endpoint-service-{service}">
        <header class="svc-head">
          <h3>{serviceLabel(service)}</h3>
          {#if done[service]}
            <span class="badge ok">Now using {done[service]}</span>
          {:else if sc?.pending_consent}
            <span class="badge wait">Your choice needed</span>
          {/if}
        </header>
        {#if sc?.reason}
          <p class="reason">{sc.reason}</p>
        {/if}
        {#if service === 'weaviate'}
          <p class="note">
            Using an existing Weaviate adds VCO’s own collections (named after your
            projects) to it; nothing already in it is changed.
          </p>
        {/if}

        {#if (sc?.candidates.length ?? 0) === 0}
          <p class="muted">No existing {serviceLabel(service)} was found.</p>
        {/if}
        <ul class="candidates">
          {#each sc?.candidates ?? [] as c, i (i)}
            {@const needsKgForThis = adoptNeedsKgConfirm(service, sc, c)}
            <li class="cand" class:recommended={c.recommended} class:incompatible={!isAdoptable(c)}>
              <div class="cand-main">
                <div class="cand-title">
                  <code>{candidateTitle(c)}</code>
                  {#if c.recommended}<span class="badge rec">Recommended</span>{/if}
                  {#if c.current}<span class="badge cur">In use</span>{/if}
                </div>
                <div class="cand-url"><code>{c.url}</code></div>
                {#if candidateFacts(c).length > 0}
                  <div class="cand-facts">{candidateFacts(c).join(' · ')}</div>
                {/if}
                <div class="cand-data">{vcoDataLine(service, c)}</div>
                {#if !isAdoptable(c)}
                  <div class="cand-bad">Not usable: {c.incompatible_reason ?? 'incompatible'}</div>
                {/if}
              </div>
              <button
                class="btn-3d {c.recommended ? 'btn-3d-primary' : 'btn-3d-ghost'}"
                disabled={!!busy ||
                  !isAdoptable(c) ||
                  c.current === true ||
                  !!done[service] ||
                  (needsKgForThis && !kgConfirmed[service])}
                title={needsKgForThis && !kgConfirmed[service]
                  ? 'This instance holds no VCO data — confirm below that VCO’s knowledge graph restarts empty.'
                  : undefined}
                onclick={() => run(service, adoptActionFor(service, c, needsKgForThis), `${service}:${i}`)}
              >
                {busy === `${service}:${i}` ? 'Applying…' : 'Use this one'}
              </button>
            </li>
          {/each}
        </ul>

        {#if service !== 'code_embed'}
          {@const needsKg = vcoCopyNeedsKgConfirm(service, sc)}
          <div class="own-copy">
            {#if needsKg || (sc?.candidates ?? []).some((c) => adoptNeedsKgConfirm(service, sc, c))}
              <label class="confirm">
                <input
                  type="checkbox"
                  checked={kgConfirmed[service] ?? false}
                  onchange={(e) => (kgConfirmed = { ...kgConfirmed, [service]: e.currentTarget.checked })}
                />
                I understand: VCO’s knowledge graph starts empty in the new copy — it
                re-seeds from each project’s <code>knowledge/</code> folder, and the code
                graph re-analyses.
              </label>
            {/if}
            <button
              class="btn-3d btn-3d-secondary"
              disabled={!!busy || !!done[service] || (needsKg && !kgConfirmed[service])}
              onclick={() => run(service, vcoCopyAction(service, needsKg), `${service}:own`)}
            >
              {busy === `${service}:own` ? 'Applying…' : 'Run VCO’s own copy'}
            </button>
          </div>
        {/if}
      </section>
    {/each}
    {#if error}
      <p class="error" role="alert">{error}</p>
    {/if}
  {/snippet}

  {#snippet footer()}
    <div class="actions">
      <button class="btn-3d btn-3d-ghost" onclick={decideLater} disabled={!!busy}>
        {shown.every((s) => done[s]) ? 'Close' : 'Decide later'}
      </button>
    </div>
  {/snippet}
</DialogRoot>

<style>
  .title {
    margin: 0 0 0.35rem;
    font-size: 1.2rem;
    font-weight: 700;
    color: var(--color-text);
  }
  .lead,
  .muted {
    margin: 0 0 0.75rem;
    color: var(--color-mid);
    font-size: 0.9rem;
  }
  .svc {
    padding: 1rem 1.1rem;
    margin-bottom: 0.9rem;
  }
  .svc-head {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.4rem;
  }
  .svc-head h3 {
    margin: 0;
    font-size: 1rem;
    color: var(--color-text);
  }
  .reason,
  .note {
    margin: 0 0 0.6rem;
    font-size: 0.85rem;
    color: var(--color-mid);
  }
  .candidates {
    list-style: none;
    margin: 0 0 0.75rem;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .cand {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.6rem 0.75rem;
    border: 1px solid var(--color-border);
    border-radius: 12px;
    background: var(--color-bg2);
  }
  .cand.recommended {
    border-color: rgba(0, 191, 166, 0.45);
  }
  .cand.incompatible {
    opacity: 0.65;
  }
  .cand-main {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }
  .cand-title {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }
  .cand-url,
  .cand-facts,
  .cand-data {
    font-size: 0.8rem;
    color: var(--color-mid);
    overflow-wrap: anywhere;
  }
  .cand-bad {
    font-size: 0.8rem;
    color: var(--color-pink);
  }
  code {
    font-family: 'JetBrains Mono', ui-monospace, monospace;
    font-size: 0.82rem;
  }
  .badge {
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 0.12rem 0.45rem;
    border-radius: 999px;
    border: 1px solid var(--color-border);
  }
  .badge.rec,
  .badge.ok {
    color: var(--color-teal);
    border-color: rgba(0, 191, 166, 0.4);
  }
  .badge.wait {
    color: var(--color-pink);
    border-color: rgba(255, 79, 160, 0.4);
  }
  .badge.cur {
    color: var(--color-purple);
    border-color: rgba(123, 95, 255, 0.4);
  }
  .own-copy {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.5rem;
  }
  .confirm {
    display: flex;
    gap: 0.5rem;
    align-items: flex-start;
    font-size: 0.82rem;
    color: var(--color-mid);
  }
  .error {
    color: var(--color-pink);
    margin: 0.5rem 0 0;
    font-size: 0.85rem;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
  }
</style>
