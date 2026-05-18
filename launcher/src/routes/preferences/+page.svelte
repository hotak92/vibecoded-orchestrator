<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { invoke, safeInvoke, listen as tauriListen } from '$lib/tauri';
  import { selectedProject } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';
  import { ui } from '$lib/stores/ui';
  import Toast from '$lib/components/Toast.svelte';
  import Dropdown from '$lib/components/Dropdown.svelte';

  // Setting key → default value
  const KEYS = [
    { key: 'watermark_enabled', label: 'Show free-tier watermark on agent outputs', kind: 'bool' as const, default: true },
    { key: 'auto_update_enabled', label: 'Auto-check for orchestrator updates', kind: 'bool' as const, default: true },
    { key: 'logging_level', label: 'Logging level', kind: 'enum' as const, default: 'info', options: ['debug', 'info', 'warning', 'error'] },
    { key: 'tray_start_minimized', label: 'Start launcher minimized to tray', kind: 'bool' as const, default: false },
    { key: 'tray_close_to_tray', label: 'Close button minimizes to tray (doesn\'t exit)', kind: 'bool' as const, default: true },
    { key: 'default_embedding_mode', label: 'Default embedding backend', kind: 'enum' as const, default: 'gpu', options: ['gpu', 'ollama'] },
  ];

  let values = $state<Record<string, any>>({});
  let loading = $state(true);

  // Onboarding re-trigger state
  let showOnboardingConfirm = $state(false);

  // ── GitHub access token (Manage Token UI, wired 2026-05-09) ───────
  // Reads via has_github_pat / get_github_pat_preview, writes via
  // register_github_pat (with EXISTS_DIFFERENT: replace-guard), clears
  // via clear_github_pat. Token is stored in the OS keychain — never
  // displayed in clear, never written to GUI-readable files.
  const PAT_REPLACE_GUARD = 'EXISTS_DIFFERENT:';
  let patPresent = $state(false);
  let patPreview = $state<string | null>(null);
  let patEditing = $state(false);
  let patNewValue = $state('');
  let patSaving = $state(false);
  let patError = $state<string | null>(null);
  let patClearing = $state(false);
  let showPatClearConfirm = $state(false);

  async function loadPat() {
    try {
      patPresent = await invoke<boolean>('has_github_pat');
      if (patPresent) {
        patPreview = await invoke<string | null>('get_github_pat_preview');
      } else {
        patPreview = null;
      }
    } catch (e) {
      patError = String(e);
    }
  }

  async function savePat() {
    patError = null;
    const token = patNewValue.trim();
    if (!token) {
      patError = 'Token is empty.';
      return;
    }
    patSaving = true;
    try {
      try {
        await invoke('register_github_pat', { token, force: false });
      } catch (e) {
        const msg = String(e);
        if (msg.startsWith(PAT_REPLACE_GUARD)) {
          const reason = msg.slice(PAT_REPLACE_GUARD.length).trim()
            || 'A different GitHub token is already saved.';
          if (!confirm(`${reason}\n\nReplace the existing token?`)) {
            patError = 'Token not saved (existing keychain entry kept).';
            return;
          }
          await invoke('register_github_pat', { token, force: true });
        } else {
          throw e;
        }
      }
      patNewValue = '';
      patEditing = false;
      await loadPat();
      toast.success('GitHub token saved');
    } catch (e) {
      patError = String(e);
    } finally {
      patSaving = false;
    }
  }

  async function clearPat() {
    showPatClearConfirm = false;
    patClearing = true;
    patError = null;
    try {
      await invoke('clear_github_pat');
      await loadPat();
      toast.success('GitHub token removed');
    } catch (e) {
      patError = String(e);
    } finally {
      patClearing = false;
    }
  }

  // ── Hardware re-detection (Bug B, v0.2.5) ──────────────────────────
  // Two-stage UX:
  //   1. "Re-detect hardware" → runs detect_system server-side, persists
  //      a fresh snapshot, returns a diff against the previous snapshot.
  //   2. If changed_fields is non-empty, surface "Apply reconfiguration"
  //      which spawns `install.py --update <flags>` from the known
  //      install path and streams progress events into a log panel.
  interface HardwareSnapshot {
    has_nvidia_gpu: boolean;
    gpu_name: string;
    has_apple_silicon: boolean;
    ram_gb: number;
    use_gpu: boolean;
    low_resource: boolean;
  }
  interface HardwareDetectionDiff {
    before: HardwareSnapshot | null;
    after: HardwareSnapshot;
    changed_fields: string[];
  }
  interface ReconfigReport {
    success: boolean;
    exit_code: number;
    log_path: string;
  }

  let hwDiff = $state<HardwareDetectionDiff | null>(null);
  let hwDetecting = $state(false);
  let hwApplying = $state(false);
  let hwError = $state<string | null>(null);
  let hwLog = $state<string[]>([]);
  let hwLastReport = $state<ReconfigReport | null>(null);
  let unlistenHwProgress: (() => void) | null = null;

  async function loadInitialHardwareSnapshot() {
    // Render the persisted snapshot (seeded at first boot) so the user
    // sees the current hardware fingerprint even before clicking
    // Re-detect. Soft-fail: an empty / missing app_state row just leaves
    // the section in the "no snapshot yet" state.
    const raw = await safeInvoke<{ value: string | null; is_set: boolean }>(
      'app_state_get',
      { key: 'launcher.hardware_snapshot' },
    );
    if (raw && raw.is_set && raw.value) {
      try {
        const snap = JSON.parse(raw.value) as HardwareSnapshot;
        hwDiff = { before: null, after: snap, changed_fields: [] };
      } catch {
        // Corrupted row — ignore; the next Re-detect will overwrite it.
      }
    }
  }

  async function redetectHardware() {
    hwError = null;
    hwDetecting = true;
    try {
      const diff = await invoke<HardwareDetectionDiff>('redetect_hardware');
      hwDiff = diff;
      if (diff.changed_fields.length === 0) {
        toast.success('Hardware unchanged');
      } else {
        toast.success(`Hardware changed (${diff.changed_fields.length} field(s))`);
      }
    } catch (e) {
      hwError = String(e);
      toast.error(e);
    } finally {
      hwDetecting = false;
    }
  }

  async function applyHardwareReconfig() {
    hwError = null;
    hwLog = [];
    hwLastReport = null;
    hwApplying = true;
    try {
      // Subscribe to progress events for the duration of this run.
      unlistenHwProgress = await tauriListen<string>(
        'hardware_reconfig_progress',
        (event) => {
          hwLog = [...hwLog, event.payload];
        },
      );
      const report = await invoke<ReconfigReport>('apply_hardware_reconfig');
      hwLastReport = report;
      if (report.success) {
        toast.success('Hardware reconfiguration complete');
      } else {
        toast.error(`Reconfiguration failed (exit ${report.exit_code})`);
      }
    } catch (e) {
      hwError = String(e);
      toast.error(e);
    } finally {
      hwApplying = false;
      if (unlistenHwProgress) {
        unlistenHwProgress();
        unlistenHwProgress = null;
      }
    }
  }

  function formatHwField(name: string, snap: HardwareSnapshot | null): string {
    if (!snap) return '—';
    switch (name) {
      case 'has_nvidia_gpu': return snap.has_nvidia_gpu ? 'yes' : 'no';
      case 'gpu_name': return snap.gpu_name || '(none)';
      case 'has_apple_silicon': return snap.has_apple_silicon ? 'yes' : 'no';
      case 'ram_gb': return `${snap.ram_gb} GB`;
      case 'use_gpu': return snap.use_gpu ? 'GPU' : 'CPU-only';
      case 'low_resource': return snap.low_resource ? 'low-resource mode' : 'standard';
      default: return '—';
    }
  }

  onDestroy(() => {
    if (unlistenHwProgress) {
      unlistenHwProgress();
      unlistenHwProgress = null;
    }
  });

  function confirmRerunOnboarding() {
    showOnboardingConfirm = false;
    ui.openOnboarding();
    goto('/');
  }

  const project = $derived($selectedProject);

  async function load() {
    if (!project) return;
    loading = true;
    try {
      const out: Record<string, any> = {};
      for (const k of KEYS) {
        try {
          const raw = await invoke<any>('get_setting_v2', {
            projectId: project.id,
            moduleId: 'launcher',
            key: k.key,
          });
          out[k.key] = raw ?? k.default;
        } catch {
          out[k.key] = k.default;
        }
      }
      values = out;
    } finally {
      loading = false;
    }
  }

  async function save(key: string, value: any) {
    if (!project) return;
    values = { ...values, [key]: value };
    try {
      await invoke('set_setting_v2', {
        projectId: project.id,
        moduleId: 'launcher',
        key,
        value,
      });
      toast.success('Saved');
    } catch (e) {
      toast.error(e);
    }
  }

  onMount(() => {
    void load();
    void loadPat();
    void loadInitialHardwareSnapshot();
  });
  $effect(() => { if (project) void load(); });
