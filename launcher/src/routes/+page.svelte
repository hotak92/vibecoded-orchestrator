<script lang="ts">
  import { onMount } from 'svelte';
  import MenuBar from '$lib/components/MenuBar.svelte';
  import RightSidebar from '$lib/components/RightSidebar.svelte';
  import StatusBar from '$lib/components/StatusBar.svelte';
  import SettingsPanel from '$lib/components/SettingsPanel.svelte';
  import ActivationModal from '$lib/components/ActivationModal.svelte';
  import InstallWizard from '$lib/components/InstallWizard.svelte';
  import McpDashboard from '$lib/components/McpDashboard.svelte';
  import ModuleCatalog from '$lib/components/ModuleCatalog.svelte';
  import OnboardingWizard from '$lib/components/OnboardingWizard.svelte';
  import ChangelogModal from '$lib/components/ChangelogModal.svelte';
  import { currentUser, auth } from '$lib/stores/auth';
  import { orchestrator } from '$lib/stores/orchestrator';

  type Tab = 'library' | 'store' | 'modules';
  let activeTab = $state<Tab>('library');
  let showSettings = $state(false);
  let showActivation = $state(false);
  let showInstallWizard = $state(false);
  let showMcpDashboard = $state(false);

  // v1.1 additions
  let showOnboarding = $state(false);
  let showChangelog = $state(false);

  function checkOnboarding() {
    try {
      if (localStorage.getItem('vct.onboarding_complete') !== 'true') {
        showOnboarding = true;
      }
    } catch {}
  }

  function checkChangelog() {
    // After a successful update, the updater store sets a flag we read here.
    try {
      if (localStorage.getItem('vct.show_changelog_after_update') === '1') {
        localStorage.removeItem('vct.show_changelog_after_update');
        showChangelog = true;
      }
    } catch {}
  }

  // Check orchestrator install status on mount
  onMount(() => {
    orchestrator.checkStatus();
    checkOnboarding();
    checkChangelog();
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

  const allApps: AppItem[] = [
    { id: 'orchestrator', name: 'Orchestrator', desc: 'Knowledge graph, code graph, and workflow automation for Claude Code. Free tier — persistent memory for your AI.', color: 'teal', icon: 'O', version: '0.1.0', checkoutUrl: '' },
    { id: 'orchestrator-pro', name: 'Orchestrator Pro', desc: 'RL-scored retrieval that learns from your usage, curated agent packs, and auto-updates.', color: 'purple', icon: 'O+', version: '0.1.0', checkoutUrl: '' },
    { id: 'transcrypt', name: 'Transcrypt', desc: 'Audio transcription with AI-powered correction and vocabulary support', color: 'teal', icon: 'T', version: '2.1.0', checkoutUrl: '' },
    { id: 'arzillibus', name: 'Arzillibus', desc: 'Smart ticketing system for events and venue management', color: 'purple', icon: 'A', version: '1.4.0', checkoutUrl: '' },
    { id: 'convertifacile', name: 'ConvertiFacile', desc: 'Universal file conversion — documents, images, audio', color: 'pink', icon: 'C', version: '1.0.0', checkoutUrl: '' },
    { id: 'dataweave', name: 'DataWeave', desc: 'Visual data pipeline builder for ETL workflows', color: 'teal', icon: 'D', version: '0.9.0', checkoutUrl: '' },
    { id: 'formcraft', name: 'FormCraft', desc: 'Drag & drop form builder with smart validations', color: 'purple', icon: 'F', version: '1.2.0', checkoutUrl: '' },
    { id: 'pixelsnap', name: 'PixelSnap', desc: 'Screenshot tool with annotations and quick sharing', color: 'pink', icon: 'P', version: '1.1.0', checkoutUrl: '' },
  ];

  let selectedApp = $state<AppItem | null>(null);

  let userApps = $derived(
    allApps.filter((app) => $currentUser?.apps?.includes(app.id))
  );

  let storeApps = $derived(
    allApps.filter((app) => !$currentUser?.apps?.includes(app.id))
  );

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

  // Reload profile when window regains focus (e.g. after LS checkout)
  onMount(() => {
    const handleFocus = () => auth.refreshProfile();
    window.addEventListener('focus', handleFocus);
    return () => window.removeEventListener('focus', handleFocus);
  });

  function handleGetApp(app: AppItem) {
    // Orchestrator base: open install wizard
    if (app.id === 'orchestrator') {
      if (orchState.status === 'installed') {
        showMcpDashboard = true;
      } else {
        showInstallWizard = true;
      }
      return;
    }

    // Orchestrator Pro: LS checkout (or activation if no URL yet)
    // Other apps: same flow
    const email = $currentUser?.email ?? '';
    const checkoutUrl = app.checkoutUrl;

    if (!checkoutUrl) {
      showActivation = true;
      return;
    }

    const separator = checkoutUrl.includes('?') ? '&' : '?';
    const url = `${checkoutUrl}${separator}checkout[email]=${encodeURIComponent(email)}`;
    window.open(url, '_blank');
  }

  /** Handle clicking an owned/installed orchestrator card */
  function handleAppCardAction(app: AppItem) {
    if (app.id === 'orchestrator' && orchState.status === 'installed') {
      showMcpDashboard = true;
      return;
    }
    selectApp(app);
  }
</script>

<div class="app-shell">
  <MenuBar
    bind:activeTab
    onOpenSettings={() => (showSettings = true)}
    onOpenActivation={() => (showActivation = true)}
  />

  <div class="app-body">
    <!-- Main content area -->
    <main class="main-content">
      <!-- Aurora subtle background -->
      <div class="main-aurora">
        <div class="aurora-subtle aurora-subtle-1"></div>
        <div class="aurora-subtle aurora-subtle-2"></div>
      </div>

      <div class="main-inner">
        {#if activeTab === 'library'}
          <div class="content-header">
            <div>
              <h1 class="content-title">Your Library</h1>
              <p class="content-subtitle">{userApps.length} tool{userApps.length !== 1 ? 's' : ''} activated</p>
            </div>
          </div>

          {#if userApps.length === 0}
            <div class="empty-state">
              <div class="empty-icon">
                <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                  <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
                </svg>
              </div>
              <h2 class="empty-title">Library is empty</h2>
              <p class="empty-text">Activate a code or browse the Store to add tools.</p>
              <button class="btn-3d btn-3d-primary" onclick={() => (activeTab = 'store')}>
                Browse Store
              </button>
            </div>
          {:else}
            <div class="app-grid">
              {#each userApps as app}
                <!-- svelte-ignore a11y_no_static_element_interactions -->
                <div
                  class="app-card glass-card"
                  class:app-card-selected={selectedApp?.id === app.id}
                  onclick={() => handleAppCardAction(app)}
                  onkeydown={(e) => { if (e.key === 'Enter') handleAppCardAction(app); }}
                  role="button"
                  tabindex="0"
                >
                  <div class="app-card-glow" style:--glow-color="rgba({getColorRgb(app.color)}, 0.5)"></div>
                  <div class="app-card-top-line" style:background="linear-gradient(90deg, transparent, {getColorVar(app.color)}, transparent)"></div>
                  <div class="app-card-icon" style:background="rgba({getColorRgb(app.color)}, 0.12)" style:border-color="rgba({getColorRgb(app.color)}, 0.25)">
                    <span style:color={getColorVar(app.color)}>{app.icon}</span>
                  </div>
                  <h3 class="app-card-name">{app.name}</h3>
                  <p class="app-card-desc">{app.desc}</p>
                  <div class="app-card-footer">
                    <span class="app-card-version">v{app.version}</span>
                    {#if app.id === 'orchestrator' && orchState.status === 'installed'}
                      <button
                        class="btn-3d btn-3d-ghost btn-3d-sm"
                        onclick={(e) => { e.stopPropagation(); showMcpDashboard = true; }}
                      >
                        Dashboard
                      </button>
                    {:else}
                      <span class="app-card-status app-card-installed">Installed</span>
                    {/if}
                  </div>
                </div>
              {/each}
            </div>
          {/if}

        {:else if activeTab === 'modules'}
          <ModuleCatalog
            onOpenActivation={() => (showActivation = true)}
            onOpenSettings={() => (showSettings = true)}
          />

        {:else if activeTab === 'store'}
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
                <div class="app-card-glow" style:--glow-color="rgba({getColorRgb(app.color)}, 0.5)"></div>
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
                      <button
                        class="btn-3d btn-3d-ghost btn-3d-sm"
                        onclick={(e) => { e.stopPropagation(); showMcpDashboard = true; }}
                      >
                        Dashboard
                      </button>
                    {:else}
                      <button
                        class="btn-3d btn-3d-ghost btn-3d-sm"
                        onclick={(e) => { e.stopPropagation(); showInstallWizard = true; }}
                      >
                        Install Free
                      </button>
                    {/if}
                  {:else if owned}
                    <span class="app-card-status app-card-installed">Owned</span>
                  {:else}
                    <button
                      class="btn-3d btn-3d-ghost btn-3d-sm"
                      onclick={(e) => { e.stopPropagation(); handleGetApp(app); }}
                    >
                      Get
                    </button>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
        {/if}
      </div>
    </main>

    <!-- Right Sidebar -->
    <RightSidebar {selectedApp} onOpenActivation={() => (showActivation = true)} />
  </div>

  <StatusBar />
</div>

<SettingsPanel bind:open={showSettings} />
<ActivationModal bind:open={showActivation} />

{#if showInstallWizard}
  <InstallWizard onClose={() => { showInstallWizard = false; orchestrator.checkStatus(); }} />
{/if}

{#if showMcpDashboard}
  <McpDashboard onClose={() => { showMcpDashboard = false; }} />
{/if}

<OnboardingWizard bind:open={showOnboarding} />
<ChangelogModal bind:open={showChangelog} />

<style>
  .app-shell {
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }

  .app-body {
    flex: 1;
    display: flex;
    overflow: hidden;
  }

  .main-content {
    flex: 1;
    position: relative;
    overflow-y: auto;
    overflow-x: hidden;
  }

  /* Aurora background */
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

  /* Header */
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

  /* Empty state */
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

  /* App Grid */
  .app-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 18px;
  }

  /* App Card */
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
</style>
