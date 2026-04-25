<script lang="ts">
  import { orchestrator, isOrchestratorBusy, type SystemDetection, type InstallConfig } from '$lib/stores/orchestrator';
  import { currentUser } from '$lib/stores/auth';

  let { onClose }: { onClose: () => void } = $props();

  type Step = 'detect' | 'configure' | 'install' | 'done';
  let step = $state<Step>('detect');
  let system = $state<SystemDetection | null>(null);
  let detecting = $state(false);
  let error = $state<string | null>(null);

  // Config options
  let installPath = $state('');
  let useGpu = $state(false);
  let cpuOnly = $state(false);
  let useOpenai = $state(false);
  let openaiKey = $state('');
  let containerRuntime = $state<string | null>(null);
  let skipContainers = $state(false);

  // Progress
  let progress = $state<{ stage: string; message: string; percentage: number } | null>(null);

  const orchState = $derived($orchestrator);

  async function detectSystem() {
    detecting = true;
    error = null;
    try {
      const detected = await orchestrator.detectSystem();
      if (!detected) {
        error = 'Tauri runtime not available (browser mode); cannot detect system.';
        return;
      }
      system = detected;
      installPath = orchState.installPath;

      // Auto-configure based on detection
      useGpu = system.has_nvidia_gpu;
      cpuOnly = !system.has_nvidia_gpu && !system.has_apple_silicon;

      if (system.has_podman && !system.has_docker) {
        containerRuntime = 'podman';
      } else if (system.has_docker && !system.has_podman) {
        containerRuntime = 'docker';
      } else if (!system.has_docker && !system.has_podman) {
        skipContainers = true;
      }

      step = 'configure';
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      detecting = false;
    }
  }

  async function startInstall() {
    error = null;
    step = 'install';

    const config: InstallConfig = {
      install_path: installPath,
      use_gpu: useGpu,
      cpu_only: cpuOnly,
      openai_key: useOpenai && openaiKey ? openaiKey : null,
      container_runtime: containerRuntime,
      skip_containers: skipContainers,
    };

    try {
      await orchestrator.install(config);
      step = 'done';
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
      step = 'configure'; // Go back so user can fix
    }
  }

  // Subscribe to progress events
  $effect(() => {
    if (orchState.progress) {
      progress = orchState.progress;
    }
  });

  // Auto-detect on mount
  $effect(() => {
    if (step === 'detect' && !detecting && !system) {
      detectSystem();
    }
  });
</script>

