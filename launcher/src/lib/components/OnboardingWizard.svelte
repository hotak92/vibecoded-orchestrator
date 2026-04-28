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

  // Bug 31: volume location picker state. Loaded once on step 3 entry.
  // - When existing volumes are detected, the picker is REPLACED by a
  //   read-only info panel and the user can NOT pick a custom path
  //   (Bug 32 contract: no override generated when volumes already exist).
  // - When no volumes exist, the user picks default vs custom.
  interface VolumeWithSize {
    name: string;
    mountpoint: string;
    size_bytes: number | null;
    size_human: string | null;
    role: string;
  }
  interface VolumesConfig {
    volumes_path: string;
    mode: string; // "default" | "detected" | "custom"
    legacy_mapping: { volume_name: string; mountpoint: string; role: string }[];
    total_size_human: string | null;
    volumes: VolumeWithSize[];
  }
  let volumesConfig = $state<VolumesConfig | null>(null);
  let volumesError = $state<string | null>(null);
  let volumeChoice = $state<'default' | 'custom'>('default');
  let customVolumesPath = $state('');
  let volumesPickError = $state<string | null>(null);

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
  let creatingProject = $state(false);
  let projectError = $state<string | null>(null);

  // 2026-04-28 fix: do NOT auto-fill projectPath with a templated value.
  // Earlier code wrote `${suggestedRoot}/${slug}` into the field as a real
  // value, which (a) looked like a concrete location the user had to delete
  // before typing their own (`/home/martino/code/agape`), and (b) implied
  // the orchestrator expected projects under `~/code/` even when the user
  // had no such directory. Now: leave the field blank, show only a generic
  // `path/to/project` placeholder, and let the user type or click Browse.
  function slugify(s: string): string {
    return s
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9._-]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 64);
  }

  async function loadStep4() {
    // No-op for step 4 right now; kept for the lifecycle hook contract.
    // Previously resolved a default project-root via suggestProjectFolder,
    // but per the no-templated-default decision above we don't pre-fill
    // anything — the user picks via Browse or types an absolute path.
  }

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

  // Re-install conflict resolution (4-option modal). When the install
  // target already contains orchestrator files and the wizard hasn't
  // picked a strategy yet, the Rust install_orchestrator command returns
  // a JSON-encoded `InstallConflictError` in its Err variant. We parse
  // it here, render the 4 options, then re-invoke install_orchestrator
  // with the chosen `conflict: { strategy, preserve_paths }`.
  type ConflictStrategy =
    | 'delete_claude_and_reinstall'
    | 'overwrite_all'
    | 'overwrite_preserve'
    | 'adopt_as_is';
  interface InstallConflictError {
    kind: 'install_conflict';
    message: string;
    install_path: string;
    source_path: string;
    mode: InstallMode;
    will_overwrite: string[];
    will_add: string[];
    preserve_candidates: string[];
  }
  // Default preserve list — keep in sync with DEFAULT_PRESERVE_LIST in
  // launcher/src-tauri/src/commands/installer.rs and install.py.
  const DEFAULT_PRESERVE_LIST: string[] = [
    'CLAUDE.md',
    '.claude/CONTEXT_STATE.md',
    '.claude/PROJECT_REGISTRY.md',
    '.env',
  ];
  let pendingConflict = $state<InstallConflictError | null>(null);
  let conflictStrategy = $state<ConflictStrategy>('overwrite_preserve');
  // Editable preserve list (one path per line). Initialised from the
  // default list when the modal opens; the user can override.
  let conflictPreserveText = $state<string>(DEFAULT_PRESERVE_LIST.join('\n'));
  let conflictShowPreserveEditor = $state(false);

  /**
   * Try to parse a Tauri Err string as an InstallConflictError. Tauri
   * surfaces our JSON-encoded error wrapped as a plain string, so we
   * detect the leading `{"kind":"install_conflict"...}` shape.
   */
  function tryParseConflictError(s: string): InstallConflictError | null {
    if (!s.includes('"kind":"install_conflict"')) return null;
    try {
      const parsed = JSON.parse(s);
      if (parsed && parsed.kind === 'install_conflict') {
        return parsed as InstallConflictError;
      }
    } catch {
      // Some Tauri runtimes prepend a label like `Error: `. Strip and retry.
      const stripped = s.replace(/^[^{]+/, '');
      try {
        const parsed = JSON.parse(stripped);
        if (parsed && parsed.kind === 'install_conflict') {
          return parsed as InstallConflictError;
        }
      } catch {
        return null;
      }
    }
    return null;
  }

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
    // Bug 31: probe existing volumes so the picker can render either the
    // chooser (no volumes) or the read-only info panel (volumes exist).
    try {
      volumesConfig = await invoke<VolumesConfig>('get_volumes_config');
    } catch (e) {
      volumesError = String(e);
    }
  }

  // Bug 31: ask the user to pick a folder for custom volumes.
  async function pickCustomVolumesFolder() {
    volumesPickError = null;
    const picked = await pickDirectory({
      title: 'Pick a folder for container volumes',
    });
    if (picked) {
      customVolumesPath = picked;
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

  async function runInstall(
    confirmOverwrite = false,
    conflict: { strategy: ConflictStrategy; preserve_paths?: string[] } | null = null,
  ) {
    installing = true;
    installError = null;
    try {
      // Bug 31: persist the volume location BEFORE install_orchestrator
      // touches anything. The backend ignores the path argument when
      // existing volumes are detected (Bug 32 contract — no override
      // generated) and falls back to the "detected" branch.
      // Treat picker errors as install errors so the user sees them.
      try {
        const chosenPath =
          volumesConfig?.mode === 'detected' || volumeChoice === 'default'
            ? 'default'
            : customVolumesPath.trim();
        if (volumeChoice === 'custom' && !customVolumesPath.trim()) {
          throw new Error('Pick a custom volumes folder or switch back to Default.');
        }
        volumesConfig = await invoke<VolumesConfig>('set_volumes_config_for_install', {
          path: chosenPath,
        });
      } catch (e) {
        volumesPickError = String(e);
        installError = String(e);
        installing = false;
        return;
      }

      // Tauri serializes the command arg name (`config`) as the JSON key,
      // so the payload must wrap install_path inside `config`. Earlier
      // versions sent `{ path, config: {} }` which Tauri rejected as
      // "missing field 'install_path'".
      //
      // Conflict resolution: if `conflict` is null AND the target is an
      // Adopt-target, the Rust side returns InstallConflictError which we
      // catch below to render the 4-option modal.
      await invoke('install_orchestrator', {
        config: buildInstallConfig(),
        confirmOverwrite,
        conflict,
      });
      installed = true;
      pendingDiff = null;
      pendingConflict = null;
      toast.success('Orchestrator installed');
    } catch (e) {
      const raw = String(e);
      const conflictErr = tryParseConflictError(raw);
      if (conflictErr) {
        // Hand off to the conflict modal. installing = false so the
        // Install button is re-enabled if the user cancels.
        pendingConflict = conflictErr;
        conflictStrategy = 'overwrite_preserve';
        conflictPreserveText = DEFAULT_PRESERVE_LIST.join('\n');
        conflictShowPreserveEditor = false;
      } else {
        installError = raw;
      }
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
   * Apply the chosen conflict strategy. Re-invokes runInstall with an
   * explicit `conflict` object — that bypasses the conflict-detect path
   * in install_orchestrator and runs the strategy directly.
   */
  // Set when applyConflictResolution should also run projects.create()
  // afterwards (= conflict fired from step 4 / finish(), not step 3).
  let conflictResumeStep4 = $state(false);

  async function applyConflictResolution() {
    if (!pendingConflict) return;
    const conflict = pendingConflict;
    const preserve_paths =
      conflictStrategy === 'overwrite_preserve'
        ? conflictPreserveText
            .split('\n')
            .map((s) => s.trim())
            .filter(Boolean)
        : undefined;
    pendingConflict = null;
    if (conflictResumeStep4) {
      // Came from step 4 — call install_orchestrator directly with the
      // strategy, then resume the project-creation we were halfway
      // through. runInstall(false, ...) is the step-3 helper and would
      // also try to advance the wizard, which we don't want here.
      conflictResumeStep4 = false;
      creatingProject = true;
      try {
        await invoke('install_orchestrator', {
          config: {
            install_path: conflict.install_path,
            use_gpu: false,
            cpu_only: false,
            openai_key: null,
            container_runtime: null,
            skip_containers: true,
          },
          confirmOverwrite: false,
          conflict: { strategy: conflictStrategy, preserve_paths },
        });
        await projects.create(projectName.trim(), conflict.install_path, 'base');
        toast.success(`Project "${projectName.trim()}" created`);
        // Trigger the same close-on-success flow as the regular finish.
        try { localStorage.setItem(KEY, 'true'); } catch {}
        open = false;
        onComplete?.();
      } catch (e) {
        projectError = e instanceof Error ? e.message : String(e);
      } finally {
        creatingProject = false;
      }
      return;
    }
    await runInstall(false, {
      strategy: conflictStrategy,
      preserve_paths,
    });
  }

  function cancelConflictResolution() {
    pendingConflict = null;
    conflictResumeStep4 = false;
    installing = false;
    creatingProject = false;
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
            conflict: null,
          });
        }
        await projects.create(name, path, 'base');
        toast.success(`Project "${name}" created`);
      } catch (e) {
        // The inline install we kicked off above can return an
        // InstallConflictError when the project path already has
        // orchestrator files (e.g. user picked an existing dir). The
        // step-3 install handler (line ~329) renders the modal in that
        // case; the step-4 path needs the same treatment, otherwise the
        // user sees a raw JSON blob in the project-error string.
        // Reported 2026-04-28 from real wizard test on Agape.
        const raw = e instanceof Error ? e.message : String(e);
        const conflictErr = tryParseConflictError(raw);
        if (conflictErr) {
          // Pre-fill the modal state and flag conflictResumeStep4 so
          // applyConflictResolution() resumes the project-creation
          // we were halfway through (instead of the step-3 install
          // pipeline, which is the default code path).
          pendingConflict = conflictErr;
          conflictStrategy = 'overwrite_preserve';
          conflictPreserveText = (conflictErr.preserve_candidates && conflictErr.preserve_candidates.length > 0
            ? conflictErr.preserve_candidates
            : DEFAULT_PRESERVE_LIST).join('\n');
          conflictShowPreserveEditor = false;
          conflictResumeStep4 = true;
          creatingProject = false;
          return;
        }
        projectError = raw;
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
    // Tilde expansion at the JS layer is best-effort — the user may have
    // typed `~/something` manually. The Rust side handles real path
    // resolution canonically, but we'd rather not pass `~/...` raw.
    // We don't have the home dir cached anymore (suggestedRoot was
    // removed when auto-fill went away). Pass through unchanged and let
    // the backend resolve, OR (future) call homeDirectory() from
    // dialog.ts before submission. For v1 the typed path is most often
    // an absolute /home/... or /Users/... path anyway.
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
  // Skip = user wants to dismiss the wizard entirely. Don't go through
  // finish() — that runs install + project-create which can hit a
  // conflict modal and trap the user. Skip just sets the
  // onboarding-complete flag and closes. The project list will be empty
  // until the user creates one from the main UI; that's fine — better
  // than a wizard that won't go away. Reported 2026-04-28.
  function skip() {
    try { localStorage.setItem(KEY, 'true'); } catch {}
    pendingConflict = null;
    conflictResumeStep4 = false;
    creatingProject = false;
    open = false;
    onComplete?.();
  }

  $effect(() => {
    if (step === 2 && !detection) void loadStep2();
    if (step === 3) void loadStep3();
    if (step === 4) void loadStep4();
  });

  // Skip the install steps when the launcher is running from inside an
  // already-installed orchestrator. The user just ran `bash first-install.sh`;
  // they don't want a "Welcome → System → Containers → Install at /home/.../
  // vibecoded-orchestrator" flow. They want to register their first project.
  // Reported 2026-04-27 from real install testing — the default-path field
  // was being concatenated with user-typed absolute paths and producing
  // garbage like /home/.../vibecoded-orch/home/.../Agape/Code.
  let alreadyInstalledRoot = $state<string | null>(null);
  let preflightChecked = $state(false);
  async function preflightSkipIfAlreadyInstalled() {
    if (!inTauri) {
      preflightChecked = true;
      return;
    }
    try {
      const root = await invoke<string | null>('detect_existing_install_root');
      if (root) {
        alreadyInstalledRoot = root;
        installPath = root;
        installed = true;
        // Jump straight to step 4 — first project. The user is past
        // installing the orchestrator; they need to register a project.
        step = 4;
      }
    } catch {
      // Non-fatal — fall through to the normal wizard flow.
    } finally {
      preflightChecked = true;
    }
  }

  onMount(() => {
    // Caller decides whether to open; we only check the flag if asked.
    void preflightSkipIfAlreadyInstalled();
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
          {#if alreadyInstalledRoot}
            <!-- 2026-04-28: when the user navigated Back to step 3 from
                 the auto-skipped step 4, the install path field default
                 (~/vibecoded-orchestrator) is misleading — vco is
                 already installed at $alreadyInstalledRoot. Show the
                 path read-only and don't expose a re-install workflow.
                 Re-running install at a different path here would
                 either create a SECOND install or break the existing
                 git checkout. Proper relocate is a copy + venv-rebuild +
                 path-rewrite — out of scope for v1. -->
            <p class="ow-secondary">
              <strong>Already installed at</strong>
              <code class="ow-mono">{alreadyInstalledRoot}</code>.
            </p>
          {:else}
            <label class="ow-label">
              <span>Install path</span>
              <input bind:value={installPath} />
            </label>
          {/if}
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
                  Your install will <strong>reuse these running services</strong>.
                  Project-level isolation comes from per-project Knowledge Graph
                  and Code Graph collections inside the shared Weaviate — your
                  data won't mix with other projects', and each project sees
                  only what it has access to.
                </p>
                <p class="ow-secondary">
                  This is the recommended setup. Memory/CPU footprint stays
                  flat as you add projects (one Weaviate, one Ollama, one
                  embedding service for everything).
                </p>
              {:else if services.none_detected}
                <p class="ow-secondary">
                  No services detected — the install will start fresh ones via
                  your container runtime. Future projects on this machine will
                  reuse the same instances unless you opt into the advanced
                  separate-containers mode below.
                </p>
              {:else}
                <p class="ow-secondary">
                  Some services detected — the install will reuse those and
                  start any that are missing.
                </p>
              {/if}
              <label class="ow-checkbox" title="Most users should leave this off — see the warning text when checked.">
                <input type="checkbox" bind:checked={useSeparateContainers} />
                <span>Use separate containers for this install (advanced)</span>
              </label>
              {#if useSeparateContainers}
                <p class="ow-secondary ow-warn">
                  <strong>What this does:</strong> spawns a <em>new</em>
                  Weaviate, Ollama and code-embed instance dedicated to this
                  install, instead of adopting the running ones. They will
                  bind to non-default ports — set <code class="ow-mono">WEAVIATE_PORT</code>,
                  <code class="ow-mono">OLLAMA_PORT</code>, <code class="ow-mono">CODE_EMBED_PORT</code>,
                  and <code class="ow-mono">VCT_FORCE_SEPARATE_CONTAINERS=1</code> in your
                  environment before clicking Install.
                </p>
                <p class="ow-secondary ow-warn">
                  <strong>When to use it:</strong> air-gapped per-project
                  installs (different KG schema, different model versions,
                  different storage class). Cost: 2-4 GB extra RAM per
                  install, fully duplicated model files on disk.
                </p>
                <p class="ow-secondary ow-warn">
                  Project-level isolation does NOT need this — that's already
                  handled by per-project KG collections in the shared
                  Weaviate.
                </p>
              {/if}
            </div>
          {:else if servicesError}
            <p class="ow-secondary">Couldn't probe services ({servicesError}).</p>
          {/if}

          <!-- Bug 31: container volumes location. Two render paths:
                  - existing volumes detected: read-only info panel,
                    no picker. Migration is via Settings → Preferences.
                  - no volumes: pick default vs custom. -->
          {#if volumesConfig}
            <div class="ow-volumes">
              <h3>Container volumes location</h3>
              {#if volumesConfig.mode === 'detected' && volumesConfig.volumes.length > 0}
                <p class="ow-secondary">
                  Existing volumes detected — keeping them in place. To move
                  them later, use Settings → Preferences → Shared services →
                  Volume location.
                </p>
                <ul class="ow-volumes-list">
                  {#each volumesConfig.volumes as v}
                    <li>
                      <span class="ow-vol-role">{v.role}</span>
                      <code class="ow-mono">{v.mountpoint}</code>
                      {#if v.size_human}
                        <span class="ow-vol-size">{v.size_human}</span>
                      {/if}
                    </li>
                  {/each}
                </ul>
              {:else}
                <p class="ow-secondary">
                  Where Weaviate's vector index, Ollama's models, and the
                  code-embed cache live. Defaults to your container engine's
                  standard location. Move it if you have limited disk on
                  <code class="ow-mono">$HOME</code>.
                </p>
                <p class="ow-secondary">
                  <strong>Note:</strong> this only matters when this install
                  spawns its own containers. If shared services were detected
                  above and you're reusing them, this setting is ignored —
                  the existing volumes the running services already use stay
                  in place.
                </p>
                <label class="ow-radio">
                  <input
                    type="radio"
                    name="volumes-choice"
                    value="default"
                    bind:group={volumeChoice}
                  />
                  <span>
                    Default (managed by your container engine):
                    <code class="ow-mono">~/.local/share/containers/storage/volumes/</code>
                  </span>
                </label>
                <label class="ow-radio">
                  <input
                    type="radio"
                    name="volumes-choice"
                    value="custom"
                    bind:group={volumeChoice}
                  />
                  <span>Custom path:</span>
                </label>
                {#if volumeChoice === 'custom'}
                  <div class="ow-volumes-custom">
                    <input
                      type="text"
                      bind:value={customVolumesPath}
                      placeholder="/mnt/big-disk/vct-volumes"
                    />
                    <button class="ow-btn" onclick={pickCustomVolumesFolder} type="button">
                      Browse…
                    </button>
                  </div>
                  {#if volumesPickError}
                    <p class="ow-error">{volumesPickError}</p>
                  {/if}
                {/if}
              {/if}
            </div>
          {:else if volumesError}
            <p class="ow-secondary">Couldn't probe volumes ({volumesError}).</p>
          {/if}

          {#if installed}
            <p class="ow-ok">Orchestrator already installed at this path.</p>
          {:else}
            <button class="ow-btn-primary" onclick={() => runInstall(false)} disabled={installing || !!sourceError}>
              {installing ? 'Installing…' : 'Install'}
            </button>
          {/if}
          {#if installError}<p class="ow-error">{installError}</p>{/if}
          <!-- Legacy pendingDiff inline modal removed: conflict resolution
               now uses the dedicated 4-option modal rendered below the
               wizard via DialogRoot (see `pendingConflict`). The script
               still exposes `pendingDiff` / `confirmAdopt` / `cancelAdopt`
               so unrelated callers keep compiling, but nothing sets
               `pendingDiff` anymore — `runInstall` skips preview_install
               entirely and lets the Rust install_orchestrator surface a
               structured `InstallConflictError` instead. -->
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
                placeholder="/path/to/your/project"
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

<!-- Re-install conflict resolution modal. Triggered when the user
     clicks Install on a path that already contains orchestrator files.
     The Rust install_orchestrator command returns an InstallConflictError
     in that case; we parse it out of the Err string in `runInstall` and
     populate `pendingConflict` to show this dialog. -->
{#if pendingConflict}
<DialogRoot
  open={true}
  width="540px"
  onClose={cancelConflictResolution}
>
  {#snippet header()}
    <h2 style="margin:0;font-size:15px;">
      Existing install detected at
      <code class="ow-mono" style="font-size:11px;">{pendingConflict!.install_path}</code>
    </h2>
  {/snippet}
  {#snippet body()}
    <p style="margin:0 0 12px;color:#ccc;font-size:13px;">
      The install path contains orchestrator files (<code>.claude/</code>,
      <code>knowledge/</code>, etc.). Choose how to handle the conflict:
    </p>

    {#if pendingConflict!.preserve_candidates.length > 0}
      <p class="ow-secondary" style="margin:0 0 10px;">
        Preserve-list files at the target ({pendingConflict!.preserve_candidates.length}):
        {#each pendingConflict!.preserve_candidates as p, i}
          <code class="ow-mono" style="margin-left:4px;">{p}</code>{i < pendingConflict!.preserve_candidates.length - 1 ? ',' : ''}
        {/each}
      </p>
    {/if}

    <div class="ow-conflict-options">
      <label class="ow-conflict-option">
        <input type="radio" bind:group={conflictStrategy} value="overwrite_preserve" />
        <div class="ow-conflict-text">
          <span class="ow-conflict-title">
            Overwrite, preserving project-specific files
            <span class="ow-conflict-recommend">Recommended</span>
          </span>
          <span class="ow-conflict-desc">
            Copy new files but leave the preserve list alone. The upstream
            versions are written next to them as <code>&lt;file&gt;.new.&lt;ext&gt;</code>;
            a notification block is appended to <code>.claude/CONTEXT_STATE.md</code>
            so Claude can merge them on your next session.
          </span>
        </div>
      </label>

      <label class="ow-conflict-option">
        <input type="radio" bind:group={conflictStrategy} value="overwrite_all" />
        <div class="ow-conflict-text">
          <span class="ow-conflict-title">Overwrite all</span>
          <span class="ow-conflict-desc">
            Copy every tracked install file on top — no preservation.
            Loses your edits to <code>CLAUDE.md</code>, <code>CONTEXT_STATE.md</code>,
            <code>PROJECT_REGISTRY.md</code>, <code>.env</code>, etc.
          </span>
        </div>
      </label>

      <label class="ow-conflict-option">
        <input type="radio" bind:group={conflictStrategy} value="delete_claude_and_reinstall" />
        <div class="ow-conflict-text">
          <span class="ow-conflict-title">Delete and replace <code>.claude/</code></span>
          <span class="ow-conflict-desc">
            Wipes ONLY the destination's <code>.claude/</code> directory
            (the rest of the install path is left alone), then performs a
            fresh install. Use this when <code>.claude/</code> is corrupt
            and you want a clean slate.
          </span>
        </div>
      </label>

      <label class="ow-conflict-option">
        <input type="radio" bind:group={conflictStrategy} value="adopt_as_is" />
        <div class="ow-conflict-text">
          <span class="ow-conflict-title">Adopt as-is</span>
          <span class="ow-conflict-desc">
            Keep the existing files exactly as they are; just register the
            project in the launcher. Use this when the install at this
            path is already current.
          </span>
        </div>
      </label>
    </div>

    {#if conflictStrategy === 'overwrite_preserve'}
      <div class="ow-conflict-preserve">
        <button
          type="button"
          class="ow-btn-link"
          style="padding:0;font-size:11px;color:#0fc;"
          onclick={() => (conflictShowPreserveEditor = !conflictShowPreserveEditor)}
        >
          {conflictShowPreserveEditor ? '▾' : '▸'} Files that will be preserved
          ({conflictPreserveText.split('\n').filter((s) => s.trim()).length})
        </button>
        {#if conflictShowPreserveEditor}
          <p class="ow-secondary" style="margin:6px 0 4px;font-size:11px;">
            One install-relative path per line. Defaults below — edit only if
            you know what you're doing.
          </p>
          <textarea
            class="ow-conflict-preserve-input"
            bind:value={conflictPreserveText}
            rows="5"
          ></textarea>
        {/if}
      </div>
    {/if}
  {/snippet}
  {#snippet footer()}
    <div class="ow-confirm-actions">
      <button class="ow-btn" onclick={cancelConflictResolution} disabled={installing}>
        Cancel
      </button>
      <button class="ow-btn-primary" onclick={applyConflictResolution} disabled={installing}>
        {installing ? 'Applying…' : 'Apply'}
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

  /* Legacy `.ow-diff*` selectors removed alongside the inline pendingDiff
     modal — see the comment above `pendingConflict` in the markup. */

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

  /* Bug 31: container volumes location picker. */
  .ow-volumes { margin-top: 12px; padding: 10px 12px; border: 1px solid rgba(255,255,255,0.08); border-radius: 6px; background: rgba(255,255,255,0.02); }
  .ow-volumes h3 { font-size: 13px; margin: 0 0 6px; color: #ccc; }
  .ow-volumes-list { list-style: none; padding: 0; margin: 0 0 6px; font-size: 12px; }
  .ow-volumes-list li { padding: 2px 0; color: #ccc; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
  .ow-vol-role { display: inline-block; min-width: 80px; color: #c4b3ff; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
  .ow-vol-size { color: #888; font-size: 11px; }
  .ow-radio { display: flex; gap: 8px; align-items: flex-start; margin-top: 6px; font-size: 12px; color: #ccc; cursor: pointer; }
  .ow-radio input { margin: 3px 0 0; flex-shrink: 0; }
  .ow-volumes-custom { display: flex; gap: 6px; margin: 6px 0 0 22px; }
  .ow-volumes-custom input[type="text"] { flex: 1; padding: 4px 8px; font-family: ui-monospace, monospace; font-size: 12px; background: rgba(0,0,0,0.3); color: #eee; border: 1px solid rgba(255,255,255,0.08); border-radius: 4px; }

  /* Re-install conflict resolution modal — 4-option radio list. */
  .ow-conflict-options { display: flex; flex-direction: column; gap: 8px; margin: 8px 0 12px; }
  .ow-conflict-option {
    display: flex; gap: 10px; align-items: flex-start;
    padding: 10px 12px; border: 1px solid rgba(255,255,255,0.08); border-radius: 6px;
    background: rgba(255,255,255,0.02); cursor: pointer;
  }
  .ow-conflict-option:hover { border-color: rgba(0,191,166,0.3); background: rgba(0,191,166,0.04); }
  .ow-conflict-option input[type="radio"] { margin: 3px 0 0; flex-shrink: 0; accent-color: rgb(0,191,166); }
  .ow-conflict-text { display: flex; flex-direction: column; gap: 4px; flex: 1; }
  .ow-conflict-title { font-size: 13px; color: #eee; font-weight: 500; display: flex; gap: 8px; align-items: baseline; }
  .ow-conflict-recommend {
    font-size: 10px; font-weight: 600; padding: 1px 6px; border-radius: 8px;
    background: rgba(0,191,166,0.18); color: #0fc; text-transform: uppercase; letter-spacing: 0.04em;
  }
  .ow-conflict-desc { font-size: 11px; color: #aaa; line-height: 1.4; }
  .ow-conflict-desc code {
    background: rgba(255,255,255,0.06); padding: 0 4px; border-radius: 3px;
    font-family: ui-monospace, monospace; font-size: 10.5px; color: #c4b3ff;
  }
  .ow-conflict-preserve { margin-top: 4px; padding: 8px 10px; border: 1px dashed rgba(255,255,255,0.1); border-radius: 5px; }
  .ow-conflict-preserve-input {
    width: 100%; padding: 6px 8px; font-family: ui-monospace, monospace; font-size: 11px;
    background: rgba(0,0,0,0.3); color: #eee; border: 1px solid rgba(255,255,255,0.08);
    border-radius: 4px; resize: vertical; box-sizing: border-box;
  }
</style>
