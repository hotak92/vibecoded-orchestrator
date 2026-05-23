<script lang="ts">
  // Module catalog browser.
  //
  // - Lists modules from `list_module_catalog` (scans bundled_manifests +
  //   ~/.vct/modules).
  // - Cross-references with `list_installed_modules(project_id)` to mark
  //   "Installed" / "Enabled toggle" / "Uninstall".
  // - Tier-required modules with `is_licensed=false` show "Upgrade to Pro".
  // - Filter pills: All / Free / Pro / Installed. Search by name+description.
  // - Install flow: clicking Install runs `install_module_for_project`. The
  //   Rust backend emits a single `module://install-complete` event; we
  //   currently render a busy spinner while waiting.

  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { goto } from '$app/navigation';
  import { modules, installedIds } from '$lib/stores/modules';
  import { selectedProject, projects } from '$lib/stores/projects';
  import { license } from '$lib/stores/license';
  import type { ModuleCatalogEntry, ModuleInstallRow } from '$lib/types/launcher';
  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import DeprecationBanner from '$lib/components/DeprecationBanner.svelte';

  type Filter = 'all' | 'free' | 'pro' | 'installed';

  let filter = $state<Filter>('all');
  let search = $state('');
  let openActivation = $state(false);
  let openSecretsPrompt = $state<ModuleCatalogEntry | null>(null);

  let { onOpenActivation, onOpenSettings }: {
    onOpenActivation: () => void;
    onOpenSettings: () => void;
  } = $props();

  const project = $derived($selectedProject);
  const projectsState = $derived($projects);
  const mState = $derived($modules);
  const installed = $derived($installedIds);
  const tier = $derived($license.cache?.orchestrator_tier ?? 'free');

  onMount(async () => {
    await modules.loadCatalog();
    if (project) {
      await modules.loadInstalled(project.id);
    }
  });

  $effect(() => {
    // Reload installed list when project changes.
    if (project) {
      modules.loadInstalled(project.id);
    }
  });

  // Bug 33: hide private-test modules from non-admin users. Admin
  // (server-classified via LS_ADMIN_VARIANT_IDS) sees ALL modules
  // including ones still in pre-release. Missing visibility field
  // defaults to public.
  const isAdminUser = $derived(tier === 'admin');

  function matchesVisibility(m: ModuleCatalogEntry): boolean {
    const v = m.visibility ?? 'public';
    if (v === 'public') return true;
    return isAdminUser;
  }

  function matchesFilter(m: ModuleCatalogEntry): boolean {
    const isInstalled =
      installed.has(m.id) || m.kind === 'bundled' || m.kind === 'installed' ||
      m.kind === 'subcomponent';
    if (filter === 'installed') return isInstalled;
    if (filter === 'free') return !m.license_required;
    if (filter === 'pro') return m.license_required;
    return true;
  }

  function matchesSearch(m: ModuleCatalogEntry): boolean {
    if (!search.trim()) return true;
    const q = search.toLowerCase();
    return (
      m.name.toLowerCase().includes(q) ||
      m.description.toLowerCase().includes(q) ||
      m.id.toLowerCase().includes(q)
    );
  }

  const visible = $derived(
    mState.catalog.filter((m) => matchesVisibility(m) && matchesFilter(m) && matchesSearch(m))
  );

  // v0.2.31 Layer 1: surface a top-of-catalog banner for any INSTALLED
  // module that has been marked deprecated. We don't show banners for
  // available-but-uninstalled modules — the badge in the card is enough
  // there; the banner is reserved for "you're actually using something
  // that's going away" signal.
  const deprecatedInstalled = $derived(
    mState.catalog.filter(
      (m) => m.deprecated && (installed.has(m.id) || m.kind === 'installed'),
    ),
  );

  function initials(name: string): string {
    return name
      .split(/[\s-_]+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((s) => s[0]?.toUpperCase() ?? '')
      .join('');
  }

  function colorFor(id: string): string {
    // Stable pseudo-random pick from the brand palette.
    let h = 0;
    for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
    const palette = ['teal', 'purple', 'pink'];
    return palette[h % palette.length];
  }

  function colorRgb(c: string): string {
    if (c === 'teal') return '0,191,166';
    if (c === 'purple') return '123,95,255';
    return '255,79,160';
  }

  async function handleInstall(m: ModuleCatalogEntry) {
    if (!project) {
      alert('Select a project from the menu bar first.');
      return;
    }
    // Tier check (best-effort UI gate; the Rust install also enforces).
    if (m.license_required && !m.is_licensed) {
      onOpenActivation();
      return;
    }
    try {
      await modules.install(project.id, m.id);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // If the error looks like a missing-secret error from the Rust side,
      // prompt the user to set it. Fallback to a generic alert.
      if (msg.toLowerCase().includes('secret') || msg.toLowerCase().includes('keychain')) {
        openSecretsPrompt = m;
      } else {
        alert(`Install failed: ${msg}`);
      }
    }
  }

  async function handleUninstall(m: ModuleCatalogEntry) {
    if (!project) return;
    const sure = confirm(`Uninstall ${m.name}? Module data is preserved unless you check Purge.`);
    if (!sure) return;
    try {
      await modules.uninstall(project.id, m.id, false);
    } catch (e) {
      alert(`Uninstall failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  async function handleUpdate(m: ModuleCatalogEntry) {
    if (!project) {
      alert('Select a project from the menu bar first.');
      return;
    }
    try {
      await modules.update(project.id, m.id);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      alert(`Update failed: ${msg}`);
    }
  }

  /**
   * Best-effort semver comparison. Splits on '.', parses the leading integer
   * of each segment (so "0.2.4-dev" → 0.2.4), and compares lexicographically.
   * Returns true iff `a` is strictly less than `b`.
   */
  function semverLess(a: string, b: string): boolean {
    const parse = (v: string): number[] =>
      v.split('.').map((s) => {
        const match = s.match(/^(\d+)/);
        return match ? parseInt(match[1], 10) : 0;
      });
    const aa = parse(a);
    const bb = parse(b);
    for (let i = 0; i < Math.max(aa.length, bb.length); i++) {
      const x = aa[i] ?? 0;
      const y = bb[i] ?? 0;
      if (x < y) return true;
      if (x > y) return false;
    }
    return false;
  }

  function hasUpdate(m: ModuleCatalogEntry, installRow: ModuleInstallRow | null): boolean {
    if (!installRow?.module_version) return false;
    return semverLess(installRow.module_version, m.version);
  }

  /** Centralized tier display label. Capitalizes "pro"/"mao"/"enterprise"/"admin". */
  function tierLabel(min: string | null | undefined): string {
    if (!min) return 'Free';
    return min.charAt(0).toUpperCase() + min.slice(1);
  }

  async function handleToggleEnabled(m: ModuleCatalogEntry, enabled: boolean) {
    if (!project) return;
    try {
      await modules.setEnabled(project.id, m.id, enabled);
    } catch (e) {
      alert(`Toggle failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  function getInstalledRow(m: ModuleCatalogEntry) {
    return mState.installed.find((r) => r.module_id === m.id) ?? null;
  }

  // v0.2.31 — module-deprecation surface (Layer 1, GUI).
  //
  // Build a hover-tooltip string for the DEPRECATED badge. Mirrors the
  // dashboard banner content but condensed to fit a `title=` attribute.
  function deprecationTooltip(m: ModuleCatalogEntry): string {
    if (!m.deprecated) return '';
    const parts: string[] = [
      m.deprecation_message || 'This module has been marked deprecated.',
    ];
    if (m.deprecation_eol_date) parts.push(`EOL: ${m.deprecation_eol_date}.`);
    if (m.deprecation_migration_url) {
      parts.push(`Migration guide: ${m.deprecation_migration_url}`);
    }
    return parts.join(' ');
  }

  // One-shot desktop notification. Fires the FIRST time we see a deprecated
  // catalog entry for a given (project_id, module_id) pair; subsequent
  // sessions skip via the launcher.db `module_deprecation_seen` row.
  //
  // v0.2.31 graceful degradation: `@tauri-apps/plugin-notification` is NOT
  // in the launcher's deps yet (verified by reading package.json on
  // 2026-05-23). Instead we surface a console.warn that the launcher's dev
  // tools / GUI logs render; the row in `module_deprecation_seen` still
  // gets inserted so re-sessions don't re-spam the console.
  // TODO(v0.2.32): swap console.warn for the Tauri notification plugin
  // once the dep is added (`pnpm add @tauri-apps/plugin-notification` +
  // `cargo add tauri-plugin-notification`).
  async function maybeFireDeprecationToast(m: ModuleCatalogEntry): Promise<void> {
    if (!m.deprecated || !project) return;
    try {
      const seen = await invoke<boolean>('has_module_deprecation_been_seen', {
        projectId: project.id,
        moduleId: m.id,
      });
      if (seen) return;
      // Mark BEFORE rendering so a concurrent mount in another window
      // doesn't double-fire. INSERT OR IGNORE on the SQLite side
      // serialises this.
      const inserted = await invoke<boolean>('mark_module_deprecation_seen', {
        projectId: project.id,
        moduleId: m.id,
      });
      if (!inserted) return;
      // Console-log degradation path — see TODO above.
      console.warn(
        `[vct] Module "${m.name}" is deprecated. ${deprecationTooltip(m)}`,
      );
    } catch (e) {
      // Soft-fail — never block the catalog render on a notification path.
      console.debug('[vct] deprecation-toast check failed:', e);
    }
  }

  $effect(() => {
    // Side-effect: when the catalog or project changes, look for
    // newly-deprecated modules and fire the one-shot notification per
    // (project, module) pair. Bounded by `module_deprecation_seen`.
    if (!project) return;
    for (const m of mState.catalog) {
      if (m.deprecated) {
        // Fire-and-forget; per-call errors land in console.debug above.
        void maybeFireDeprecationToast(m);
      }
    }
  });
</script>

<div class="catalog">
  <div class="catalog-header">
    <div>
      <h1 class="catalog-title">Modules</h1>
      <p class="catalog-subtitle">
        {#if project}
          Installing into <span class="proj-name">{project.name}</span> ({project.host})
        {:else if projectsState.projects.length === 0}
          Create a project first to install modules
        {:else}
          Select a project from the menu bar
        {/if}
      </p>
    </div>
    <input
      type="text"
      class="search-input"
      placeholder="Search modules…"
      bind:value={search}
    />
  </div>

  <div class="filters">
    {#each ['all', 'free', 'pro', 'installed'] as f}
      <button
        class="filter-pill"
        class:active={filter === f}
        onclick={() => (filter = f as Filter)}
      >
        {f.charAt(0).toUpperCase() + f.slice(1)}
        {#if f === 'installed'}
          <span class="filter-count">{installed.size}</span>
        {/if}
      </button>
    {/each}
  </div>

  {#each deprecatedInstalled as m (m.id)}
    <DeprecationBanner module={m} />
  {/each}

  {#if mState.loading && visible.length === 0}
    <div class="catalog-empty">Loading catalog…</div>
  {:else if visible.length === 0}
    <div class="catalog-empty">
      {#if mState.catalog.length === 0}
        <p>No modules in catalog yet.</p>
        <p class="hint">
          Bundled manifests live in <span class="mono">~/.vct/bundled_manifests</span>;
          installed modules live in <span class="mono">~/.vct/modules</span>.
        </p>
      {:else}
        <p>No modules match the current filter.</p>
      {/if}
    </div>
  {:else}
    <div class="card-grid">
      {#each visible as m (m.id)}
        {@const isInstalled = installed.has(m.id)}
        {@const installRow = getInstalledRow(m)}
        {@const color = colorFor(m.id)}
        {@const installing = mState.installingId === m.id}
        <div class="module-card glass-card" style:--accent="rgb({colorRgb(color)})">
          <div class="card-head">
            <div class="card-icon" style:background="rgba({colorRgb(color)}, 0.12)" style:border-color="rgba({colorRgb(color)}, 0.25)">
              <span style:color="rgb({colorRgb(color)})">{initials(m.name)}</span>
            </div>
            <div class="card-title-block">
              <div class="card-title-row">
                <h3 class="card-name">{m.name}</h3>
                {#if m.license_required}
                  <span class="tier-badge">
                    {tierLabel(m.min_orchestrator_tier === 'free' ? 'pro' : m.min_orchestrator_tier)}
                  </span>
                {:else}
                  <span class="tier-badge tier-free">Free</span>
                {/if}
                {#if m.deprecated}
                  <!-- v0.2.31 Layer 1: deprecation badge. Hover tooltip
                       carries the full message + EOL date + migration URL. -->
                  <span class="tier-badge tier-deprecated" title={deprecationTooltip(m)}>
                    Deprecated
                  </span>
                {/if}
              </div>
              <p class="card-meta">
                <span class="mono">v{m.version}</span> · {m.category}
              </p>
            </div>
          </div>
          <p class="card-desc">{m.description || 'No description provided.'}</p>
          {#if m.compatibility_hosts.length}
            <p class="card-hosts">
              Hosts: {m.compatibility_hosts.join(', ')}
            </p>
          {/if}
          {#if installRow?.last_error}
            <div class="card-error">
              {installRow.last_error}
            </div>
          {/if}
          <div class="card-footer">
            {#if m.kind === 'bundled'}
              <!-- Bug 16: launcher itself — no Install/Uninstall -->
              <span class="status-badge status-badge-bundled">Bundled</span>
            {:else if m.kind === 'subcomponent'}
              <!-- Bug 16: KG / Code Graph — shipped with orchestrator -->
              <span class="status-badge status-badge-included"
                >Included with {m.parent_id}</span
              >
              {#if m.cta_route}
                <button
                  class="btn-3d btn-3d-ghost btn-3d-sm"
                  onclick={() => goto(m.cta_route)}
                >
                  → Open dashboard
                </button>
              {/if}
            {:else if m.kind === 'installed' || isInstalled}
              <!-- Installed: toggle + (optional Update) + Uninstall -->
              <label class="enabled-toggle">
                <input
                  type="checkbox"
                  checked={installRow?.enabled ?? true}
                  onchange={(e) => handleToggleEnabled(m, (e.target as HTMLInputElement).checked)}
                  aria-label="Enable or disable {m.name}"
                />
                <span>Enabled</span>
              </label>
              {#if installRow}
                {#if hasUpdate(m, installRow)}
                  <button
                    class="btn-3d btn-3d-primary btn-3d-sm"
                    onclick={() => handleUpdate(m)}
                    disabled={installing}
                    aria-label="Update {m.name} from version {installRow.module_version} to version {m.version}"
                  >
                    {#if installing}
                      <span class="spinner-sm"></span>
                      Updating…
                    {:else}
                      Update v{installRow.module_version} → v{m.version}
                    {/if}
                  </button>
                {/if}
                <button
                  class="btn-3d btn-3d-ghost btn-3d-sm"
                  onclick={() => handleUninstall(m)}
                  aria-label="Uninstall {m.name}"
                >
                  Uninstall
                </button>
              {:else}
                <span class="status-badge status-badge-bundled">Installed</span>
              {/if}
            {:else if m.license_required && !m.is_licensed}
              <!-- Not installed + tier-required + unlicensed: activate, not "upgrade". -->
              <button
                class="btn-3d btn-3d-secondary btn-3d-sm"
                onclick={onOpenActivation}
                aria-label="Activate {tierLabel(m.min_orchestrator_tier)} license for {m.name}"
              >
                Activate license
              </button>
            {:else}
              <button
                class="btn-3d btn-3d-primary btn-3d-sm"
                onclick={() => handleInstall(m)}
                disabled={installing || !project}
                aria-label="Install {m.name}"
              >
                {#if installing}
                  <span class="spinner-sm"></span>
                  Installing…
                {:else}
                  Install
                {/if}
              </button>
            {/if}
          </div>
        </div>
      {/each}
    </div>
  {/if}

  {#if mState.error}
    <div class="msg msg-error">{mState.error}</div>
  {/if}
</div>

<!-- Missing-secret prompt — Bug 26: native <dialog> top-layer via DialogRoot. -->
{#if openSecretsPrompt}
  {@const promptModule = openSecretsPrompt}
  <DialogRoot
    open={true}
    width="440px"
    onClose={() => (openSecretsPrompt = null)}
  >
    {#snippet header()}
      <h2 class="secret-prompt-title">Set required secret</h2>
    {/snippet}
    {#snippet body()}
      <p class="modal-desc">
        <strong>{promptModule.name}</strong> needs a secret to be set
        before it can run. Open Settings → Secrets to add the required key.
      </p>
      <div class="modal-actions">
        <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={() => (openSecretsPrompt = null)}>
          Cancel
        </button>
        <button
          class="btn-3d btn-3d-primary btn-3d-sm"
          onclick={() => {
            openSecretsPrompt = null;
            onOpenSettings();
          }}
        >
          Open Secrets
        </button>
      </div>
    {/snippet}
  </DialogRoot>
{/if}

<style>
  .catalog {
    position: relative;
    z-index: 1;
  }

  .catalog-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 20px;
    margin-bottom: 18px;
  }

  .catalog-title {
    font-size: 22px;
    font-weight: 800;
    color: var(--color-text);
    letter-spacing: -0.5px;
  }

  .catalog-subtitle {
    font-size: 13px;
    color: var(--color-mid);
    margin-top: 2px;
  }

  .proj-name {
    color: var(--color-teal);
    font-weight: 600;
  }

  .search-input {
    width: 240px;
    padding: 8px 14px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    color: var(--color-text);
    font-size: 12px;
    outline: none;
  }
  .search-input:focus {
    border-color: rgba(0, 191, 166, 0.5);
  }

  .filters {
    display: flex;
    gap: 6px;
    margin-bottom: 16px;
  }

  .filter-pill {
    padding: 5px 12px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 999px;
    color: var(--color-mid);
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s ease;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .filter-pill:hover {
    color: var(--color-text);
  }

  .filter-pill.active {
    background: rgba(0, 191, 166, 0.12);
    border-color: rgba(0, 191, 166, 0.3);
    color: var(--color-teal);
  }

  .filter-count {
    background: rgba(0, 191, 166, 0.2);
    border-radius: 999px;
    padding: 0 6px;
    font-size: 10px;
  }

  .catalog-empty {
    padding: 60px 20px;
    text-align: center;
    color: var(--color-mid);
    font-size: 13px;
  }

  .catalog-empty .hint {
    font-size: 11px;
    color: var(--color-muted);
    margin-top: 8px;
  }

  .card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 16px;
  }

  .module-card {
    padding: 18px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .card-head {
    display: flex;
    align-items: flex-start;
    gap: 12px;
  }

  .card-icon {
    width: 44px;
    height: 44px;
    border-radius: 12px;
    border: 1px solid;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  .card-icon span {
    font-size: 14px;
    font-weight: 800;
    letter-spacing: 0.5px;
  }

  .card-title-block {
    flex: 1;
    min-width: 0;
  }

  .card-title-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .card-name {
    font-size: 14px;
    font-weight: 800;
    color: var(--color-text);
  }

  .tier-badge {
    font-size: 9px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 999px;
    background: rgba(123, 95, 255, 0.15);
    color: var(--color-purple);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .tier-badge.tier-free {
    background: rgba(0, 191, 166, 0.12);
    color: var(--color-teal);
  }

  /* v0.2.31 Layer 1: deprecated badge. Amber to read as "warning, but
     not error" — the module keeps working until EOL. */
  .tier-badge.tier-deprecated {
    background: rgba(255, 159, 28, 0.14);
    color: rgb(255, 159, 28);
    cursor: help;
  }

  /* Bug 16: kind-aware status badges. */
  .status-badge {
    font-size: 10px;
    font-weight: 700;
    padding: 4px 10px;
    border-radius: 999px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .status-badge-bundled {
    background: rgba(123, 95, 255, 0.12);
    color: var(--color-purple, #b29bff);
    border: 1px solid rgba(123, 95, 255, 0.3);
  }
  .status-badge-included {
    background: rgba(0, 191, 166, 0.08);
    color: var(--color-teal);
    border: 1px solid rgba(0, 191, 166, 0.25);
  }

  .card-meta {
    font-size: 11px;
    color: var(--color-muted);
    margin-top: 2px;
  }

  .card-desc {
    font-size: 12px;
    color: var(--color-mid);
    line-height: 1.5;
    flex: 1;
  }

  .card-hosts {
    font-size: 10px;
    color: var(--color-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .card-error {
    padding: 6px 10px;
    background: rgba(255, 79, 160, 0.08);
    border: 1px solid rgba(255, 79, 160, 0.2);
    border-radius: 8px;
    color: var(--color-pink);
    font-size: 10px;
  }

  .card-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding-top: 6px;
  }

  .enabled-toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--color-mid);
    cursor: pointer;
  }

  .enabled-toggle input {
    accent-color: var(--color-teal);
  }

  .mono {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }

  .msg {
    margin-top: 16px;
    padding: 10px 14px;
    border-radius: 10px;
    font-size: 12px;
  }

  .msg-error {
    background: rgba(255, 79, 160, 0.1);
    border: 1px solid rgba(255, 79, 160, 0.25);
    color: var(--color-pink);
  }

  .spinner-sm {
    display: inline-block;
    width: 12px;
    height: 12px;
    border: 2px solid rgba(0, 0, 0, 0.2);
    border-top-color: var(--color-bg);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  /* Bug 26: backdrop / sizing / header / body now handled by DialogRoot. */
  .secret-prompt-title {
    font-size: 15px;
    font-weight: 700;
    color: var(--color-text);
    margin: 0;
  }
  .modal-desc {
    font-size: 13px;
    color: var(--color-mid);
    line-height: 1.5;
    margin-bottom: 14px;
  }

  .modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
</style>
