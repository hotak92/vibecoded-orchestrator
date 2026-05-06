<script lang="ts">
  import { onMount } from 'svelte';
  import { getVersion } from '@tauri-apps/api/app';
  import { auth, currentUser } from '$lib/stores/auth';
  import { settings } from '$lib/stores/settings';
  import { ui } from '$lib/stores/ui';
  import { invoke } from '$lib/tauri';
  import { clearOnboardingComplete } from '$lib/onboarding';
  import SecretsPanel from './SecretsPanel.svelte';
  import DialogRoot from '$lib/components/DialogRoot.svelte';

  let appVersion = $state('');
  onMount(async () => {
    try {
      appVersion = await getVersion();
    } catch {
      appVersion = '';
    }
  });

  let { open = $bindable(false) }: { open: boolean } = $props();

  let activeSection = $state<'profile' | 'downloads' | 'secrets' | 'preferences' | 'about'>('profile');

  // When callers open Settings via ui.openSettings('secrets') (e.g.
  // the per-project SecretsTab "Open secrets panel" button), jump
  // straight to that section instead of staying on whatever was last
  // active. The store field is consumed-once and cleared by
  // closeSettings(), so this only fires when a caller explicitly
  // requested an initial section.
  $effect(() => {
    if (open && $ui.settingsInitialSection) {
      activeSection = $ui.settingsInitialSection;
    }
  });

  // Shared services status. These are the per-machine Weaviate /
  // Ollama / code_embed instances that all orchestrator installs reuse via
  // KG_COLLECTION namespacing inside the shared Weaviate. Probed on demand
  // when the user opens the Preferences panel.
  interface ServicesStatus {
    weaviate_url: string | null;
    ollama_url: string | null;
    code_embed_url: string | null;
    all_detected: boolean;
    none_detected: boolean;
  }
  let services = $state<ServicesStatus | null>(null);
  let servicesLoading = $state(false);
  let servicesError = $state<string | null>(null);

  async function refreshServices() {
    servicesLoading = true;
    servicesError = null;
    try {
      services = await invoke<ServicesStatus>('detect_existing_services');
    } catch (e) {
      servicesError = String(e);
    } finally {
      servicesLoading = false;
    }
  }

  // Bug 31: volume location row. Reflects the current launcher.toml +
  // detected mountpoints. Migrate dialog (Change…) builds a dry-run
  // plan, shows it, and only fires the actual migration on Confirm.
  interface VolumeWithSize {
    name: string;
    mountpoint: string;
    size_bytes: number | null;
    size_human: string | null;
    role: string;
  }
  interface VolumesConfig {
    volumes_path: string;
    mode: string;
    legacy_mapping: { volume_name: string; mountpoint: string; role: string }[];
    total_size_human: string | null;
    volumes: VolumeWithSize[];
  }
  interface MigrationPlan {
    from_mode: string;
    to_path: string;
    volumes_to_copy: VolumeWithSize[];
    total_bytes: number;
    total_human: string;
    estimated_seconds: number;
    free_bytes_at_target: number | null;
    insufficient_free_space: boolean;
    warnings: string[];
  }
  let volumesConfig = $state<VolumesConfig | null>(null);
  let volumesLoading = $state(false);
  let volumesError = $state<string | null>(null);
  let migratingVolumes = $state(false);
  let migratePath = $state('');
  let migrationPlan = $state<MigrationPlan | null>(null);
  let migrationError = $state<string | null>(null);

  // Reviewer A + B round-2: surface real phase progress instead of a
  // dead "Migrating…" spinner. Backend emits 'volumes://migrate-progress'
  // events from migrate_volumes (commands/volumes.rs::MigratePhase).
  interface MigratePhaseEvent {
    phase:
      | 'stopping_containers'
      | { copying_volume: { volume_role: string; index: number; total: number } }
      | 'writing_override'
      | 'starting_containers'
      | 'waiting_for_health'
      | 'removing_legacy_volumes'
      | 'done'
      | { rolling_back: { reason: string } };
    message: string;
  }
  let migrationPhaseLabel = $state<string | null>(null);
  let migrationCopyProgress = $state<{ index: number; total: number } | null>(null);

  async function refreshVolumes() {
    volumesLoading = true;
    volumesError = null;
    try {
      volumesConfig = await invoke<VolumesConfig>('get_volumes_config');
    } catch (e) {
      volumesError = String(e);
    } finally {
      volumesLoading = false;
    }
  }

  async function startMigrationDryRun() {
    if (!migratePath.trim()) {
      migrationError = 'Pick a target path first.';
      return;
    }
    migrationError = null;
    try {
      migrationPlan = await invoke<MigrationPlan>('set_volumes_config_dry_run', {
        path: migratePath.trim(),
      });
    } catch (e) {
      migrationError = String(e);
    }
  }

  async function confirmMigration() {
    if (!migrationPlan) return;
    if (migrationPlan.insufficient_free_space) {
      migrationError = 'Insufficient free space at target — pick a larger volume.';
      return;
    }
    migratingVolumes = true;
    migrationError = null;
    migrationPhaseLabel = 'Starting…';
    migrationCopyProgress = null;

    // Subscribe to phase events for the duration of this call.
    // listen() is dynamically imported so the import doesn't pollute the
    // top-level namespace if Tauri's event API is unavailable in tests.
    const { listen } = await import('@tauri-apps/api/event');
    const unlisten = await listen<MigratePhaseEvent>(
      'volumes://migrate-progress',
      (ev) => {
        const { phase, message } = ev.payload;
        migrationPhaseLabel = message;
        if (typeof phase === 'object' && 'copying_volume' in phase) {
          migrationCopyProgress = {
            index: phase.copying_volume.index,
            total: phase.copying_volume.total,
          };
        } else {
          migrationCopyProgress = null;
        }
      }
    );

    try {
      await invoke('migrate_volumes', {
        path: migrationPlan.to_path,
        confirmed: true,
      });
      migrationPlan = null;
      migratePath = '';
      await refreshVolumes();
    } catch (e) {
      migrationError = String(e);
    } finally {
      unlisten();
      migratingVolumes = false;
      migrationPhaseLabel = null;
      migrationCopyProgress = null;
    }
  }

  function cancelMigration() {
    migrationPlan = null;
    migratePath = '';
    migrationError = null;
  }

  // Re-probe whenever the user navigates into Preferences. Cheap (≤2s).
  $effect(() => {
    if (activeSection === 'preferences' && services === null && !servicesLoading) {
      void refreshServices();
    }
    if (activeSection === 'preferences' && volumesConfig === null && !volumesLoading) {
      void refreshVolumes();
    }
  });

  // Profile edit state
  let editName = $state($currentUser?.name ?? '');
  let profileSaved = $state(false);

  currentUser.subscribe((u) => {
    if (u) editName = u.name;
  });

  async function saveProfile() {
    if (!editName.trim()) return;
    await auth.updateProfile(editName.trim());
    profileSaved = true;
    setTimeout(() => { profileSaved = false; }, 2000);
  }

  // Settings
  let installPath = $state('');
  let autoUpdate = $state(true);
  let launchOnStartup = $state(false);

  settings.subscribe((s) => {
    installPath = s.installPath;
    autoUpdate = s.autoUpdate;
    launchOnStartup = s.launchOnStartup;
  });

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') open = false;
  }

  const sections = [
    { id: 'profile' as const, label: 'Profile' },
    { id: 'downloads' as const, label: 'Downloads' },
    { id: 'secrets' as const, label: 'Secrets' },
    { id: 'preferences' as const, label: 'Preferences' },
    { id: 'about' as const, label: 'About' },
  ];

  // Bug 14 fix (2026-05-05): onboarding-completion now lives in launcher.db
  // (per VCT_STATE_DIR-isolated paths). Re-running clears the DB flag via
  // the `clearOnboardingComplete` helper, then sets a one-shot localStorage
  // signal `vct.onboarding_force` that survives only until the next layout
  // mount (which consumes + deletes it). The signal stays in localStorage
  // because it's request-scoped, not state-scoped — it never crosses
  // launcher instances.
  async function rerunOnboarding() {
    await clearOnboardingComplete();
    try {
      localStorage.setItem('vct.onboarding_force', '1');
    } catch { /* ignore */ }
    open = false;
    // Trigger a reload so layout's onMount re-evaluates the gate. Avoids
    // having to plumb the force flag through Svelte stores.
    window.location.reload();
  }
