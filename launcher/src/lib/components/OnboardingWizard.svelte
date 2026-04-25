<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';

  let {
    open = $bindable<boolean>(false),
    onComplete,
  }: { open: boolean; onComplete?: () => void } = $props();

  const KEY = 'vct.onboarding_complete';

  let step = $state<1 | 2 | 3 | 4>(1);

  // Step 2: detection
  let detection = $state<any>(null);
  let detectError = $state<string | null>(null);

  // Step 3: install
  let installPath = $state('');
  let installing = $state(false);
  let installed = $state(false);
  let installError = $state<string | null>(null);

  async function loadStep2() {
    try {
      detection = await invoke<any>('detect_system');
      installPath = await invoke<string>('get_default_install_path');
      installed = await invoke<boolean>('check_install_status', { path: installPath });
    } catch (e) {
      detectError = String(e);
    }
  }

  async function runInstall() {
    installing = true;
    installError = null;
    try {
      await invoke('install_orchestrator', {
        path: installPath,
        config: {},
      });
      installed = true;
      toast.success('Orchestrator installed');
    } catch (e) {
      installError = String(e);
    } finally {
      installing = false;
    }
  }

  function finish() {
    try { localStorage.setItem(KEY, 'true'); } catch {}
    open = false;
    onComplete?.();
  }

  function next() {
    if (step < 4) step = (step + 1) as any;
    if (step === 2 && !detection) void loadStep2();
  }
  function prev() { if (step > 1) step = (step - 1) as any; }
  function skip() { finish(); }

  $effect(() => {
    if (step === 2 && !detection) void loadStep2();
  });

  onMount(() => {
    // Caller decides whether to open; we only check the flag if asked.
  });

  export function shouldShow(): boolean {
    try { return localStorage.getItem(KEY) !== 'true'; }
    catch { return true; }
  }
</script>

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="ow-back">
    <div class="ow-modal">
      <header class="ow-header">
        <h2>Welcome to VibeCoded Tools</h2>
        <ol class="ow-steps">
          <li class:active={step >= 1} class:current={step === 1}>1. Welcome</li>
          <li class:active={step >= 2} class:current={step === 2}>2. System</li>
          <li class:active={step >= 3} class:current={step === 3}>3. Containers</li>
          <li class:active={step >= 4} class:current={step === 4}>4. First project</li>
        </ol>
      </header>

      <div class="ow-body">
        {#if step === 1}
          <p>This launcher manages projects, modules, and infrastructure for VibeCoded Tools.</p>
          <p class="ow-secondary">
            We'll detect your system, install the local orchestrator (Weaviate + Ollama containers), and create your
            first project.
          </p>
          <p class="ow-secondary">
            You can skip any step. Default settings are sensible for most setups.
          </p>
        {:else if step === 2}
          {#if detectError}
            <p class="ow-error">{detectError}</p>
          {:else if !detection}
            <p class="ow-secondary">Detecting…</p>
          {:else}
            <table class="ow-table">
              <tbody>
                <tr><th>OS</th><td>{detection.os ?? '—'}</td></tr>
                <tr><th>Arch</th><td>{detection.arch ?? '—'}</td></tr>
                <tr><th>Python</th><td>{detection.python_version ?? 'not found'}</td></tr>
                <tr><th>Container runtime</th><td>{detection.container_runtime ?? 'not found'}</td></tr>
                <tr><th>RAM</th><td>{detection.ram_gb ?? '—'} GB</td></tr>
              </tbody>
            </table>
            <p class="ow-secondary">If something is missing, install it before continuing.</p>
          {/if}
        {:else if step === 3}
          <label class="ow-label">
            <span>Install path</span>
            <input bind:value={installPath} />
          </label>
          {#if installed}
            <p class="ow-ok">Orchestrator already installed at this path.</p>
          {:else}
            <button class="ow-btn-primary" onclick={runInstall} disabled={installing}>
              {installing ? 'Installing…' : 'Install orchestrator'}
            </button>
          {/if}
          {#if installError}<p class="ow-error">{installError}</p>{/if}
        {:else if step === 4}
          <p>Time to create your first project.</p>
          <p class="ow-secondary">
            Click <strong>Finish</strong> below; you'll land on the main screen where the
            project selector in the menu bar opens a "create project" form. Pick a folder
            you want to use as the project root.
          </p>
        {/if}
      </div>

      <footer class="ow-footer">
        <button class="ow-btn-link" onclick={skip}>Skip onboarding</button>
        <div class="ow-nav">
          {#if step > 1}<button class="ow-btn" onclick={prev}>Back</button>{/if}
          {#if step < 4}
            <button class="ow-btn-primary" onclick={next}>Next →</button>
          {:else}
            <button class="ow-btn-primary" onclick={finish}>Finish</button>
          {/if}
        </div>
      </footer>
    </div>
  </div>
{/if}

<style>
  .ow-back { position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 1200; display: flex; align-items: center; justify-content: center; }
  .ow-modal { background: #1a1a22; border: 1px solid rgba(255,255,255,0.08); border-radius: 10px; padding: 0; width: 560px; max-width: 92vw; }
  .ow-header { padding: 16px 20px 8px; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .ow-header h2 { margin: 0 0 12px; font-size: 16px; }
  .ow-steps { list-style: none; padding: 0; margin: 0; display: flex; gap: 8px; font-size: 11px; color: #666; }
  .ow-steps li.active { color: #c4b3ff; }
  .ow-steps li.current { color: #0fc; font-weight: 600; }
  .ow-body { padding: 16px 20px; min-height: 160px; font-size: 13px; line-height: 1.55; color: #ccc; }
  .ow-secondary { color: #888; font-size: 12px; }
  .ow-error { color: #f99; font-size: 12px; }
  .ow-ok { color: #0fc; font-size: 12px; }
  .ow-table { font-size: 12px; border-collapse: collapse; }
  .ow-table th { text-align: left; color: #888; font-weight: 500; padding: 4px 12px 4px 0; }
  .ow-table td { padding: 4px 0; color: #ccc; }
  .ow-label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: #888; margin-bottom: 8px; }
  .ow-label input {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); color: inherit;
    padding: 5px 10px; border-radius: 4px; font-size: 13px; font-family: ui-monospace, monospace;
  }
  .ow-footer { display: flex; justify-content: space-between; align-items: center; padding: 12px 20px; border-top: 1px solid rgba(255,255,255,0.06); }
  .ow-nav { display: flex; gap: 6px; }
  .ow-btn, .ow-btn-primary, .ow-btn-link {
    padding: 5px 14px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 500;
    border: 1px solid rgba(255,255,255,0.1); background: rgba(255,255,255,0.05); color: inherit;
  }
  .ow-btn-primary { background: rgb(0,191,166); border-color: rgb(0,191,166); color: #000; font-weight: 600; }
  .ow-btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .ow-btn-link { background: none; border: none; color: #888; }
  .ow-btn-link:hover { color: #ccc; }
</style>
