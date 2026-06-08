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

  import { onMount } from 'svelte';
  import { getVersion } from '@tauri-apps/api/app';
  import { currentUser } from '$lib/stores/auth';
  import { projects, selectedProject } from '$lib/stores/projects';
  import { orchestrator } from '$lib/stores/orchestrator';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import { ui } from '$lib/stores/ui';
  import { modules } from '$lib/stores/modules';
  import { moduleActionForKind, detectModuleErrorAfterAction } from '$lib/module-status-display';
  import Dropdown from '$lib/components/Dropdown.svelte';

  // v0.2.43 (Fabio branch feat/launcher-logo-circular-white): brand
  // footer at the bottom of the right sidebar (logo + "VCT Launcher" +
  // version). Replaces the menubar logo (moved here) AND the
  // statusbar version string (also moved here to avoid duplication).
  let appVersion = $state('');
  onMount(async () => {
    try {
      appVersion = await getVersion();
    } catch {
      appVersion = '';
    }
  });

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
    /**
     * v0.2.33 (Agent E, L11): the catalog entry's `kind` field
     * straight from `ModuleCatalogEntry.kind`. When set, this is
     * the AUTHORITATIVE signal for the right-rail Status badge
     * (the same field the home tile renders).
     *
     * Pre-v0.2.33 the right-rail inferred status from a static
     * COMING_SOON_IDS list, which fell through to "Installed" for
     * any unknown id (e.g. an `available` paid module the catalog
     * had just learnt about via L0). User-reported bug: home tile
     * says "Available" but the right-rail says "Installed" for the
     * same `vct-rl-reranker` row. Routing both through the same
     * `kind` field eliminates the drift.
     *
     * Optional for back-compat with callers that don't have a
     * catalog entry (e.g. orchestrator-only flows).
     */
    catalogKind?:
      | 'bundled'
      | 'available'
      | 'installed'
      | 'update_available'
      | 'broken'
      | 'subcomponent'
      | 'coming_soon';
  }

  let {
    selectedApp = null,
    onOpenActivation,
  }: {
    selectedApp: App | null;
    onOpenActivation: () => void;
  } = $props();

  const orchState = $derived($orchestrator);
  // v0.2.33 (Agent E, L11): COMING_SOON_IDS is retained as a fallback
  // for callers that don't pass a catalogKind (e.g. legacy code paths
  // that still construct an `App` from scratch). New callers should
  // pass `catalogKind` straight from `ModuleCatalogEntry.kind` so the
  // right-rail badge agrees with the home tile.
  const COMING_SOON_IDS = new Set([
    'orchestrator-pro', 'mao', 'transcrypt', 'arzillibus',
    'convertifacile', 'dataweave', 'formcraft', 'pixelsnap',
    'rl-reranker',
  ]);
  // Effective stage: explicit prop wins; otherwise derive from
  // catalogKind (v0.2.33 single source of truth); finally fall
  // back to legacy heuristics for back-compat.
  const effectiveStage = $derived.by(() => {
    if (!selectedApp) return 'shipped' as const;
    if (selectedApp.stage) return selectedApp.stage;
    // v0.2.33 (Agent E, L11): if the caller passed the catalog
    // entry's kind, that's the authoritative signal — same source
    // as the home tile.
    if (selectedApp.catalogKind) {
      switch (selectedApp.catalogKind) {
        case 'bundled':
        case 'installed':
        case 'update_available':
        case 'subcomponent':
        case 'broken':
          return 'shipped' as const;
        case 'available':
          return 'not_installed' as const;
        case 'coming_soon':
          return 'coming_soon' as const;
      }
    }
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

  /**
   * v0.2.33 (Agent E, L11): the canonical Status-row label. Driven
   * by the catalog `kind` when available so the home tile and the
   * right-rail show the same string for the same module.
   *
   * Maps:
   *   - 'available'       → 'Not installed'
   *   - 'installed'       → 'Installed'
   *   - 'update_available'→ 'Update available'
   *   - 'broken'          → 'Reinstall needed'
   *   - 'bundled'         → 'Installed' (the launcher itself)
   *   - 'subcomponent'    → 'Included'
   *   - 'coming_soon'     → 'Coming soon'
   *
   * When `catalogKind` is absent, falls back to effectiveStage —
   * keeps the pre-v0.2.33 markup in lock-step for legacy callers.
   */
  const statusLabel = $derived.by(() => {
    if (!selectedApp) return 'Installed';
    if (selectedApp.catalogKind) {
      switch (selectedApp.catalogKind) {
        case 'available':
          return 'Not installed';
        case 'installed':
          return 'Installed';
        case 'update_available':
          return 'Update available';
        case 'broken':
          return 'Reinstall needed';
        case 'bundled':
          return 'Installed';
        case 'subcomponent':
          return 'Included';
        case 'coming_soon':
          return 'Coming soon';
      }
    }
    if (effectiveStage === 'coming_soon') return 'Coming soon';
    if (effectiveStage === 'not_installed') return 'Not installed';
    return 'Installed';
  });

  const statusClass = $derived.by(() => {
    if (effectiveStage === 'coming_soon') return 'sidebar-info-status-soon';
    if (effectiveStage === 'not_installed') return 'sidebar-info-status-pending';
    if (selectedApp?.catalogKind === 'broken') return 'sidebar-info-status-warn';
    if (selectedApp?.catalogKind === 'update_available')
      return 'sidebar-info-status-warn';
    return '';
  });
  // Whether to show the install/launch quick actions.
  const showLaunchActions = $derived(
    selectedApp !== null && effectiveStage === 'shipped' && selectedApp.id === 'orchestrator'
  );
  const showInstallAction = $derived(
    selectedApp !== null && effectiveStage === 'not_installed' && selectedApp.id === 'orchestrator'
  );

  // Module repair/update action (Reinstall / Retry / Update) for an
  // actionable catalog kind. Distinct from showLaunchActions/showInstallAction
  // (which stay orchestrator-only): this surfaces the SAME action the
  // /modules tile and the Home card now expose, so the right-rail status
  // chip stops being a dead label for broken/update_available modules. The
  // mapping is centralised in `moduleActionForKind`. NOT Pro-gated — an
  // actionable kind is already-installed; only a selected project is
  // required (install/update are per-project).
  const moduleRepairAction = $derived(
    selectedApp ? moduleActionForKind(selectedApp.catalogKind) : null
  );
  let moduleRepairBusy = $state(false);

  async function runModuleRepair() {
    if (!selectedApp || !moduleRepairAction) return;
    const project = $selectedProject;
    if (!project) {
      toast.error('Select a project first to install or update modules.');
      return;
    }
    moduleRepairBusy = true;
    const toastKey = `module:${selectedApp.id}:${moduleRepairAction.method}`;
    console.info('[right-rail] module repair start', {
      module: selectedApp.id,
      method: moduleRepairAction.method,
      project: project.id,
    });
    try {
      const row =
        moduleRepairAction.method === 'install'
          ? await modules.install(project.id, selectedApp.id)
          : await modules.update(project.id, selectedApp.id);

      // Same caveat as the Home handler: the command resolves even when the
      // container start failed, but the resolved row can be misleadingly
      // clean (status='installed', last_error=null) — the real failure only
      // surfaces once the catalog recomputes `kind` to 'error'/'broken'
      // (verified via live test 2026-06-06). Reload both surfaces and
      // inspect them together (see detectModuleErrorAfterAction).
      await modules.loadCatalog();
      await modules.loadInstalled(project.id);
      console.info('[right-rail] module repair returned row', {
        module: selectedApp.id,
        status: row?.status,
        last_error: row?.last_error,
        container_name: row?.container_name,
      });
      const errMsg = detectModuleErrorAfterAction(
        selectedApp.id,
        $modules.catalog,
        $modules.installed,
      );
      if (errMsg) {
        toast.error(`${selectedApp.name}: ${errMsg}`, { key: toastKey });
      } else {
        toast.success(
          `${selectedApp.name} ${moduleRepairAction.method === 'install' ? 'reinstalled' : 'updated'}`,
          { key: toastKey },
        );
      }
    } catch (e) {
      console.error('[right-rail] module repair threw', { module: selectedApp.id, e });
      toast.error(e, { key: toastKey });
    } finally {
      moduleRepairBusy = false;
    }
  }

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
    {:else if moduleRepairAction}
      <!-- Actionable module (broken/error/update_available): expose the
           Reinstall/Retry/Update action here too, so the right-rail status
           chip is no longer a dead label. Same command path as the /modules
           tile and the Home card (moduleActionForKind). Disabled + tooltip
           when no project is selected (install/update are per-project). -->
      <div class="sidebar-section">
        <h4 class="sidebar-label">Quick Actions</h4>
        <div class="sidebar-actions">
          <button
            class="btn-3d btn-3d-primary btn-3d-sm sidebar-action-btn"
            disabled={moduleRepairBusy || !current}
            title={!current ? 'Select a project first' : ''}
            onclick={runModuleRepair}
          >
            {moduleRepairBusy ? 'Working…' : moduleRepairAction.label}
          </button>
          <p class="sidebar-info-key" style:font-size="11px" style:line-height="1.5">
            {moduleRepairAction.method === 'update'
              ? 'A newer version is available for this module.'
              : 'This module needs to be reinstalled to work.'}
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
          <!-- v0.2.33 (Agent E, L11): single source of truth. When
               the caller passes `catalogKind` (every Home tile path
               now does), this row's label matches the tile's badge
               byte-for-byte. Pre-v0.2.33 fall-through left this row
               always rendering "Installed" for unknown ids; that
               drift is the bug we're closing. -->
          <span class="sidebar-info-status {statusClass}">{statusLabel}</span>
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

  <!-- v0.2.43 (Fabio): brand footer. Sits at the bottom of the right
       sidebar with margin-top:auto, separated from the content above
       by a divider line. Replaces the menubar logo (visual duplicate
       of the Windows titlebar icon) and the statusbar version string. -->
  <div class="rs-brand-footer" aria-label="VCT Launcher brand">
    <!-- logo-512 (512x512) instead of logo.png (64x64): the footer renders
         at 80px CSS, so the 64px raster was upscaled (+25%, worse on HiDPI
         scaling) and looked pixelated. The 512px source is crisp at any
         render size / display scaling. -->
    <img src="/logo-512.png" alt="" class="rs-brand-logo" aria-hidden="true" />
    <div class="rs-brand-name">VCT Launcher</div>
    {#if appVersion}
      <div class="rs-brand-version">v{appVersion}</div>
    {/if}
  </div>
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

  /* v0.2.43 (Fabio): brand footer at the bottom of the right sidebar.
     `margin-top: auto` pushes it down while the content above stays
     packed at the top. A 1px top border mirrors the existing
     `.sidebar-divider` style for visual continuity. */
  .rs-brand-footer {
    margin-top: auto;
    padding: 20px 16px 16px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
  }
  .rs-brand-logo {
    width: 80px;
    height: 80px;
    user-select: none;
    -webkit-user-drag: none;
    pointer-events: none;
  }
  .rs-brand-name {
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.3px;
    color: var(--color-text, #F1F5F9);
  }
  .rs-brand-version {
    font-size: 10px;
    color: var(--color-muted, #475569);
    font-family: ui-monospace, monospace;
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
  /* v0.2.33 (Agent E, L11): warn variant covers
     `update_available` + `broken` — both surface as amber so the user
     knows "this isn't a clean Installed state" without conflating them
     with coming-soon / not-installed. */
  .sidebar-info-status-warn {
    color: #f1c40f;
    background: rgba(241, 196, 15, 0.12);
    border: 1px solid rgba(241, 196, 15, 0.30);
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
