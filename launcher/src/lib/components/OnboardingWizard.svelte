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
  // Bug 17: install is now a local file copy from the launcher's
  // bundled repo source. We display the source path so the user
  // can confirm what's being copied. Loaded once on step 3 entry.
  let sourcePath = $state<string | null>(null);
  let sourceError = $state<string | null>(null);

  // Bug 8: adopt-confirm modal. Populated by `previewInstall` before
  // any actual write happens. If `mode === 'adopt'` we show the diff and
  // require explicit confirmation; otherwise we proceed straight to
  // install.
  type InstallMode = 'fresh' | 'fresh_into_existing' | 'adopt';
  interface InstallDiff {
    mode: InstallMode;
    will_overwrite: string[];
    will_add: string[];
    user_paths_preserved: boolean;
  }
  let pendingDiff = $state<InstallDiff | null>(null);

  async function loadStep2() {
    try {
      detection = await invoke<any>('detect_system');
      installPath = await invoke<string>('get_default_install_path');
      installed = await invoke<boolean>('check_install_status', { path: installPath });
    } catch (e) {
      detectError = String(e);
    }
  }

  async function loadStep3() {
    if (sourcePath !== null || sourceError !== null) return;
    try {
      sourcePath = await invoke<string>('get_local_repo_source');
    } catch (e) {
      sourceError = String(e);
    }
  }

  function buildInstallConfig() {
    return {
      install_path: installPath,
      use_gpu: false,
      cpu_only: false,
      openai_key: null,
      container_runtime: null,
      skip_containers: false,
    };
  }

  async function runInstall(confirmOverwrite = false) {
    installing = true;
    installError = null;
    try {
      // Bug 8: preview before write. If the target is an Adopt-style
      // folder (has .claude/, knowledge/, etc.) and the user hasn't
      // confirmed yet, show the diff modal and stop here.
      if (!confirmOverwrite) {
        const diff = await invoke<InstallDiff>('preview_install', {
          config: buildInstallConfig(),
        });
        if (diff.mode === 'adopt' && diff.will_overwrite.length > 0) {
          pendingDiff = diff;
          installing = false;
          return;
        }
      }

      // Tauri serializes the command arg name (`config`) as the JSON key,
      // so the payload must wrap install_path inside `config`. Earlier
      // versions sent `{ path, config: {} }` which Tauri rejected as
      // "missing field 'install_path'".
      await invoke('install_orchestrator', {
        config: buildInstallConfig(),
        confirmOverwrite,
      });
      installed = true;
      pendingDiff = null;
      toast.success('Orchestrator installed');
    } catch (e) {
      installError = String(e);
    } finally {
      installing = false;
    }
  }

  async function confirmAdopt() {
    pendingDiff = null;
    await runInstall(true);
  }
  function cancelAdopt() {
    pendingDiff = null;
    installing = false;
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
    if (step === 3) void loadStep3();
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
                <tr><th>OS</th><td>{detection.os ?? '—'} {detection.arch ?? ''}</td></tr>
                <tr>
                  <th>Python</th>
                  <td>{detection.has_python ? `Python ${detection.python_version}` : 'not found'}</td>
                </tr>
                <tr>
                  <th>Container runtime</th>
                  <td>{detection.container_runtime ?? 'not found (install podman or docker)'}</td>
                </tr>
                <tr>
                  <th>RAM</th>
                  <td>{detection.ram_gb ? `${detection.ram_gb} GB` : '—'}</td>
                </tr>
                <tr>
                  <th>GPU</th>
                  <td>
                    {#if detection.has_nvidia_gpu}
                      {detection.gpu_name} — {detection.vram_gb ? `${detection.vram_gb} GB VRAM (${detection.gpu_vendor ?? 'NVIDIA'})` : 'NVIDIA'}
                    {:else if detection.gpu_vendor === 'AMD'}
                      {detection.vram_gb ? `${detection.vram_gb} GB VRAM (AMD)` : 'AMD'}
                    {:else if detection.has_apple_silicon}
                      Apple Silicon (unified memory)
                    {:else}
                      none detected (CPU only)
                    {/if}
                  </td>
                </tr>
              </tbody>
            </table>
            <p class="ow-secondary">If something is missing, install it before continuing.</p>
          {/if}
        {:else if step === 3}
          <label class="ow-label">
            <span>Install path</span>
            <input bind:value={installPath} />
          </label>
          {#if sourcePath}
            <p class="ow-secondary">
              Copying from <code class="ow-mono">{sourcePath}</code> (local — no network needed)
            </p>
          {:else if sourceError}
            <p class="ow-error">Source not found: {sourceError}</p>
          {/if}
          {#if installed}
            <p class="ow-ok">Orchestrator already installed at this path.</p>
          {:else}
            <button class="ow-btn-primary" onclick={() => runInstall(false)} disabled={installing || !!sourceError}>
              {installing ? 'Installing…' : 'Install'}
            </button>
          {/if}
          {#if installError}<p class="ow-error">{installError}</p>{/if}

          {#if pendingDiff}
            <div class="ow-diff">
              <h3>Existing orchestrator files detected</h3>
              <p class="ow-secondary ow-banner">
                Your code outside <code>.claude/</code>, <code>knowledge/</code>,
                <code>state/</code>, etc. will <strong>not</strong> be touched.
              </p>
              {#if pendingDiff.will_overwrite.length}
                <details open>
                  <summary>Will overwrite ({pendingDiff.will_overwrite.length})</summary>
                  <ul class="ow-paths">
                    {#each pendingDiff.will_overwrite as p}<li><code>{p}</code></li>{/each}
                  </ul>
                </details>
              {/if}
              {#if pendingDiff.will_add.length}
                <details>
                  <summary>Will add ({pendingDiff.will_add.length})</summary>
                  <ul class="ow-paths">
                    {#each pendingDiff.will_add as p}<li><code>{p}</code></li>{/each}
                  </ul>
                </details>
              {/if}
              <div class="ow-diff-actions">
                <button class="ow-btn" onclick={cancelAdopt}>Cancel</button>
                <button class="ow-btn-primary" onclick={confirmAdopt}>Confirm adopt</button>
              </div>
            </div>
          {/if}
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
  /* Bug 19: backdrop padding + modal max-height: calc(100vh - 4rem) so
     the modal can never extend above the viewport top. z-index 9999 to
     win against any in-app chrome. overflow: hidden on backdrop (NOT
     auto) — body scrolls inside the modal, the backdrop never moves. */
  .ow-back {
    position: fixed; inset: 0; background: rgba(0,0,0,0.7);
    z-index: 9999; display: flex; align-items: center; justify-content: center;
    padding: 2rem; overflow: hidden;
  }
  .ow-modal {
    background: #1a1a22; border: 1px solid rgba(255,255,255,0.08);
    border-radius: 10px; padding: 0; width: 560px;
    max-width: min(92vw, 600px); max-height: calc(100vh - 4rem);
    display: flex; flex-direction: column; overflow: hidden;
  }
  .ow-header { padding: 16px 20px 8px; border-bottom: 1px solid rgba(255,255,255,0.06); flex-shrink: 0; }
  .ow-header h2 { margin: 0 0 12px; font-size: 16px; }
  .ow-steps { list-style: none; padding: 0; margin: 0; display: flex; gap: 8px; font-size: 11px; color: #666; }
  .ow-steps li.active { color: #c4b3ff; }
  .ow-steps li.current { color: #0fc; font-weight: 600; }
  .ow-body { padding: 16px 20px; min-height: 0; font-size: 13px; line-height: 1.55; color: #ccc; overflow-y: auto; flex: 1 1 auto; }
  .ow-secondary { color: #888; font-size: 12px; }
  .ow-mono { font-family: ui-monospace, monospace; font-size: 11px; color: #c4b3ff; background: rgba(255,255,255,0.04); padding: 1px 5px; border-radius: 3px; word-break: break-all; }
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
  .ow-footer { display: flex; justify-content: space-between; align-items: center; padding: 12px 20px; border-top: 1px solid rgba(255,255,255,0.06); flex-shrink: 0; }
  .ow-nav { display: flex; gap: 6px; }
  .ow-btn, .ow-btn-primary, .ow-btn-link {
    padding: 5px 14px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 500;
    border: 1px solid rgba(255,255,255,0.1); background: rgba(255,255,255,0.05); color: inherit;
  }
  .ow-btn-primary { background: rgb(0,191,166); border-color: rgb(0,191,166); color: #000; font-weight: 600; }
  .ow-btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .ow-btn-link { background: none; border: none; color: #888; }
  .ow-btn-link:hover { color: #ccc; }

  .ow-diff { margin-top: 12px; padding: 10px 12px; border: 1px solid rgba(0,191,166,0.25); background: rgba(0,191,166,0.05); border-radius: 6px; }
  .ow-diff h3 { font-size: 13px; margin: 0 0 6px; color: #ccc; }
  .ow-banner { margin: 0 0 8px; }
  .ow-banner code { background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; font-family: ui-monospace, monospace; font-size: 11px; }
  .ow-paths { list-style: none; padding: 4px 0 0 12px; margin: 0; max-height: 120px; overflow-y: auto; font-size: 11px; }
  .ow-paths li { padding: 2px 0; color: #ccc; }
  .ow-paths code { font-family: ui-monospace, monospace; font-size: 11px; color: #c4b3ff; }
  .ow-diff details summary { cursor: pointer; font-size: 12px; color: #0fc; padding: 4px 0; }
  .ow-diff-actions { display: flex; justify-content: flex-end; gap: 6px; margin-top: 10px; }
</style>
