<script lang="ts">
  // Stream 2 (2026-05-19): generic schema renderer for module-contributed
  // config tabs.
  //
  // Reads a `ConfigTab` schema (parsed by Rust `manifest::GuiBlock`,
  // serialized down through `get_module_nav_items`) and dispatches each
  // control by `kind` to the matching widget. State persistence:
  //
  //   * Every control's value is mirrored into the launcher DB's
  //     `module_settings` table via `get_module_setting` /
  //     `set_module_setting` Tauri commands. The renderer is the
  //     SOURCE OF TRUTH for "what did the user pick".
  //   * Manifest-declared `on_change` Tauri commands run AFTER the
  //     generic persistence — their job is the side-effect path
  //     (containers, files, services). They don't replace storage.
  //   * Button `action` commands always run on click (with optional
  //     confirm prompt). They don't read/write `module_settings`
  //     directly; commands that need to may do so server-side.
  //
  // Tooltips: EVERY interactive control renders a "?" affordance next
  // to its label whose `title` attribute is the manifest's tooltip
  // (falls back to the label itself). This is non-negotiable: the
  // user explicitly chose schema-rendered tabs partly because they
  // wanted guaranteed mouseover help.
  //
  // Project context: per-project state needs `projectId`. The active
  // project comes from the `selectedProject` store; if no project is
  // selected we render a placeholder and disable controls (the
  // dedicated rl_settings Tauri commands all reject empty project_id).

  import { onMount } from 'svelte';
  import { invoke, tauriAvailable } from '$lib/tauri';
  import { projects, selectedProject } from '$lib/stores/projects';
  import {
    isActionDescriptor,
    type ActionRef,
    type ConfigControl,
    type ConfigTab,
  } from '$lib/types/manifest';
  import TextInputControl from '$lib/components/module-controls/TextInputControl.svelte';
  import NumberInputControl from '$lib/components/module-controls/NumberInputControl.svelte';
  import StatusDisplayControl from '$lib/components/module-controls/StatusDisplayControl.svelte';
  import FilePickerControl from '$lib/components/module-controls/FilePickerControl.svelte';
  import LinkControl from '$lib/components/module-controls/LinkControl.svelte';
  import {
    sectionUsesProjectId,
    substituteEmbeddingSourceInAction,
  } from '$lib/components/module-controls/configTabHelpers';

  // ─── Schema types ──────────────────────────────────────────────────────
  //
  // v0.2.26: the discriminated union + ActionRef types live in
  // `$lib/types/manifest`. The 5 legacy kinds (checkbox / multi_select /
  // button / select / info) are rendered inline below; the 5 new kinds
  // (text_input / number_input / status_display / file_picker / link)
  // each have a dedicated component under `module-controls/`.
  //
  // Adding a new kind requires:
  //   1. New variant in `manifest::ConfigControl` (Rust)
  //   2. New variant in `$lib/types/manifest`
  //   3. New case + component import below
  //   4. Doc update on both sides.

  let { configTab, moduleId }: { configTab: ConfigTab; moduleId: string } =
    $props();

  const projectId = $derived($selectedProject?.id ?? '');
  const hasProject = $derived(projectId !== '');

  // ─── v0.2.32 L3: per-section project picker ────────────────────────────
  //
  // Any section whose controls reference `{{project_id}}` in any action
  // (path/body/legacy-string) renders a section-local picker that
  // OVERRIDES the global `selectedProject`. Implementation strategy:
  //
  //   * `sectionRequiresProject[i]` — cached boolean derived from the
  //     manifest. Computed once on mount; the schema doesn't mutate at
  //     runtime so this is safe to memoise.
  //   * `pickedProjectIdBySection[i]` — user's pick. Defaults to the
  //     global `selectedProject` when null.
  //   * `effectiveProjectId(i)` — `pickedProjectIdBySection[i] ?? projectId`.
  //     This is what flows into every dispatchAction / persist call for
  //     the controls inside section `i`.
  //
  // Backward compatibility: sections that don't reference `{{project_id}}`
  // skip the picker entirely (the legacy global-project-only behaviour).
  let sectionRequiresProject = $state<Record<number, boolean>>({});
  let pickedProjectIdBySection = $state<Record<number, string>>({});

  function effectiveProjectId(sectionIdx: number): string {
    return pickedProjectIdBySection[sectionIdx] ?? projectId;
  }

  function sectionHasEffectiveProject(sectionIdx: number): boolean {
    if (sectionRequiresProject[sectionIdx]) {
      return effectiveProjectId(sectionIdx) !== '';
    }
    return hasProject;
  }

  // ─── v0.2.32 L7: embedding-source cache ────────────────────────────────
  //
  // The `{{embedding_source_from_project_kg_binding}}` placeholder is
  // substituted client-side at dispatch time. Resolution happens once on
  // mount (and again when the active project changes); the resolved
  // value is cached PER project_id so picker-driven dispatches that
  // target a different project pick up the right value.
  //
  // The Tauri command (`get_project_embedding_source`) always returns a
  // non-empty string (falls back to `"qwen3"`), so a cache miss + lookup
  // failure is treated as a transient — the dispatcher will see the
  // unsubstituted token and fail loudly with a clear error.
  let embeddingSourceByProjectId = $state<Record<string, string>>({});

  async function ensureEmbeddingSourceCached(pid: string): Promise<void> {
    if (!pid) return;
    if (embeddingSourceByProjectId[pid] !== undefined) return;
    if (!tauriAvailable()) return;
    try {
      const src = await invoke<string>('get_project_embedding_source', {
        projectId: pid,
      });
      if (typeof src === 'string' && src.length > 0) {
        embeddingSourceByProjectId[pid] = src;
      }
    } catch (e) {
      // Soft-fail: leaves the cache empty for this project. The next
      // dispatch will retry implicitly (since the slot is still
      // undefined). A persistent failure surfaces via the dispatcher's
      // "unknown placeholder" error path on the next action.
      console.warn(
        `[ModuleConfigTab] get_project_embedding_source(${pid}) failed:`,
        e,
      );
    }
  }

  // Per-control state caches. Keys are `<section_idx>:<control_id>`.
  // `values` is the persisted value (from `get_module_setting` on mount
  // + writes on change). `optionsByControl` caches dynamic options
  // returned by a multi_select's `options_source` command.
  let values = $state<Record<string, unknown>>({});
  let optionsByControl = $state<Record<string, { value: string; label: string }[]>>({});

  // Section collapse state. Keyed by section_idx. Initialized lazily
  // (we don't want to overwrite user toggles on every reactive run).
  let collapsedSections = $state<Record<number, boolean>>({});

  // Per-control mutation in-flight indicator. Used to disable
  // controls + show a small spinner. Keyed by `<section_idx>:<control_id>`.
  let busy = $state<Record<string, boolean>>({});

  // Per-control error message. Same key shape as `busy`.
  let errors = $state<Record<string, string>>({});

  // Inline confirm dialog state. Set when a button's `confirm` is
  // populated; cleared when user confirms or cancels. Native
  // window.confirm() would be simpler but breaks WebKit/Tauri focus
  // sometimes; using inline dialog state for predictable behaviour.
  let pendingConfirm = $state<
    | { key: string; prompt: string; onConfirm: () => Promise<void> }
    | null
  >(null);

  function ckey(sectionIdx: number, controlId: string): string {
    return `${sectionIdx}:${controlId}`;
  }

  // Initialize collapse state + load persisted values once on mount.
  onMount(async () => {
    // Default collapse state from `initially_collapsed`.
    configTab.sections.forEach((s, i) => {
      if (s.collapsible && s.initially_collapsed) {
        collapsedSections[i] = true;
      }
    });

    // v0.2.32 L3: cache the "does this section reference project_id?"
    // bit per section. Computed once because the manifest is stable for
    // the lifetime of the rendered tab.
    configTab.sections.forEach((s, i) => {
      sectionRequiresProject[i] = sectionUsesProjectId(s);
    });

    // v0.2.32 L3: hydrate `projects` store so the per-section picker
    // can render the dropdown. The store is shared with the menu bar's
    // selector — `.load()` is idempotent and cheap when already loaded.
    if (tauriAvailable()) {
      try {
        await projects.load();
      } catch (e) {
        console.warn('[ModuleConfigTab] projects.load failed:', e);
      }
    }

    // v0.2.32 L7: warm the embedding-source cache for the active
    // project. Picker-driven dispatches that target a different project
    // lazy-load on first reference.
    if (projectId) {
      await ensureEmbeddingSourceCached(projectId);
    }

    if (!tauriAvailable()) return;

    // Load persisted values + multi_select options for every section
    // that has an effective project (global or section-local picker).
    for (let i = 0; i < configTab.sections.length; i++) {
      if (sectionHasEffectiveProject(i)) {
        await loadSectionState(i);
      }
    }
  });

  /**
   * Load persisted control values + multi_select options for a single
   * section, using the section's effective project id. Extracted from
   * `onMount` so the section-local project picker (v0.2.32 L3) can
   * re-run this when the user changes the section's pick — otherwise
   * the controls would still show the previous project's saved values.
   */
  async function loadSectionState(sectionIdx: number) {
    if (!tauriAvailable()) return;
    const pid = effectiveProjectId(sectionIdx);
    if (!pid) return;

    // Make sure the embedding-source cache is warm for the section's
    // effective project before any dispatchAction call below.
    await ensureEmbeddingSourceCached(pid);

    // Fetch persisted value for every control whose kind supports
    // state. Buttons + info are stateless.
    for (const control of configTab.sections[sectionIdx].controls) {
      if (control.kind === 'button' || control.kind === 'info') continue;
      try {
        const v = await invoke<unknown>('get_module_setting', {
          moduleId,
          controlId: control.id,
          projectId: pid,
        });
        if (v !== null && v !== undefined) {
          values[ckey(sectionIdx, control.id)] = v;
        }
      } catch (e) {
        // Soft-fail: missing persisted value is non-fatal, control
        // falls back to its declared default.
        console.warn(
          `[ModuleConfigTab] get_module_setting failed for ${moduleId}/${control.id}:`,
          e,
        );
      }
    }

    // Eagerly load options for each multi_select in this section.
    // Failures keep the option list empty + render an error inline.
    for (const control of configTab.sections[sectionIdx].controls) {
      if (control.kind !== 'multi_select') continue;
      const k = ckey(sectionIdx, control.id);
      try {
        const opts = await dispatchAction<{ value: string; label: string }[]>(
          control.options_source,
          null,
          {},
          pid,
        );
        optionsByControl[k] = opts ?? [];
      } catch (e) {
        errors[k] = `Failed to load options: ${e instanceof Error ? e.message : String(e)}`;
      }
    }
  }

  /**
   * Handler invoked when the user picks a project in a section-local
   * picker (v0.2.32 L3). Updates the picked-id map, clears stale
   * cached values + options for the section, then re-fetches state
   * against the new project.
   */
  async function onSectionProjectChange(sectionIdx: number, newProjectId: string) {
    pickedProjectIdBySection[sectionIdx] = newProjectId;

    // Drop stale per-control state for this section so the next render
    // can either show the new project's persisted value or fall back to
    // the control's declared default.
    for (const control of configTab.sections[sectionIdx].controls) {
      const k = ckey(sectionIdx, control.id);
      delete values[k];
      delete optionsByControl[k];
      delete errors[k];
    }

    if (newProjectId) {
      await loadSectionState(sectionIdx);
    }
  }

  /**
   * Route an ActionRef through the right Tauri command:
   *   - string  → `invoke(action_string, { moduleId, projectId, value })`
   *   - object  → `invoke('module_dispatch_action', { ... })`
   *
   * The string form preserves the v0.2.20-v0.2.25 wire contract for
   * legacy in-tree commands that read `moduleId` / `projectId` directly.
   * Some legacy callers also expect extra args (e.g. retrain commands
   * want `project_ids`); the call sites that need those still pass them
   * through `invoke()` directly so this helper stays uniform.
   *
   * Re-thrown errors let the caller toast or set inline state. All
   * dispatch calls in this file go through here so back-compat lives
   * in one place.
   */
  /**
   * Snapshot the renderer's current per-control values keyed by plain
   * control-id (NOT the `<section>:<id>` composite key the renderer
   * uses internally). The dispatcher feeds this map to its
   * `{{control:<id>}}` substitution resolver so descriptor bodies can
   * reference sibling controls without each one having to be re-read
   * from `module_settings` on every dispatch.
   *
   * Conflict policy: if two sections define a control with the same
   * id, the later section wins. This matches how `dispatchAction`'s
   * `extraArgs` already handles sibling references in the legacy path.
   */
  function siblingValuesSnapshot(): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (let i = 0; i < configTab.sections.length; i++) {
      for (const control of configTab.sections[i].controls) {
        if (control.kind === 'button' || control.kind === 'info') continue;
        const v = values[ckey(i, control.id)];
        if (v !== undefined) out[control.id] = v;
      }
    }
    return out;
  }

  async function dispatchAction<T = unknown>(
    action: ActionRef,
    value: unknown = null,
    extraArgs: Record<string, unknown> = {},
    effectivePid: string = projectId,
  ): Promise<T> {
    // v0.2.32 L7: lazily fetch the embedding-source for whichever
    // project we're actually dispatching against. Section-local pickers
    // can target a project different from the global active one — make
    // sure we substitute the right value.
    await ensureEmbeddingSourceCached(effectivePid);
    const embeddingSource = embeddingSourceByProjectId[effectivePid] ?? '';

    if (isActionDescriptor(action)) {
      // v0.2.32 L7: pre-substitute the embedding-source token inside
      // the descriptor BEFORE the dispatcher sees it. Doing this
      // client-side keeps the Rust dispatcher's placeholder set
      // unchanged. When the token isn't present, substitution is a
      // no-op (the helper short-circuits on string include).
      const substituted = substituteEmbeddingSourceInAction(
        action,
        embeddingSource,
      );
      // v0.2.26 follow-up (reviewer finding 3.2): pass the sibling
      // values snapshot so descriptor bodies can reference other
      // controls via `{{control:<id>}}`. Without this, that token
      // unconditionally failed at substitute time even though the
      // dispatcher's resolver plumbing was correct.
      return invoke<T>('module_dispatch_action', {
        moduleId,
        projectId: effectivePid,
        action: substituted,
        value,
        siblingValues: siblingValuesSnapshot(),
      });
    }
    // Legacy string actions: forward the embedding-source as an extra
    // arg. Commands that don't read it are unaffected (Tauri ignores
    // unknown args); commands that DO read it get the resolved value.
    return invoke<T>(action, {
      moduleId,
      projectId: effectivePid,
      value,
      embeddingSource,
      ...extraArgs,
    });
  }

  async function persistAndNotify(
    sectionIdx: number,
    control: Extract<
      ConfigControl,
      { kind: 'checkbox' | 'multi_select' | 'select' }
    >,
    newValue: unknown,
  ) {
    const k = ckey(sectionIdx, control.id);
    busy[k] = true;
    errors[k] = '';
    try {
      // v0.2.32 L3: persist + dispatch against the EFFECTIVE project
      // (per-section pick wins over the global selection).
      const pid = effectiveProjectId(sectionIdx);
      values[k] = newValue;
      // 1. Generic persistence (source of truth).
      await invoke('set_module_setting', {
        moduleId,
        controlId: control.id,
        value: newValue,
        projectId: pid,
      });
      // 2. Optional `on_change` side-effect command. Manifest-declared.
      //    v0.2.26: `on_change` is an ActionRef (legacy string OR
      //    declarative descriptor). The dispatcher routes accordingly.
      if (control.on_change) {
        await dispatchAction(control.on_change, newValue, {}, pid);
      }
    } catch (e) {
      errors[k] = e instanceof Error ? e.message : String(e);
    } finally {
      busy[k] = false;
    }
  }

  async function runButtonAction(
    sectionIdx: number,
    control: Extract<ConfigControl, { kind: 'button' }>,
  ) {
    const k = ckey(sectionIdx, control.id);
    busy[k] = true;
    errors[k] = '';
    try {
      // Forward both `projectId` and the selected `global_train_projects`
      // multi-select value when present — the retrain Tauri commands
      // expect `project_ids` so we sniff a sibling multi_select named
      // `global_train_projects` and pass its current selection. This
      // keeps the manifest's per-section UX flow ("pick projects, then
      // press retrain") working without a dedicated wire-up table.
      //
      // v0.2.26: this sniffing only applies to LEGACY string actions —
      // declarative descriptors carry their own payload in `body` and
      // ignore the sibling-multi_select convention entirely.
      const extraArgs: Record<string, unknown> = {};
      // Forward known multi_select values as bonus args. The schema is
      // small enough that an O(controls) scan is fine.
      for (const section of configTab.sections) {
        for (const sibling of section.controls) {
          if (sibling.kind !== 'multi_select') continue;
          const skey = `${configTab.sections.indexOf(section)}:${sibling.id}`;
          const v = values[skey];
          if (Array.isArray(v)) {
            // Multi-select value comes out as Array<string> (option values).
            extraArgs[sibling.id] = v;
            // Convenience alias for the RL commands which use `project_ids`.
            if (sibling.id === 'global_train_projects') {
              extraArgs.projectIds = v;
            }
          }
        }
      }
      // v0.2.32 L3: dispatch against the section's effective project.
      await dispatchAction(
        control.action,
        null,
        extraArgs,
        effectiveProjectId(sectionIdx),
      );
    } catch (e) {
      errors[k] = e instanceof Error ? e.message : String(e);
    } finally {
      busy[k] = false;
    }
  }

  function onButtonClick(
    sectionIdx: number,
    control: Extract<ConfigControl, { kind: 'button' }>,
  ) {
    const k = ckey(sectionIdx, control.id);
    if (control.confirm) {
      pendingConfirm = {
        key: k,
        prompt: control.confirm,
        onConfirm: async () => {
          pendingConfirm = null;
          await runButtonAction(sectionIdx, control);
        },
      };
    } else {
      void runButtonAction(sectionIdx, control);
    }
  }

  function toggleSection(idx: number) {
    collapsedSections[idx] = !collapsedSections[idx];
  }
