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
  import { modules } from '$lib/stores/modules';
  import { ui } from '$lib/stores/ui';
  import { selectedProject } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';
  import { moduleActionForKind } from '$lib/module-status-display';
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
    const handleFocus = () => auth.refreshProfile();
    window.addEventListener('focus', handleFocus);
    return () => window.removeEventListener('focus', handleFocus);
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

  let cards = $derived<AppCard[]>(
    modulesState.catalog.map((entry) => ({
      entry,
      color: colorFor(entry),
      icon: iconFor(entry),
      badge: badgeFor(entry),
      badgeKind: entry.kind,
    }))
  );

  let selectedCard = $state<AppCard | null>(null);

  function getColorRgb(color: 'teal' | 'purple' | 'pink'): string {
    if (color === 'teal') return '0,191,166';
    if (color === 'purple') return '123,95,255';
    return '255,79,160';
  }

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

  // Per-card action (Reinstall / Retry / Update) for actionable kinds. The
  // catalog `kind` → {label, method} mapping is centralised in
  // `moduleActionForKind` so Home, RightSidebar, and ModuleCatalog stay in
  // lockstep. install/update are per-project, so a project must be selected
  // (the button is disabled + tooltipped otherwise). UPSERT-safe commands,
  // so a double-click can't corrupt the row.
  let cardActionBusyId = $state<string | null>(null);

  async function handleCardModuleAction(e: MouseEvent, c: AppCard) {
    e.stopPropagation(); // don't also toggle the right sidebar
    const action = moduleActionForKind(c.badgeKind);
    if (!action) return;
    const project = $selectedProject;
    if (!project) {
      toast.error('Select a project first to install or update modules.');
      return;
    }
    cardActionBusyId = c.entry.id;
    try {
      if (action.method === 'install') {
        await modules.install(project.id, c.entry.id);
        toast.success(`${c.entry.name} reinstalled`);
      } else {
        await modules.update(project.id, c.entry.id);
        toast.success(`${c.entry.name} updated`);
      }
      await modules.loadCatalog();
    } catch (err) {
      toast.error(err);
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
                {:else if moduleActionForKind(c.badgeKind)}
                  <!-- Actionable status (broken/error/update_available):
                       expose the action button here too, not just on the
                       /modules page. Disabled + tooltipped when no project
                       is selected (install/update are per-project). -->
                  <button
                    class="btn-3d btn-3d-primary btn-3d-sm"
                    disabled={cardActionBusyId === c.entry.id || !$selectedProject}
                    title={!$selectedProject ? 'Select a project first' : ''}
                    onclick={(e) => handleCardModuleAction(e, c)}
                  >
                    {cardActionBusyId === c.entry.id
                      ? '…'
                      : moduleActionForKind(c.badgeKind)?.label}
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
