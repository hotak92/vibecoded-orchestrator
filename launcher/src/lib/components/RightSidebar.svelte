<script lang="ts">
  // Right sidebar — project info + Launch button.
  //
  // The Launch button spawns VS Code with the selected project's
  // folder via the `launch_project_in_editor` Tauri command.
  //
  // - No projects → button disabled, tooltip explains why.
  // - One project → click launches it directly.
  // - Multiple projects → click opens a Dropdown picker; on selection we
  //   launch and remember the last-launched id in localStorage.

  import { currentUser } from '$lib/stores/auth';
  import { projects, selectedProject } from '$lib/stores/projects';
  import { orchestrator } from '$lib/stores/orchestrator';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import { ui } from '$lib/stores/ui';
  import Dropdown from '$lib/components/Dropdown.svelte';

  interface App {
    id: string;
    name: string;
    color: string;
    version?: string;
    /**
     * Whether the app is shipped + installed. Defaults to true only for
     * apps the launcher itself ships (orchestrator + bundled subcomponents).
     * For Store catalog entries, the caller passes the real install state
     * derived from the user's owned-apps list / orchestrator status.
     */
    installed?: boolean;
    /**
     * Lifecycle stage. Drives which Quick Action buttons render.
     * - `shipped` (default): the app is real and installed; show Launch + Check Update.
     * - `not_installed`: app is real but not installed locally; show Install (orchestrator) or Get (paid).
     * - `coming_soon`: pre-release; suppress Launch/Update entirely, show
     *   only an info note. This is the bug-fix for 2026-04-30: previously
     *   every selectedApp got Launch + Check Update regardless of stage.
     */
    stage?: 'shipped' | 'not_installed' | 'coming_soon';
  }

  let {
    selectedApp = null,
    onOpenActivation,
  }: {
    selectedApp: App | null;
    onOpenActivation: () => void;
  } = $props();

  const orchState = $derived($orchestrator);
  // Effective stage: explicit prop wins; otherwise infer for known IDs.
  // Coming-soon products get inferred from a static set so the home
  // catalog (which doesn't pass stage) still suppresses Launch/Update.
  const COMING_SOON_IDS = new Set([
    'orchestrator-pro', 'mao', 'transcrypt', 'arzillibus',
    'convertifacile', 'dataweave', 'formcraft', 'pixelsnap',
    'rl-reranker',
  ]);
  const effectiveStage = $derived.by(() => {
    if (!selectedApp) return 'shipped' as const;
    if (selectedApp.stage) return selectedApp.stage;
    if (selectedApp.id === 'orchestrator') {
      return orchState.status === 'installed' ? 'shipped' : 'not_installed';
    }
    if (COMING_SOON_IDS.has(selectedApp.id)) return 'coming_soon';
    // Subcomponents (knowledge-graph, code-graph) are always shipped if
    // VCO itself is installed.
    if (selectedApp.id === 'knowledge-graph' || selectedApp.id === 'code-graph') {
      return orchState.status === 'installed' ? 'shipped' : 'not_installed';
    }
    return 'shipped' as const;
  });
  // Whether to show the install/launch quick actions.
  const showLaunchActions = $derived(
    selectedApp !== null && effectiveStage === 'shipped' && selectedApp.id === 'orchestrator'
  );
  const showInstallAction = $derived(
    selectedApp !== null && effectiveStage === 'not_installed' && selectedApp.id === 'orchestrator'
  );

  type LaunchState = 'idle' | 'starting' | 'running' | 'error';
  let launchState = $state<LaunchState>('idle');
  let showPicker = $state(false);
  let pickerValue = $state<string>('');

  let updateStatus = $state<string | null>(null);

  const pState = $derived($projects);
  const current = $derived($selectedProject);
  const projectList = $derived(pState.projects);
  const hasProjects = $derived(projectList.length > 0);

  const launchLabel = $derived.by(() => {
    if (launchState === 'starting') return 'Starting…';
    if (launchState === 'running') return 'Open project';
    if (launchState === 'error') return 'Retry';
    return 'Open project';
  });

  // Bug 24: support both surfaces. `surface` arg drives the backend
  // dispatch — 'auto' picks vscode if available, falls back to cli.
  async function doLaunch(projectId: string, surface: 'auto' | 'vscode' | 'cli' = 'auto') {
    if (launchState === 'starting') return; // debounce
    const proj = projectList.find((p) => p.id === projectId);
    if (!proj) {
      toast.error('Project not found');
      return;
    }
    launchState = 'starting';
    try {
      await invoke<void>('launch_project_in_editor', { projectId, surface });
      try {
        localStorage.setItem('vct.last_launched_project_id', projectId);
      } catch {}
      launchState = 'running';
      toast.success(`Opened ${proj.name}`);
    } catch (e) {
      launchState = 'error';
      toast.error(e);
    }
  }

  function onLaunchClick() {
    if (!hasProjects) return; // button is disabled, but be defensive
    if (projectList.length === 1) {
      void doLaunch(projectList[0]!.id);
      return;
    }
    // Multiple projects: open picker. Default selection = current project,
    // else last-launched, else first project.
    const last = (() => {
      try {
        return localStorage.getItem('vct.last_launched_project_id') ?? '';
      } catch {
        return '';
      }
    })();
    pickerValue =
      current?.id ??
      projectList.find((p) => p.id === last)?.id ??
      projectList[0]?.id ??
      '';
    showPicker = true;
  }

  function onPickerChange(v: string) {
    pickerValue = v;
    showPicker = false;
    if (v) void doLaunch(v);
  }

  function handleCheckUpdate() {
    if (!selectedApp) return;
    updateStatus = 'Checking…';
    setTimeout(() => {
      updateStatus = 'Up to date';
      setTimeout(() => {
        updateStatus = null;
      }, 2000);
    }, 1000);
  }

  function getColorRgb(color: string): string {
    if (color === 'teal') return '0,191,166';
    if (color === 'purple') return '123,95,255';
    return '255,79,160';
  }
