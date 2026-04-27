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
  import AdminBadge from '$lib/components/AdminBadge.svelte';
  import BrowserModeBanner from '$lib/components/BrowserModeBanner.svelte';
  import SettingsPanel from '$lib/components/SettingsPanel.svelte';
  import ActivationModal from '$lib/components/ActivationModal.svelte';
  import InstallWizard from '$lib/components/InstallWizard.svelte';
  import McpDashboard from '$lib/components/McpDashboard.svelte';
  import OnboardingWizard from '$lib/components/OnboardingWizard.svelte';
  import ChangelogModal from '$lib/components/ChangelogModal.svelte';
  import ExternalServicesDialog from '$lib/components/ExternalServicesDialog.svelte';
  import Toast from '$lib/components/Toast.svelte';
  import { selectedProject, projects } from '$lib/stores/projects';
  import { startChangePoller, onChange } from '$lib/stores/changes';

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

  let showChangelog = $state(false);

  // showOnboarding is driven by the ui store so any route (e.g. /preferences)
  // can open the wizard without a page reload. The onMount below seeds it from
  // localStorage on first load; after that ui.openOnboarding() is the entry point.
  // We mirror it into a local $state so OnboardingWizard can use bind:open (which
  // needs a writable binding). When the wizard sets open=false we sync back to
  // the store; when the store sets showOnboarding=true we forward to local state.
  let showOnboarding = $state(false);
  $effect(() => {
    if ($ui.showOnboarding && !showOnboarding) showOnboarding = true;
  });
  $effect(() => {
    if (!showOnboarding && $ui.showOnboarding) ui.closeOnboarding();
  });

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
      // Bug 14: `vct.onboarding_force` is set by Settings → Preferences →
      // Run setup wizard. Honor it ahead of the normal "completed" check so
      // users with a populated ~/.vct/launcher.db can still re-trigger the
      // wizard on demand. We clear the force flag immediately so it only
      // fires once.
      const forced = localStorage.getItem('vct.onboarding_force') === '1';
      if (forced) {
        localStorage.removeItem('vct.onboarding_force');
        localStorage.removeItem('vct.onboarding_complete');
      }
      if (forced || localStorage.getItem('vct.onboarding_complete') !== 'true') {
        ui.openOnboarding();
      }
      if (localStorage.getItem('vct.show_changelog_after_update') === '1') {
        localStorage.removeItem('vct.show_changelog_after_update');
        showChangelog = true;
      }
    } catch {}

    // Start the change-log poller (P7). Re-fetches the project list
    // whenever any window mutates `projects`. Other stores subscribe to
    // their own tables via `onChange(...)` in their own modules.
    void startChangePoller();
    const unsub = onChange('projects', () => {
      void projects.load();
    });
    return () => unsub();
  });

  // Routes that render outside the chrome shell (no MenuBar / Sidebar /
  // StatusBar). Auth pages are the only such case today.
  const fullBleed = $derived($page.url.pathname.startsWith('/auth'));

  // Remember the last "section" (top-level path segment) the user visited
  // while a project was selected. /p/<slug>/+page.svelte reads this on
  // arrival to redirect deep-link visitors to the most recent view of
  // that project. Documented in docs/MULTI_TENANT_URLS.md.
  const REMEMBER = new Set(['kg', 'codegraph', 'coordination', 'audit', 'project', 'hub', 'mcp', 'telemetry']);
  $effect(() => {
    if (typeof localStorage === 'undefined') return;
    const sel = $selectedProject;
    if (!sel) return;
    const segs = $page.url.pathname.split('/').filter(Boolean);
    const top = segs[0];
    if (top && REMEMBER.has(top)) {
      try {
        localStorage.setItem('vct.last_section.' + sel.id, '/' + top);
      } catch {}
    }
  });
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
  <ExternalServicesDialog />
  <Toast />
  <AdminBadge />
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
