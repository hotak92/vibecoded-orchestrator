<script lang="ts">
  import { auth, currentUser } from '$lib/stores/auth';
  import { license } from '$lib/stores/license';
  import { ui } from '$lib/stores/ui';
  import { selectedProject } from '$lib/stores/projects';
  import { projectColor } from '$lib/project-color';
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import ProjectSelector from './ProjectSelector.svelte';
  import UpdateBadge from './UpdateBadge.svelte';
  import NotificationBell from './NotificationBell.svelte';

  // MenuBar is mounted from the root layout, so it renders on every route.
  // Top-level navigation moved to the left Sidebar; MenuBar now hosts:
  //   - logo + project selector (project context, never disappears)
  //   - tier pill + update badge + user menu

  let showUserMenu = $state(false);
  let menuWrapperEl: HTMLDivElement;

  const licenseState = $derived($license);
  const tier = $derived(licenseState.cache?.orchestrator_tier ?? 'free');
  const accent = $derived(projectColor($selectedProject?.id));

  onMount(() => {
    license.load();
  });

  function handleLogout() {
    showUserMenu = false;
    auth.logout();
    goto('/auth');
  }

  function handleClickOutside(e: MouseEvent) {
    if (showUserMenu && menuWrapperEl && !menuWrapperEl.contains(e.target as Node)) {
      showUserMenu = false;
    }
  }

  function tierLabel(t: string): string {
    return t.charAt(0).toUpperCase() + t.slice(1);
  }

  // Bug 21: scan all known projects for outdated orchestrator installs.
  // Computed lazily when the user menu opens (cheap — one Tauri call per
  // project, all FS-local).
  type OrchestratorState = {
    installed: boolean;
    version: string | null;
    version_status: 'current' | 'outdated' | 'unknown';
    bundled_version: string | null;
    config_health: { file: string; ok: boolean; error: string | null }[];
  };
  let updatableProjectIds = $state<string[]>([]);
  let scanningUpdates = $state(false);
  let updatingAll = $state(false);

  async function scanForUpdates() {
    if (scanningUpdates) return;
    scanningUpdates = true;
    try {
      const list = await invoke<{ id: string; folder_path: string }[]>('list_projects_v2');
      const outdated: string[] = [];
      for (const p of list) {
        try {
          const s = await invoke<OrchestratorState>('inspect_orchestrator_at', {
            path: p.folder_path,
          });
          if (s.installed && s.version_status === 'outdated') {
            outdated.push(p.id);
          }
        } catch {
          // Skip — folder unreadable / missing.
        }
      }
      updatableProjectIds = outdated;
    } finally {
      scanningUpdates = false;
    }
  }

  async function updateAllProjects() {
    if (updatingAll) return;
    updatingAll = true;
    try {
      const list = await invoke<{ id: string; folder_path: string }[]>('list_projects_v2');
      const targets = list.filter((p) => updatableProjectIds.includes(p.id));
      let ok = 0;
      let fail = 0;
      for (const p of targets) {
        try {
          await invoke('update_orchestrator_at', { path: p.folder_path });
          ok += 1;
        } catch (e) {
          console.error('update failed', p.id, e);
          fail += 1;
        }
      }
      if (fail === 0) {
        toast.success(`Updated ${ok} project${ok === 1 ? '' : 's'}`);
      } else {
        toast.error(`${ok} updated, ${fail} failed`);
      }
      await scanForUpdates();
    } finally {
      updatingAll = false;
      showUserMenu = false;
    }
  }

  $effect(() => {
    if (showUserMenu) void scanForUpdates();
  });
</script>

<svelte:window onclick={handleClickOutside} />