</script>

<aside class="right-sidebar">
  {#if selectedApp}
    <div class="sidebar-section">
      <div
        class="sidebar-app-icon"
        style:background="rgba({getColorRgb(selectedApp.color)}, 0.15)"
        style:border-color="rgba({getColorRgb(selectedApp.color)}, 0.3)"
      >
        <span class="sidebar-app-letter">{selectedApp.name.charAt(0)}</span>
      </div>
      <h3 class="sidebar-app-name">{selectedApp.name}</h3>
    </div>

    <div class="sidebar-divider"></div>

    {#if showLaunchActions}
      <div class="sidebar-section">
        <h4 class="sidebar-label">Quick Actions</h4>
        <div class="sidebar-actions">
          <button
            class="btn-3d btn-3d-primary btn-3d-sm sidebar-action-btn"
            onclick={onLaunchClick}
            disabled={!hasProjects || launchState === 'starting'}
            title={hasProjects
              ? 'Open the selected project (VS Code if installed, else terminal CLI)'
              : 'Create a project first to launch the orchestrator'}
          >
            {launchLabel}
          </button>
          {#if showPicker && projectList.length > 1}
            <div class="launch-picker">
              <Dropdown
                options={projectList.map((p) => ({ value: p.id, label: p.name }))}
                value={pickerValue}
                onChange={onPickerChange}
                placeholder="Pick a project to open…"
              />
            </div>
          {/if}
          <button
            class="btn-3d btn-3d-ghost btn-3d-sm sidebar-action-btn"
            onclick={handleCheckUpdate}
          >
            {updateStatus ?? 'Check Update'}
          </button>
        </div>
      </div>

      <div class="sidebar-divider"></div>
    {:else if showInstallAction}
      <div class="sidebar-section">
        <h4 class="sidebar-label">Quick Actions</h4>
        <div class="sidebar-actions">
          <button
            class="btn-3d btn-3d-primary btn-3d-sm sidebar-action-btn"
            onclick={() => ui.openInstallWizard()}
          >
            Install Free
          </button>
          <p class="sidebar-info-key" style:font-size="11px" style:line-height="1.5">
            VCO isn't installed yet on this machine. Install Free runs the
            wizard with sensible defaults.
          </p>
        </div>
      </div>

      <div class="sidebar-divider"></div>
    {:else if effectiveStage === 'coming_soon'}
      <div class="sidebar-section">
        <h4 class="sidebar-label">Status</h4>
        <div class="sidebar-coming-soon">
          <span class="sidebar-coming-soon-badge">Coming soon</span>
          <p class="sidebar-coming-soon-note">
            Not yet shippable. No install or launch actions until release.
          </p>
        </div>
      </div>

      <div class="sidebar-divider"></div>
    {/if}

    <div class="sidebar-section">
      <h4 class="sidebar-label">App Info</h4>
      <div class="sidebar-info">
        {#if selectedApp.version}
          <div class="sidebar-info-row">
            <span class="sidebar-info-key">Version</span>
            <span class="sidebar-info-value">v{selectedApp.version}</span>
          </div>
        {/if}
        <div class="sidebar-info-row">
          <span class="sidebar-info-key">Status</span>
          {#if effectiveStage === 'coming_soon'}
            <span class="sidebar-info-status sidebar-info-status-soon">Coming soon</span>
          {:else if effectiveStage === 'not_installed'}
            <span class="sidebar-info-status sidebar-info-status-pending">Not installed</span>
          {:else}
            <span class="sidebar-info-status">Installed</span>
          {/if}
        </div>
      </div>
    </div>
  {:else}
    <div class="sidebar-section">
      <div class="sidebar-profile">
        <div class="sidebar-profile-avatar">
          {$currentUser?.name?.charAt(0).toUpperCase() ?? '?'}
        </div>
        <div>
          <p class="sidebar-profile-name">{$currentUser?.name ?? 'User'}</p>
          <p class="sidebar-profile-email">{$currentUser?.email ?? ''}</p>
        </div>
      </div>
    </div>

    <div class="sidebar-divider"></div>

    <div class="sidebar-section">
      <h4 class="sidebar-label">Quick Access</h4>
      <div class="sidebar-actions">
        <!-- Bug 15: even without an app selected, expose Launch so the
             user can open a project in VS Code from the homepage. -->
        <button
          class="btn-3d btn-3d-primary btn-3d-sm sidebar-action-btn"
          onclick={onLaunchClick}
          disabled={!hasProjects || launchState === 'starting'}
          title={hasProjects
            ? 'Open the selected project (VS Code if installed, else terminal CLI)'
            : 'Create a project first to launch the orchestrator'}
        >
          {launchLabel}
        </button>
        {#if showPicker && projectList.length > 1}
          <div class="launch-picker">
            <Dropdown
              options={projectList.map((p) => ({ value: p.id, label: p.name }))}
              value={pickerValue}
              onChange={onPickerChange}
              placeholder="Pick a project to open…"
            />
          </div>
        {/if}
        <button class="btn-3d btn-3d-ghost btn-3d-sm sidebar-action-btn" onclick={onOpenActivation}>
          Activate Code
        </button>
      </div>
    </div>
  {/if}
</aside>

<style>
  .right-sidebar {
    width: 260px;
    flex-shrink: 0;
    background: rgba(8, 15, 40, 0.6);
    border-left: 1px solid rgba(255, 255, 255, 0.06);
    backdrop-filter: blur(16px);
    overflow-y: auto;
    display: flex;
    flex-direction: column;
  }

  .sidebar-section {
    padding: 20px;
  }

  .sidebar-divider {
    height: 1px;
    background: rgba(255, 255, 255, 0.06);
    margin: 0 16px;
  }

  .sidebar-label {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--color-muted);
    margin-bottom: 14px;
  }

  .sidebar-app-icon {
    width: 56px;
    height: 56px;
    border-radius: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 1px solid;
    margin-bottom: 14px;
  }

  .sidebar-app-letter {
    font-size: 22px;
    font-weight: 800;
    color: var(--color-text);
  }

  .sidebar-app-name {
    font-size: 16px;
    font-weight: 800;
    color: var(--color-text);
  }

  .sidebar-actions {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .sidebar-action-btn {
    width: 100%;
    font-size: 12px;
    padding: 8px 14px;
  }
  .sidebar-action-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .launch-picker {
    margin-top: 4px;
  }

  .sidebar-info {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .sidebar-info-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .sidebar-info-key {
    font-size: 12px;
    color: var(--color-muted);
  }

  .sidebar-info-value {
    font-size: 12px;
    font-weight: 600;
    color: var(--color-mid);
  }

  .sidebar-info-status {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-teal);
    background: rgba(0, 191, 166, 0.1);
    padding: 2px 10px;
    border-radius: 20px;
  }
  .sidebar-info-status-soon {
    color: var(--color-pink, #ff4fa0);
    background: rgba(255, 79, 160, 0.12);
    border: 1px solid rgba(255, 79, 160, 0.25);
  }
  .sidebar-info-status-pending {
    color: var(--color-muted);
    background: rgba(255, 255, 255, 0.06);
  }
  .sidebar-coming-soon {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .sidebar-coming-soon-badge {
    align-self: flex-start;
    font-size: 11px;
    font-weight: 700;
    padding: 3px 10px;
    border-radius: 20px;
    color: var(--color-pink, #ff4fa0);
    background: rgba(255, 79, 160, 0.12);
    border: 1px solid rgba(255, 79, 160, 0.25);
  }
  .sidebar-coming-soon-note {
    font-size: 11px;
    color: var(--color-muted);
    line-height: 1.5;
  }

  .sidebar-profile {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .sidebar-profile-avatar {
    width: 40px;
    height: 40px;
    border-radius: 12px;
    background: linear-gradient(135deg, var(--color-purple), var(--color-pink));
    display: flex;
    align-items: center;
    justify-content: center;
    color: white;
    font-weight: 700;
    font-size: 16px;
    flex-shrink: 0;
  }

  .sidebar-profile-name {
    font-size: 14px;
    font-weight: 700;
    color: var(--color-text);
  }

  .sidebar-profile-email {
    font-size: 11px;
    color: var(--color-mid);
    margin-top: 1px;
  }
</style>
