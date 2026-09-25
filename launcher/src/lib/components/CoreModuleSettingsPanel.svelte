<!--
  SPDX-License-Identifier: AGPL-3.0-or-later

  v0.2.97 — "Module settings": the `settings` block of every manifest the
  launcher knows — the modules bundled with it (`launcher/bundled_manifests/`)
  and the installed catalog modules — listed and, where the value lives in
  module settings, editable.

  Mount site: /preferences/modules, under the host-wide enable toggles.

  Each module also shows the HTTP APIs its manifest `provides`
  (`{ "kind": "http_api", "base_url": … }`), with the URL's placeholder
  RESOLVED by Rust (`PlaceholderCtx::resolve`: `vct-hub-api`'s `{hub_port}` →
  the running hub's port) — the one reader of `provides[].base_url` (R7b F11).

  Before this panel no launcher surface read a manifest's `settings` block,
  so `docs/VCT_MODULE_MANIFEST_SPEC.md` §8's "user-editable via the launcher
  GUI" was false — `vct-hub-api`'s machine-wide `VCT_HUB_PORT` could only be
  set from Rust code.

  One value, one home (round 2): each listed setting carries its binding
  (Rust `module_setting_bindings`).
    * `stored` — the value lives in module settings and a named reader reads
      it there: editable here.
    * `elsewhere` — the live value has another home (the project's KG
      binding, the machine's shared-KG pick, the service configuration): the
      LIVE value is shown read-only with where it is set and, when one
      exists, a button to its editor. `set_module_setting` refuses to store a
      copy.

  Reuse, not a second form:
    * storage is the generic `get_module_setting` / `set_module_setting`
      pair (`commands/module_gui.rs`), which route a DECLARED setting by its
      manifest scope and re-validate every write;
    * integer and free-text settings render with the config-tab renderer's
      own `NumberInputControl` / `TextInputControl` (given a `validate`
      check and, for a machine-wide setting, `projectId={null}`);
    * the few kinds those controls do not cover (options → select, boolean,
      multiselect) save through `saveModuleSetting`, the same command.
  All decisions live in `$lib/module-settings`, which is unit-tested.

  Scope:
    * Machine-wide settings (`scope: "global"`) have one value for this
      computer.
    * Per-project settings are for the project picked at the top (defaults
      to the launcher's selected project). An installed module's per-project
      settings are shown only for a project it is installed and enabled in.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { tauriAvailable } from '$lib/tauri';
  import { projects, selectedProject } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';
  import NumberInputControl from '$lib/components/module-controls/NumberInputControl.svelte';
  import TextInputControl from '$lib/components/module-controls/TextInputControl.svelte';
  import {
    checkSettingValue,
    editorHref,
    hasEditableMachineWide,
    isEditable,
    listModuleSettings,
    liveSettingValues,
    liveValueFor,
    loadModuleSetting,
    machineWideNote,
    moduleOffersProject,
    numberSettingControl,
    saveModuleSetting,
    settingLabel,
    settingProjectArg,
    settingWidget,
    splitByScope,
    textSettingControl,
    httpApiLines,
    type ListedModuleSettings,
    type ListedSetting,
    type LiveSettingValue,
    type ManifestSettingDecl,
  } from '$lib/module-settings';

  let modules = $state<ListedModuleSettings[]>([]);
  let live = $state<LiveSettingValue[]>([]);
  let loading = $state(true);
  let loadError = $state<string | null>(null);

  // The project per-project settings are edited for. `null` = follow the
  // launcher's selected project until the user picks one here.
  let pickedProjectId = $state<string | null>(null);
  const projectId = $derived(pickedProjectId ?? $selectedProject?.id ?? '');
  const projectName = $derived(
    $projects.projects.find((p) => p.id === projectId)?.name ?? 'the picked project',
  );
  const hasPerProject = $derived(
    modules.some((m) => splitByScope(m).perProject.length > 0),
  );

  // Values of the inline widgets (select / checkbox / multiselect), keyed by
  // `<module>:<key>:<project arg>`. The number/text controls load their own.
  let values = $state<Record<string, unknown>>({});
  let busy = $state<Record<string, boolean>>({});
  let errors = $state<Record<string, string>>({});

  function vkey(moduleId: string, decl: ManifestSettingDecl): string {
    return `${moduleId}:${decl.key}:${settingProjectArg(decl, projectId) ?? '<machine>'}`;
  }

  function isInline(decl: ManifestSettingDecl): boolean {
    const w = settingWidget(decl);
    return w === 'select' || w === 'checkbox' || w === 'multiselect';
  }

  function projectNames(ids: string[]): string {
    const names = ids.map((id) => $projects.projects.find((p) => p.id === id)?.name ?? id);
    return names.join(', ');
  }

  async function loadInlineValues(): Promise<void> {
    if (!tauriAvailable()) return;
    for (const m of modules) {
      for (const d of m.settings) {
        if (!isEditable(d) || !isInline(d)) continue;
        const arg = settingProjectArg(d, projectId);
        if (arg !== null && !moduleOffersProject(m, arg)) continue;
        const k = vkey(m.module_id, d);
        try {
          values[k] = await loadModuleSetting(m.module_id, d, projectId);
          errors[k] = '';
        } catch (e) {
          errors[k] = `Could not read the current value: ${e instanceof Error ? e.message : String(e)}`;
        }
      }
    }
  }

  async function loadLive(): Promise<void> {
    if (!tauriAvailable()) return;
    try {
      live = await liveSettingValues(projectId);
    } catch (e) {
      console.warn('[CoreModuleSettingsPanel] module_setting_live_values failed:', e);
      live = [];
    }
  }

  /**
   * Save an inline widget's value. On a refusal the widget is put back to
   * the stored value (`revert`) so it never shows a value that was not saved.
   */
  async function save(
    m: ListedModuleSettings,
    decl: ManifestSettingDecl,
    value: unknown,
    revert?: () => void,
  ) {
    const k = vkey(m.module_id, decl);
    busy[k] = true;
    errors[k] = '';
    try {
      await saveModuleSetting(m.module_id, decl, projectId, value);
      values[k] = value;
      toast.success(`Saved ${settingLabel(decl)}`);
    } catch (e) {
      errors[k] = e instanceof Error ? e.message : String(e);
      revert?.();
    } finally {
      busy[k] = false;
    }
  }

  function toggleChoice(
    m: ListedModuleSettings,
    decl: ManifestSettingDecl,
    option: string,
    checked: boolean,
    revert: () => void,
  ) {
    const current = Array.isArray(values[vkey(m.module_id, decl)])
      ? (values[vkey(m.module_id, decl)] as string[])
      : [];
    const next = checked
      ? [...current.filter((o) => o !== option), option]
      : current.filter((o) => o !== option);
    void save(m, decl, next, revert);
  }

  onMount(async () => {
    if (!tauriAvailable()) {
      loadError = 'Module settings require the desktop launcher.';
      loading = false;
      return;
    }
    try {
      await projects.load();
    } catch (e) {
      console.warn('[CoreModuleSettingsPanel] projects.load failed:', e);
    }
    try {
      modules = await listModuleSettings();
    } catch (e) {
      loadError = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  });

  // Re-read the inline widgets and the live values whenever the edited
  // project changes (the number/text controls re-mount through `{#key}`).
  $effect(() => {
    void projectId;
    void modules;
    void loadInlineValues();
    void loadLive();
  });
</script>

{#snippet readOnly(m: ListedModuleSettings, s: ListedSetting)}
  {@const lv = liveValueFor(live, m.module_id, s.key)}
  {@const href = editorHref(s.binding, projectId)}
  <div class="field field-readonly">
    <p class="field-label">
      {settingLabel(s)}
      <span class="badge badge-readonly">Set elsewhere</span>
    </p>
    <p class="field-live">
      {#if lv?.value}
        <code>{lv.value}</code>
      {:else}
        <span class="muted">—</span>
      {/if}
      {#if lv?.note}
        <span class="field-note">{lv.note}</span>
      {/if}
    </p>
    {#if s.binding.kind === 'elsewhere'}
      <p class="field-desc">{s.binding.home}</p>
      {#if href && s.binding.editor_label}
        <button type="button" class="editor-link" onclick={() => goto(href)}>
          {s.binding.editor_label} →
        </button>
      {/if}
    {/if}
    <p class="field-key"><code>{s.key}</code></p>
  </div>
{/snippet}

{#snippet field(m: ListedModuleSettings, decl: ListedSetting)}
  {@const arg = settingProjectArg(decl, projectId)}
  {@const k = vkey(m.module_id, decl)}
  {@const widget = settingWidget(decl)}
  {@const fieldId = `cms-${m.module_id}-${decl.key}`}
  {@const noProject = arg === ''}
  {#if !isEditable(decl)}
    {@render readOnly(m, decl)}
  {:else}
    <div class="field">
      {#if widget === 'number'}
        <NumberInputControl
          control={numberSettingControl(decl)}
          moduleId={m.module_id}
          projectId={arg}
          validate={(v) => checkSettingValue(decl, v)}
        />
      {:else if widget === 'text'}
        <TextInputControl
          control={textSettingControl(decl)}
          moduleId={m.module_id}
          projectId={arg}
          validate={(v) => checkSettingValue(decl, v)}
        />
      {:else if widget === 'select'}
        <label class="field-label" for={fieldId}>{settingLabel(decl)}</label>
        <select
          id={fieldId}
          value={(values[k] as string | undefined) ?? ''}
          disabled={noProject || busy[k]}
          aria-invalid={errors[k] ? true : undefined}
          aria-describedby={errors[k] ? `${fieldId}-err` : undefined}
          onchange={(e) => {
            const el = e.currentTarget as HTMLSelectElement;
            void save(m, decl, el.value, () => (el.value = String(values[k] ?? '')));
          }}
        >
          {#if !decl.required}
            <option value="">— not set —</option>
          {/if}
          {#each decl.options ?? [] as opt}
            <option value={opt}>{opt}</option>
          {/each}
        </select>
      {:else if widget === 'checkbox'}
        <label class="field-check">
          <input
            type="checkbox"
            checked={values[k] === true}
            disabled={noProject || busy[k]}
            onchange={(e) => {
              const el = e.currentTarget as HTMLInputElement;
              void save(m, decl, el.checked, () => (el.checked = values[k] === true));
            }}
          />
          <span class="field-label">{settingLabel(decl)}</span>
        </label>
      {:else if widget === 'multiselect'}
        <fieldset class="field-multi" disabled={noProject || busy[k]}>
          <legend class="field-label">{settingLabel(decl)}</legend>
          {#each decl.options ?? [] as opt}
            <label class="field-check">
              <input
                type="checkbox"
                checked={Array.isArray(values[k]) && (values[k] as string[]).includes(opt)}
                onchange={(e) => {
                  const el = e.currentTarget as HTMLInputElement;
                  toggleChoice(m, decl, opt, el.checked, () => (el.checked = !el.checked));
                }}
              />
              <span>{opt}</span>
            </label>
          {/each}
        </fieldset>
      {:else}
        <p class="field-label">{settingLabel(decl)}</p>
        <p class="field-error">
          This launcher cannot edit a setting of type “{decl.type}”. Update the launcher.
        </p>
      {/if}
      {#if decl.description}
        <p class="field-desc">{decl.description}</p>
      {/if}
      <p class="field-key"><code>{decl.key}</code></p>
      {#if errors[k]}
        <p id="{fieldId}-err" class="field-error" role="alert">{errors[k]}</p>
      {/if}
    </div>
  {/if}
{/snippet}

<section class="cms" aria-labelledby="cms-title">
  <header>
    <h2 id="cms-title">Module settings</h2>
    <p class="hint">
      Settings declared by the modules bundled with the launcher and by the
      modules you installed. A value is checked against the module's declared
      limits before it is saved. Settings marked “Set elsewhere” have their own
      home; their current value is shown here.
    </p>
  </header>

  {#if loading}
    <p class="loading" aria-live="polite">Loading module settings…</p>
  {:else if loadError}
    <p class="load-error" role="alert">{loadError}</p>
  {:else if modules.length === 0}
    <p class="loading">No module declares a setting or an HTTP API.</p>
  {:else}
    {#if hasPerProject}
      <div class="project-picker">
        <label for="cms-project">Per-project settings are for</label>
        {#if $projects.projects.length === 0}
          <span class="muted">No projects registered — per-project settings are unavailable.</span>
        {:else}
          <select
            id="cms-project"
            value={projectId}
            onchange={(e) => (pickedProjectId = (e.currentTarget as HTMLSelectElement).value)}
          >
            {#if projectId === ''}
              <option value="" disabled>— pick a project —</option>
            {/if}
            {#each $projects.projects as proj}
              <option value={proj.id}>{proj.name}</option>
            {/each}
          </select>
        {/if}
      </div>
    {/if}

    <ul class="modules">
      {#each modules as m (m.module_id)}
        {@const groups = splitByScope(m)}
        <li class="module-card">
          <h3>
            {m.name}
            <code class="module-id">{m.module_id}</code>
            {#if m.origin === 'installed'}
              <span class="badge">Installed module</span>
            {:else if m.origin === 'dev_passthrough'}
              <span class="badge" title="Shown because VCT_LAUNCHER_DEV_CATALOG_PASSTHROUGH is set. Not installed: only its config tab reads these values until it is.">Module in development</span>
            {/if}
          </h3>

          {#each httpApiLines(m) as api (api.url)}
            <!-- The manifest's `provides` http_api, its port resolved for
                 this machine (Rust `PlaceholderCtx::resolve`). -->
            <p class="provides" data-testid="module-http-api">
              <span class="badge">HTTP API</span>
              <code>{api.url}</code>
              {#if api.description}
                <span class="field-desc">{api.description}</span>
              {/if}
            </p>
          {/each}

          {#if groups.machineWide.length > 0}
            <div class="group">
              <div class="group-head">
                <span class="badge badge-machine">Machine-wide</span>
                {#if hasEditableMachineWide(m)}
                  <span class="group-note">{machineWideNote(m)}</span>
                {:else}
                  <span class="group-note">One value for this computer.</span>
                {/if}
              </div>
              {#each groups.machineWide as decl (decl.key)}
                {@render field(m, decl)}
              {/each}
            </div>
          {/if}

          {#if groups.perProject.length > 0}
            <div class="group">
              <div class="group-head">
                <span class="badge">Per project</span>
                <span class="group-note">
                  {projectId === '' ? 'Pick a project above to see these.' : `For ${projectName}.`}
                </span>
              </div>
              {#if projectId !== '' && !moduleOffersProject(m, projectId)}
                <p class="muted not-here">
                  {m.name} is not installed or enabled in {projectName}, so it has no
                  per-project settings there.
                  {#if m.projects && m.projects.length > 0}
                    Installed in: {projectNames(m.projects)}.
                  {/if}
                </p>
              {:else}
                {#key projectId}
                  {#each groups.perProject as decl (decl.key)}
                    {@render field(m, decl)}
                  {/each}
                {/key}
              {/if}
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .cms {
    max-width: 760px;
    margin: 0 auto;
    padding: 0 1.5rem 2rem;
    color: var(--color-text, #f1f5f9);
  }
  header h2 {
    margin: 0 0 0.4rem 0;
    font-size: 1.3rem;
  }
  .hint,
  .loading,
  .muted {
    color: var(--color-mid, #94a3b8);
    font-size: 0.9rem;
    line-height: 1.5;
  }
  .hint {
    margin: 0 0 1.2rem 0;
  }
  .not-here {
    margin: 0.25rem 0 0;
    font-size: 0.85rem;
  }
  .load-error,
  .field-error {
    color: var(--color-pink, #ff4fa0);
    font-size: 0.85rem;
    margin: 0.35rem 0 0;
  }
  .project-picker {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 1rem;
    font-size: 0.9rem;
  }
  .project-picker select,
  .field select {
    background: var(--color-bg2, #080f28);
    color: var(--color-text, #f1f5f9);
    border: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
    border-radius: 6px;
    padding: 0.4rem 0.6rem;
    font-size: 0.9rem;
    max-width: 100%;
  }
  .project-picker select:focus-visible,
  .field select:focus-visible,
  .field-check input:focus-visible,
  .editor-link:focus-visible {
    outline: 2px solid var(--color-teal, #00bfa6);
    outline-offset: 2px;
  }
  ul.modules {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  .module-card {
    background: var(--color-card, rgba(255, 255, 255, 0.04));
    border: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
    border-radius: var(--radius-card, 16px);
    padding: 1rem 1.1rem;
  }
  .module-card h3 {
    margin: 0 0 0.75rem 0;
    font-size: 1rem;
    font-weight: 700;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.5rem;
  }
  .module-id,
  .field-key code,
  .field-live code {
    font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
    font-size: 0.75rem;
    color: var(--color-mid, #94a3b8);
  }
  .field-live code {
    font-size: 0.85rem;
    color: var(--color-text, #f1f5f9);
    background: var(--color-bg2, #080f28);
    border: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
    border-radius: 6px;
    padding: 0.2rem 0.45rem;
    word-break: break-all;
  }
  .group + .group {
    margin-top: 1rem;
    padding-top: 1rem;
    border-top: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
  }
  .group-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.6rem;
  }
  .group-note,
  .field-note {
    font-size: 0.8rem;
    color: var(--color-mid, #94a3b8);
    line-height: 1.45;
  }
  .group-note {
    flex: 1 1 16rem;
  }
  .badge {
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 0.15rem 0.5rem;
    border-radius: 999px;
    border: 1px solid rgba(0, 191, 166, 0.35);
    color: var(--color-teal, #00bfa6);
    background: rgba(0, 191, 166, 0.08);
  }
  .badge-machine {
    border-color: rgba(123, 95, 255, 0.4);
    color: var(--color-purple-hover, #8f77ff);
    background: rgba(123, 95, 255, 0.1);
  }
  .badge-readonly {
    margin-left: 0.4rem;
    border-color: rgba(148, 163, 184, 0.35);
    color: var(--color-mid, #94a3b8);
    background: rgba(148, 163, 184, 0.08);
  }
  .field {
    padding: 0.5rem 0;
  }
  .field-label {
    font-size: 13px;
    font-weight: 600;
    display: block;
    margin: 0 0 0.3rem;
  }
  .field-live {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
    margin: 0;
  }
  .field-check {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.9rem;
  }
  .field-check .field-label {
    margin: 0;
  }
  .field-multi {
    border: none;
    margin: 0;
    padding: 0;
  }
  .provides {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.45rem;
    margin: 0 0 0.75rem 0;
    font-size: 0.85rem;
  }
  .provides .field-desc {
    flex-basis: 100%;
    margin: 0;
  }
  .field-desc {
    margin: 0.35rem 0 0;
    font-size: 0.8rem;
    color: var(--color-mid, #94a3b8);
    line-height: 1.45;
  }
  .field-key {
    margin: 0.2rem 0 0;
  }
  .editor-link {
    margin-top: 0.4rem;
    background: transparent;
    border: 1px solid rgba(0, 191, 166, 0.35);
    color: var(--color-teal, #00bfa6);
    border-radius: 6px;
    padding: 0.3rem 0.7rem;
    font-size: 0.8rem;
    cursor: pointer;
  }
  .editor-link:hover {
    background: rgba(0, 191, 166, 0.08);
  }
  @media (max-width: 480px) {
    .cms {
      padding: 0 0.75rem 1.5rem;
    }
    .project-picker select {
      width: 100%;
    }
  }
</style>
