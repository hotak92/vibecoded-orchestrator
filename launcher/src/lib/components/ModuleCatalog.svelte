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
  import { toast } from '$lib/stores/toast';
  import type { ModuleCatalogEntry } from '$lib/types/launcher';
  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import DeprecationBanner from '$lib/components/DeprecationBanner.svelte';
  // v0.2.33 (Agent E, 2026-05-25):
  //   - L9 banner+modal for aggregated manifest-parse errors.
  //   - L0 freshness indicator (stale / unavailable).
  //   - Dev-affordance toast (review §10.c).
  import ManifestParseErrorBanner from '$lib/components/ManifestParseErrorBanner.svelte';
  import ManifestParseErrorModal from '$lib/components/ManifestParseErrorModal.svelte';
  import L0StatusIndicator from '$lib/components/L0StatusIndicator.svelte';
  import DevAffordanceToast from '$lib/components/DevAffordanceToast.svelte';
  // v0.2.35 (Agent J): per-status display contract for the tile.
  // Replaces the previous inline gating (which conflated "row exists"
  // with "module healthy" and left status='error'/'broken' rows stuck
  // with only an Uninstall CTA — no retry path short of a full
  // Uninstall-then-Install loop).
  import {
    resolveTileDisplay,
    truncateLastError,
    statusBadgeLabel,
    detectModuleErrorAfterAction,
  } from '$lib/module-status-display';
  // v0.2.35 Agent M (2026-05-26): preflight modal shown when the
  // install-pipeline preflight (`check_container_runtime_available`)
  // returns `available: false`. Runs on every Install click — see the
  // handleInstall flow below for the gating logic.
  import InstallPreflightRuntimeModal from '$lib/components/InstallPreflightRuntimeModal.svelte';

  type Filter = 'all' | 'free' | 'pro' | 'installed';

  let filter = $state<Filter>('all');
  let search = $state('');
  let openActivation = $state(false);
  let openSecretsPrompt = $state<ModuleCatalogEntry | null>(null);

  // v0.2.35 Agent M (2026-05-26): preflight modal state. Opens when a
  // user clicks Install and the container-runtime preflight returns
  // `available: false`. The `pendingInstallModule` is the module that
  // would be installed once the runtime becomes available; the modal's
  // "Detect again with success" path resumes the install against this
  // module without the user having to re-click the catalog button.
  interface PreflightRuntimeAvailability {
    available: boolean;
    detected: string | null;
    platform: string;
    install_url: string | null;
  }
  let preflightModalOpen = $state(false);
  let preflightAvailability = $state<PreflightRuntimeAvailability | null>(null);
  let pendingInstallModule = $state<ModuleCatalogEntry | null>(null);
  // v0.2.33 (Agent E, L9): modal open state for the parse-error
  // detail view. Closed by default; opened from the banner click.
  let parseErrorModalOpen = $state(false);
  // v0.2.33 (Agent E): retry-in-flight flag for the L0 unavailable
  // banner. Local state — the store's `loading` flag is too coarse
  // (it gates initial render too).
  let l0Retrying = $state(false);
  // v0.2.33 (Agent E, L9): hash of parse-error payload last logged,
  // so a Modules-tab remount that returns the SAME errors doesn't
  // double-append the JSONL line on every re-render.
  let lastLoggedParseErrorsHash = $state('');

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

  // v0.2.33 (Agent E, L9): persist every fresh batch of parse errors
  // to `<install>/state/logs/launcher_errors.jsonl` (or
  // `~/.vct/launcher_errors.jsonl` when no install root is resolvable).
  // The Rust command swallows write errors — this side effect is
  // purely "best-effort logging for postmortem". We guard against
  // double-logging the same payload by hashing module_id|source|error
  // tuples; a remount that re-reads the same store state won't re-write.
  $effect(() => {
    const errs = mState.parseErrors;
    if (errs.length === 0) {
      // Reset so the next non-empty batch logs.
      lastLoggedParseErrorsHash = '';
      return;
    }
    const hash = errs
      .map((e) => `${e.module_id}|${e.source}|${e.error}`)
      .join('\n');
    if (hash === lastLoggedParseErrorsHash) return;
    lastLoggedParseErrorsHash = hash;
    void invoke<string>('log_manifest_parse_errors', { errors: errs }).catch(
      (e) => {
        // Soft-fail: the Rust side already swallows write errors,
        // so the only way we land here is a transport / argument
        // error. Surface to console for the dev who's looking.
        console.warn('[ModuleCatalog] log_manifest_parse_errors failed:', e);
      },
    );
  });

  async function reloadCatalog() {
    l0Retrying = true;
    try {
      await modules.loadCatalog();
    } finally {
      l0Retrying = false;
    }
  }

  // v0.2.34 (Agent C): always-visible ↻ refresh button — additive
  // surface to L0StatusIndicator's stale/unavailable banners. The
  // existing reloadCatalog goes through `list_module_catalog` which
  // honours the 15-min DB-backed TTL; the manual refresh path here
  // calls `refresh_module_catalog` (via modules.forceRefresh) which
  // bypasses the cache. Why separate: clicking ↻ in the happy-path
  // must do something visible — going through the cached path would
  // be a silent no-op on second click within 15 minutes.
  let manualRefreshing = $state(false);

  async function handleManualRefresh() {
    if (manualRefreshing) return;
    manualRefreshing = true;
    try {
      const ok = await modules.forceRefresh();
      if (!ok) {
        // forceRefresh stores the error on the store; surface it
        // through the toast so the user sees something concrete
        // (the store-level `error` text is already rendered at the
        // bottom of the catalog as a fallback, but that's far from
        // the click target).
        const errText = mState.error || 'Refresh failed.';
        toast.error(`Catalog refresh failed: ${errText}`);
      }
    } finally {
      manualRefreshing = false;
    }
  }

  // v0.2.34 (Agent C): "Fetched 3m ago" display next to the refresh
  // button. Derives from `l0Status` (which carries either `fetched_at`
  // or `cached_fetched_at`). The `now` tick state forces re-evaluation
  // of the relative-time string once per minute so the user sees the
  // counter advance without having to re-trigger a fetch.
  let now = $state(Date.now());
  $effect(() => {
    const handle = setInterval(() => {
      now = Date.now();
    }, 60 * 1000);
    return () => clearInterval(handle);
  });

  function fetchedAtIso(status: typeof mState.l0Status): string | null {
    if (!status) return null;
    if (status.kind === 'ok') return status.fetched_at;
    if (status.kind === 'stale') return status.cached_fetched_at;
    return null; // 'unavailable' has no timestamp surface
  }

  function relativeFetched(iso: string | null, _tick: number): string {
    if (!iso) return '';
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return '';
    const deltaMs = Math.max(0, _tick - t);
    const sec = Math.floor(deltaMs / 1000);
    if (sec < 30) return 'just now';
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr}h ago`;
    const day = Math.floor(hr / 24);
    return `${day}d ago`;
  }

  const lastFetchedLabel = $derived(
    relativeFetched(fetchedAtIso(mState.l0Status), now),
  );

  async function dismissDevHint() {
    await modules.dismissDevAffordance();
  }

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

    // v0.2.35 Agent M (2026-05-26): container-runtime preflight. Runs on
    // EVERY install click (not gated behind a "first-time" flag) so that
    // a user who uninstalls their runtime mid-session gets an actionable
    // modal instead of the cryptic "no container runtime found" error
    // from `installer_engine::detect_container_runtime` deep inside
    // `run_install`. The preflight command never returns Err — failures
    // surface as `available: false` so the modal renders rather than
    // a generic error toast.
    try {
      const availability = await invoke<PreflightRuntimeAvailability>(
        'check_container_runtime_available',
      );
      if (!availability.available) {
        // Open modal + remember the module so "Detect again → available"
        // can resume the install without a second click. We deliberately
        // do NOT call `modules.install` here — the install only starts
        // once the user dismisses the modal via the Proceed path.
        preflightAvailability = availability;
        pendingInstallModule = m;
        preflightModalOpen = true;
        return;
      }
    } catch (e) {
      // The Rust command's contract is "never Err", but the IPC
      // transport itself could fail (Tauri unavailable in dev browser
      // mode). Fall through to the install attempt — `install_module_
      // for_project` will surface its own error if the runtime really
      // is missing.
      console.warn('[ModuleCatalog] preflight runtime check failed, proceeding without gate:', e);
    }

    await runInstall(m);
  }

  // Extracted from `handleInstall` so the preflight modal's "Detect
  // again → available" success path can call back into the install flow
  // without re-triggering the runtime preflight (we just confirmed it).
  async function runInstall(m: ModuleCatalogEntry) {
    if (!project) return;
    // Shared dedup/auto-resolve key with the bell inbox: a later success
    // for the same module action clears the stored error.
    const toastKey = `module:${m.id}:install`;
    try {
      await modules.install(project.id, m.id);
      // The resolved row can be misleadingly clean (status='installed',
      // last_error=null) even when the container START failed — the real
      // failure only surfaces after the catalog recomputes `kind`. Reload
      // both surfaces, then inspect (see detectModuleErrorAfterAction).
      await modules.loadCatalog();
      await modules.loadInstalled(project.id);
      const errMsg = detectModuleErrorAfterAction(m.id, $modules.catalog, $modules.installed);
      if (errMsg) {
        toast.error(`${m.name}: ${errMsg}`, { key: toastKey });
      } else {
        toast.success(`${m.name} installed`, { key: toastKey });
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // If the error looks like a missing-secret error from the Rust side,
      // prompt the user to set it. Fallback to a generic alert.
      if (msg.toLowerCase().includes('secret') || msg.toLowerCase().includes('keychain')) {
        openSecretsPrompt = m;
      } else {
        toast.error(`Install failed: ${msg}`, { key: toastKey });
      }
    }
  }

  // v0.2.35 Agent M (2026-05-26): preflight modal callbacks.
  //
  // onProceed fires after a successful "Detect again" inside the modal
  // (the modal has already updated its own `available` state to true).
  // Resume the install against the originally-clicked module.
  //
  // onCancel fires for the Cancel button, the Escape key, or a backdrop
  // click. Discard the pending module — the user has explicitly aborted.
  function handlePreflightProceed() {
    const m = pendingInstallModule;
    pendingInstallModule = null;
    preflightAvailability = null;
    if (m) {
      void runInstall(m);
    }
  }

  function handlePreflightCancel() {
    pendingInstallModule = null;
    preflightAvailability = null;
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
    const toastKey = `module:${m.id}:update`;
    try {
      await modules.update(project.id, m.id);
      // Same caveat as runInstall: re-read catalog + installed and inspect
      // the recomputed kind — the resolved row can be misleadingly clean.
      await modules.loadCatalog();
      await modules.loadInstalled(project.id);
      const errMsg = detectModuleErrorAfterAction(m.id, $modules.catalog, $modules.installed);
      if (errMsg) {
        toast.error(`${m.name}: ${errMsg}`, { key: toastKey });
      } else {
        toast.success(`${m.name} updated`, { key: toastKey });
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`Update failed: ${msg}`, { key: toastKey });
    }
  }

  // v0.2.35 (Agent J): `semverLess` + `hasUpdate` extracted into
  // `$lib/module-status-display` (same comparison logic, re-exported
  // as part of `resolveTileDisplay`'s `can_update` field). The
  // refactor lets unit tests cover the full gating matrix without a
  // Svelte runtime.

  // v0.2.35 (Agent J): retry-install handler. Calls the SAME
  // `install_module_for_project` Tauri command as the first-time
  // install path — the v0.2.34 UPSERT contract (Agent A) means the
  // existing error/broken row is overwritten rather than triggering
  // a UNIQUE-constraint crash. Keeps `handleInstall`'s missing-secret
  // detection so the retry path can also surface the Open-Secrets prompt
  // if the previous failure was a secret omission and the user hasn't
  // fixed it yet.
  async function handleRetryInstall(m: ModuleCatalogEntry) {
    await handleInstall(m);
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

  // NEW-3 (2026-05-28): "Start" affordance for service/container modules
  // whose container_name is NULL after install. Invokes the generic
  // `start_module_container` Tauri command which calls
  // `start_container_after_install` (already used by the Phase-1E
  // auto-start path — just wasn't reachable for `runtime.type="service"`
  // before the gate widening).
  let startingModuleId = $state<string | null>(null);

  async function handleStartContainer(m: ModuleCatalogEntry) {
    if (!project) {
      alert('Select a project from the menu bar first.');
      return;
    }
    startingModuleId = m.id;
    try {
      await invoke('start_module_container', {
        projectId: project.id,
        moduleId: m.id,
      });
      await modules.loadInstalled(project.id);
    } catch (e) {
      alert(`Start failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      startingModuleId = null;
    }
  }

  function isLongRunningRuntime(runtimeType: string | undefined): boolean {
    return runtimeType === 'container' || runtimeType === 'service';
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

  <!-- v0.2.35 (a11y, Agent O): the filter pills are toggle buttons whose
       "active" state was previously color-only. Adding aria-pressed makes
       the toggle state programmatically queryable; screen readers
       announce "pressed" / "not pressed" along with the label so users
       know which filter is active without relying on visual styling. -->
  <div class="filters" role="group" aria-label="Catalog filters">
    {#each ['all', 'free', 'pro', 'installed'] as f}
      <button
        type="button"
        class="filter-pill"
        class:active={filter === f}
        aria-pressed={filter === f}
        onclick={() => (filter = f as Filter)}
      >
        {f.charAt(0).toUpperCase() + f.slice(1)}
        {#if f === 'installed'}
          <span class="filter-count">{installed.size}</span>
        {/if}
      </button>
    {/each}
    <!-- v0.2.34 (Agent C): always-visible refresh button, mounted
         UNCONDITIONALLY (NOT inside L0StatusIndicator's status-gated
         render). Bypasses the 15-min DB-backed TTL via
         `refresh_module_catalog` so the user has a reliable manual
         force-refresh path — particularly important for the
         "publisher uploaded their L0 entry mid-session" case where
         the 15-min stale-empty cache would otherwise lock the user
         out of fresh data. -->
    <div class="refresh-cluster">
      {#if lastFetchedLabel}
        <span class="last-fetched" aria-label="Catalog last fetched">
          Fetched {lastFetchedLabel}
        </span>
      {/if}
      <button
        type="button"
        class="refresh-btn"
        onclick={handleManualRefresh}
        disabled={manualRefreshing}
        aria-label="Refresh module catalog"
        title="Refresh catalog (bypasses 15-min cache)"
      >
        {#if manualRefreshing}
          <span class="spinner-sm" aria-hidden="true"></span>
          <span class="refresh-label">Refreshing…</span>
        {:else}
          <span class="refresh-icon" aria-hidden="true">↻</span>
          <span class="refresh-label">Refresh</span>
        {/if}
      </button>
    </div>
  </div>

  <!-- v0.2.33 (Agent E): L0 freshness indicator + L9 parse-error
       banner. Banner is placed above DeprecationBanner so the user
       reads "catalog couldn't be parsed → deprecated modules →
       catalog body" top-to-bottom. -->
  <L0StatusIndicator
    status={mState.l0Status}
    onRetry={reloadCatalog}
    retrying={l0Retrying}
  />
  <ManifestParseErrorBanner
    errors={mState.parseErrors}
    onOpen={() => (parseErrorModalOpen = true)}
  />

  {#each deprecatedInstalled as m (m.id)}
    <DeprecationBanner module={m} />
  {/each}

  {#if mState.loading && visible.length === 0}
    <div class="catalog-empty">Loading catalog…</div>
  {:else if !project}
    <!-- v0.2.32 V2 (2026-05-23): when no project is selected the subtitle
         above already says "Create a project first" or "Select a project
         from the menu bar". Showing "No modules in catalog yet" in
         parallel is misleading (the catalog has plenty — it's the install
         target that's missing). Suppress the catalog body entirely until
         the user picks a project. -->
    <div class="catalog-empty">
      <p>Modules install into a specific project.</p>
      <p class="hint">
        {#if projectsState.projects.length === 0}
          Create a project first using the menu bar above.
        {:else}
          Select a project from the menu bar above to browse and install modules.
        {/if}
      </p>
    </div>
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
        {@const installRow = getInstalledRow(m)}
        {@const color = colorFor(m.id)}
        {@const installing = mState.installingId === m.id}
        {@const display = resolveTileDisplay(m, installRow, installing)}
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
          {#if display.kind === 'errored'}
            <!-- v0.2.35 (Agent J): error/broken tile — explicit
                 "Reason: …" label with click-to-expand for long
                 errors. The full `last_error` payload is still
                 available via the `<details>` element so triage
                 (especially in support / bug-report flows) doesn't
                 require copy-pasting from launcher.db. -->
            {@const truncated = truncateLastError(display.last_error)}
            <div class="card-error card-error-labelled" data-testid="card-error-labelled">
              <div class="card-error-head">
                <span class="card-error-label">
                  {statusBadgeLabel(display.status)}:
                </span>
                <span class="card-error-msg" data-testid="card-error-msg">
                  {truncated.display || 'no details recorded'}
                </span>
              </div>
              {#if truncated.truncated && display.last_error}
                <details class="card-error-details">
                  <summary>Show full error</summary>
                  <pre class="card-error-full">{display.last_error}</pre>
                </details>
              {/if}
            </div>
          {:else if installRow?.last_error}
            <!-- Historic last_error on a recovered row — kept
                 unlabelled to match the pre-v0.2.35 behavior so we
                 don't regress the happy path. The reconciler normally
                 clears this on a successful subsequent install. -->
            <div class="card-error">
              {installRow.last_error}
            </div>
          {/if}
          <div class="card-footer">
            {#if display.kind === 'bundled'}
              <!-- Bug 16: launcher itself — no Install/Uninstall -->
              <span class="status-badge status-badge-bundled">Bundled</span>
            {:else if display.kind === 'included'}
              <!-- Bug 16: KG / Code Graph — shipped with orchestrator -->
              <span class="status-badge status-badge-included"
                >Included with {display.parent_id}</span
              >
              {#if display.cta_route}
                <button
                  class="btn-3d btn-3d-ghost btn-3d-sm"
                  onclick={() => goto(display.cta_route)}
                >
                  → Open dashboard
                </button>
              {/if}
            {:else if display.kind === 'installed'}
              <!-- Installed: toggle + (optional Update) + (optional Start) + Uninstall -->
              <label class="enabled-toggle">
                <input
                  type="checkbox"
                  checked={display.install_row.enabled}
                  onchange={(e) => handleToggleEnabled(m, (e.target as HTMLInputElement).checked)}
                  aria-label="Enable or disable {m.name}"
                />
                <span>Enabled</span>
              </label>
              {#if display.can_update}
                <button
                  class="btn-3d btn-3d-primary btn-3d-sm"
                  onclick={() => handleUpdate(m)}
                  disabled={installing}
                  aria-label="Update {m.name} from version {display.install_row.module_version} to version {m.version}"
                >
                  Update v{display.install_row.module_version} → v{m.version}
                </button>
              {/if}
              <!-- NEW-3 (2026-05-28): "Start" button — defence-in-depth for
                   service/container modules whose container was never created
                   (e.g. because the old gate excluded runtime.type="service").
                   Condition: no container_name recorded AND runtime is a
                   long-running type. -->
              {#if !display.install_row.container_name && isLongRunningRuntime(m.runtime_type)}
                <button
                  class="btn-3d btn-3d-primary btn-3d-sm"
                  onclick={() => handleStartContainer(m)}
                  disabled={startingModuleId === m.id || !project}
                  aria-label="Start {m.name} container"
                  data-testid="btn-start-container"
                >
                  {startingModuleId === m.id ? 'Starting…' : 'Start'}
                </button>
              {/if}
              <button
                class="btn-3d btn-3d-ghost btn-3d-sm"
                onclick={() => handleUninstall(m)}
                aria-label="Uninstall {m.name}"
                data-testid="btn-uninstall"
              >
                Uninstall
              </button>
            {:else if display.kind === 'errored'}
              <!-- v0.2.35 (Agent J): retry + uninstall pair for
                   status='error' (install pipeline failed) and
                   status='broken' (reconciler found ~/.vct/modules/<id>
                   missing on startup). Same Tauri command for both —
                   `install_module_for_project` (UPSERT under v0.2.34
                   Agent A's contract) overwrites the existing row. -->
              <button
                class="btn-3d btn-3d-primary btn-3d-sm"
                onclick={() => handleRetryInstall(m)}
                disabled={installing || !project}
                aria-label="Retry installing {m.name}"
                data-testid="btn-retry"
              >
                Retry install
              </button>
              <button
                class="btn-3d btn-3d-ghost btn-3d-sm"
                onclick={() => handleUninstall(m)}
                aria-label="Uninstall {m.name}"
                data-testid="btn-uninstall"
              >
                Uninstall
              </button>
            {:else if display.kind === 'installing'}
              <!-- In-flight install/retry/update — spinner-only, no buttons. -->
              <span class="status-badge status-badge-bundled">
                <span class="spinner-sm" aria-hidden="true"></span>
                Installing…
              </span>
            {:else if display.kind === 'available' && display.needs_license}
              <!-- Not installed + tier-required + unlicensed: activate, not "upgrade". -->
              <button
                class="btn-3d btn-3d-secondary btn-3d-sm"
                onclick={onOpenActivation}
                aria-label="Activate {tierLabel(m.min_orchestrator_tier)} license for {m.name}"
              >
                Activate license
              </button>
            {:else}
              <!-- display.kind === 'available' && !needs_license -->
              <button
                class="btn-3d btn-3d-primary btn-3d-sm"
                onclick={() => handleInstall(m)}
                disabled={installing || !project}
                aria-label="Install {m.name}"
                data-testid="btn-install"
              >
                Install
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

<!-- v0.2.33 (Agent E): L9 parse-error detail modal, paired with the
     banner above. Reload calls back into the store. -->
<ManifestParseErrorModal
  open={parseErrorModalOpen}
  errors={mState.parseErrors}
  onClose={() => (parseErrorModalOpen = false)}
  onReload={async () => {
    await reloadCatalog();
    // If the reload cleared the parse errors, the user has nothing
    // left to inspect — close the modal so they see the clean catalog.
    if (mState.parseErrors.length === 0) {
      parseErrorModalOpen = false;
    }
  }}
/>

<!-- v0.2.33 (Agent E, review §10.c): dev-affordance toast. Mounted
     here so it surfaces over the entire Modules tab, not just the
     catalog grid. Render is gated on `devAffordanceHint != null`,
     which the Rust side already cross-references with the
     `paid-modules/` + env-var + dismissed checks. -->
<DevAffordanceToast
  hint={mState.devAffordanceHint}
  onDismiss={dismissDevHint}
/>

<!-- v0.2.35 Agent M (2026-05-26): install-pipeline preflight modal.
     Renders when `handleInstall` calls `check_container_runtime_available`
     and gets `available: false`. The modal owns its own "Detect again"
     retry loop; only the "available now" branch calls back into the
     install flow via `handlePreflightProceed`. -->
<InstallPreflightRuntimeModal
  bind:open={preflightModalOpen}
  availability={preflightAvailability}
  onProceed={handlePreflightProceed}
  onCancel={handlePreflightCancel}
/>

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

  /* v0.2.32 V1 (2026-05-23): align the search input vertically with the
     H1+subtitle block instead of pinning to the top edge. Previously
     `flex-start` placed the input at the H1's top, leaving a visual gap
     below it that read as "input disconnected from the title". */
  .catalog-header {
    display: flex;
    align-items: center;
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
    align-items: center;
  }

  /* v0.2.34 (Agent C): refresh cluster on the right edge of the
     filter row. Uses `margin-left: auto` so the existing filter
     pills stay left-anchored regardless of how many fit. */
  .refresh-cluster {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .last-fetched {
    font-size: 11px;
    color: var(--color-muted);
    white-space: nowrap;
  }

  .refresh-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 12px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 999px;
    color: var(--color-mid);
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .refresh-btn:hover:not(:disabled) {
    color: var(--color-text);
    border-color: rgba(0, 191, 166, 0.3);
    background: rgba(0, 191, 166, 0.08);
  }
  .refresh-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .refresh-icon {
    font-size: 13px;
    line-height: 1;
  }
  .refresh-label {
    font-size: 11px;
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

  /* v0.2.35 (Agent J): labelled variant for error/broken tiles. The
     label tag ("Install failed:" / "Files missing:") goes in bold; the
     message body stays inline so a short error reads as one line. */
  .card-error-labelled {
    font-size: 11px;
    line-height: 1.4;
  }
  .card-error-head {
    display: flex;
    gap: 6px;
    align-items: baseline;
    flex-wrap: wrap;
  }
  .card-error-label {
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    font-size: 10px;
    flex-shrink: 0;
  }
  .card-error-msg {
    word-break: break-word;
    flex: 1;
    min-width: 0;
  }
  .card-error-details {
    margin-top: 6px;
    font-size: 10px;
  }
  .card-error-details summary {
    cursor: pointer;
    color: var(--color-pink);
    text-decoration: underline;
    text-underline-offset: 2px;
  }
  .card-error-full {
    margin-top: 4px;
    padding: 6px 8px;
    background: rgba(0, 0, 0, 0.25);
    border-radius: 6px;
    color: var(--color-text);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 10px;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 200px;
    overflow-y: auto;
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