</script>

<div class="pr-page">
  <header class="pr-header">
    <button class="pr-back" onclick={() => goto('/')}>← Back</button>
    <h1>Preferences</h1>
  </header>

  <main class="pr-main">
    <!-- Project-scoped settings (KG / module dropdowns) require a selected
         project. Onboarding and Launcher self-update are app-level — they
         work for new users who don't have a project yet, so they live
         OUTSIDE the project guard. -->

    {#if !project}
      <p class="pr-empty">Select a project from the menu bar to edit project-scoped settings.</p>
    {:else if loading}
      <p class="pr-empty">Loading project settings…</p>
    {:else}
      <p class="pr-hint">
        Settings scoped to <code>{project.name}</code>. They're stored under the <code>launcher</code> module
        namespace in <code>~/.vct/launcher.db</code>.
      </p>
      <ul class="pr-list">
        {#each KEYS as k}
          <li class="pr-row">
            <strong>{k.label}</strong>
            {#if k.kind === 'bool'}
              <input
                type="checkbox"
                checked={values[k.key] === true}
                onchange={(e) => save(k.key, (e.target as HTMLInputElement).checked)}
              />
            {:else if k.kind === 'enum' && k.options}
              <div class="pr-dd">
                <Dropdown
                  options={k.options.map((opt: string) => ({ value: opt, label: opt }))}
                  value={values[k.key]}
                  onChange={(v: string) => save(k.key, v)}
                />
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}

    <!-- Onboarding + Updates: app-level, available regardless of project state. -->
    <section class="pr-section">
      <h2 class="pr-section-title">Onboarding</h2>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Re-run onboarding wizard</strong>
          <span class="pr-onboarding-hint">
            Walks through project setup, KG bindings, and module recommendations again.
            Existing projects and settings won't be affected.
          </span>
        </div>
        <button class="pr-btn" onclick={() => (showOnboardingConfirm = true)}>
          Re-run wizard
        </button>
      </div>
    </section>

    <section class="pr-section">
      <h2 class="pr-section-title">Updates</h2>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Launcher self-update</strong>
          <span class="pr-onboarding-hint">
            Pulls launcher updates from the upstream repo. Daily check, manual apply.
            User-owned files (CONTEXT_STATE.md, logs, runtime state) are never overwritten.
          </span>
        </div>
        <button class="pr-btn" onclick={() => goto('/preferences/updates')}>
          Open
        </button>
      </div>
    </section>

    <!-- PR-10A storage UX (v0.2.11): deep link to the per-service
         storage picker. Adjacent to Updates because both are about
         runtime infrastructure (container data location vs. orchestrator
         self-update). -->
    <section class="pr-section">
      <h2 class="pr-section-title">Storage</h2>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Container data location</strong>
          <span class="pr-onboarding-hint">
            Choose named volumes (recommended) or a custom bind path for
            Weaviate, Ollama, and code-embed data. Pre-existing volumes
            from earlier installs can be reused in place.
          </span>
        </div>
        <button class="pr-btn" onclick={() => goto('/preferences/storage')}>
          Open
        </button>
      </div>
      <!-- v0.2.16 (W4 / 0.11): advanced view of Weaviate code-graph
           inventory, including prefixes whose project is no longer
           registered. GUI defaults hide these; surfaced here for
           clean-up + diagnostics. -->
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Show untracked Weaviate collections</strong>
          <span class="pr-onboarding-hint">
            Full inventory of code-graph data including dead-project
            leftovers (collections whose prefix no longer matches a
            registered project). Day-to-day surfaces hide these for
            clarity — open this view to clean them up or reference them
            before re-importing the project.
          </span>
        </div>
        <button class="pr-btn" onclick={() => goto('/preferences/weaviate-untracked')}>
          Open
        </button>
      </div>
    </section>

    <!-- Secrets (PR-4, v0.2.11). Cross-cutting OS-keychain manager;
         deep-linked from the sidebar too. The Open button takes the
         user to the dedicated route so the import sub-page stays one
         click away from the manager. -->
    <section class="pr-section">
      <h2 class="pr-section-title">Secrets</h2>
      <div class="pr-onboarding-row">
        <div class="pr-onboarding-text">
          <strong>Keychain manager</strong>
          <span class="pr-onboarding-hint">
            Per-project, shared, and global keychain entries used by orchestrator
            modules. Includes a bulk-import surface for migrating existing on-disk
            secrets (<code>~/.vct-secrets/</code> and project <code>.env</code> files)
            into the OS keychain.
          </span>
        </div>
        <button class="pr-btn" onclick={() => goto('/preferences/secrets')}>
          Open
        </button>
      </div>
    </section>

    <section class="pr-section">
      <h2 class="pr-section-title">GitHub access token</h2>
      <div class="pr-pat-row">
        <div class="pr-onboarding-text">
          <strong>
            {patPresent ? 'Token saved' : 'No token saved'}
            {#if patPresent && patPreview}<span class="pr-pat-preview">{patPreview}</span>{/if}
          </strong>
          <span class="pr-onboarding-hint">
            Stored in your OS keychain. Used by the launcher's update flow and propagated to
            registered projects' env files (<code>GITHUB_TOKEN</code>) when active. Replacing
            the token rotates it everywhere; clearing it removes it from the keychain (your
            <code>~/.vct-secrets/shared/github_pat</code> file, if any, is left untouched).
          </span>
        </div>
        <div class="pr-pat-actions">
          {#if !patEditing}
            <button class="pr-btn" onclick={() => { patEditing = true; patError = null; }}>
              {patPresent ? 'Replace…' : 'Add token…'}
            </button>
            {#if patPresent}
              <button
                class="pr-btn pr-btn-danger"
                disabled={patClearing}
                onclick={() => (showPatClearConfirm = true)}
              >
                {patClearing ? 'Clearing…' : 'Clear'}
              </button>
            {/if}
          {/if}
        </div>
      </div>

      {#if patEditing}
        <div class="pr-pat-edit">
          <input
            class="pr-pat-input"
            type="password"
            placeholder="ghp_…"
            bind:value={patNewValue}
            disabled={patSaving}
          />
          <div class="pr-pat-edit-actions">
            <button
              class="pr-btn"
              onclick={() => { patEditing = false; patNewValue = ''; patError = null; }}
              disabled={patSaving}
            >
              Cancel
            </button>
            <button
              class="pr-btn-primary"
              onclick={savePat}
              disabled={patSaving || !patNewValue.trim()}
            >
              {patSaving ? 'Saving…' : 'Save'}
            </button>
          </div>
          <p class="pr-pat-hint">
            Generate at github.com → Settings → Developer settings → Personal access tokens.
            Scope <code>repo</code> is enough for read-only update checks; add <code>workflow</code>
            if you need to push commits that modify <code>.github/workflows/</code>.
          </p>
        </div>
      {/if}

      {#if patError}<p class="pr-error">{patError}</p>{/if}
    </section>

    <!-- Hardware re-detection (Bug B, v0.2.5).
         Two-stage UX: Re-detect → optional Apply reconfig. The persisted
         snapshot is seeded at first launcher boot so the "currently
         detected" panel renders even before the user clicks Re-detect. -->
    <section class="pr-section">
      <h2 class="pr-section-title">Hardware</h2>
      <div class="pr-hw-card">
        <div class="pr-hw-header">
          <div class="pr-onboarding-text">
            <strong>Detected hardware</strong>
            <span class="pr-onboarding-hint">
              Updated at install time and whenever you click Re-detect. If you upgrade
              your GPU or RAM, re-detect so the orchestrator's containers and models can
              be reconfigured to use the new resources.
            </span>
          </div>
          <button
            class="pr-btn"
            onclick={() => void redetectHardware()}
            disabled={hwDetecting || hwApplying}
          >
            {hwDetecting ? 'Detecting…' : 'Re-detect hardware'}
          </button>
        </div>

        {#if hwDiff}
          <div class="pr-hw-grid">
            <div class="pr-hw-row"><span class="pr-hw-label">GPU (NVIDIA)</span>
              <span class="pr-hw-value">{formatHwField('has_nvidia_gpu', hwDiff.after)}</span></div>
            <div class="pr-hw-row"><span class="pr-hw-label">GPU name</span>
              <span class="pr-hw-value">{formatHwField('gpu_name', hwDiff.after)}</span></div>
            <div class="pr-hw-row"><span class="pr-hw-label">Apple Silicon</span>
              <span class="pr-hw-value">{formatHwField('has_apple_silicon', hwDiff.after)}</span></div>
            <div class="pr-hw-row"><span class="pr-hw-label">RAM</span>
              <span class="pr-hw-value">{formatHwField('ram_gb', hwDiff.after)}</span></div>
            <div class="pr-hw-row"><span class="pr-hw-label">Compute mode</span>
              <span class="pr-hw-value">{formatHwField('use_gpu', hwDiff.after)}</span></div>
            <div class="pr-hw-row"><span class="pr-hw-label">Resource tier</span>
              <span class="pr-hw-value">{formatHwField('low_resource', hwDiff.after)}</span></div>
          </div>
        {:else}
          <p class="pr-hw-empty">No hardware snapshot yet — click Re-detect.</p>
        {/if}

        {#if hwDiff && hwDiff.changed_fields.length > 0 && hwDiff.before}
          <div class="pr-hw-diff">
            <h3 class="pr-hw-diff-title">Changes since last detection</h3>
            <ul class="pr-hw-diff-list">
              {#each hwDiff.changed_fields as field}
                <li class="pr-hw-diff-row">
                  <span class="pr-hw-diff-field">{field}</span>
                  <span class="pr-hw-diff-before">{formatHwField(field, hwDiff.before)}</span>
                  <span class="pr-hw-diff-arrow">→</span>
                  <span class="pr-hw-diff-after">{formatHwField(field, hwDiff.after)}</span>
                </li>
              {/each}
            </ul>
            <button
              class="pr-btn-primary"
              onclick={() => void applyHardwareReconfig()}
              disabled={hwApplying || hwDetecting}
            >
              {hwApplying ? 'Applying…' : 'Apply reconfiguration'}
            </button>
            <p class="pr-onboarding-hint">
              Runs <code>install.py --update</code> from the known install path with
              flags derived from the detected hardware. Containers and models will be
              reconfigured — services may restart briefly.
            </p>
          </div>
        {/if}

        {#if hwLog.length > 0}
          <div class="pr-hw-log">
            <div class="pr-hw-log-header">
              <strong>Reconfiguration output</strong>
              {#if hwLastReport}
                <span class={hwLastReport.success ? 'pr-hw-log-ok' : 'pr-hw-log-fail'}>
                  exit {hwLastReport.exit_code} ({hwLastReport.success ? 'ok' : 'failed'})
                </span>
              {/if}
            </div>
            <pre class="pr-hw-log-body">{hwLog.join('\n')}</pre>
            {#if hwLastReport}
              <p class="pr-onboarding-hint">Log: <code>{hwLastReport.log_path}</code></p>
            {/if}
          </div>
        {/if}

        {#if hwError}<p class="pr-error">{hwError}</p>{/if}
      </div>
    </section>
  </main>
</div>

{#if showOnboardingConfirm}
  <!-- Confirmation modal rendered as a simple overlay so we avoid pulling in
       DialogRoot (which is a layout-level component). This keeps /preferences
       self-contained. -->
  <div class="pr-overlay" role="presentation" onclick={() => (showOnboardingConfirm = false)}>
    <div class="pr-modal" role="dialog" aria-modal="true" aria-labelledby="pr-modal-title"
         onclick={(e) => e.stopPropagation()}>
      <h3 id="pr-modal-title" class="pr-modal-title">Re-run onboarding wizard?</h3>
      <p class="pr-modal-body">
        This will show the setup wizard again from step 1.
        Your existing projects, secrets, and settings won't be changed.
      </p>
      <div class="pr-modal-actions">
        <button class="pr-btn" onclick={() => (showOnboardingConfirm = false)}>Cancel</button>
        <button class="pr-btn-primary" onclick={confirmRerunOnboarding}>Show wizard</button>
      </div>
    </div>
  </div>
{/if}

{#if showPatClearConfirm}
  <div class="pr-overlay" role="presentation" onclick={() => (showPatClearConfirm = false)}>
    <div class="pr-modal" role="dialog" aria-modal="true" aria-labelledby="pr-pat-clear-title"
         onclick={(e) => e.stopPropagation()}>
      <h3 id="pr-pat-clear-title" class="pr-modal-title">Clear GitHub token?</h3>
      <p class="pr-modal-body">
        Removes the token from your OS keychain and strips <code>GITHUB_TOKEN</code>
        from every registered project's env files on the next refresh. The
        <code>~/.vct-secrets/shared/github_pat</code> file (if any) is left untouched —
        delete it manually if you want it gone too.
      </p>
      <div class="pr-modal-actions">
        <button class="pr-btn" onclick={() => (showPatClearConfirm = false)}>Cancel</button>
        <button class="pr-btn-primary" onclick={clearPat}>Clear token</button>
      </div>
    </div>
  </div>
{/if}

<Toast />

<style>
  .pr-page { min-height: 100vh; background: var(--color-bg, #0e0e16); color: var(--color-light, #e8e8ee); }
  .pr-header { display: flex; align-items: center; gap: 12px; padding: 10px 24px; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .pr-header h1 { font-size: 16px; margin: 0; }
  .pr-back { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); color: inherit; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }
  .pr-empty { padding: 40px; text-align: center; color: #888; }
  .pr-main { max-width: 720px; margin: 0 auto; padding: 16px; }
  .pr-hint { font-size: 11px; color: #888; margin: 0 0 14px; line-height: 1.5; }
  .pr-hint code { background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; }
  .pr-list { list-style: none; padding: 0; margin: 0; background: rgba(255,255,255,0.03); border-radius: 6px; }
  .pr-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 14px; border-bottom: 1px solid rgba(255,255,255,0.04);
    font-size: 12px;
  }
  .pr-row:last-child { border-bottom: none; }
  .pr-row strong { color: #ccc; font-weight: 500; }
  /* Bug 12 systemic: native <select> replaced with <Dropdown>. */
  .pr-dd { width: 180px; }

  .pr-section { margin-top: 20px; }
  .pr-section-title { font-size: 11px; font-weight: 600; color: #888; text-transform: uppercase; letter-spacing: 0.07em; margin: 0 0 8px; }
  .pr-onboarding-row {
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
    padding: 10px 14px; background: rgba(255,255,255,0.03); border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.06);
  }
  .pr-onboarding-text { display: flex; flex-direction: column; gap: 3px; }
  .pr-onboarding-text strong { font-size: 12px; color: #ccc; font-weight: 500; }
  .pr-onboarding-hint { font-size: 11px; color: #888; max-width: 480px; line-height: 1.5; }
  .pr-btn {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 5px 14px; border-radius: 4px; cursor: pointer;
    font-size: 12px; font-weight: 500; white-space: nowrap; flex-shrink: 0;
  }
  .pr-btn:hover { background: rgba(255,255,255,0.09); }
  .pr-btn-primary {
    background: rgb(0,191,166); border: 1px solid rgb(0,191,166);
    color: #000; padding: 5px 14px; border-radius: 4px; cursor: pointer;
    font-size: 12px; font-weight: 600; white-space: nowrap;
  }
  .pr-btn-primary:hover { background: rgb(0,210,183); }

  /* GitHub PAT section */
  .pr-pat-row {
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
    padding: 10px 14px; background: rgba(255,255,255,0.03); border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.06);
  }
  .pr-pat-preview {
    margin-left: 8px; font-family: ui-monospace, monospace; font-size: 11px;
    color: #888; background: rgba(255,255,255,0.06); padding: 1px 6px; border-radius: 3px;
  }
  .pr-pat-actions { display: flex; gap: 6px; flex-shrink: 0; }
  .pr-btn-danger {
    background: rgba(229,77,77,0.12);
    border: 1px solid rgba(229,77,77,0.3);
    color: rgb(255,140,140);
  }
  .pr-btn-danger:hover { background: rgba(229,77,77,0.2); }
  .pr-pat-edit {
    margin-top: 8px; padding: 12px 14px; background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06); border-radius: 6px;
    display: flex; flex-direction: column; gap: 8px;
  }
  .pr-pat-input {
    background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.1);
    color: #e8e8ee; padding: 6px 10px; border-radius: 4px; font-size: 12px;
    font-family: ui-monospace, monospace;
  }
  .pr-pat-edit-actions { display: flex; gap: 6px; justify-content: flex-end; }
  .pr-pat-hint { font-size: 11px; color: #888; line-height: 1.5; margin: 0; }
  .pr-pat-hint code {
    background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px;
    font-size: 10.5px;
  }
  .pr-error {
    margin-top: 8px; padding: 8px 12px;
    background: rgba(229,77,77,0.1); border: 1px solid rgba(229,77,77,0.25);
    border-radius: 4px; color: rgb(255,140,140); font-size: 11px;
  }

  /* Confirmation modal */
  .pr-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.55);
    display: flex; align-items: center; justify-content: center; z-index: 9000;
  }
  .pr-modal {
    background: #1a1a26; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px;
    padding: 20px 24px; max-width: 400px; width: 100%;
  }
  .pr-modal-title { font-size: 14px; font-weight: 600; color: #e8e8ee; margin: 0 0 10px; }
  .pr-modal-body { font-size: 12px; color: #888; line-height: 1.6; margin: 0 0 16px; }
  .pr-modal-actions { display: flex; gap: 8px; justify-content: flex-end; }

  /* Hardware re-detection (Bug B, v0.2.5) */
  .pr-hw-card {
    padding: 12px 14px; background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06); border-radius: 6px;
    display: flex; flex-direction: column; gap: 12px;
  }
  .pr-hw-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
  .pr-hw-empty { font-size: 11px; color: #888; margin: 0; }
  .pr-hw-grid {
    display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px 16px; font-size: 11.5px;
  }
  .pr-hw-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .pr-hw-label { color: #888; }
  .pr-hw-value { color: #ccc; font-family: ui-monospace, monospace; }
  .pr-hw-diff {
    padding: 10px 12px; background: rgba(255,200,80,0.06);
    border: 1px solid rgba(255,200,80,0.2); border-radius: 6px;
    display: flex; flex-direction: column; gap: 8px;
  }
  .pr-hw-diff-title { font-size: 12px; font-weight: 600; color: rgb(255,200,120); margin: 0; }
  .pr-hw-diff-list { list-style: none; padding: 0; margin: 0; font-size: 11.5px; }
  .pr-hw-diff-row {
    display: grid; grid-template-columns: 1fr auto auto auto;
    gap: 8px; align-items: center; padding: 3px 0;
  }
  .pr-hw-diff-field { color: #ccc; font-family: ui-monospace, monospace; }
  .pr-hw-diff-before { color: #888; font-family: ui-monospace, monospace; text-decoration: line-through; }
  .pr-hw-diff-arrow { color: #666; }
  .pr-hw-diff-after { color: rgb(0,191,166); font-family: ui-monospace, monospace; font-weight: 500; }
  .pr-hw-log {
    padding: 10px 12px; background: rgba(0,0,0,0.4);
    border: 1px solid rgba(255,255,255,0.06); border-radius: 6px;
    display: flex; flex-direction: column; gap: 6px;
  }
  .pr-hw-log-header { display: flex; align-items: center; justify-content: space-between; }
  .pr-hw-log-header strong { font-size: 11.5px; color: #ccc; font-weight: 500; }
  .pr-hw-log-ok { font-size: 11px; color: rgb(120,220,180); }
  .pr-hw-log-fail { font-size: 11px; color: rgb(255,140,140); }
  .pr-hw-log-body {
    margin: 0; padding: 8px 10px; background: rgba(0,0,0,0.4);
    border-radius: 4px; font-family: ui-monospace, monospace; font-size: 10.5px;
    color: #ccc; max-height: 240px; overflow: auto; white-space: pre-wrap; word-break: break-word;
  }
</style>