<header class="menu-bar" style:--project-accent={accent}>
  <div class="accent-strip" aria-hidden="true"></div>
  <!-- Left: Logo + project selector -->
  <div class="menu-left">
    <!-- v0.2.43 (Fabio branch feat/launcher-logo-circular-white):
         brand logo moved from here to the StatusBar footer (see
         StatusBar.svelte `.status-brand`). The Windows titlebar already
         carries the embedded .ico icon — duplicating it here was visually
         noisy. ProjectSelector is now the first menubar element. -->
    <ProjectSelector />
  </div>

  <!-- Right: Notification bell + Tier badge + Update badge + Avatar -->
  <div class="menu-right">
    <NotificationBell />
    <UpdateBadge />

    <button
      class="tier-pill"
      class:tier-free={tier === 'free'}
      class:tier-paid={tier !== 'free'}
      onclick={() => ui.openActivation()}
      title={tier === 'free' ? 'Activate Pro' : `Tier: ${tierLabel(tier)}`}
    >
      {#if tier === 'free'}
        Free tier — Activate Pro
      {:else}
        {tierLabel(tier)}
      {/if}
    </button>

    <div class="menu-user-wrapper" bind:this={menuWrapperEl}>
      <button class="menu-avatar" onclick={(e) => { e.stopPropagation(); showUserMenu = !showUserMenu; }}>
        <span>{$currentUser?.name?.charAt(0).toUpperCase() ?? '?'}</span>
      </button>

      {#if showUserMenu}
        <div class="user-menu">
          <div class="user-menu-header">
            <p class="user-menu-name">{$currentUser?.name}</p>
            <p class="user-menu-email">{$currentUser?.email}</p>
          </div>
          <div class="user-menu-divider"></div>
          <!-- v0.2.23 F2 wave 2b (2026-05-21): the Settings popover was
               merged into /preferences. This menu item navigates there
               directly instead of mounting a modal. -->
          <button class="user-menu-item" onclick={() => { showUserMenu = false; goto('/preferences'); }}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
            </svg>
            Settings
          </button>
          {#if updatableProjectIds.length > 0}
            <button class="user-menu-item user-menu-update" onclick={updateAllProjects} disabled={updatingAll}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/>
                <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
              </svg>
              {updatingAll ? 'Updating…' : `Update ${updatableProjectIds.length} project${updatableProjectIds.length === 1 ? '' : 's'}`}
            </button>
          {/if}
          <button class="user-menu-item" onclick={() => { showUserMenu = false; ui.openActivation(); }}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
            </svg>
            Activation Codes
          </button>
          <!-- v0.2.40 L1: per-paid-module License Manager. Lives next
               to the legacy Activation Codes item; the two surfaces
               write to the same keychain underneath (the orchestrator-
               root slot in the new modal is the same secret the
               Activation Codes flow manages). -->
          <button class="user-menu-item" onclick={() => { showUserMenu = false; ui.openLicenseManager(); }}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>
            </svg>
            License Keys
          </button>
          <div class="user-menu-divider"></div>
          <button class="user-menu-item user-menu-logout" onclick={handleLogout}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>
            </svg>
            Sign Out
          </button>
        </div>
      {/if}
    </div>
  </div>
</header>

<style>
  .menu-bar {
    position: relative;
    z-index: 100;
    height: 52px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 20px;
    background: rgba(8, 15, 40, 0.85);
    backdrop-filter: blur(20px);
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    flex-shrink: 0;
    -webkit-app-region: drag;
  }

  .menu-bar :global(*) {
    -webkit-app-region: no-drag;
  }
  .accent-strip {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    /* Bumped from 3px → 5px after the multi-tenant power-user flagged
       the original strip as too thin for safety-critical "which project
       am I in?" recognition. Paired with the tinted project-name pill
       below, this gives two reinforcing accent surfaces. */
    height: 5px;
    background: var(--project-accent, transparent);
    box-shadow: 0 1px 8px var(--project-accent, transparent);
    transition: background 0.3s ease, box-shadow 0.3s ease;
    pointer-events: none;
    opacity: 0.95;
  }

  .menu-left {
    display: flex;
    align-items: center;
    gap: 20px;
  }

  .menu-right {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .tier-pill {
    padding: 4px 12px;
    font-size: 11px;
    font-weight: 700;
    border-radius: 999px;
    border: 1px solid;
    background: none;
    cursor: pointer;
    transition: all 0.15s ease;
    text-transform: capitalize;
  }
  .tier-free {
    border-color: rgba(255, 255, 255, 0.12);
    color: var(--color-mid);
  }
  .tier-free:hover {
    border-color: rgba(0, 191, 166, 0.5);
    color: var(--color-teal);
  }
  .tier-paid {
    border-color: rgba(0, 191, 166, 0.4);
    color: var(--color-teal);
    background: rgba(0, 191, 166, 0.08);
  }
  .tier-paid:hover {
    background: rgba(0, 191, 166, 0.14);
  }

  .menu-user-wrapper {
    position: relative;
  }

  .menu-avatar {
    width: 32px;
    height: 32px;
    border-radius: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(135deg, var(--color-purple), var(--color-pink));
    color: white;
    font-weight: 700;
    font-size: 13px;
    border: 2px solid transparent;
    cursor: pointer;
    transition: all 0.25s ease;
  }

  .menu-avatar:hover {
    border-color: rgba(255, 255, 255, 0.2);
    transform: translateY(-1px) scale(1.05);
    box-shadow: 0 4px 16px rgba(123, 95, 255, 0.3);
  }

  .user-menu {
    position: fixed;
    top: 52px;
    right: 16px;
    width: 220px;
    background: rgba(13, 23, 53, 0.95);
    backdrop-filter: blur(24px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 14px;
    padding: 6px;
    z-index: 200;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
    animation: menu-appear 0.15s ease-out;
  }

  @keyframes menu-appear {
    from { opacity: 0; transform: translateY(-8px) scale(0.96); }
    to { opacity: 1; transform: translateY(0) scale(1); }
  }

  .user-menu-header {
    padding: 10px 12px;
  }

  .user-menu-name {
    font-size: 13px;
    font-weight: 700;
    color: var(--color-text);
  }

  .user-menu-email {
    font-size: 11px;
    color: var(--color-mid);
    margin-top: 2px;
  }

  .user-menu-divider {
    height: 1px;
    background: rgba(255, 255, 255, 0.06);
    margin: 4px 8px;
  }

  .user-menu-item {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    padding: 8px 12px;
    font-size: 13px;
    font-weight: 500;
    color: var(--color-mid);
    background: none;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    text-align: left;
    transition: all 0.15s ease;
  }

  .user-menu-item:hover {
    color: var(--color-text);
    background: rgba(255, 255, 255, 0.06);
  }

  .user-menu-logout:hover {
    color: var(--color-pink);
    background: rgba(255, 79, 160, 0.08);
  }
  .user-menu-update {
    color: var(--color-teal, #0fc);
  }
  .user-menu-update:hover {
    background: rgba(0, 191, 166, 0.1);
  }
  .user-menu-update:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
