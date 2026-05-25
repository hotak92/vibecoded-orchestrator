<script lang="ts">
  import { invoke } from '$lib/tauri';
  import { currentUser } from '$lib/stores/auth';
  import DialogRoot from '$lib/components/DialogRoot.svelte';

  let { onClose }: { onClose: () => void } = $props();

  interface McpSetting {
    label: string;
    value: string;
    setting_type: string;
    description: string;
    editable: boolean;
  }

  interface McpServer {
    id: string;
    name: string;
    description: string;
    enabled: boolean;
    command: string;
    min_tier: string;
    port: number | null;
    configurable: boolean;
    settings: Record<string, McpSetting>;
  }

  interface FeatureFlags {
    tier: string;
    can_auto_update: boolean;
    can_disable_watermark: boolean;
    has_rl_retrieval: boolean;
    has_curated_agents: boolean;
    has_mao: boolean;
  }

  interface OrchestratorConfig {
    install_path: string;
    tier: string;
    watermark_enabled: boolean;
    auto_update_enabled: boolean;
    rl_retrieval_enabled: boolean;
    telemetry_enabled: boolean;
    telemetry_anonymous_usage: boolean;
    mcp_servers: McpServer[];
  }

  let config = $state<OrchestratorConfig | null>(null);
  let features = $state<FeatureFlags | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let editingMcp = $state<string | null>(null);
  let activeTab = $state<'services' | 'features' | 'telemetry'>('services');

  async function loadConfig() {
    loading = true;
    try {
      const userApps = $currentUser?.apps ?? [];
      const [cfg, flags] = await Promise.all([
        invoke<OrchestratorConfig>('get_orchestrator_config'),
        invoke<FeatureFlags>('get_feature_flags', { userApps }),
      ]);
      config = cfg;
      features = flags;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  async function toggleMcp(mcpId: string, enabled: boolean) {
    error = null;
    try {
      const userApps = $currentUser?.apps ?? [];
      const servers = await invoke<McpServer[]>('toggle_mcp_server', { mcpId, enabled, userApps });
      if (config) config.mcp_servers = servers;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  async function updateMcpSetting(mcpId: string, key: string, value: string) {
    error = null;
    try {
      const updated = await invoke<McpServer>('update_mcp_setting', {
        mcpId, settingKey: key, settingValue: value,
      });
      if (config) {
        const idx = config.mcp_servers.findIndex(s => s.id === mcpId);
        if (idx >= 0) config.mcp_servers[idx] = updated;
      }
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  async function updateSetting(key: string, value: string) {
    error = null;
    try {
      const userApps = $currentUser?.apps ?? [];
      config = await invoke<OrchestratorConfig>('update_orchestrator_setting', { key, value, userApps });
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  function tierLabel(tier: string): string {
    if (tier === 'free') return 'Free';
    if (tier === 'pro') return 'Pro';
    if (tier === 'mao') return 'MAO';
    return tier;
  }

  function tierColor(tier: string): string {
    if (tier === 'free') return 'var(--color-teal)';
    if (tier === 'pro') return 'var(--color-purple)';
    if (tier === 'mao') return 'var(--color-pink)';
    return 'var(--color-mid)';
  }

  function isContainerService(cmd: string): boolean {
    return cmd.startsWith('container:');
  }

  // Load on mount
  $effect(() => { loadConfig(); });
</script>

<!-- Bug 26: native <dialog> top-layer rendering via DialogRoot. -->
<DialogRoot open={true} width="680px" onClose={onClose}>
  {#snippet header()}
    <div class="dashboard-header-row">
      <h2>Orchestrator Dashboard</h2>
      {#if features}
        <span class="tier-badge" style="--tier-color: {tierColor(features.tier)}">
          {tierLabel(features.tier)}
        </span>
      {/if}
      <button class="close-btn" onclick={onClose} aria-label="Close">&times;</button>
    </div>
    <!-- Tabs (rendered in header so they stay pinned above the scrolling body) -->
    <div class="tab-bar">
      <button class="tab" class:active={activeTab === 'services'} onclick={() => activeTab = 'services'}>
        Services
      </button>
      <button class="tab" class:active={activeTab === 'features'} onclick={() => activeTab = 'features'}>
        Features
      </button>
      <button class="tab" class:active={activeTab === 'telemetry'} onclick={() => activeTab = 'telemetry'}>
        Privacy
      </button>
    </div>
  {/snippet}
  {#snippet body()}
    {#if loading}
      <div class="spinner"></div>
    {:else if config && features}
      <div class="dashboard-body-inner">

        {#if error}
          <div class="error-box">{error}</div>
        {/if}

        <!-- Tab: Services (MCP servers) -->
        {#if activeTab === 'services'}
          <div class="services-grid">
            {#each config.mcp_servers as server}
              <div class="service-card" class:disabled={!server.enabled}>
                <div class="service-header">
                  <div class="service-info">
                    <h3>{server.name}</h3>
                    <p class="service-desc">{server.description}</p>
                    {#if isContainerService(server.command)}
                      <span class="service-badge container">Container</span>
                    {:else}
                      <span class="service-badge process">Process</span>
                    {/if}
                    {#if server.port}
                      <span class="service-badge port">:{server.port}</span>
                    {/if}
                  </div>
                  <label class="toggle-switch">
                    <input type="checkbox" checked={server.enabled}
                      onchange={(e) => toggleMcp(server.id, (e.target as HTMLInputElement).checked)} />
                    <span class="toggle-slider"></span>
                  </label>
                </div>

                <!-- Settings (expandable) -->
                {#if server.configurable && Object.keys(server.settings).length > 0}
                  <button class="settings-toggle"
                    onclick={() => editingMcp = editingMcp === server.id ? null : server.id}>
                    {editingMcp === server.id ? 'Hide settings' : 'Settings'}
                  </button>

                  {#if editingMcp === server.id}
                    <div class="settings-panel">
                      {#each Object.entries(server.settings) as [key, setting]}
                        {@const inputId = `mcp-setting-${server.id}-${key}`}
                        <div class="setting-row">
                          <label class="setting-label" title={setting.description} for={inputId}>
                            {setting.label}
                          </label>
                          {#if setting.setting_type === 'bool'}
                            <input id={inputId} type="checkbox" checked={setting.value === 'true'}
                              disabled={!setting.editable}
                              onchange={(e) => updateMcpSetting(server.id, key, String((e.target as HTMLInputElement).checked))} />
                          {:else if setting.setting_type === 'secret'}
                            <input id={inputId} type="password" value={setting.value}
                              disabled={!setting.editable}
                              onblur={(e) => updateMcpSetting(server.id, key, (e.target as HTMLInputElement).value)}
                              placeholder="Enter value..." />
                          {:else}
                            <input id={inputId} type="text" value={setting.value}
                              disabled={!setting.editable}
                              onblur={(e) => updateMcpSetting(server.id, key, (e.target as HTMLInputElement).value)} />
                          {/if}
                        </div>
                      {/each}
                    </div>
                  {/if}
                {/if}
              </div>
            {/each}
          </div>

        <!-- Tab: Features -->
        {:else if activeTab === 'features'}
          <div class="features-list">
            <!-- Watermark -->
            <div class="feature-row">
              <div class="feature-info">
                <h3>Watermark</h3>
                <p>Adds "Made with VibeCoded Tools" comment to new files</p>
              </div>
              <label class="toggle-switch">
                <input type="checkbox" checked={config.watermark_enabled}
                  disabled={!features.can_disable_watermark && config.watermark_enabled}
                  onchange={(e) => updateSetting('watermark_enabled', String((e.target as HTMLInputElement).checked))} />
                <span class="toggle-slider"></span>
              </label>
              {#if !features.can_disable_watermark}
                <span class="upgrade-hint">Pro to disable</span>
              {/if}
            </div>

            <!-- Auto-update -->
            <div class="feature-row">
              <div class="feature-info">
                <h3>Auto-update</h3>
                <p>Automatically check and install orchestrator updates</p>
              </div>
              <label class="toggle-switch">
                <input type="checkbox" checked={config.auto_update_enabled}
                  disabled={!features.can_auto_update}
                  onchange={(e) => updateSetting('auto_update_enabled', String((e.target as HTMLInputElement).checked))} />
                <span class="toggle-slider"></span>
              </label>
              {#if !features.can_auto_update}
                <span class="upgrade-hint">Pro</span>
              {/if}
            </div>

            <!-- RL Retrieval -->
            <div class="feature-row">
              <div class="feature-info">
                <h3>RL-Scored Retrieval</h3>
                <p>Reinforcement learning reranking — learns from your usage patterns</p>
              </div>
              <label class="toggle-switch">
                <input type="checkbox" checked={config.rl_retrieval_enabled}
                  disabled={!features.has_rl_retrieval}
                  onchange={(e) => updateSetting('rl_retrieval_enabled', String((e.target as HTMLInputElement).checked))} />
                <span class="toggle-slider"></span>
              </label>
              {#if !features.has_rl_retrieval}
                <span class="upgrade-hint">Pro</span>
              {/if}
            </div>
          </div>

        <!-- Tab: Telemetry -->
        {:else if activeTab === 'telemetry'}
          <div class="features-list">
            <div class="telemetry-intro">
              <p>We collect minimal, anonymous data to improve the product. No code, file contents, or personal information is ever collected.</p>
            </div>

            <div class="feature-row">
              <div class="feature-info">
                <h3>Telemetry</h3>
                <p>License validation + product version (required for paid tiers)</p>
              </div>
              <label class="toggle-switch">
                <input type="checkbox" checked={config.telemetry_enabled}
                  onchange={(e) => updateSetting('telemetry_enabled', String((e.target as HTMLInputElement).checked))} />
                <span class="toggle-slider"></span>
              </label>
            </div>

            <div class="feature-row">
              <div class="feature-info">
                <h3>Anonymous usage analytics</h3>
                <p>Feature usage counts and performance metrics (no code or content)</p>
              </div>
              <label class="toggle-switch">
                <input type="checkbox" checked={config.telemetry_anonymous_usage}
                  onchange={(e) => updateSetting('telemetry_anonymous_usage', String((e.target as HTMLInputElement).checked))} />
                <span class="toggle-slider"></span>
              </label>
            </div>
          </div>
        {/if}
      </div>
    {/if}
  {/snippet}
</DialogRoot>

<style>
  /* Bug 26: backdrop / sizing / shell now handled by DialogRoot. */
  .dashboard-header-row {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .dashboard-header-row h2 { font-size: 18px; font-weight: 600; flex: 1; margin: 0; }
  .close-btn {
    background: none; border: none; color: var(--color-mid);
    font-size: 24px; cursor: pointer; padding: 0 4px;
  }
  .close-btn:hover { color: var(--color-text); }
  .tier-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--tier-color);
    border: 1px solid var(--tier-color);
    opacity: 0.9;
  }
  .dashboard-body-inner { padding-top: 4px; }

  /* Tabs (sit at the bottom of the dialog header). */
  .tab-bar {
    display: flex;
    border-bottom: 1px solid var(--color-border);
    margin: 12px -20px -16px;  /* counter dialog-header padding so tabs span full width */
    padding: 0 20px;
  }
  .tab {
    padding: 12px 20px;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--color-mid);
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
  }
  .tab:hover { color: var(--color-text); }
  .tab.active { color: var(--color-teal); border-bottom-color: var(--color-teal); }

  /* Service cards */
  .services-grid { display: flex; flex-direction: column; gap: 12px; }
  .service-card {
    background: var(--color-card);
    border: 1px solid var(--color-border);
    border-radius: 12px;
    padding: 16px;
    transition: border-color 0.2s;
  }
  .service-card:hover { border-color: rgba(255, 255, 255, 0.12); }
  .service-card.disabled { opacity: 0.6; }
  .service-header { display: flex; justify-content: space-between; align-items: flex-start; }
  .service-info h3 { font-size: 15px; font-weight: 600; margin-bottom: 4px; }
  .service-desc { font-size: 12px; color: var(--color-muted); margin-bottom: 8px; }
  .service-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 500;
    margin-right: 6px;
  }
  .service-badge.container { background: rgba(0, 191, 166, 0.1); color: var(--color-teal); }
  .service-badge.process { background: rgba(123, 95, 255, 0.1); color: var(--color-purple); }
  .service-badge.port { background: rgba(255, 255, 255, 0.05); color: var(--color-mid); }

  /* Toggle switch */
  .toggle-switch { position: relative; display: inline-block; width: 44px; height: 24px; flex-shrink: 0; }
  .toggle-switch input { opacity: 0; width: 0; height: 0; }
  .toggle-slider {
    position: absolute; inset: 0;
    background: var(--color-muted);
    border-radius: 24px;
    cursor: pointer;
    transition: background 0.3s;
  }
  .toggle-slider::before {
    content: '';
    position: absolute;
    height: 18px; width: 18px;
    left: 3px; bottom: 3px;
    background: white;
    border-radius: 50%;
    transition: transform 0.3s;
  }
  .toggle-switch input:checked + .toggle-slider { background: var(--color-teal); }
  .toggle-switch input:checked + .toggle-slider::before { transform: translateX(20px); }
  .toggle-switch input:disabled + .toggle-slider { opacity: 0.4; cursor: not-allowed; }

  /* Settings panel */
  .settings-toggle {
    background: none;
    border: none;
    color: var(--color-teal);
    font-size: 12px;
    cursor: pointer;
    padding: 4px 0;
    margin-top: 8px;
  }
  .settings-toggle:hover { text-decoration: underline; }
  .settings-panel {
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--color-border);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .setting-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
  }
  .setting-label {
    font-size: 13px;
    color: var(--color-mid);
    flex-shrink: 0;
    cursor: help;
  }
  .setting-row input[type="text"],
  .setting-row input[type="password"] {
    flex: 1;
    padding: 6px 10px;
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 6px;
    color: var(--color-text);
    font-size: 13px;
    font-family: 'JetBrains Mono', monospace;
  }
  .setting-row input:focus { outline: none; border-color: var(--color-teal); }
  .setting-row input:disabled { opacity: 0.5; }

  /* Features list */
  .features-list { display: flex; flex-direction: column; gap: 16px; }
  .feature-row {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 14px 16px;
    background: var(--color-card);
    border: 1px solid var(--color-border);
    border-radius: 12px;
  }
  .feature-info { flex: 1; }
  .feature-info h3 { font-size: 14px; font-weight: 600; margin-bottom: 2px; }
  .feature-info p { font-size: 12px; color: var(--color-muted); }
  .upgrade-hint {
    font-size: 11px;
    color: var(--color-purple);
    background: rgba(123, 95, 255, 0.1);
    padding: 2px 8px;
    border-radius: 4px;
    white-space: nowrap;
  }

  /* Telemetry */
  .telemetry-intro { margin-bottom: 8px; }
  .telemetry-intro p { font-size: 13px; color: var(--color-mid); line-height: 1.6; }

  /* Error */
  .error-box {
    padding: 10px 14px;
    background: rgba(255, 79, 79, 0.1);
    border: 1px solid rgba(255, 79, 79, 0.3);
    border-radius: 8px;
    font-size: 13px;
    margin-bottom: 16px;
  }

  /* Spinner */
  .spinner {
    width: 32px; height: 32px;
    border: 3px solid var(--color-border);
    border-top-color: var(--color-teal);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin: 48px auto;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
