<script lang="ts">
  // Home / Library — renders the current orchestrator install's module catalog.
  //
  // Source of truth: `list_module_catalog` (commands::modules in Rust). The
  // built-in entries are launcher + orchestrator + KG + Code Graph, plus
  // one explicit Coming-Soon entry (RL Reranker, Pro tier). The home page
  // does not advertise modules that don't exist yet.
  //
  // Layout chrome (MenuBar, Sidebar, StatusBar, modals) lives in
  // +layout.svelte so it persists across every route. This page is just
  // the home content.

  import { onMount } from 'svelte';
  import RightSidebar from '$lib/components/RightSidebar.svelte';
  // v0.2.22 — Item #12: first-class banner when neither Podman nor
  // Docker is detected. Renders nothing when at least one runtime is
  // present, so the home page is unchanged in the happy-path case.
  import RuntimeMissingBanner from '$lib/components/RuntimeMissingBanner.svelte';
  import { auth } from '$lib/stores/auth';
  import { orchestrator } from '$lib/stores/orchestrator';
  import { modules, installedIds } from '$lib/stores/modules';
  import { ui } from '$lib/stores/ui';
  import { selectedProject } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';
  import {
    detectModuleErrorAfterAction,
    installProgressLabel,
    resolveProjectScopedAction,
  } from '$lib/module-status-display';
  import type { ModuleCatalogAction } from '$lib/module-status-display';
  import { getColorRgb } from '$lib/color-rgb';
  import type { ModuleCatalogEntry } from '$lib/types/launcher';

  onMount(() => {
    // v0.2.32 UB2 (2026-05-23): orchestrator.checkStatus() moved up to
    // +layout.svelte's onMount + periodic refresh (so the status badge
    // refreshes regardless of which route the launcher opens to, and
    // doesn't go stale after install/uninstall from another process).
    // We DON'T re-call it here — the layout already did.
    //
    // Populate `system` (has_podman / has_docker) so RuntimeMissingBanner
    // can decide whether to render. The banner self-triggers detection
    // too as a fallback, but the home page is the first surface a user
    // sees so triggering here avoids the brief "no banner yet" window.
    void orchestrator.detectSystem();
    modules.loadCatalog();
    // v0.2.92: proactively load the per-project install rows (not just
    // after an action resolves) so `resolveProjectScopedAction` can gate
    // the card's button correctly BEFORE the user ever clicks it — see
    // the `$effect` below for the project-change case (mirrors the same
    // onMount + $effect pattern ModuleCatalog.svelte already uses).
    // `loadInstalledSpeculative` (not the plain `loadInstalled`) — this
    // call isn't part of a user action that already narrates its own
    // outcome, so a failure needs its own one-shot toast (see the
    // store's docstring); until it resolves (or if it fails),
    // `hasInstallRowForProject` below reads `null` (unknown) rather than
    // guessing `false`.
    if ($selectedProject) {
      void modules.loadInstalledSpeculative($selectedProject.id);
    }
    const handleFocus = () => auth.refreshProfile();
    window.addEventListener('focus', handleFocus);
    return () => window.removeEventListener('focus', handleFocus);
  });

  // v0.2.92: reload the per-project install rows whenever the selected
  // project changes, so switching projects doesn't leave a stale
  // "Update"/"Retry" button pointed at a project that has no row for
  // this module (see `resolveProjectScopedAction`).
  $effect(() => {
    const project = $selectedProject;
    if (project) {
      void modules.loadInstalledSpeculative(project.id);
    }
  });

  const orchState = $derived($orchestrator);
  const modulesState = $derived($modules);

  // Card view-model derived from the catalog. Pure presentation — colour,
  // icon glyph, click target. No business logic, no hardcoded ids beyond
  // mapping known categories to a colour scheme.
  interface AppCard {
    entry: ModuleCatalogEntry;
    color: 'teal' | 'purple' | 'pink';
    icon: string;
    badge: string;
    badgeKind:
      | 'bundled'
      | 'installed'
      | 'available'
      | 'update_available'
      | 'broken'
      | 'subcomponent'
      | 'coming_soon';
    // v0.2.92: the project-aware action for this card, resolved once
    // here so the template and `handleCardModuleAction` agree on exactly
    // what clicking the button does (see `resolveProjectScopedAction`).
    action: ModuleCatalogAction | null;
    // v0.2.92: true while we don't yet know whether the selected project
    // has an install row for this module (store still loading / errored
    // / not yet requested). The template renders `action` DISABLED while
    // this is true, rather than guessing which operation is correct.
    pending: boolean;
  }

  function colorFor(e: ModuleCatalogEntry): 'teal' | 'purple' | 'pink' {
    if (e.id === 'vct-launcher') return 'pink';
    if (e.id === 'orchestrator') return 'teal';
    if (e.kind === 'subcomponent') return 'purple';
    if (e.kind === 'coming_soon') return 'pink';
    return 'teal';
  }

  function iconFor(e: ModuleCatalogEntry): string {
    // First letter of the name; specific overrides for clarity.
    if (e.id === 'vct-launcher') return 'L';
    if (e.id === 'orchestrator') return 'O';
    if (e.id === 'knowledge-graph') return 'K';
    if (e.id === 'code-graph') return 'C';
    if (e.id === 'rl-reranker') return 'R';
    return e.name.charAt(0).toUpperCase();
  }

  function badgeFor(e: ModuleCatalogEntry): string {
    if (e.kind === 'bundled') return 'Bundled';
    if (e.kind === 'installed') return 'Installed';
    if (e.kind === 'update_available') return 'Update available';
    if (e.kind === 'broken') return 'Reinstall needed';
    if (e.kind === 'subcomponent') return 'Included';
    if (e.kind === 'coming_soon') {
      const tier = (e.coming_soon_tier ?? '').toUpperCase();
      const tierLabel = tier ? `${tier} · ` : '';
      const target = e.coming_soon_target ? ` (${e.coming_soon_target})` : '';
      return `${tierLabel}Coming Soon${target}`;
    }
    return 'Available';
  }

  // v0.2.92: tri-state signal for "does the SELECTED project have an
  // install row for module X" — `null` (unknown) unless the store's
  // `installed` array demonstrably reflects THIS project (matched by id)
  // from a load that actually succeeded. Computed once per render rather
  // than per-card so every card in the same render agrees on whether the
  // project's state is known yet.
  //
  // CLAUDE.md "Conservative defaults on best-effort paths": a project
  // switch racing ahead of `loadInstalledSpeculative`'s resolve, or that
  // load failing outright, must NOT be read as "loaded and empty" — both
  // leave `modulesState.installedProjectId` NOT matching the selected
  // project, which is exactly what this check guards against.
  let installedKnownForSelectedProject = $derived(
    $selectedProject !== null && modulesState.installedProjectId === $selectedProject.id,
  );

  let cards = $derived<AppCard[]>(
    modulesState.catalog.map((entry) => {
      // v0.2.92: cross-check the catalog's (cross-project) `kind` against
      // this project's actual install rows before deciding what the
      // button does — see `resolveProjectScopedAction` for the full rationale.
      const hasRowForProject: boolean | null = installedKnownForSelectedProject
        ? $installedIds.has(entry.id)
        : null;
      const { kindOverride, action, pending } = resolveProjectScopedAction(
        entry.kind,
        hasRowForProject,
      );
      // `badgeFor` already owns every kind → text mapping (including
      // 'available' → 'Available'); route the override THROUGH it rather
      // than hardcoding the override's display text a second time here.
      const effectiveEntry = kindOverride ? { ...entry, kind: kindOverride } : entry;
      return {
        entry,
        color: colorFor(entry),
        icon: iconFor(entry),
        badge: badgeFor(effectiveEntry),
        badgeKind: entry.kind,
        action,
        pending,
      };
    })
  );

  let selectedCard = $state<AppCard | null>(null);

  function getColorVar(color: 'teal' | 'purple' | 'pink'): string {
    if (color === 'teal') return 'var(--color-teal)';
    if (color === 'purple') return 'var(--color-purple)';
    return 'var(--color-pink)';
  }

  function selectCard(c: AppCard) {
    selectedCard = selectedCard?.entry.id === c.entry.id ? null : c;
  }

  function handleCardAction(c: AppCard) {
    // 1. Orchestrator MCP dashboard if running.
    if (c.entry.id === 'orchestrator' && orchState.status === 'installed') {
      ui.openMcpDashboard();
      return;
    }
    // 2. Subcomponent CTA (e.g. KG → /kg).
    if (c.entry.kind === 'subcomponent' && c.entry.cta_route) {
      window.location.assign(c.entry.cta_route);
      return;
    }
    // 3. Coming-soon: open the right sidebar with the description; the
    //    Learn-more CTA over there can later link to a roadmap page or
    //    waitlist form. We do NOT advance to install.
    selectCard(c);
  }

  // Per-card action (Reinstall / Retry / Update / Install) for actionable
  // kinds. The catalog `kind` → {label, method} mapping is centralised in
  // `resolveProjectScopedAction` (which itself wraps `moduleActionForKind` with
  // a project-scoped override — see its docstring) so Home, RightSidebar,
  // and ModuleCatalog stay in lockstep. install/update are per-project, so
  // a project must be selected (the button is disabled + tooltipped
  // otherwise). UPSERT-safe commands, so a double-click can't corrupt the
  // row. `c.action` (not a fresh `moduleActionForKind` call) is used here
  // so the dispatched command always matches what the button rendered.
  let cardActionBusyId = $state<string | null>(null);

  async function handleCardModuleAction(e: MouseEvent, c: AppCard) {
    e.stopPropagation(); // don't also toggle the right sidebar
    const action = c.action;
    if (!action) return;
    // v0.2.92: backstop — the button is already rendered `disabled` while
    // `c.pending` is true (see the template), but a click that somehow
    // slips through (e.g. a queued event from just before the disabled
    // attribute landed) must not fire a guessed operation.
    if (c.pending) return;
    const project = $selectedProject;
    if (!project) {
      toast.error('Select a project first to install or update modules.');
      return;
    }
    cardActionBusyId = c.entry.id;
    // Toast key for the bell inbox: an error and a later success for the
    // SAME module action cancel out (auto-resolve).
    const toastKey = `module:${c.entry.id}:${action.method}`;
    console.info('[home] module action start', {
      module: c.entry.id,
      method: action.method,
      project: project.id,
    });
    try {
      const row =
        action.method === 'install'
          ? await modules.install(project.id, c.entry.id)
          : await modules.update(project.id, c.entry.id);

      // CRITICAL: install_module_for_project / update_module_for_project
      // resolve even when the CONTAINER START failed — but the resolved row
      // can be misleadingly clean (status='installed', last_error=null);
      // the real failure only surfaces once the catalog recomputes `kind`
      // to 'error'/'broken' (verified via live test 2026-06-06, RL docker
      // exit 125). So reload BOTH surfaces and inspect them together rather
      // than trusting the immediate row (see detectModuleErrorAfterAction).
      await modules.loadCatalog();
      await modules.loadInstalled(project.id);
      console.info('[home] module action returned row', {
        module: c.entry.id,
        status: row?.status,
        last_error: row?.last_error,
        container_name: row?.container_name,
      });
      const errMsg = detectModuleErrorAfterAction(
        c.entry.id,
        $modules.catalog,
        $modules.installed,
      );
      if (errMsg) {
        toast.error(`${c.entry.name}: ${errMsg}`, { key: toastKey });
      } else {
        // v0.2.92: word the success toast off `action.label`, not just
        // `action.method` — the fresh-install fallback (no row for this
        // project) and the broken/error Reinstall/Retry paths all share
        // method:'install', but only the latter two are actually
        // "re"-installs from this project's point of view.
        const successVerb =
          action.method === 'update'
            ? 'updated'
            : action.label === 'Install'
              ? 'installed'
              : 'reinstalled';
        toast.success(`${c.entry.name} ${successVerb}`, { key: toastKey });
      }
    } catch (err) {
      console.error('[home] module action threw', { module: c.entry.id, err });
      toast.error(err, { key: toastKey });
    } finally {
      cardActionBusyId = null;
    }
  }
