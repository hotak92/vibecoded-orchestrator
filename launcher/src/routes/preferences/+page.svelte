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

  onMount(load);
  $effect(() => { if (project) void load(); });
</script>

<div class="pr-page">
  <header class="pr-header">
    <button class="pr-back" onclick={() => goto('/')}>← Back</button>
    <h1>Preferences</h1>
  </header>

  {#if !project}
    <p class="pr-empty">Select a project from the menu bar.</p>
  {:else if loading}
    <p class="pr-empty">Loading…</p>
  {:else}
    <main class="pr-main">
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
    </main>
  {/if}
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
