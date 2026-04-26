<script lang="ts">
  import { onMount } from 'svelte';
  import RightSidebar from '$lib/components/RightSidebar.svelte';
  import { currentUser, auth } from '$lib/stores/auth';
  import { orchestrator } from '$lib/stores/orchestrator';
  import { ui } from '$lib/stores/ui';

  onMount(() => {
    orchestrator.checkStatus();
    const handleFocus = () => auth.refreshProfile();
    window.addEventListener('focus', handleFocus);
    return () => window.removeEventListener('focus', handleFocus);
  });

  const orchState = $derived($orchestrator);

  interface AppItem {
    id: string;
    name: string;
    desc: string;
    color: string;
    icon: string;
    version: string;
    checkoutUrl?: string;
  }

  // The Pro card description embeds the price grid because RightSidebar
  // shows the long-form pitch and the card itself needs only a one-liner.
  // MAO is rendered as Coming Soon; clicking the card surfaces an early-access
  // hint rather than a checkout link.
  const allApps: AppItem[] = [
    { id: 'orchestrator', name: 'Orchestrator', desc: 'Knowledge graph, code graph, and workflow automation for Claude Code. Free tier — persistent memory for your AI.', color: 'teal', icon: 'O', version: '0.1.0', checkoutUrl: '' },
    { id: 'orchestrator-pro', name: 'Orchestrator Pro', desc: 'RL-scored retrieval that learns from your usage, curated agent packs, auto-updates. €19/mo · €149/yr · €199 lifetime.', color: 'purple', icon: 'O+', version: '0.1.0', checkoutUrl: '' },
    { id: 'mao', name: 'Multi-Agent Orchestrator (MAO)', desc: 'Coming soon — 10 specialist agents + Maestro coordinator + Planner pipeline + Tauri UI on top of Standard.', color: 'pink', icon: 'M', version: 'preview', checkoutUrl: '' },
    { id: 'transcrypt', name: 'Transcrypt', desc: 'Audio transcription with AI-powered correction and vocabulary support', color: 'teal', icon: 'T', version: '2.1.0', checkoutUrl: '' },
    { id: 'arzillibus', name: 'Arzillibus', desc: 'Smart ticketing system for events and venue management', color: 'purple', icon: 'A', version: '1.4.0', checkoutUrl: '' },
    { id: 'convertifacile', name: 'ConvertiFacile', desc: 'Universal file conversion — documents, images, audio', color: 'pink', icon: 'C', version: '1.0.0', checkoutUrl: '' },
    { id: 'dataweave', name: 'DataWeave', desc: 'Visual data pipeline builder for ETL workflows', color: 'teal', icon: 'D', version: '0.9.0', checkoutUrl: '' },
    { id: 'formcraft', name: 'FormCraft', desc: 'Drag & drop form builder with smart validations', color: 'purple', icon: 'F', version: '1.2.0', checkoutUrl: '' },
    { id: 'pixelsnap', name: 'PixelSnap', desc: 'Screenshot tool with annotations and quick sharing', color: 'pink', icon: 'P', version: '1.1.0', checkoutUrl: '' },
  ];

  // Apps that are not yet purchasable. "Get" / "Activate" buttons are
  // replaced by a "Coming soon" badge for these IDs.
  const COMING_SOON = new Set(['mao']);

  let selectedApp = $state<AppItem | null>(null);

  function getColorRgb(color: string): string {
    if (color === 'teal') return '0,191,166';
    if (color === 'purple') return '123,95,255';
    return '255,79,160';
  }

  function getColorVar(color: string): string {
    if (color === 'teal') return 'var(--color-teal)';
    if (color === 'purple') return 'var(--color-purple)';
    return 'var(--color-pink)';
  }

  function selectApp(app: AppItem) {
    selectedApp = selectedApp?.id === app.id ? null : app;
  }

  function handleGetApp(app: AppItem) {
    if (app.id === 'orchestrator') {
      if (orchState.status === 'installed') {
        ui.openMcpDashboard();
      } else {
        ui.openInstallWizard();
      }
      return;
    }

    const email = $currentUser?.email ?? '';
    const checkoutUrl = app.checkoutUrl;
    if (!checkoutUrl) {
      ui.openActivation();
      return;
    }
    const separator = checkoutUrl.includes('?') ? '&' : '?';
    const url = `${checkoutUrl}${separator}checkout[email]=${encodeURIComponent(email)}`;
    window.open(url, '_blank');
  }
</script>

<div class="page">
  <div class="content">
    <div class="main-inner">
      <div class="content-header">
        <div>
          <h1 class="content-title">Store</h1>
          <p class="content-subtitle">Discover VibeCoded Tools</p>
        </div>
      </div>

      <div class="app-grid">
        {#each allApps as app}
          {@const owned = $currentUser?.apps?.includes(app.id)}
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <div
            class="app-card glass-card"
            class:app-card-selected={selectedApp?.id === app.id}
            onclick={() => selectApp(app)}
            onkeydown={(e) => { if (e.key === 'Enter') selectApp(app); }}
            role="button"
            tabindex="0"
          >
            <div class="app-card-top-line" style:background="linear-gradient(90deg, transparent, {getColorVar(app.color)}, transparent)"></div>
            <div class="app-card-icon" style:background="rgba({getColorRgb(app.color)}, 0.12)" style:border-color="rgba({getColorRgb(app.color)}, 0.25)">
              <span style:color={getColorVar(app.color)}>{app.icon}</span>
            </div>
            <h3 class="app-card-name">{app.name}</h3>
            <p class="app-card-desc">{app.desc}</p>
            <div class="app-card-footer">
              <span class="app-card-version">v{app.version}</span>
              {#if app.id === 'orchestrator'}
                {#if orchState.status === 'installed'}
                  <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={(e) => { e.stopPropagation(); ui.openMcpDashboard(); }}>
                    Dashboard
                  </button>
                {:else}
                  <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={(e) => { e.stopPropagation(); ui.openInstallWizard(); }}>
                    Install Free
                  </button>
                {/if}
              {:else if COMING_SOON.has(app.id)}
                <span class="app-card-status app-card-soon">Coming soon</span>
              {:else if owned}
                <span class="app-card-status app-card-installed">Owned</span>
              {:else}
                <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={(e) => { e.stopPropagation(); handleGetApp(app); }}>
                  Get
                </button>
              {/if}
            </div>
          </div>
        {/each}
      </div>
    </div>
  </div>

  <RightSidebar {selectedApp} onOpenActivation={() => ui.openActivation()} />
</div>

<style>
  .page {
    display: flex;
    height: 100%;
    overflow: hidden;
  }
  .content {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
  }
  .main-inner {
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
  .app-card-soon {
    color: var(--color-pink, #ff4fa0);
    background: rgba(255, 79, 160, 0.1);
  }
</style>
