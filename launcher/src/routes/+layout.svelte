<script lang="ts">
  import '../app.css';
  import { isAuthenticated, authLoading } from '$lib/stores/auth';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { ui } from '$lib/stores/ui';
  import { orchestrator } from '$lib/stores/orchestrator';
  import { onMount } from 'svelte';

  import MenuBar from '$lib/components/MenuBar.svelte';
  import Sidebar from '$lib/components/Sidebar.svelte';
  import StatusBar from '$lib/components/StatusBar.svelte';
  import BrowserModeBanner from '$lib/components/BrowserModeBanner.svelte';
  import SettingsPanel from '$lib/components/SettingsPanel.svelte';
  import ActivationModal from '$lib/components/ActivationModal.svelte';
  import InstallWizard from '$lib/components/InstallWizard.svelte';
  import McpDashboard from '$lib/components/McpDashboard.svelte';
  import OnboardingWizard from '$lib/components/OnboardingWizard.svelte';
  import ChangelogModal from '$lib/components/ChangelogModal.svelte';
  import Toast from '$lib/components/Toast.svelte';

  let { children } = $props();

  const uiState = $derived($ui);

  // Local mirrors so the modals can keep their `bind:open` ergonomics. We
  // reflect ui-store state into these and propagate user-driven closes
  // back into the store.
  let showSettings = $state(false);
  let showActivation = $state(false);
  $effect(() => {
    showSettings = uiState.showSettings;
  });
  $effect(() => {
    showActivation = uiState.showActivation;
  });
  $effect(() => {
    if (!showSettings && uiState.showSettings) ui.closeSettings();
  });
  $effect(() => {
    if (!showActivation && uiState.showActivation) ui.closeActivation();
  });

  let showOnboarding = $state(false);
  let showChangelog = $state(false);

  $effect(() => {
    if ($authLoading) return; // Wait for session check

    const onAuthPage = $page.url.pathname.startsWith('/auth');

    if (!$isAuthenticated && !onAuthPage) {
      goto('/auth');
    } else if ($isAuthenticated && onAuthPage) {
      goto('/');
    }
  });

  onMount(() => {
    // Check onboarding / changelog gates once per app load.
    try {
      if (localStorage.getItem('vct.onboarding_complete') !== 'true') {
        showOnboarding = true;
      }
      if (localStorage.getItem('vct.show_changelog_after_update') === '1') {
        localStorage.removeItem('vct.show_changelog_after_update');
        showChangelog = true;
      }
    } catch {}
  });

  // Routes that render outside the chrome shell (no MenuBar / Sidebar /
  // StatusBar). Auth pages are the only such case today.
  const fullBleed = $derived($page.url.pathname.startsWith('/auth'));
</script>

{#if $authLoading}
  <div class="loading-screen">
    <div class="loading-logo">
      <div class="loading-icon">
        <span>V</span>
      </div>
      <p>Loading...</p>
    </div>
  </div>
{:else if fullBleed}
  {@render children()}
{:else}
  <div class="app-shell">
    <BrowserModeBanner />
    <MenuBar />
    <div class="app-body">
      <Sidebar />
      <main class="main-content">
        {@render children()}
      </main>
    </div>
    <StatusBar />
  </div>

  <!-- Global modals — any route can open them via the `ui` store. -->
  <SettingsPanel bind:open={showSettings} />
  <ActivationModal bind:open={showActivation} />

  {#if uiState.showInstallWizard}
    <InstallWizard
      onClose={() => {
        ui.closeInstallWizard();
        orchestrator.checkStatus();
      }}
    />
  {/if}

  {#if uiState.showMcpDashboard}
    <McpDashboard onClose={() => ui.closeMcpDashboard()} />
  {/if}

  <OnboardingWizard bind:open={showOnboarding} />
  <ChangelogModal bind:open={showChangelog} />
  <Toast />
{/if}

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
    min-height: 0;
  }

  .main-content {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
    position: relative;
  }

  .loading-screen {
    width: 100%;
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--color-bg);
  }

  .loading-logo {
    text-align: center;
  }

  .loading-icon {
    width: 56px;
    height: 56px;
    border-radius: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(135deg, var(--color-teal), var(--color-purple));
    margin: 0 auto 16px;
    animation: pulse-glow 1.5s ease-in-out infinite;
  }

  .loading-icon span {
    color: var(--color-bg);
    font-weight: 900;
    font-size: 22px;
  }

  .loading-logo p {
    color: var(--color-mid);
    font-size: 13px;
  }

  @keyframes pulse-glow {
    0%, 100% { box-shadow: 0 0 20px rgba(0, 191, 166, 0.2); }
    50% { box-shadow: 0 0 40px rgba(0, 191, 166, 0.5); }
  }
</style>