</script>

<!-- Bug 26: native <dialog> top-layer rendering via DialogRoot.
     The two-column nav/content layout fills the dialog content shell
     directly; body padding is cancelled with a negative-margin wrapper
     so the inner panel can paint edge-to-edge. -->
<DialogRoot bind:open width="760px" onClose={() => { open = false; }}>
  {#snippet body()}
    <div class="settings-shell">
      <!-- Left nav -->
      <div class="settings-nav">
        <h2 class="settings-title">Settings</h2>
        {#each sections as section}
          <button
            class="settings-nav-item"
            class:active={activeSection === section.id}
            onclick={() => (activeSection = section.id)}
          >
            {section.label}
          </button>
        {/each}
      </div>

      <!-- Right content -->
      <div class="settings-content">
        <button class="settings-close" onclick={() => (open = false)} aria-label="Close">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M18 6L6 18"/><path d="M6 6l12 12"/>
          </svg>
        </button>

        {#if activeSection === 'profile'}
          <h3 class="section-title">Profile</h3>
          <div class="form-group">
            <label for="settings-name">Display Name</label>
            <input
              id="settings-name"
              type="text"
              bind:value={editName}
              class="form-input"
            />
          </div>
          <div class="form-group">
            <label>Email</label>
            <input
              type="email"
              value={$currentUser?.email ?? ''}
              class="form-input"
              disabled
            />
            <p class="form-hint">Email cannot be changed here</p>
          </div>
          <div class="form-actions">
            <button class="btn-3d btn-3d-primary" onclick={saveProfile}>
              Save Changes
            </button>
            {#if profileSaved}
              <span class="save-msg">Saved!</span>
            {/if}
          </div>

        {:else if activeSection === 'downloads'}
          <h3 class="section-title">Downloads</h3>
          <div class="form-group">
            <label for="install-path">Install Location</label>
            <input
              id="install-path"
              type="text"
              bind:value={installPath}
              class="form-input mono"
              onchange={() => settings.updateSetting('installPath', installPath)}
            />
            <p class="form-hint">Where apps will be downloaded and installed</p>
          </div>
          <div class="form-group">
            <label class="toggle-row">
              <input
                type="checkbox"
                bind:checked={autoUpdate}
                onchange={() => settings.updateSetting('autoUpdate', autoUpdate)}
              />
              <span class="toggle-label">Auto-update apps</span>
            </label>
            <p class="form-hint">Automatically download and install app updates</p>
          </div>
          <div class="form-group">
            <label class="toggle-row">
              <input
                type="checkbox"
                bind:checked={launchOnStartup}
                onchange={() => settings.updateSetting('launchOnStartup', launchOnStartup)}
              />
              <span class="toggle-label">Launch on system startup</span>
            </label>
          </div>

        {:else if activeSection === 'secrets'}
          <SecretsPanel />

        {:else if activeSection === 'preferences'}
          <h3 class="section-title">Preferences</h3>
          <div class="form-group">
            <p class="form-hint" style="margin-bottom: 10px;">
              Re-runs the four-step setup wizard you saw on first launch
              (system detection, container install, first project). Useful
              if your local DB is stale or you want to revisit the install
              path.
            </p>
            <button class="btn-3d btn-3d-primary" onclick={rerunOnboarding}>
              Run setup wizard
            </button>
          </div>

          <!-- Bug 29: shared services status. These run once per machine
               and every orchestrator install talks to the same instances. -->
          <div class="form-group" style="margin-top: 18px;">
            <h4 class="subsection-title">Shared services</h4>
            <p class="form-hint" style="margin: 0 0 8px;">
              Used by every orchestrator install on this machine. Per-install
              isolation comes from separate Knowledge Graph collections
              inside the shared Weaviate, not from separate containers.
            </p>
            {#if servicesLoading}
              <p class="form-hint">Probing…</p>
            {:else if servicesError}
              <p class="form-hint" style="color:#f99;">Couldn't probe: {servicesError}</p>
            {:else if services}
              <ul class="services-list">
                <li class:on={services.weaviate_url}>
                  <span class="dot"></span>
                  <span class="lbl">Weaviate</span>
                  <code class="mono">
                    {services.weaviate_url ?? 'http://localhost:8081 (not running)'}
                  </code>
                </li>
                <li class:on={services.ollama_url}>
                  <span class="dot"></span>
                  <span class="lbl">Ollama</span>
                  <code class="mono">
                    {services.ollama_url ?? 'http://localhost:11435 (not running)'}
                  </code>
                </li>
                <li class:on={services.code_embed_url}>
                  <span class="dot"></span>
                  <span class="lbl">code_embed</span>
                  <code class="mono">
                    {services.code_embed_url ?? 'http://localhost:11440 (not running)'}
                  </code>
                </li>
              </ul>
              <button class="btn-3d" onclick={refreshServices}>Refresh</button>
            {/if}
          </div>

          <!-- Bug 31: container volumes location. Shows the current
               volumes_path mode (default / detected / custom) plus a
               Change… button that opens a dry-run plan dialog. -->
          <div class="form-group" style="margin-top: 18px;">
            <h4 class="subsection-title">Volume location</h4>
            <p class="form-hint" style="margin: 0 0 8px;">
              Where Weaviate's vector index, Ollama's models, and the
              code-embed cache live. Changing this safely copies all
              data, verifies new bind-mounts come up healthy, then
              removes the old volumes. On any failure the migration
              rolls back without touching your data.
            </p>
            {#if volumesLoading}
              <p class="form-hint">Probing…</p>
            {:else if volumesError}
              <p class="form-hint" style="color:#f99;">Couldn't probe: {volumesError}</p>
            {:else if volumesConfig}
              <ul class="volumes-list">
                {#each volumesConfig.volumes as v}
                  <li>
                    <span class="vol-role">{v.role}</span>
                    <code class="mono">{v.mountpoint}</code>
                    {#if v.size_human}
                      <span class="vol-size">{v.size_human}</span>
                    {/if}
                  </li>
                {/each}
              </ul>
              <p class="form-hint" style="margin: 6px 0;">
                Mode: <strong>{volumesConfig.mode}</strong>
                {#if volumesConfig.total_size_human}
                  · {volumesConfig.total_size_human} total
                {/if}
              </p>

              {#if migrationPlan}
                <!-- Confirm dialog (inline) -->
                <div class="migrate-confirm">
                  <p>
                    Move <strong>{migrationPlan.total_human}</strong>
                    from {migrationPlan.from_mode} to
                    <code class="mono">{migrationPlan.to_path}</code>
                    (~{Math.ceil(migrationPlan.estimated_seconds / 60)} min on local SSD)?
                  </p>
                  {#each migrationPlan.warnings as w}
                    <p class="form-hint" style="color: #ffb84a;">{w}</p>
                  {/each}
                  <div class="migrate-actions">
                    <button class="btn-3d" onclick={cancelMigration} disabled={migratingVolumes}>
                      Cancel
                    </button>
                    <button
                      class="btn-3d btn-3d-primary"
                      onclick={confirmMigration}
                      disabled={migratingVolumes || migrationPlan.insufficient_free_space}
                    >
                      {migratingVolumes ? 'Migrating…' : 'Confirm migration'}
                    </button>
                  </div>
                  {#if migratingVolumes && migrationPhaseLabel}
                    <p class="form-hint" style="color:#9cf;">
                      {migrationPhaseLabel}{#if migrationCopyProgress} ({migrationCopyProgress.index}/{migrationCopyProgress.total}){/if}
                    </p>
                  {/if}
                  {#if migrationError}<p class="form-hint" style="color:#f99;">{migrationError}</p>{/if}
                </div>
              {:else}
                <div class="migrate-row">
                  <input
                    type="text"
                    class="migrate-input"
                    bind:value={migratePath}
                    placeholder="/mnt/big-disk/vct-volumes"
                  />
                  <button class="btn-3d" onclick={startMigrationDryRun}>Change…</button>
                  <button class="btn-3d" onclick={refreshVolumes}>Refresh</button>
                </div>
                {#if migrationError}<p class="form-hint" style="color:#f99;">{migrationError}</p>{/if}
              {/if}
            {/if}
          </div>

        {:else if activeSection === 'about'}
          <h3 class="section-title">About</h3>
          <div class="about-info">
            <div class="about-logo">
              <div class="about-logo-icon">
                <span>V</span>
              </div>
              <div>
                <p class="about-name">VCT Launcher</p>
                <p class="about-version">{appVersion ? `v${appVersion}` : ''}</p>
              </div>
            </div>
            <div class="about-rows">
              <div class="about-row">
                <span class="about-label">Framework</span>
                <span class="about-value">Tauri 2 + SvelteKit</span>
              </div>
              <div class="about-row">
                <span class="about-label">Website</span>
                <span class="about-value about-link">vibecodedtools.com</span>
              </div>
              <div class="about-row">
                <span class="about-label">License</span>
                <span class="about-value">MIT</span>
              </div>
            </div>
          </div>
        {/if}
      </div>
    </div>
  {/snippet}
</DialogRoot>

<style>
  /* Bug 26: backdrop / sizing / outer shell now handled by DialogRoot.
     The negative-margin wrapper cancels the default .dialog-body padding
     so the two-column settings panel can paint edge-to-edge inside the
     dialog content shell. */
  .settings-shell {
    margin: -16px -20px;
    height: 560px;
    max-height: calc(100vh - 4rem);
    display: flex;
    overflow: hidden;
  }

  @keyframes fade-in {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  .settings-nav {
    width: 180px;
    flex-shrink: 0;
    padding: 20px 12px;
    border-right: 1px solid rgba(255, 255, 255, 0.06);
    background: rgba(0, 0, 0, 0.15);
  }

  .settings-title {
    font-size: 14px;
    font-weight: 800;
    color: var(--color-text);
    padding: 0 10px;
    margin-bottom: 16px;
  }

  .settings-nav-item {
    display: block;
    width: 100%;
    padding: 8px 10px;
    font-size: 13px;
    font-weight: 500;
    color: var(--color-mid);
    background: none;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    text-align: left;
    transition: all 0.15s ease;
    margin-bottom: 2px;
  }

  .settings-nav-item:hover {
    color: var(--color-text);
    background: rgba(255, 255, 255, 0.04);
  }

  .settings-nav-item.active {
    color: var(--color-text);
    background: rgba(0, 191, 166, 0.1);
  }

  .settings-content {
    flex: 1;
    padding: 24px;
    overflow-y: auto;
    position: relative;
  }

  .settings-close {
    position: absolute;
    top: 16px;
    right: 16px;
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 1px solid transparent;
    border-radius: 8px;
    color: var(--color-mid);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .settings-close:hover {
    color: var(--color-text);
    background: rgba(255, 255, 255, 0.06);
  }

  .section-title {
    font-size: 18px;
    font-weight: 700;
    color: var(--color-text);
    margin-bottom: 24px;
  }

  .form-group {
    margin-bottom: 20px;
  }

  .form-group label {
    display: block;
    font-size: 12px;
    font-weight: 600;
    color: var(--color-mid);
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .form-input {
    width: 100%;
    padding: 10px 14px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 10px;
    color: var(--color-text);
    font-size: 13px;
    font-family: inherit;
    outline: none;
    transition: all 0.2s ease;
  }

  .form-input.mono {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 12px;
  }

  .form-input:focus {
    border-color: rgba(0, 191, 166, 0.5);
    box-shadow: 0 0 0 3px rgba(0, 191, 166, 0.1);
  }

  .form-input:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .form-hint {
    font-size: 11px;
    color: var(--color-muted);
    margin-top: 4px;
  }

  .form-actions {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .save-msg {
    font-size: 13px;
    font-weight: 600;
    color: var(--color-teal);
    animation: fade-in 0.2s ease-out;
  }

  .toggle-row {
    display: flex;
    align-items: center;
    gap: 10px;
    cursor: pointer;
    text-transform: none !important;
    letter-spacing: 0 !important;
  }

  .toggle-row input[type='checkbox'] {
    width: 18px;
    height: 18px;
    accent-color: var(--color-teal);
    cursor: pointer;
  }

  .toggle-label {
    font-size: 13px;
    font-weight: 500;
    color: var(--color-text);
  }

  /* About */
  .about-info {
    display: flex;
    flex-direction: column;
    gap: 24px;
  }

  .about-logo {
    display: flex;
    align-items: center;
    gap: 14px;
  }

  .about-logo-icon {
    width: 48px;
    height: 48px;
    border-radius: 14px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(135deg, var(--color-teal), var(--color-purple));
    box-shadow: 0 4px 16px rgba(0, 191, 166, 0.25);
  }

  .about-logo-icon span {
    color: var(--color-bg);
    font-weight: 900;
    font-size: 20px;
  }

  .about-name {
    font-size: 16px;
    font-weight: 700;
    color: var(--color-text);
  }

  .about-version {
    font-size: 12px;
    color: var(--color-mid);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }

  .about-rows {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .about-row {
    display: flex;
    justify-content: space-between;
    padding: 8px 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  }

  .about-label {
    font-size: 13px;
    color: var(--color-mid);
  }

  .about-value {
    font-size: 13px;
    font-weight: 600;
    color: var(--color-text);
  }

  .about-link {
    color: var(--color-teal);
  }

  /* Bug 29: shared services panel in Preferences. */
  .subsection-title {
    font-size: 13px;
    margin: 0 0 6px;
    color: #ccc;
  }
  .services-list {
    list-style: none;
    padding: 0;
    margin: 0 0 10px;
    font-size: 12px;
  }
  .services-list li {
    display: flex;
    gap: 8px;
    align-items: baseline;
    padding: 4px 0;
    color: #999;
  }
  .services-list li.on {
    color: #ccc;
  }
  .services-list .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #555;
    display: inline-block;
    flex-shrink: 0;
    align-self: center;
  }
  .services-list li.on .dot {
    background: rgb(0, 191, 166);
  }
  .services-list .lbl {
    min-width: 80px;
  }
  .services-list .mono {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    color: #c4b3ff;
    background: rgba(255, 255, 255, 0.04);
    padding: 1px 5px;
    border-radius: 3px;
    word-break: break-all;
  }

  /* Bug 31: volume location row + migrate dialog. */
  .volumes-list { list-style: none; padding: 0; margin: 0 0 6px; }
  .volumes-list li { padding: 3px 0; color: #ccc; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; font-size: 12px; }
  .vol-role { display: inline-block; min-width: 80px; color: #c4b3ff; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
  .vol-size { color: #888; font-size: 11px; }
  .migrate-row { display: flex; gap: 6px; align-items: center; margin-top: 6px; }
  .migrate-input { flex: 1; padding: 4px 8px; font-family: ui-monospace, monospace; font-size: 12px; background: rgba(0,0,0,0.3); color: #eee; border: 1px solid rgba(255,255,255,0.08); border-radius: 4px; }
  .migrate-confirm { margin-top: 8px; padding: 10px 12px; border: 1px solid rgba(255,184,74,0.3); border-radius: 6px; background: rgba(255,184,74,0.04); }
  .migrate-confirm p { margin: 4px 0; font-size: 12px; color: #ddd; }
  .migrate-actions { display: flex; gap: 8px; margin-top: 8px; }
</style>