</script>

<div class="tab">
  <header class="tab-header">
    <h1>{configTab.title}</h1>
    {#if configTab.description}
      <p class="tab-description">{configTab.description}</p>
    {/if}
    {#if !hasProject}
      <div class="banner warning">
        Select a project from the project picker before changing per-project
        settings. Controls are disabled until then.
      </div>
    {/if}
  </header>

  {#each configTab.sections as section, sectionIdx}
    {@const collapsed = collapsedSections[sectionIdx] === true}
    {@const requiresPicker = sectionRequiresProject[sectionIdx] === true}
    {@const sectionPid = effectiveProjectId(sectionIdx)}
    {@const sectionHasProject = sectionHasEffectiveProject(sectionIdx)}
    {@const sectionDisabled = !sectionHasProject}
    <section class="config-section">
      <button
        type="button"
        class="section-header"
        class:collapsible={section.collapsible}
        onclick={() => section.collapsible && toggleSection(sectionIdx)}
        aria-expanded={!collapsed}
      >
        <span class="section-title">
          {#if section.collapsible}
            <span class="chevron" class:collapsed>{collapsed ? '▸' : '▾'}</span>
          {/if}
          {section.title}
        </span>
        {#if section.description}
          <span class="section-description">{section.description}</span>
        {/if}
      </button>

      {#if !collapsed}
        <!--
          v0.2.32 L3: section-local project picker. Rendered above the
          controls whenever any control in the section references
          `{{project_id}}`. Overrides the global `selectedProject` for
          dispatches that originate from this section's controls.
        -->
        {#if requiresPicker}
          <div class="section-project-picker">
            <label class="section-project-picker-label">
              Project for this section:
              {#if $projects.projects.length === 0}
                <span class="section-project-picker-empty">
                  No projects registered — controls disabled.
                </span>
              {:else}
                <select
                  value={sectionPid}
                  onchange={(e) =>
                    onSectionProjectChange(
                      sectionIdx,
                      (e.target as HTMLSelectElement).value,
                    )}
                >
                  {#if !sectionPid}
                    <option value="" disabled selected>— pick a project —</option>
                  {/if}
                  {#each $projects.projects as proj}
                    <option value={proj.id}>{proj.name}</option>
                  {/each}
                </select>
              {/if}
              <span
                class="tooltip-affordance"
                title="This section affects per-project state. Pick which project the controls below operate on."
                aria-label="More info"
              >?</span>
            </label>
          </div>
        {/if}

        <div class="controls">
          {#each section.controls as control}
            {@const k = ckey(sectionIdx, control.id)}
            {@const isBusy = busy[k] === true}
            {@const err = errors[k]}
            <div class="control control-{control.kind}">
              {#if control.kind === 'info'}
                <div class="info-banner info-{control.variant ?? 'info'}">
                  {control.text}
                </div>
              {:else if control.kind === 'checkbox'}
                {@const currentVal = (values[k] as boolean | undefined) ?? control.default ?? false}
                <label class="control-row">
                  <input
                    type="checkbox"
                    checked={currentVal}
                    disabled={sectionDisabled || isBusy}
                    onchange={(e) =>
                      persistAndNotify(sectionIdx, control, (e.target as HTMLInputElement).checked)}
                  />
                  <span class="control-label">{control.label}</span>
                  <span
                    class="tooltip-affordance"
                    title={control.tooltip ?? control.label}
                    aria-label="More info"
                  >?</span>
                </label>
              {:else if control.kind === 'select'}
                {@const currentVal = (values[k] as string | undefined) ?? control.default ?? ''}
                <div class="control-row">
                  <span class="control-label">{control.label}</span>
                  <select
                    value={currentVal}
                    disabled={sectionDisabled || isBusy}
                    onchange={(e) =>
                      persistAndNotify(sectionIdx, control, (e.target as HTMLSelectElement).value)}
                  >
                    {#each control.options as opt}
                      <option value={opt.value}>{opt.label}</option>
                    {/each}
                  </select>
                  <span
                    class="tooltip-affordance"
                    title={control.tooltip ?? control.label}
                    aria-label="More info"
                  >?</span>
                </div>
              {:else if control.kind === 'multi_select'}
                {@const currentVal = (values[k] as string[] | undefined) ?? []}
                {@const opts = optionsByControl[k] ?? []}
                <div class="control-row vstack">
                  <span class="control-label-row">
                    <span class="control-label">{control.label}</span>
                    <span
                      class="tooltip-affordance"
                      title={control.tooltip ?? control.label}
                      aria-label="More info"
                    >?</span>
                  </span>
                  {#if opts.length === 0}
                    <p class="empty-options">
                      No options available yet. Enable
                      "Use this project's data to train the global model" on at
                      least one project to populate this list.
                    </p>
                  {:else}
                    <div class="multi-select-list">
                      {#each opts as opt}
                        {@const isChecked = currentVal.includes(opt.value)}
                        <label class="checkbox-row">
                          <input
                            type="checkbox"
                            checked={isChecked}
                            disabled={sectionDisabled || isBusy}
                            onchange={(e) => {
                              const checked = (e.target as HTMLInputElement).checked;
                              const next = new Set(currentVal);
                              if (checked) next.add(opt.value);
                              else next.delete(opt.value);
                              void persistAndNotify(sectionIdx, control, Array.from(next));
                            }}
                          />
                          <span>{opt.label}</span>
                        </label>
                      {/each}
                    </div>
                  {/if}
                </div>
              {:else if control.kind === 'button'}
                <div class="control-row">
                  <button
                    type="button"
                    class="action-button variant-{control.variant ?? 'secondary'}"
                    disabled={sectionDisabled || isBusy}
                    onclick={() => onButtonClick(sectionIdx, control)}
                  >
                    {isBusy ? '…' : control.label}
                  </button>
                  <span
                    class="tooltip-affordance"
                    title={control.tooltip ?? control.label}
                    aria-label="More info"
                  >?</span>
                </div>
              {:else if control.kind === 'text_input'}
                <TextInputControl
                  {control}
                  {moduleId}
                  projectId={sectionPid}
                  disabled={sectionDisabled}
                />
              {:else if control.kind === 'number_input'}
                <NumberInputControl
                  {control}
                  {moduleId}
                  projectId={sectionPid}
                  disabled={sectionDisabled}
                />
              {:else if control.kind === 'status_display'}
                <StatusDisplayControl
                  {control}
                  {moduleId}
                  projectId={sectionPid}
                  disabled={sectionDisabled}
                />
              {:else if control.kind === 'file_picker'}
                <FilePickerControl
                  {control}
                  {moduleId}
                  projectId={sectionPid}
                  disabled={sectionDisabled}
                />
              {:else if control.kind === 'link'}
                <LinkControl {control} disabled={sectionDisabled} />
              {/if}

              {#if err}
                <p class="control-error">{err}</p>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    </section>
  {/each}

  {#if pendingConfirm}
    <div
      class="confirm-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Confirm action"
    >
      <div class="confirm-box">
        <p>{pendingConfirm.prompt}</p>
        <div class="confirm-actions">
          <button
            type="button"
            class="action-button variant-secondary"
            onclick={() => (pendingConfirm = null)}
          >
            Cancel
          </button>
          <button
            type="button"
            class="action-button variant-primary"
            onclick={() => pendingConfirm && pendingConfirm.onConfirm()}
          >
            Confirm
          </button>
        </div>
      </div>
    </div>
  {/if}
</div>

<style>
  .tab {
    padding: 24px;
    max-width: 920px;
    margin: 0 auto;
  }

  .tab-header h1 {
    margin: 0 0 6px 0;
    font-size: 20px;
    font-weight: 700;
  }

  .tab-description {
    margin: 0 0 16px 0;
    color: var(--color-muted);
    font-size: 13px;
  }

  .banner.warning {
    margin: 12px 0;
    padding: 10px 14px;
    border-radius: 8px;
    background: rgba(241, 196, 15, 0.10);
    border: 1px solid rgba(241, 196, 15, 0.30);
    color: #f1c40f;
    font-size: 13px;
  }

  .config-section {
    margin-top: 20px;
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 10px;
    overflow: hidden;
  }

  .section-header {
    width: 100%;
    background: rgba(255, 255, 255, 0.02);
    padding: 12px 16px;
    border: 0;
    text-align: left;
    color: inherit;
    cursor: default;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .section-header.collapsible {
    cursor: pointer;
  }
  .section-header.collapsible:hover {
    background: rgba(255, 255, 255, 0.04);
  }

  .section-title {
    font-size: 14px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .chevron {
    color: var(--color-muted);
    font-size: 12px;
    width: 14px;
    display: inline-block;
  }

  .section-description {
    font-size: 12px;
    color: var(--color-muted);
  }

  .controls {
    padding: 12px 16px 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  /* v0.2.32 L3: per-section project picker bar. Sits above .controls
     when the section's manifest references `{{project_id}}`. */
  .section-project-picker {
    padding: 10px 16px;
    background: rgba(0, 191, 166, 0.05);
    border-top: 1px solid rgba(0, 191, 166, 0.18);
    border-bottom: 1px solid rgba(0, 191, 166, 0.10);
  }
  .section-project-picker-label {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--color-muted);
  }
  .section-project-picker-label select {
    font-size: 12px;
    padding: 3px 8px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.04);
    color: var(--color-text);
    border: 1px solid rgba(255, 255, 255, 0.10);
  }
  .section-project-picker-empty {
    color: #e74c3c;
    font-size: 12px;
  }

  .control {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .control-row {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .control-row.vstack {
    align-items: flex-start;
    flex-direction: column;
  }

  .control-label {
    font-size: 13px;
    flex: 1;
  }
  .control-label-row {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .tooltip-affordance {
    display: inline-flex;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    align-items: center;
    justify-content: center;
    background: rgba(255, 255, 255, 0.08);
    color: var(--color-muted);
    font-size: 10px;
    font-weight: 700;
    cursor: help;
    flex-shrink: 0;
  }
  .tooltip-affordance:hover {
    background: rgba(255, 255, 255, 0.16);
    color: var(--color-text);
  }

  .info-banner {
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 12px;
  }
  .info-banner.info-info {
    background: rgba(0, 191, 166, 0.08);
    border: 1px solid rgba(0, 191, 166, 0.20);
    color: var(--color-text);
  }
  .info-banner.info-warning {
    background: rgba(241, 196, 15, 0.10);
    border: 1px solid rgba(241, 196, 15, 0.30);
    color: #f1c40f;
  }

  .multi-select-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
    width: 100%;
    padding-left: 16px;
  }
  .checkbox-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
  }
  .empty-options {
    margin: 0;
    color: var(--color-muted);
    font-size: 12px;
    padding-left: 16px;
  }

  .action-button {
    padding: 6px 14px;
    border-radius: 6px;
    border: 1px solid transparent;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
  }
  .action-button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .action-button.variant-primary {
    background: rgba(0, 191, 166, 0.18);
    color: #00bfa6;
    border-color: rgba(0, 191, 166, 0.40);
  }
  .action-button.variant-primary:hover:not(:disabled) {
    background: rgba(0, 191, 166, 0.28);
  }
  .action-button.variant-secondary {
    background: rgba(255, 255, 255, 0.06);
    color: var(--color-text);
    border-color: rgba(255, 255, 255, 0.10);
  }
  .action-button.variant-secondary:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.10);
  }
  .action-button.variant-danger {
    background: rgba(231, 76, 60, 0.16);
    color: #e74c3c;
    border-color: rgba(231, 76, 60, 0.40);
  }
  .action-button.variant-danger:hover:not(:disabled) {
    background: rgba(231, 76, 60, 0.24);
  }

  .control-error {
    margin: 4px 0 0 0;
    color: #e74c3c;
    font-size: 12px;
  }

  .confirm-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }
  .confirm-box {
    background: var(--color-bg, #0d1538);
    padding: 20px 24px;
    border-radius: 10px;
    border: 1px solid rgba(255, 255, 255, 0.10);
    max-width: 460px;
    width: calc(100% - 48px);
  }
  .confirm-box p {
    margin: 0 0 16px 0;
    line-height: 1.5;
    font-size: 14px;
  }
  .confirm-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
</style>