</script>

<!-- v0.2.32 M1 (2026-05-23): per-route document title for browser/OS
     window-title consistency. -->
<svelte:head>
  <title>Home — VCT Launcher</title>
</svelte:head>

<div class="page">
  <div class="content">
    <div class="main-aurora">
      <div class="aurora-subtle aurora-subtle-1"></div>
      <div class="aurora-subtle aurora-subtle-2"></div>
    </div>

    <div class="main-inner">
      <!-- v0.2.22 Item #12: runtime-missing banner. Self-mounts/unmounts
           based on `system.has_podman` / `system.has_docker` in the
           orchestrator store. No-op when at least one runtime exists. -->
      <RuntimeMissingBanner />

      <div class="content-header">
        <div>
          <h1 class="content-title">Your Library</h1>
          <p class="content-subtitle">
            {cards.length} component{cards.length !== 1 ? 's' : ''}
          </p>
        </div>
      </div>

      {#if modulesState.loading && cards.length === 0}
        <div class="empty-state">
          <p class="empty-text">Loading catalog…</p>
        </div>
      {:else if cards.length === 0}
        <div class="empty-state">
          <div class="empty-icon">
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
            </svg>
          </div>
          <h2 class="empty-title">Catalog unavailable</h2>
          <p class="empty-text">
            Couldn't load the module catalog. Make sure the launcher is running
            and the orchestrator is reachable.
          </p>
        </div>
      {:else}
        <div class="app-grid">
          {#each cards as c (c.entry.id)}
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div
              class="app-card glass-card"
              class:app-card-selected={selectedCard?.entry.id === c.entry.id}
              class:app-card-coming-soon={c.badgeKind === 'coming_soon'}
              onclick={() => handleCardAction(c)}
              onkeydown={(e) => { if (e.key === 'Enter') handleCardAction(c); }}
              role="button"
              tabindex="0"
            >
              <div class="app-card-glow" style:--glow-color="rgba({getColorRgb(c.color)}, 0.5)"></div>
              <div class="app-card-top-line" style:background="linear-gradient(90deg, transparent, {getColorVar(c.color)}, transparent)"></div>
              <div class="app-card-icon" style:background="rgba({getColorRgb(c.color)}, 0.12)" style:border-color="rgba({getColorRgb(c.color)}, 0.25)">
                <span style:color={getColorVar(c.color)}>{c.icon}</span>
              </div>
              <h3 class="app-card-name">{c.entry.name}</h3>
              <p class="app-card-desc">{c.entry.description}</p>
              <div class="app-card-footer">
                <span class="app-card-version">v{c.entry.version}</span>
                {#if c.badgeKind === 'coming_soon'}
                  <span class="app-card-status app-card-coming-soon-badge">{c.badge}</span>
                {:else if c.entry.id === 'orchestrator' && orchState.status === 'installed'}
                  <button
                    class="btn-3d btn-3d-ghost btn-3d-sm"
                    onclick={(e) => { e.stopPropagation(); ui.openMcpDashboard(); }}
                  >
                    Dashboard
                  </button>
                {:else if c.badgeKind === 'subcomponent' && c.entry.cta_route}
                  <button
                    class="btn-3d btn-3d-ghost btn-3d-sm"
                    onclick={(e) => { e.stopPropagation(); window.location.assign(c.entry.cta_route); }}
                  >
                    Open dashboard
                  </button>
                {:else if cardActionBusyId === c.entry.id}
                  <!-- v0.2.92: in-flight install/retry/update. The field
                       report (2026-08-31) was that this state gave NO
                       visual feedback for ~3 real minutes (the RL Reranker
                       image pull) — the button just showed a static "…"
                       and the card kept showing the old version the whole
                       time. Mirrors the exact spinner + live-stage idiom
                       ModuleCatalog.svelte already uses on the /modules
                       page (`status-badge status-badge-bundled` +
                       `.spinner-sm`), fed by the SAME
                       `module://install-progress` events the backend
                       already emits for both `run_install` and
                       `run_upgrade` (installer_engine.rs) — real phases
                       ("Fetching updated source", "Running pre-upgrade
                       step 1/2", "Applying module DB migrations", …), not
                       an invented progress sequence. Falls back to a
                       plain "Installing…"/"Updating…" before the first
                       event arrives (network latency to the first emit). -->
                  <span class="app-card-status app-card-status-busy">
                    <span class="spinner-sm" aria-hidden="true"></span>
                    {installProgressLabel(modulesState.installProgress[c.entry.id] ?? null) ??
                      (c.action?.method === 'update' ? 'Updating…' : 'Installing…')}
                  </span>
                {:else if c.action}
                  <!-- Actionable status (broken/error/update_available, or
                       the project-scoped Install fallback resolved by
                       `resolveProjectScopedAction`): expose the action button
                       here too, not just on the /modules page. Disabled +
                       tooltipped when no project is selected (install/
                       update are per-project), OR while `c.pending` is
                       true — v0.2.92: we don't yet know whether this
                       project has an install row for the module (the
                       store's per-project load hasn't resolved yet, or
                       it failed), so rather than guess Update vs Install
                       we render the un-overridden action disabled until
                       the answer is known (CLAUDE.md "Conservative
                       defaults on best-effort paths"). -->
                  <button
                    class="btn-3d btn-3d-primary btn-3d-sm"
                    disabled={!$selectedProject || c.pending}
                    title={!$selectedProject
                      ? 'Select a project first'
                      : c.pending
                        ? "Checking this project's install status…"
                        : ''}
                    onclick={(e) => handleCardModuleAction(e, c)}
                  >
                    {c.action.label}
                  </button>
                {:else}
                  <span class="app-card-status app-card-installed">{c.badge}</span>
                {/if}
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  </div>

  <!-- v0.2.33 (Agent E, L11): pass the catalog entry's `kind` straight
       through so the right-rail Status row reads from the same source
       as the tile badge. Pre-v0.2.33 the right-rail inferred status
       from a static COMING_SOON_IDS lookup, which fell through to
       "Installed" for any module not in that list — leading to the
       user-reported drift on `vct-rl-reranker` (tile said Available,
       right-rail said Installed). -->
  <RightSidebar
    selectedApp={selectedCard ? {
      id: selectedCard.entry.id,
      name: selectedCard.entry.name,
      color: selectedCard.color,
      version: selectedCard.entry.version,
      catalogKind: selectedCard.entry.kind,
    } : null}
    onOpenActivation={() => ui.openActivation()}
  />
</div>

<style>
  .page {
    display: flex;
    height: 100%;
    overflow: hidden;
  }

  .content {
    flex: 1;
    position: relative;
    overflow-y: auto;
    overflow-x: hidden;
  }

  .main-aurora {
    position: absolute;
    inset: 0;
    pointer-events: none;
    overflow: hidden;
  }

  .aurora-subtle {
    position: absolute;
    border-radius: 50%;
    filter: blur(120px);
    opacity: 0.07;
  }

  .aurora-subtle-1 {
    width: 600px;
    height: 400px;
    background: var(--color-teal);
    top: -100px;
    right: -150px;
    animation: aurora-drift 12s ease-in-out infinite;
  }

  .aurora-subtle-2 {
    width: 500px;
    height: 350px;
    background: var(--color-purple);
    bottom: -80px;
    left: -100px;
    animation: aurora-drift 12s ease-in-out infinite reverse;
  }

  @keyframes aurora-drift {
    0%, 100% { transform: translate(0, 0); }
    50% { transform: translate(40px, -30px); }
  }

  .main-inner {
    position: relative;
    z-index: 1;
    padding: 28px 32px;
  }

  .content-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 28px;
  }

  .content-title {
    font-size: 22px;
    font-weight: 800;
    color: var(--color-text);
    letter-spacing: -0.5px;
  }

  .content-subtitle {
    font-size: 13px;
    color: var(--color-mid);
    margin-top: 2px;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: 80px 20px;
  }

  .empty-icon {
    width: 72px;
    height: 72px;
    border-radius: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    color: var(--color-muted);
    margin-bottom: 20px;
  }

  .empty-title {
    font-size: 18px;
    font-weight: 700;
    color: var(--color-text);
    margin-bottom: 8px;
  }

  .empty-text {
    font-size: 13px;
    color: var(--color-mid);
    max-width: 300px;
    margin-bottom: 24px;
  }

  .app-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 18px;
  }

  .app-card {
    position: relative;
    text-align: left;
    padding: 22px;
    cursor: pointer;
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, 0.06);
    font-family: inherit;
    color: inherit;
  }

  .app-card-glow {
    position: absolute;
    top: -50%;
    left: -50%;
    width: 200%;
    height: 200%;
    background: radial-gradient(circle at center, var(--glow-color, transparent) 0%, transparent 60%);
    opacity: 0;
    transition: opacity 0.4s ease;
    pointer-events: none;
  }

  .app-card:hover .app-card-glow {
    opacity: 0.06;
  }

  .app-card-selected {
    border-color: rgba(0, 191, 166, 0.3) !important;
    box-shadow:
      0 0 30px rgba(0, 191, 166, 0.08),
      0 8px 32px rgba(0, 0, 0, 0.2),
      inset 0 1px 0 rgba(255, 255, 255, 0.06) !important;
  }

  .app-card-top-line {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 2px;
    opacity: 0;
    transition: opacity 0.3s ease;
  }

  .app-card:hover .app-card-top-line {
    opacity: 1;
  }

  .app-card-icon {
    width: 48px;
    height: 48px;
    border-radius: 14px;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 1px solid;
    margin-bottom: 16px;
    transition: transform 0.3s ease, box-shadow 0.3s ease;
  }

  .app-card:hover .app-card-icon {
    transform: scale(1.08) translateY(-2px);
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.2);
  }

  .app-card-icon span {
    font-size: 20px;
    font-weight: 800;
  }

  .app-card-name {
    font-size: 15px;
    font-weight: 800;
    color: var(--color-text);
    margin-bottom: 6px;
  }

  .app-card-desc {
    font-size: 12px;
    color: var(--color-mid);
    line-height: 1.5;
    margin-bottom: 16px;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .app-card-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .app-card-version {
    font-size: 11px;
    color: var(--color-muted);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }

  .app-card-status {
    font-size: 11px;
    font-weight: 600;
    padding: 2px 10px;
    border-radius: 20px;
  }

  .app-card-installed {
    color: var(--color-teal);
    background: rgba(0, 191, 166, 0.1);
  }

  /* v0.2.92: in-flight install/update badge. Same purple "working"
     vocabulary as ModuleCatalog.svelte's `.status-badge-bundled` (the
     /modules page tile) so the two surfaces read consistently — a
     module mid-install looks the same whether the user is on the Home
     Library grid or the Modules page. */
  .app-card-status-busy {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--color-purple, #b29bff);
    background: rgba(123, 95, 255, 0.12);
    border: 1px solid rgba(123, 95, 255, 0.3);
    white-space: nowrap;
  }

  .spinner-sm {
    display: inline-block;
    width: 10px;
    height: 10px;
    flex-shrink: 0;
    border: 2px solid rgba(123, 95, 255, 0.25);
    border-top-color: var(--color-purple, #b29bff);
    border-radius: 50%;
    animation: app-card-spin 0.6s linear infinite;
  }

  @keyframes app-card-spin {
    to {
      transform: rotate(360deg);
    }
  }

  /* Coming-soon visual state: dimmer card, pink badge — same pattern as
     other "not yet available" affordances elsewhere in the launcher. */
  .app-card-coming-soon {
    opacity: 0.78;
  }

  .app-card-coming-soon:hover {
    opacity: 1;
  }

  .app-card-coming-soon-badge {
    color: var(--color-pink);
    background: rgba(255, 79, 160, 0.12);
    border: 1px solid rgba(255, 79, 160, 0.25);
  }
</style>