<div class="wizard-overlay" onclick={onClose}>
  <div class="wizard-modal" onclick={(e) => e.stopPropagation()}>
    <!-- Header -->
    <div class="wizard-header">
      <h2>
        {#if step === 'detect'}Detecting System...
        {:else if step === 'configure'}Configure Installation
        {:else if step === 'install'}Installing...
        {:else}Installation Complete!
        {/if}
      </h2>
      <button class="close-btn" onclick={onClose}>&times;</button>
    </div>

    <!-- Step: Detect -->
    {#if step === 'detect'}
      <div class="wizard-body">
        <div class="detect-spinner">
          <div class="spinner"></div>
          <p>Scanning your system for GPU, Python, Docker/Podman...</p>
        </div>
      </div>

    <!-- Step: Configure -->
    {:else if step === 'configure' && system}
      <div class="wizard-body">
        <!-- System summary -->
        <div class="system-summary">
          <h3>System Detected</h3>
          <div class="summary-grid">
            <div class="summary-item" class:ok={system.has_python} class:missing={!system.has_python}>
              <span class="label">Python</span>
              <span class="value">{system.has_python ? system.python_version : 'Not found'}</span>
            </div>
            <div class="summary-item" class:ok={system.has_nvidia_gpu || system.has_apple_silicon} class:neutral={!system.has_nvidia_gpu && !system.has_apple_silicon}>
              <span class="label">GPU</span>
              <span class="value">{system.has_nvidia_gpu ? system.gpu_name : system.has_apple_silicon ? 'Apple Silicon' : 'None (CPU mode)'}</span>
            </div>
            <div class="summary-item" class:ok={system.has_docker || system.has_podman} class:missing={!system.has_docker && !system.has_podman}>
              <span class="label">Containers</span>
              <span class="value">{system.has_docker ? 'Docker' : ''}{system.has_docker && system.has_podman ? ' + ' : ''}{system.has_podman ? 'Podman' : ''}{!system.has_docker && !system.has_podman ? 'Not found' : ''}</span>
            </div>
            <div class="summary-item" class:ok={system.has_claude_cli} class:missing={!system.has_claude_cli}>
              <span class="label">Claude CLI</span>
              <span class="value">{system.has_claude_cli ? 'Installed' : 'Not found'}</span>
            </div>
            <div class="summary-item" class:ok={system.has_git} class:missing={!system.has_git}>
              <span class="label">Git</span>
              <span class="value">{system.has_git ? 'Installed' : 'Not found'}</span>
            </div>
          </div>
        </div>

        <!-- Prerequisites check -->
        {#if !system.has_python}
          <div class="prereq-error">Python 3.11+ is required. <a href="https://python.org" target="_blank">Download Python</a></div>
        {/if}
        {#if !system.has_git}
          <div class="prereq-error">Git is required. <a href="https://git-scm.com" target="_blank">Download Git</a></div>
        {/if}
        {#if !system.has_docker && !system.has_podman}
          <div class="prereq-warn">Docker or Podman recommended. <a href="https://docs.docker.com/get-docker/" target="_blank">Get Docker</a> or you can skip containers.</div>
        {/if}

        <!-- Config options -->
        <div class="config-section">
          <h3>Configuration</h3>

          <div class="config-field">
            <label>Install path</label>
            <input type="text" bind:value={installPath} />
          </div>

          <!-- GPU toggle -->
          {#if system.has_nvidia_gpu}
            <div class="config-field">
              <label class="toggle-row">
                <span class="toggle-label">
                  <span class="toggle-name">Use GPU</span>
                  <span class="toggle-desc">{system.gpu_name} — enables CodeSage code embeddings + faster Ollama</span>
                </span>
                <input type="checkbox" class="toggle" bind:checked={useGpu}
                  onchange={() => { if (useGpu) cpuOnly = false; }} />
              </label>
            </div>
          {/if}

          <!-- Container runtime -->
          {#if system.has_docker || system.has_podman}
            <div class="config-field">
              <label>Container runtime</label>
              <div class="select-row">
                {#if system.has_docker && system.has_podman}
                  <select bind:value={containerRuntime}>
                    <option value="docker">Docker</option>
                    <option value="podman">Podman</option>
                  </select>
                {:else}
                  <span class="auto-value">{system.has_docker ? 'Docker' : 'Podman'} (only one available)</span>
                {/if}
                <label class="checkbox-label">
                  <input type="checkbox" bind:checked={skipContainers} />
                  <span>Skip containers (manual setup)</span>
                </label>
              </div>
            </div>
          {:else}
            <div class="config-field">
              <label class="checkbox-label">
                <input type="checkbox" bind:checked={skipContainers} />
                Skip container setup (Docker/Podman not found)
              </label>
            </div>
          {/if}

          <!-- Embedding mode -->
          <div class="config-field">
            <label>Embedding mode</label>
            <div class="radio-group">
              <label class="radio-option" class:selected={!cpuOnly && !useOpenai}>
                <input type="radio" name="embed" checked={!cpuOnly && !useOpenai}
                  onchange={() => { cpuOnly = false; useOpenai = false; }} />
                <span class="radio-label">{useGpu ? 'GPU' : 'Local'} embeddings</span>
                <span class="radio-desc">{useGpu ? 'CodeSage + qwen3 (best)' : 'qwen3-embedding on CPU'}</span>
              </label>
              <label class="radio-option" class:selected={useOpenai}>
                <input type="radio" name="embed" checked={useOpenai}
                  onchange={() => { cpuOnly = false; useOpenai = true; }} />
                <span class="radio-label">OpenAI API</span>
                <span class="radio-desc">text-embedding-3-small (needs API key)</span>
              </label>
            </div>
          </div>

          {#if useOpenai}
            <div class="config-field">
              <label>OpenAI API Key</label>
              <input type="password" bind:value={openaiKey} placeholder="sk-..." />
            </div>
          {/if}
        </div>

        {#if error}
          <div class="error-box">{error}</div>
        {/if}
      </div>

      <div class="wizard-footer">
        <button class="btn-secondary" onclick={onClose}>Cancel</button>
        <button class="btn-primary" onclick={startInstall}
          disabled={!system.has_python || !system.has_git}>
          Install Orchestrator
        </button>
      </div>

    <!-- Step: Installing -->
    {:else if step === 'install'}
      <div class="wizard-body">
        <div class="install-progress">
          {#if progress}
            <div class="progress-bar-container">
              <div class="progress-bar" style="width: {progress.percentage}%"></div>
            </div>
            <p class="progress-stage">{progress.stage}</p>
            <p class="progress-message">{progress.message}</p>
          {:else}
            <div class="spinner"></div>
            <p>Starting installation...</p>
          {/if}
        </div>
      </div>

    <!-- Step: Done -->
    {:else if step === 'done'}
      <div class="wizard-body">
        <div class="done-section">
          <div class="done-icon">&#10003;</div>
          <h3>Orchestrator Installed!</h3>
          <p>Open your project in VS Code and run <code>claude</code> to start using it.</p>
          <p class="done-path">Installed at: <code>{installPath}</code></p>
        </div>
      </div>
      <div class="wizard-footer">
        <button class="btn-primary" onclick={onClose}>Done</button>
      </div>
    {/if}
  </div>
</div>

<style>
  .wizard-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.7);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    backdrop-filter: blur(4px);
  }
  .wizard-modal {
    background: var(--color-bg2);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-card);
    width: 600px;
    max-height: 80vh;
    overflow-y: auto;
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.5);
  }
  .wizard-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px 24px;
    border-bottom: 1px solid var(--color-border);
  }
  .wizard-header h2 { font-size: 18px; font-weight: 600; }
  .close-btn {
    background: none;
    border: none;
    color: var(--color-mid);
    font-size: 24px;
    cursor: pointer;
    padding: 0 4px;
  }
  .close-btn:hover { color: var(--color-text); }
  .wizard-body { padding: 24px; }
  .wizard-footer {
    display: flex;
    justify-content: flex-end;
    gap: 12px;
    padding: 16px 24px;
    border-top: 1px solid var(--color-border);
  }

  /* System summary */
  .system-summary { margin-bottom: 24px; }
  .system-summary h3 { font-size: 14px; font-weight: 600; color: var(--color-mid); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }
  .summary-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .summary-item {
    display: flex;
    justify-content: space-between;
    padding: 8px 12px;
    border-radius: 8px;
    background: var(--color-card);
    border: 1px solid var(--color-border);
  }
  .summary-item.ok { border-color: rgba(0, 191, 166, 0.3); }
  .summary-item.missing { border-color: rgba(255, 79, 79, 0.3); }
  .summary-item .label { color: var(--color-mid); font-size: 13px; }
  .summary-item .value { font-size: 13px; font-weight: 500; }

  /* Config */
  .config-section { margin-top: 20px; }
  .config-section h3 { font-size: 14px; font-weight: 600; color: var(--color-mid); margin-bottom: 16px; text-transform: uppercase; letter-spacing: 1px; }
  .config-field { margin-bottom: 16px; }
  .config-field > label { display: block; font-size: 13px; color: var(--color-mid); margin-bottom: 6px; }
  .config-field input[type="text"],
  .config-field input[type="password"] {
    width: 100%;
    padding: 8px 12px;
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 8px;
    color: var(--color-text);
    font-size: 14px;
    font-family: 'JetBrains Mono', monospace;
  }
  .config-field input:focus { outline: none; border-color: var(--color-teal); }

  .radio-group { display: flex; flex-direction: column; gap: 8px; }
  .radio-option {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    border-radius: 8px;
    background: var(--color-card);
    border: 1px solid var(--color-border);
    cursor: pointer;
    transition: border-color 0.2s;
  }
  .radio-option:hover { border-color: var(--color-teal); }
  .radio-option.selected { border-color: var(--color-teal); background: rgba(0, 191, 166, 0.05); }
  .radio-option input[type="radio"] { accent-color: var(--color-teal); }
  .radio-label { font-size: 14px; font-weight: 500; }
  .radio-desc { font-size: 12px; color: var(--color-muted); margin-left: auto; }

  .checkbox-label { display: flex; align-items: center; gap: 8px; font-size: 14px; cursor: pointer; }
  .checkbox-label input { accent-color: var(--color-teal); }

  /* Toggle row */
  .toggle-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 14px;
    border-radius: 8px;
    background: var(--color-card);
    border: 1px solid var(--color-border);
    cursor: pointer;
  }
  .toggle-label { display: flex; flex-direction: column; gap: 2px; }
  .toggle-name { font-size: 14px; font-weight: 500; }
  .toggle-desc { font-size: 12px; color: var(--color-muted); }
  .toggle {
    width: 44px;
    height: 24px;
    accent-color: var(--color-teal);
    cursor: pointer;
  }

  /* Select */
  .select-row { display: flex; align-items: center; gap: 16px; }
  .select-row select {
    padding: 8px 12px;
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 8px;
    color: var(--color-text);
    font-size: 14px;
  }
  .select-row select:focus { outline: none; border-color: var(--color-teal); }
  .auto-value { font-size: 14px; color: var(--color-mid); }

  /* Progress */
  .install-progress { text-align: center; padding: 40px 0; }
  .progress-bar-container {
    width: 100%;
    height: 8px;
    background: var(--color-card);
    border-radius: 4px;
    overflow: hidden;
    margin-bottom: 16px;
  }
  .progress-bar {
    height: 100%;
    background: linear-gradient(90deg, var(--color-teal), var(--color-purple));
    border-radius: 4px;
    transition: width 0.3s ease;
  }
  .progress-stage { font-size: 12px; color: var(--color-muted); text-transform: uppercase; letter-spacing: 1px; }
  .progress-message { font-size: 14px; margin-top: 8px; }

  /* Prereqs */
  .prereq-error { padding: 10px 14px; background: rgba(255, 79, 79, 0.1); border: 1px solid rgba(255, 79, 79, 0.3); border-radius: 8px; font-size: 13px; margin-bottom: 12px; }
  .prereq-error a { color: var(--color-teal); }
  .prereq-warn { padding: 10px 14px; background: rgba(255, 191, 0, 0.08); border: 1px solid rgba(255, 191, 0, 0.2); border-radius: 8px; font-size: 13px; margin-bottom: 12px; }
  .prereq-warn a { color: var(--color-teal); }
  .error-box { padding: 10px 14px; background: rgba(255, 79, 79, 0.1); border: 1px solid rgba(255, 79, 79, 0.3); border-radius: 8px; font-size: 13px; margin-top: 16px; }

  /* Done */
  .done-section { text-align: center; padding: 32px 0; }
  .done-icon { font-size: 48px; color: var(--color-teal); margin-bottom: 16px; }
  .done-section h3 { font-size: 20px; margin-bottom: 12px; }
  .done-section code { background: var(--color-card); padding: 2px 8px; border-radius: 4px; font-size: 13px; }
  .done-path { font-size: 13px; color: var(--color-muted); margin-top: 12px; }

  /* Buttons */
  .btn-primary {
    padding: 10px 24px;
    background: var(--color-teal);
    color: #000;
    border: none;
    border-radius: 8px;
    font-weight: 600;
    font-size: 14px;
    cursor: pointer;
    transition: background 0.2s;
  }
  .btn-primary:hover { background: var(--color-teal-hover); }
  .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-secondary {
    padding: 10px 24px;
    background: var(--color-card);
    color: var(--color-text);
    border: 1px solid var(--color-border);
    border-radius: 8px;
    font-size: 14px;
    cursor: pointer;
  }
  .btn-secondary:hover { border-color: var(--color-mid); }

  /* Spinner */
  .spinner {
    width: 32px;
    height: 32px;
    border: 3px solid var(--color-border);
    border-top-color: var(--color-teal);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin: 0 auto 16px;
  }
  .detect-spinner { text-align: center; padding: 48px 0; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
