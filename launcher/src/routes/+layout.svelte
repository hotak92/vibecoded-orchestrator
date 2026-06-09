<script lang="ts">
  import '../app.css';
  import { isAuthenticated, authLoading } from '$lib/stores/auth';
  import { goto, afterNavigate } from '$app/navigation';
  import { page } from '$app/stores';
  import { ui } from '$lib/stores/ui';
  import { orchestrator } from '$lib/stores/orchestrator';
  import { isOnboardingComplete, clearOnboardingComplete } from '$lib/onboarding';
  import { onMount } from 'svelte';

  import MenuBar from '$lib/components/MenuBar.svelte';
  import Sidebar from '$lib/components/Sidebar.svelte';
  import StatusBar from '$lib/components/StatusBar.svelte';
  import BrowserModeBanner from '$lib/components/BrowserModeBanner.svelte';
  // v0.2.23 F2 wave 2b (2026-05-21): SettingsPanel popover deleted.
  // The user-icon Settings popover was merged into /preferences (which
  // already hosted ~8 sections; the popover's unique sections — Profile,
  // Downloads, Shared services, Embedding profile, Volume location,
  // About — were appended as new sections). All callers now navigate
  // to /preferences via goto(). The Secrets sub-tab dropped entirely
  // because it duplicated /preferences/secrets.
  import ActivationModal from '$lib/components/ActivationModal.svelte';
  // v0.2.40 L1: per-paid-module license manager modal. Opened via
  // `ui.openLicenseManager()`. Mount + close pattern mirrors
  // `InstallWizard` (the wiring guide we sent to a contributor). Namespacing
  // discipline per the A3 collision audit: the store flag is
  // `showLicenseManager` (NOT `showLicense` / `showModal`) so another contributor's
  // parallel orchestrator-update-progress branch doesn't collide on
  // rebase. See `.claude/context/reviews/v0240-pre-push-2026-05-30
  // /discovery-A3-fabio-branch-collision-audit.md`.
  import LicenseManagerModal from '$lib/components/LicenseManagerModal.svelte';
  // v0.2.43 (contributor branch feat/orchestrator-update-progress-modal): full-
  // screen blocking overlay for the orchestrator self-update flow. Mount +
  // close pattern mirrors `InstallWizard` and the L1 `LicenseManagerModal`
  // sibling above. Opened by `ui.openOrchestratorUpdateProgress()` from
  // `UpdateBadge.svelte::handleAction`; the modal self-closes after the
  // hold+fade completion lifecycle (1.8 s + 400 ms) or on user-dismiss in
  // the error path. Namespacing reserved by A3 collision audit.
  import OrchestratorUpdateProgressModal from '$lib/components/OrchestratorUpdateProgressModal.svelte';
  import InstallWizard from '$lib/components/InstallWizard.svelte';
  import McpDashboard from '$lib/components/McpDashboard.svelte';
  import OnboardingWizard from '$lib/components/OnboardingWizard.svelte';
  import ChangelogModal from '$lib/components/ChangelogModal.svelte';
  import CdiDriftModal from '$lib/components/CdiDriftModal.svelte';
  import ExternalServicesDialog from '$lib/components/ExternalServicesDialog.svelte';
  import NoContainerRuntimeDialog from '$lib/components/NoContainerRuntimeDialog.svelte';
  import InstallHealthGate from '$lib/components/InstallHealthGate.svelte';
  // v0.2.15 (Agent D, 2026-05-17): launcher self-restart banner. Renders
  // when install.py wrote a `launcher_restart_required` or
  // `launcher_binary_swap_failed_locked` entry to UPDATE_DEFERRED.md.
  // Polls every 5s so it picks up entries from background install runs.
  import LauncherRestartBanner from '$lib/components/LauncherRestartBanner.svelte';
  // PR-8 (v0.2.11 / 2026-05-15): one-time legacy-collection notice. Auto-
  // shown when (a) Weaviate has at least one ClaudeOrchestrator_<Suffix>
  // class with objects AND (b) at least one user project has a different
  // code-graph prefix (= victim of the PR-7 hardcoded-name bug) AND
  // (c) the user hasn't dismissed it before. Hidden silently otherwise.
  import LegacyCollectionsModal from '$lib/components/LegacyCollectionsModal.svelte';
  import WeightsUpdatePrompt from '$lib/components/WeightsUpdatePrompt.svelte';
  import Toast from '$lib/components/Toast.svelte';
  import { invoke } from '$lib/tauri';
  import { selectedProject, projects } from '$lib/stores/projects';
  import { startChangePoller, onChange } from '$lib/stores/changes';
  import type { LegacyCodegraphReport } from '$lib/types/identity';

  let { children } = $props();

  const uiState = $derived($ui);

  // Scroll-reset on navigation. The chrome shell keeps a single scrolling
  // container (`.main-content`); SvelteKit's client-side router does NOT
  // reset its scrollTop between routes (the element persists across
  // navigations), so arriving on a new page inherited the previous page's
  // scroll offset — e.g. landing mid-way down /preferences/secrets.
  // afterNavigate fires after the new page's DOM is committed; we snap the
  // container back to the top. Hash links (in-page anchors) are left alone.
  let mainEl = $state<HTMLElement | null>(null);
  afterNavigate((nav) => {
    if (nav.to?.url.hash) return;
    if (mainEl) mainEl.scrollTop = 0;
  });

  // Local mirrors so the modals can keep their `bind:open` ergonomics. We
  // reflect ui-store state into these and propagate user-driven closes
  // back into the store.
  // v0.2.23 F2 wave 2b: showSettings + back-sync removed — the popover
  // was deleted in favour of the /preferences page. The `ui.openSettings`
  // store action is kept as a thin compatibility shim that routes to
  // /preferences (see $lib/stores/ui.ts) so any leftover callers in
  // off-limits files (e.g. modules/+page.svelte, owned by the F2a
  // Orchestrator Core agent) keep working without coordination churn.
  let showActivation = $state(false);
  $effect(() => {
    showActivation = uiState.showActivation;
  });
  $effect(() => {
    if (!showActivation && uiState.showActivation) ui.closeActivation();
  });

  let showChangelog = $state(false);

  // PR-8 (v0.2.11 / 2026-05-15): one-shot legacy-code-graph-collections
  // notice. Mounted only when (a) the detect command finds at least one
  // stale `ClaudeOrchestrator_*` Weaviate class with objects + at least
  // one user project with a non-legacy code-graph prefix AND (b) the
  // user has not dismissed the notice before. Dismissal is persisted
  // via `set_legacy_codegraph_notice_dismissed` so subsequent launches
  // stay quiet.
  let showLegacyCollections = $state(false);

  // 2026-04-28 fix (Bug A): the OnboardingWizard is driven directly by
  // the ui store now — no more local mirror + two-effect bridge. The
  // previous pattern raced on Svelte 5's effect ordering: when the
  // wizard set open=false, the back-sync effect tried to call
  // ui.closeOnboarding() AFTER the forward effect had already
  // re-flipped showOnboarding back to true (because $ui.showOnboarding
  // was still true at that microtask), so the wizard closed for one
  // frame and immediately reopened. The wizard now exposes onClose;
  // we gate its mount with `{#if $ui.showOnboarding}` and the wizard
  // calls onClose() when the user dismisses it. Same pattern used by
  // InstallWizard and McpDashboard above. Single source of truth.

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
    // Hide the boot splash (#vct-splash in app.html) now that the Svelte app
    // has mounted. The splash is plain inline HTML/CSS rendered by the WebView
    // before this code runs, so cold-start reads as "Avvio in corso…" instead
    // of a black/frozen WebView while the backend's blocking boot probes run.
    // We add `.vct-splash-hide` (opacity→0 over 280ms) then remove the node
    // after the fade so it never intercepts pointer events. Wrapped in a guard
    // because the element is absent in unit/SSR contexts.
    {
      const splash = document.getElementById('vct-splash');
      if (splash) {
        splash.classList.add('vct-splash-hide');
        setTimeout(() => splash.remove(), 320);
      }
    }

    // Check onboarding / changelog gates once per app load.
    //
    // Bug 14 fix (2026-05-05): the `onboarding_complete` flag now lives in
    // launcher.db (via crate::paths::vct_root_dir) instead of WebView
    // localStorage, so VCT_STATE_DIR isolation actually works. The
    // `onboarding_force` flag stays in localStorage — it's a one-shot
    // signal from the Settings page that gets consumed on the very next
    // mount, never crosses launcher instances.
    //
    // The async wrapper handles a one-shot localStorage→DB upgrade for
    // existing users so they don't see the wizard again after this fix
    // ships.
    (async () => {
      try {
        const forced = localStorage.getItem('vct.onboarding_force') === '1';
        if (forced) {
          localStorage.removeItem('vct.onboarding_force');
          await clearOnboardingComplete();
          ui.openOnboarding(); // sets onboardingForced=true in the store
        } else {
          const complete = await isOnboardingComplete();
          if (!complete) {
            ui.autoOpenOnboarding(); // first-launch auto-open; preflight may auto-close
          }
        }
        if (localStorage.getItem('vct.show_changelog_after_update') === '1') {
          localStorage.removeItem('vct.show_changelog_after_update');
          showChangelog = true;
        }
      } catch (e) {
        console.warn('[layout] onboarding gate failed:', e);
      }
    })();

    // Start the change-log poller (P7). Re-fetches the project list
    // whenever any window mutates `projects`. Other stores subscribe to
    // their own tables via `onChange(...)` in their own modules.
    void startChangePoller();
    const unsub = onChange('projects', () => {
      void projects.load();
    });

    // PR-8 (v0.2.11 / 2026-05-15): one-shot legacy-collections check.
    // Runs in parallel with onboarding so a fresh install isn't blocked
    // on Weaviate's HTTP `/v1/schema` round-trip. Silent on every
    // negative outcome (no legacy data, all projects use the legacy
    // prefix intentionally, Weaviate unreachable, already-dismissed,
    // running in browser mode without Tauri). The Rust command itself
    // soft-fails to an empty report so we never error here.
    (async () => {
      try {
        const dismissed = await invoke<boolean>('get_legacy_codegraph_notice_dismissed');
        if (dismissed) return;
        const report = await invoke<LegacyCodegraphReport>('list_legacy_codegraph_collections');
        if (report.action_recommended) {
          showLegacyCollections = true;
        }
      } catch (e) {
        // Browser mode + Tauri-command-unavailable lands here; not a real failure.
        console.debug('[layout] legacy-collections check skipped:', e);
      }
    })();

    // v0.2.52 V52-AD: subscribe to the boot-time auto-enable probe.
    // The Rust side emits `vct-rl-auto-enable-available` ONCE per
    // launcher boot when rl_events has >= 500 rows AND the global RL
    // reranker toggle is still `false` (the install-time default).
    // We surface a toast with a navigate-CTA; the user clicks through
    // to /preferences/modules to flip the toggle. The toast is one-
    // shot (Toast store dedupes on `key`) so re-firing across boots
    // doesn't spam. Suppressed when the user has dismissed via
    // localStorage flag (set when they navigate from the toast).
    (async () => {
      try {
        const { listen: rlListen } = await import('$lib/tauri');
        const { toast: rlToast } = await import('$lib/stores/toast');
        rlListen<{ event_count: number; threshold: number; module_id: string }>(
          'vct-rl-auto-enable-available',
          (e) => {
            const dismissed = localStorage.getItem('vct.rl_auto_enable_dismissed') === '1';
            if (dismissed) return;
            const { event_count, threshold } = e.payload;
            rlToast.info(
              `RL Reranker: ${event_count}/${threshold} training events ` +
                `accumulated — visit Preferences → Modules to enable.`,
            );
          },
        );
      } catch (e) {
        // Browser mode or transient hiccup — silent skip; the user can
        // still navigate to /preferences/modules manually.
        console.debug('[layout] rl-auto-enable subscription skipped:', e);
      }
    })();

    // v0.2.32 UB2 (2026-05-23): periodic orchestrator-status refresh.
    // The status badge previously updated only on home-page mount, so a
    // user who installed/uninstalled the orchestrator from another
    // process (CLI, launcher in a second window) never saw the badge
    // refresh until they restarted the launcher. We now run an initial
    // check on layout mount (regardless of which route the launcher
    // opened to) and re-poll once an hour. Cheap: each check is a
    // single file-existence probe + manifest read.
    void orchestrator.checkStatus();
    const orchStatusInterval = setInterval(
      () => void orchestrator.checkStatus(),
      60 * 60 * 1000,
    );

    return () => {
      unsub();
      clearInterval(orchStatusInterval);
    };
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
    <!-- v0.2.15 (Agent D): post-update launcher-restart prompt. Mounted
         above MenuBar so it's the first thing the user sees regardless
         of which page they're on when install.py finishes. -->
    <LauncherRestartBanner />
    <MenuBar />
    <div class="app-body">
      <Sidebar />
      <main class="main-content" bind:this={mainEl}>
        {@render children()}
      </main>
    </div>
    <StatusBar />
  </div>

  <!-- Global modals — any route can open them via the `ui` store.
       (v0.2.23 F2 wave 2b: SettingsPanel removed; ui.openSettings()
       now routes to /preferences.) -->
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

  {#if uiState.showOnboarding}
    <OnboardingWizard
      force={uiState.onboardingForced}
      onClose={() => ui.closeOnboarding()}
    />
  {/if}

  <!-- v0.2.40 L1: per-paid-module license manager. Opened via
       `ui.openLicenseManager()`. The flag name is `showLicenseManager`
       per the A3 collision-audit guidance (avoids overlap with another contributor's
       in-progress orchestrator-update-progress modal flag). Same
       open/close shape as InstallWizard above. -->
  {#if uiState.showLicenseManager}
    <LicenseManagerModal onClose={() => ui.closeLicenseManager()} />
  {/if}
  <!-- v0.2.43 (contributor): full-screen blocking overlay during self-update.
       Opened by `ui.openOrchestratorUpdateProgress()` from
       UpdateBadge.handleAction right before invoking any updater action
       (runUpdate / applyPendingInstall / runRestart). The modal subscribes
       to `$orchestrator.progress` directly (no dup listener) and self-
       closes after the completion hold+fade timer expires. -->
  {#if uiState.showOrchestratorUpdateProgress}
    <OrchestratorUpdateProgressModal />
  {/if}
  <ChangelogModal bind:open={showChangelog} />
  <ExternalServicesDialog />
  <NoContainerRuntimeDialog />
  <!-- PR-8 (v0.2.11 / 2026-05-15): legacy code-graph collections notice.
       Self-dismisses by flipping `showLegacyCollections` to false when the
       user clicks Dismiss / completes cleanup / closes the dialog. -->
  {#if showLegacyCollections}
    <LegacyCollectionsModal onClose={() => (showLegacyCollections = false)} />
  {/if}
  <!-- CDI / NVIDIA driver-version drift detector. Runs once at app
       startup; auto-opens a blocking modal only when it finds drift
       (Linux + NVIDIA + stale CDI spec). Silent on macOS, Windows, or
       hosts without nvidia-smi. See gpu.rs for forensics. -->
  <CdiDriftModal />
  <WeightsUpdatePrompt />
  <!-- Highest-priority gate: blocks the UI when the launcher binary is
       running from inside an install root that never had first-install
       executed. Self-bypasses in developer mode and once the user has
       acknowledged it. Mounted last so it stacks above every other modal. -->
  <InstallHealthGate />
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
