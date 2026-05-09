<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { invoke } from '$lib/tauri';
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
</style>
