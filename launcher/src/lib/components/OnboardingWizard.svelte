<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import { projects } from '$lib/stores/projects';
  import { pickDirectory, suggestProjectFolder } from '$lib/dialog';
  import { isTauriRuntime } from '$lib/tauri';
  import DialogRoot from '$lib/components/DialogRoot.svelte';

  let {
    open = $bindable<boolean>(false),
    onComplete,
  }: { open: boolean; onComplete?: () => void } = $props();

  const inTauri = isTauriRuntime();

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

  // Bug 29: shared-container detection. On step 3 entry we probe the three
  // default ports and tell the user whether their install will REUSE existing
  // services or START fresh ones. Advanced users can opt into per-install
  // separate containers via the checkbox (sets VCT_FORCE_SEPARATE_CONTAINERS=1
  // in install.py via the env block — currently a future enhancement; for now
  // the checkbox is a UI hint that maps to skip_containers behavior).
  interface ServicesStatus {
    weaviate_url: string | null;
    ollama_url: string | null;
    code_embed_url: string | null;
    all_detected: boolean;
    none_detected: boolean;
  }
  let services = $state<ServicesStatus | null>(null);
  let servicesError = $state<string | null>(null);
  let useSeparateContainers = $state(false);

  // Bug 22: optional GitHub PAT for future auto-update flow.
  let githubPat = $state('');
  let savingPat = $state(false);
  let patError = $state<string | null>(null);
  let patSaved = $state(false);

  // Bug 28: step 3 → step 4 advance confirmation when install hasn't run.
  // Shows {install_now | skip | cancel} so the user makes an explicit
  // choice instead of silently advancing.
  let showSkipInstallConfirm = $state(false);

  // Bug 28: step 4 project create state (Name + Path).
  let projectName = $state('');
  let projectPath = $state('');
  let projectPathTouched = $state(false);
  let suggestedRoot = $state('~/code');
  let creatingProject = $state(false);
  let projectError = $state<string | null>(null);

  function slugify(s: string): string {
    return s
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9._-]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 64);
  }

  async function loadStep4() {
    // Resolve a sane default root for the path field. In Tauri this
    // returns an absolute path like "/home/you/code"; in browser mode
    // it stays as the literal "~/code" so the user knows it'll be
    // expanded. Only run once per modal session.
    if (suggestedRoot !== '~/code') return;
    try {
      const root = await suggestProjectFolder();
      suggestedRoot = root || '~/code';
    } catch {
      suggestedRoot = '~/code';
    }
  }

  // Auto-suggest path from name until the user edits the path field.
  $effect(() => {
    if (step !== 4 || projectPathTouched) return;
    const root = suggestedRoot || '~/code';
    projectPath = `${root}/${slugify(projectName) || 'my-project'}`;
  });

  async function browseProjectFolder() {
    const picked = await pickDirectory({
      defaultPath: projectPath || undefined,
      title: 'Select project folder',
    });
    if (picked) {
      projectPath = picked;
      projectPathTouched = true;
    }
  }

  async function savePat() {
    patError = null;
    if (!githubPat.trim()) {
      patError = 'Token is empty.';
      return;
    }
    savingPat = true;
    try {
      await invoke('register_github_pat', { token: githubPat.trim() });
      patSaved = true;
      githubPat = '';
      toast.success('GitHub token saved');
    } catch (e) {
      patError = String(e);
    } finally {
      savingPat = false;
    }
  }

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
    if (sourcePath === null && sourceError === null) {
      try {
        sourcePath = await invoke<string>('get_local_repo_source');
      } catch (e) {
        sourceError = String(e);
      }
    }
    // Bug 29: probe shared services every time we land on step 3 — this is
    // cheap (capped at 2s wall time) and the user might have started or
    // stopped containers between the previous step and now.
    try {
      services = await invoke<ServicesStatus>('detect_existing_services');
    } catch (e) {
      servicesError = String(e);
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

  /**
   * Bug 28: finish must leave the user with at least one project record.
   * Step 4 collects name + path; if name+path are filled we create the
   * project here. If the orchestrator wasn't installed in step 3 (user
   * skipped or it failed), install it inline at the project's path
   * before creating the record. This is the safety net that prevents
   * the empty-projects state the user hit.
   */
  async function finish() {
    projectError = null;
    const name = projectName.trim();
    const path = expandTilde(projectPath.trim());
    if (name && path) {
      creatingProject = true;
      try {
        // If no orchestrator was installed at this path yet, install
        // here inline (skip_containers=true keeps it fast — the user
        // can opt into containers later from the dashboard).
        const alreadyInstalled = await invoke<boolean>('check_install_status', { path });
        if (!alreadyInstalled) {
          await invoke('install_orchestrator', {
            config: {
              install_path: path,
              use_gpu: false,
              cpu_only: false,
              openai_key: null,
              container_runtime: null,
              skip_containers: true,
            },
            confirmOverwrite: false,
          });
        }
        await projects.create(name, path, 'base');
        toast.success(`Project "${name}" created`);
      } catch (e) {
        projectError = e instanceof Error ? e.message : String(e);
        creatingProject = false;
        return;
      } finally {
        creatingProject = false;
      }
    }
    try { localStorage.setItem(KEY, 'true'); } catch {}
    open = false;
    onComplete?.();
  }

  function expandTilde(p: string): string {
    if (!p.startsWith('~')) return p;
    // Best-effort: suggestedRoot looks like "/home/you/code" or "~/code".
    // Strip the trailing "/code" portion to recover the home dir.
    const root = suggestedRoot || '';
    const home = root.replace(/[\\/]code$/, '');
    if (home && !home.startsWith('~')) {
      return home + p.slice(1);
    }
    return p;
  }

  /**
   * Bug 28: when leaving step 3 without having installed, intercept
   * with an explicit confirmation dialog. The user picks: install now
   * (run install + advance), skip (advance without install), or cancel
   * (stay on step 3). No more silent skips.
   */
  function next() {
    if (step === 3 && !installed) {
      showSkipInstallConfirm = true;
      return;
    }
    advance();
  }

  function advance() {
    if (step < 4) step = (step + 1) as any;
    if (step === 2 && !detection) void loadStep2();
    if (step === 4) void loadStep4();
  }

  async function skipInstallAndAdvance() {
    showSkipInstallConfirm = false;
    advance();
  }

  async function installNowAndAdvance() {
    showSkipInstallConfirm = false;
    await runInstall(false);
    if (installed) advance();
  }

  function cancelSkipInstall() {
    showSkipInstallConfirm = false;
  }
  function prev() { if (step > 1) step = (step - 1) as any; }
  function skip() { finish(); }

  $effect(() => {
    if (step === 2 && !detection) void loadStep2();
    if (step === 3) void loadStep3();
    if (step === 4) void loadStep4();
  });

  onMount(() => {
    // Caller decides whether to open; we only check the flag if asked.
  });

  export function shouldShow(): boolean {
    try { return localStorage.getItem(KEY) !== 'true'; }
    catch { return true; }
  }
</script>

<!-- Bug 26: native <dialog> top-layer rendering via DialogRoot. -->
<DialogRoot bind:open width="600px" closeOnEscape={false} closeOnBackdrop={false}>
  {#snippet header()}
      <header class="ow-header">
        <h2>Welcome to VibeCoded Tools</h2>
        <ol class="ow-steps">
          <li class:active={step >= 1} class:current={step === 1}>1. Welcome</li>
          <li class:active={step >= 2} class:current={step === 2}>2. System</li>
          <li class:active={step >= 3} class:current={step === 3}>3. Containers</li>
          <li class:active={step >= 4} class:current={step === 4}>4. First project</li>
        </ol>
      </header>
  {/snippet}
  {#snippet body()}
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
          <!-- Bug 27: removed the "Copying from <repo path>" line — that
               leaked an internal implementation detail (the bundled repo
               source path) the user doesn't need. We still keep the
               sourceError surfaced so install failures get explained. -->
          {#if sourceError}
            <p class="ow-error">Source not found: {sourceError}</p>
          {/if}

          <!-- Bug 29: shared-container detection. We probed the three
               default service endpoints (Weaviate / Ollama / code_embed) on
               step entry — show the user what was found so they understand
               the install will reuse them, not start duplicates that would
               port-conflict. -->
          {#if services}
            <div class="ow-services" class:ok={services.all_detected}>
              <h3>Shared services {services.all_detected ? '(detected)' : ''}</h3>
              <ul class="ow-services-list">
                <li>
                  {services.weaviate_url ? '✓' : '·'} Weaviate
                  <code class="ow-mono">
                    {services.weaviate_url ?? 'http://localhost:8081 (not running)'}
                  </code>
                </li>
                <li>
                  {services.ollama_url ? '✓' : '·'} Ollama
                  <code class="ow-mono">
                    {services.ollama_url ?? 'http://localhost:11435 (not running)'}
                  </code>
                </li>
                <li>
                  {services.code_embed_url ? '✓' : '·'} code_embed
                  <code class="ow-mono">
                    {services.code_embed_url ?? 'http://localhost:11440 (not running)'}
                  </code>
                </li>
              </ul>
              {#if services.all_detected}
                <p class="ow-secondary">
                  Your install will reuse these. Per-install isolation comes from
                  separate Knowledge Graph collections inside the shared Weaviate.
                </p>
              {:else if services.none_detected}
                <p class="ow-secondary">
                  No services detected — the install will start them via your
                  container runtime.
                </p>
              {:else}
                <p class="ow-secondary">
                  Some services detected — the install will reuse those and start
                  any that are missing.
                </p>
              {/if}
              <label class="ow-checkbox" title="Most users should leave this off.">
                <input type="checkbox" bind:checked={useSeparateContainers} />
                <span>Use separate containers for this install (advanced)</span>
              </label>
              {#if useSeparateContainers}
                <p class="ow-secondary ow-warn">
                  You'll need to set <code class="ow-mono">VCT_FORCE_SEPARATE_CONTAINERS=1</code>
                  and pick non-default ports (WEAVIATE_PORT / OLLAMA_PORT /
                  CODE_EMBED_PORT) in your environment before running the install.
                </p>
              {/if}
            </div>
          {:else if servicesError}
            <p class="ow-secondary">Couldn't probe services ({servicesError}).</p>
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
          <!-- Bug 28: collect name + path here so the user actually
               ends up with a project record after Finish. Previously
               step 4 was a "go do it yourself" prompt and a user who
               clicked Finish without manually creating a project ended
               up with an empty Projects list. -->
          <p>Create your first project.</p>
          <p class="ow-secondary">
            We'll register it in the launcher and (if needed) install the
            orchestrator at this folder so the project is ready to use.
          </p>

          <label class="ow-label">
            <span>Project name</span>
            <input
              type="text"
              bind:value={projectName}
              placeholder="my-project"
            />
          </label>
          <label class="ow-label">
            <span>Project folder</span>
            <div class="ow-path-row">
              <input
                type="text"
                class="ow-path-input"
                bind:value={projectPath}
                oninput={() => (projectPathTouched = true)}
                placeholder="~/code/my-project"
              />
              <button
                type="button"
                class="ow-btn"
                onclick={browseProjectFolder}
                disabled={!inTauri}
                title={inTauri ? 'Browse for folder' : 'Browse requires the desktop app'}
              >
                Browse…
              </button>
            </div>
          </label>
          <p class="ow-secondary">
            Absolute path. Folder will be created if it doesn't exist.
            {#if !inTauri}(Browse requires the desktop app — type the path manually here.){/if}
          </p>
          {#if projectError}<p class="ow-error">{projectError}</p>{/if}

          <!-- Bug 22: optional GitHub access for future auto-update flow. -->
          <div class="ow-pat">
            <h3>Updates (optional)</h3>
            <p class="ow-secondary">
              Local install + per-project updates work without a GitHub
              token. Adding a read-only token now will let the launcher
              fetch newer orchestrator versions from upstream once
              auto-update lands. No token = 60 GitHub API requests per
              hour (anonymous). With token = 5000/hour.
            </p>
            {#if patSaved}
              <p class="ow-ok">Token saved to <code class="ow-mono">~/.vct-secrets/github_pat</code>.</p>
            {:else}
              <label class="ow-label">
                <span>GitHub token (optional)</span>
                <input type="password" bind:value={githubPat} placeholder="ghp_…" />
              </label>
              <p class="ow-secondary">
                How to get one: github.com → Settings → Developer settings →
                Personal access tokens → Generate new (classic). Scope
                <code class="ow-mono">public_repo</code> is enough.
              </p>
              <div class="ow-pat-actions">
                <button class="ow-btn-primary" onclick={savePat} disabled={savingPat || !githubPat.trim()}>
                  {savingPat ? 'Saving…' : 'Save token'}
                </button>
              </div>
              {#if patError}<p class="ow-error">{patError}</p>{/if}
            {/if}
          </div>
        {/if}
  {/snippet}
  {#snippet footer()}
      <div class="ow-footer">
        <button class="ow-btn-link" onclick={skip}>Skip onboarding</button>
        <div class="ow-nav">
          {#if step > 1}<button class="ow-btn" onclick={prev}>Back</button>{/if}
          {#if step < 4}
            <button class="ow-btn-primary" onclick={next}>Next →</button>
          {:else}
            <button class="ow-btn-primary" onclick={finish} disabled={creatingProject}>
              {creatingProject ? 'Creating…' : 'Finish'}
            </button>
          {/if}
        </div>
      </div>
  {/snippet}
</DialogRoot>

<!-- Bug 28: explicit skip-install confirmation. Triggered when the user
     hits Next on step 3 without having installed the orchestrator. We
     refuse to silently advance — the user picks Install, Skip, or
     Cancel. -->
{#if showSkipInstallConfirm}
<DialogRoot
  open={true}
  width="460px"
  onClose={cancelSkipInstall}
>
  {#snippet header()}
    <h2 style="margin:0;font-size:15px;">Install the orchestrator?</h2>
  {/snippet}
  {#snippet body()}
    <p style="margin:0 0 10px;color:#ccc;">
      You haven't installed the orchestrator at
      <code class="ow-mono">{installPath}</code> yet.
    </p>
    <p style="margin:0 0 10px;color:#888;font-size:12px;">
      Without it, the project you create on the next step will be a bare
      folder — Knowledge Graph, Code Graph, and hooks will not work
      until you install later from the dashboard.
    </p>
  {/snippet}
  {#snippet footer()}
    <div class="ow-confirm-actions">
      <button class="ow-btn" onclick={cancelSkipInstall} disabled={installing}>
        Cancel
      </button>
      <button class="ow-btn" onclick={skipInstallAndAdvance} disabled={installing}>
        Skip and install later
      </button>
      <button class="ow-btn-primary" onclick={installNowAndAdvance} disabled={installing || !!sourceError}>
        {installing ? 'Installing…' : 'Install now'}
      </button>
    </div>
  {/snippet}
</DialogRoot>
{/if}

<style>
  /* Bug 26: backdrop / centering / max-height now handled by DialogRoot.
     This block only owns the wizard-specific header / body / footer
     layout (step indicator, button rows). */
  .ow-header h2 { margin: 0 0 12px; font-size: 16px; }
  .ow-steps { list-style: none; padding: 0; margin: 0; display: flex; gap: 8px; font-size: 11px; color: #666; }
  .ow-steps li.active { color: #c4b3ff; }
  .ow-steps li.current { color: #0fc; font-weight: 600; }
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
  .ow-footer { display: flex; justify-content: space-between; align-items: center; }
  .ow-path-row { display: flex; gap: 6px; align-items: stretch; }
  .ow-path-input { flex: 1; }
  .ow-confirm-actions { display: flex; gap: 6px; justify-content: flex-end; }
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

  /* Bug 22: optional GitHub PAT section */
  .ow-pat { margin-top: 14px; padding: 10px 12px; border: 1px solid rgba(255,255,255,0.08); border-radius: 6px; background: rgba(255,255,255,0.02); }
  .ow-pat h3 { font-size: 13px; margin: 0 0 6px; color: #ccc; }
  .ow-pat input { width: 100%; }
  .ow-pat-actions { display: flex; justify-content: flex-end; margin-top: 8px; }

  /* Bug 29: shared-container detection panel on step 3. */
  .ow-services { margin-top: 12px; padding: 10px 12px; border: 1px solid rgba(255,255,255,0.08); border-radius: 6px; background: rgba(255,255,255,0.02); }
  .ow-services.ok { border-color: rgba(0,191,166,0.25); background: rgba(0,191,166,0.05); }
  .ow-services h3 { font-size: 13px; margin: 0 0 6px; color: #ccc; }
  .ow-services-list { list-style: none; padding: 0; margin: 0 0 6px; font-size: 12px; }
  .ow-services-list li { padding: 2px 0; color: #ccc; display: flex; gap: 8px; align-items: baseline; }
  .ow-checkbox { display: flex; gap: 6px; align-items: center; margin-top: 8px; font-size: 12px; color: #ccc; cursor: pointer; }
  .ow-checkbox input { margin: 0; }
  .ow-warn { color: #ffb84a; }
</style>
