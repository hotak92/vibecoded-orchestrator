<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  // Phase 1 of the diagrams-integration plan (2026-05-24): launcher tab
  // that lists project diagrams (Mermaid + Excalidraw), previews them,
  // and exposes snapshot + access controls.
  //
  // Sibling Phase 1.1 (Tauri commands) and 1.5.A (diagram_indexer.py)
  // land in parallel; the `invoke<...>` calls here are stubs against
  // the plan's command signatures. Until the backend lands they fail
  // with a clear "command not found" error which the toast layer
  // surfaces — no silent UI breakage.
  //
  // Layout: split-pane (CSS grid). Left = diagram list + add modal.
  // Right = preview of the selected diagram with snapshot toolbar.
  // Bottom strip = snapshot timeline.
  import { onDestroy, onMount } from 'svelte';
  import { invoke, listen } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type {
    DiagramRow,
    DiagramSnapshotRow,
    DiagramType,
    DiagramChangedPayload,
    SnapshotTrigger,
  } from '$lib/types/project-state';
  import Dropdown from '$lib/components/Dropdown.svelte';

  // ─── Props ────────────────────────────────────────────────────────────
  let { projectId }: { projectId: string } = $props();

  // ─── Module gate ──────────────────────────────────────────────────────
  // The diagrams module can be disabled per-project (plan §1.5.7). When
  // disabled, the rest of the tab dims and shows an explanatory message;
  // the toggle stays interactive so the user can re-enable.
  let moduleActive = $state<boolean | null>(null); // null = loading
  let moduleToggling = $state(false);

  async function loadModuleState() {
    try {
      const active = await invoke<boolean>('is_project_module_active', {
        projectId,
        moduleName: 'diagrams',
      });
      moduleActive = active;
    } catch (e) {
      // Phase 1.1 not landed yet → assume active so the rest of the UI
      // is reachable. The downstream `list_project_diagrams` call will
      // surface its own error if THAT command is also missing.
      console.warn('[diagrams] is_project_module_active not available:', e);
      moduleActive = true;
    }
  }

  async function toggleModule() {
    if (moduleActive === null) return;
    const next = !moduleActive;
    moduleToggling = true;
    try {
      await invoke('set_project_module_enabled', {
        projectId,
        moduleName: 'diagrams',
        enabled: next,
      });
      moduleActive = next;
      toast.success(`Diagrams module ${next ? 'enabled' : 'disabled'}`);
      if (next) await load();
    } catch (e) {
      toast.error(e);
    } finally {
      moduleToggling = false;
    }
  }

  // ─── Diagram list ─────────────────────────────────────────────────────
  let diagrams = $state<DiagramRow[]>([]);
  let loading = $state(true);
  let selectedId = $state<number | null>(null);
  // 5-second fallback polling kicks in if `diagram-changed` events are
  // not delivered within 10s of mount (Phase 1.5.A may not have wired
  // the chokidar push yet at merge time). pollTimer is `$state` so the
  // `Live updates pending` badge re-renders when it transitions
  // null → number; the handle itself never gets used reactively, only
  // the truthiness of "are we polling?".
  let livePushOk = $state(false);
  let pollTimer = $state<ReturnType<typeof setInterval> | null>(null);

  const selected = $derived(
    selectedId == null ? null : (diagrams.find((d) => d.id === selectedId) ?? null),
  );

  async function load() {
    loading = true;
    try {
      const rows = await invoke<DiagramRow[]>('list_project_diagrams', {
        projectId,
      });
      diagrams = rows;
      // If the previously-selected diagram disappeared (deleted /
      // unregistered), drop the selection so the right pane resets.
      if (selectedId != null && !rows.some((d) => d.id === selectedId)) {
        selectedId = null;
      }
      // If nothing selected and we have diagrams, select the first
      // (newest by `updated_at` if the backend returns in that order).
      if (selectedId == null && rows.length > 0) {
        selectedId = rows[0].id;
      }
    } catch (e) {
      toast.error(e);
      diagrams = [];
    } finally {
      loading = false;
    }
  }

  async function toggleDiagram(diagram: DiagramRow) {
    const next = !diagram.enabled;
    try {
      await invoke('set_project_diagram_enabled', {
        projectId,
        name: diagram.diagram_name,
        enabled: next,
      });
      diagram.enabled = next;
    } catch (e) {
      toast.error(e);
    }
  }

  async function unregister(diagram: DiagramRow) {
    if (
      !confirm(
        `Unregister diagram "${diagram.diagram_name}"? The file on disk is NOT removed.`,
      )
    )
      return;
    try {
      await invoke('unregister_project_diagram', {
        projectId,
        name: diagram.diagram_name,
      });
      toast.success('Unregistered');
      await load();
    } catch (e) {
      toast.error(e);
    }
  }

  // ─── Add diagram modal ────────────────────────────────────────────────
  const TYPE_OPTIONS = [
    { value: 'mermaid', label: 'Mermaid (.mmd)' },
    { value: 'excalidraw', label: 'Excalidraw (.excalidraw)' },
  ];
  let showAdd = $state(false);
  let newType = $state<DiagramType>('mermaid');
  let newName = $state('');
  let newCategoryPath = $state('');
  let adding = $state(false);

  async function register() {
    const name = newName.trim();
    const category = newCategoryPath.trim();
    if (!name) {
      toast.error('Diagram name required');
      return;
    }
    if (!category) {
      // Plan §1.5.1: path is enforced to be at least one category dir deep.
      toast.error('Category path required (e.g. gui/auth)');
      return;
    }
    // Validate matches the enforced shape so we fail fast in the UI
    // rather than having the wrapper MCP reject after a round-trip.
    if (!/^[a-z0-9][a-z0-9-]*$/.test(name)) {
      toast.error('Name must be lowercase-kebab (regex [a-z0-9][a-z0-9-]*)');
      return;
    }
    if (!/^[a-z0-9][a-z0-9/-]*[a-z0-9]$/.test(category)) {
      toast.error('Category path must be kebab segments separated by /');
      return;
    }
    const ext = newType === 'mermaid' ? 'mmd' : 'excalidraw';
    const filePath = `.claude/diagrams/${category}/${name}.${ext}`;
    adding = true;
    try {
      const row = await invoke<DiagramRow>('register_project_diagram', {
        projectId,
        req: {
          diagram_name: name,
          diagram_type: newType,
          file_path: filePath,
          category_path: category,
        },
      });
      // Best-effort: ask the launcher to drop a starter file on disk so
      // the user has something to render immediately. Treat absent
      // command as "user will create file manually" with a hint toast.
      try {
        await invoke('create_starter_diagram_file', {
          projectId,
          diagramId: row.id,
        });
      } catch (e) {
        console.warn('[diagrams] starter-file helper not available:', e);
        toast.info(
          `Registered. Create ${filePath} manually to start editing.`,
        );
      }
      newName = '';
      newCategoryPath = '';
      showAdd = false;
      await load();
      selectedId = row.id;
    } catch (e) {
      toast.error(e);
    } finally {
      adding = false;
    }
  }

  // ─── Right pane: preview + snapshots ─────────────────────────────────
  let previewSvg = $state<string>('');
  let previewError = $state<string | null>(null);
  let mermaidVersionWarning = $state<string | null>(null);
  let snapshots = $state<DiagramSnapshotRow[]>([]);
  let creatingSnapshot = $state(false);
  let mermaidModulePromise: Promise<typeof import('mermaid').default> | null = null;

  async function ensureMermaid() {
    if (!mermaidModulePromise) {
      mermaidModulePromise = (async () => {
        const mod = await import('mermaid');
        // Version assertion against the Vite-build-time pin from
        // bundled_mcp_versions.toml. Drift = orchestrator/launcher
        // version mismatch; surface it loudly but don't break preview.
        try {
          const expected = (import.meta.env.VITE_MERMAID_PIN ?? 'unknown') as string;
          // mermaid exposes `version` on the default export in 11.x;
          // older versions exposed it on the package itself. Read both
          // defensively.
          const actual =
            (mod.default as unknown as { version?: string })?.version ??
            (mod as unknown as { version?: string })?.version ??
            'unknown';
          if (expected !== 'unknown' && actual !== 'unknown' && actual !== expected) {
            mermaidVersionWarning =
              `Mermaid version drift: pin=${expected}, loaded=${actual}. ` +
              'Update launcher/package.json or bundled_mcp_versions.toml so they match.';
            console.error('[diagrams]', mermaidVersionWarning);
          }
        } catch (e) {
          console.warn('[diagrams] mermaid version check failed:', e);
        }
        // Self-hosted fonts — disable mermaid's CDN font fetch. The
        // default theme uses system fonts so this is safe; if we add a
        // custom theme later we'll bundle the fonts under static/fonts.
        mod.default.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          // Theme fonts pulled from the OS so no network reach.
          fontFamily: 'system-ui, -apple-system, sans-serif',
        });
        return mod.default;
      })();
    }
    return mermaidModulePromise;
  }

  async function readFile(absPath: string): Promise<string> {
    // Backend command will resolve relative `file_path` against the
    // project root. Phase 1.1 owns the implementation; we call it by
    // name so the merge integrator can wire it up.
    return await invoke<string>('read_project_diagram_source', {
      projectId,
      relPath: absPath,
    });
  }

  let renderToken = 0;

  async function renderPreview() {
    if (!selected) {
      previewSvg = '';
      previewError = null;
      return;
    }
    const myToken = ++renderToken;
    if (selected.diagram_type === 'mermaid') {
      try {
        const source = await readFile(selected.file_path);
        const mermaid = await ensureMermaid();
        if (myToken !== renderToken) return; // a newer render started
        const id = `diagram-preview-${selected.id}-${myToken}`;
        const { svg } = await mermaid.render(id, source);
        if (myToken !== renderToken) return;
        previewSvg = svg;
        previewError = null;
      } catch (e) {
        previewSvg = '';
        previewError = e instanceof Error ? e.message : String(e);
      }
    } else {
      // Excalidraw embedded editor ships in Phase 2; placeholder for now.
      previewSvg = '';
      previewError = null;
    }
  }

  async function loadSnapshots() {
    if (!selected) {
      snapshots = [];
      return;
    }
    try {
      snapshots = await invoke<DiagramSnapshotRow[]>('list_diagram_snapshots', {
        diagramId: selected.id,
      });
    } catch (e) {
      console.warn('[diagrams] list_diagram_snapshots failed:', e);
      snapshots = [];
    }
  }

  async function saveSnapshot() {
    if (!selected) return;
    const label = prompt('Snapshot label (optional):') ?? '';
    creatingSnapshot = true;
    try {
      await invoke('create_diagram_snapshot', {
        diagramId: selected.id,
        trigger: 'manual' satisfies SnapshotTrigger,
        label: label.trim() || null,
      });
      toast.success('Snapshot saved');
      await loadSnapshots();
    } catch (e) {
      toast.error(e);
    } finally {
      creatingSnapshot = false;
    }
  }

  async function restoreSnapshot(snap: DiagramSnapshotRow) {
    if (
      !confirm(
        `Restore snapshot from ${new Date(snap.created_at * 1000).toLocaleString()}? ` +
          'Current file content will be overwritten (a fresh snapshot is taken first).',
      )
    )
      return;
    try {
      await invoke('restore_diagram_snapshot', { snapshotId: snap.id });
      toast.success('Snapshot restored');
      await renderPreview();
      await loadSnapshots();
    } catch (e) {
      toast.error(e);
    }
  }

  async function openInEditor() {
    if (!selected) return;
    try {
      const { openPath } = await import('@tauri-apps/plugin-opener');
      // Backend resolves a project-relative path to an absolute path.
      const absPath = await invoke<string>('resolve_project_path', {
        projectId,
        relPath: selected.file_path,
      });
      await openPath(absPath);
    } catch (e) {
      toast.error(e);
    }
  }

  async function exportSvg() {
    if (!selected || selected.diagram_type !== 'mermaid' || !previewSvg) return;
    try {
      const { save } = await import('@tauri-apps/plugin-dialog');
      const path = await save({
        defaultPath: `${selected.diagram_name}.svg`,
        filters: [{ name: 'SVG', extensions: ['svg'] }],
      });
      if (!path) return;
      await invoke('write_text_file', { path, contents: previewSvg });
      toast.success('SVG exported');
    } catch (e) {
      toast.error(e);
    }
  }

  // ─── File-watch event flow ───────────────────────────────────────────
  let unlistenChanged: (() => void) | null = null;

  async function subscribeToChanges() {
    try {
      // Ask backend to start watching (idempotent). Backend pushes
      // `diagram-changed` events through Tauri's event bus.
      await invoke('subscribe_to_diagram_changes', { projectId });
      unlistenChanged = await listen<DiagramChangedPayload>(
        'diagram-changed',
        (e) => {
          livePushOk = true;
          const payload = e.payload;
          if (payload.project_id !== projectId) return;
          if (payload.kind === 'create' || payload.kind === 'delete') {
            void load();
          } else if (payload.kind === 'edit') {
            void load();
            if (selected && payload.diagram_id === selected.id) {
              void renderPreview();
            }
          } else if (payload.kind === 'snapshot') {
            if (selected && payload.diagram_id === selected.id) {
              void loadSnapshots();
            }
          }
        },
      );
      // Schedule a probe: if no push arrives within 10s, fall back to
      // polling so the user still sees fresh data.
      setTimeout(() => {
        if (!livePushOk && pollTimer == null) {
          pollTimer = setInterval(() => {
            void load();
            if (selected) void loadSnapshots();
          }, 5000);
        }
      }, 10_000);
    } catch (e) {
      console.warn('[diagrams] subscribe_to_diagram_changes failed:', e);
      // Backend not wired yet → poll.
      pollTimer = setInterval(() => {
        void load();
      }, 5000);
    }
  }

  // ─── Lifecycle ───────────────────────────────────────────────────────
  onMount(async () => {
    await loadModuleState();
    if (moduleActive) {
      await load();
      await subscribeToChanges();
    }
  });

  $effect(() => {
    // Re-render preview + reload snapshots when the selection changes.
    if (selected) {
      void renderPreview();
      void loadSnapshots();
    } else {
      previewSvg = '';
      previewError = null;
      snapshots = [];
    }
  });

  $effect(() => {
    // Re-load everything when the project itself changes (nav between
    // projects without remount).
    if (projectId && moduleActive) {
      void load();
    }
  });

  onDestroy(() => {
    if (unlistenChanged) unlistenChanged();
    if (pollTimer) clearInterval(pollTimer);
  });

  // ─── Small helpers ────────────────────────────────────────────────────
  function fmtDate(unixSeconds: number): string {
    return new Date(unixSeconds * 1000).toLocaleString();
  }

  function typeIcon(t: DiagramType): string {
    return t === 'mermaid' ? 'M' : 'E';
  }
</script>

<section class="ps-tab">
  <header class="ps-tab-header">
    <h3>Diagrams</h3>
    {#if moduleActive !== null}
      <label class="ps-module-toggle" title="When disabled, diagrams MCP is not registered for this project and Claude cannot save new diagrams here.">
        <input
          type="checkbox"
          checked={moduleActive}
          disabled={moduleToggling}
          onchange={toggleModule}
          aria-label="Enable diagrams module for this project"
        />
        <span>Diagrams module {moduleActive ? 'enabled' : 'disabled'}</span>
      </label>
    {/if}
  </header>

  {#if moduleActive === null}
    <p class="ps-loading">Loading…</p>
  {:else if !moduleActive}
    <div class="ps-disabled-state">
      <p class="ps-empty">Diagrams module disabled for this project.</p>
      <p class="ps-empty-hint">
        Re-enable above to register the Mermaid / Excalidraw MCPs, list
        diagrams, and use the embedded preview. Disabled state preserves
        any diagrams that already exist on disk — they just don't appear
        in this tab and Claude can't author new ones via the MCP.
      </p>
    </div>
  {:else}
    {#if mermaidVersionWarning}
      <div class="ps-warning" role="alert">
        {mermaidVersionWarning}
      </div>
    {/if}

    <div class="diagrams-split" class:dimmed={moduleToggling}>
      <!-- ─── Left pane: list + add ───────────────────────────────────── -->
      <aside class="diagrams-list" aria-label="Project diagrams list">
        <div class="diagrams-list-header">
          <button
            class="ps-btn-primary"
            onclick={() => (showAdd = !showAdd)}
            aria-expanded={showAdd}
          >
            {showAdd ? 'Cancel' : '+ Add diagram'}
          </button>
        </div>

        {#if showAdd}
          <div class="ps-form" role="dialog" aria-label="Add diagram">
            <label class="ps-form-row">
              <span>Type</span>
              <Dropdown options={TYPE_OPTIONS} bind:value={newType} />
            </label>
            <label class="ps-form-row">
              <span>Name</span>
              <input
                bind:value={newName}
                placeholder="login-form"
                aria-describedby="diagram-name-hint"
              />
            </label>
            <label class="ps-form-row">
              <span>Category path</span>
              <input
                bind:value={newCategoryPath}
                placeholder="gui/auth"
                aria-describedby="diagram-path-hint"
              />
            </label>
            <p id="diagram-name-hint" class="ps-hint">
              Lowercase-kebab: <code>[a-z0-9][a-z0-9-]*</code>.
            </p>
            <p id="diagram-path-hint" class="ps-hint">
              Multi-level allowed. Becomes the diagram's primary tags
              (e.g. <code>gui/auth/</code> → tags <code>gui</code>, <code>auth</code>).
            </p>
            <button
              class="ps-btn-primary"
              onclick={register}
              disabled={adding}
            >
              {adding ? 'Registering…' : 'Register'}
            </button>
          </div>
        {/if}

        {#if loading}
          <p class="ps-loading">Loading…</p>
        {:else if diagrams.length === 0}
          <p class="ps-empty">
            No diagrams registered. Use <code>+ Add diagram</code> or save
            via the Mermaid / Excalidraw MCPs.
          </p>
        {:else}
          <ul class="diagrams-rows" role="listbox" aria-label="Diagrams">
            {#each diagrams as d (d.id)}
              <li
                class="diagrams-row-wrapper"
                class:active={selectedId === d.id}
                class:disabled-row={!d.enabled}
              >
                <!-- Row activator: clicking selects the diagram. The
                     enabled-toggle + delete button are siblings (not
                     nested) so we don't end up with button-in-button or
                     have to stopPropagation on a <label>. -->
                <button
                  class="diagrams-row"
                  onclick={() => (selectedId = d.id)}
                  role="option"
                  aria-selected={selectedId === d.id}
                >
                  <span class="diagrams-row-icon diagrams-row-icon-{d.diagram_type}" aria-hidden="true">
                    {typeIcon(d.diagram_type)}
                  </span>
                  <span class="diagrams-row-main">
                    <span class="diagrams-row-name">{d.diagram_name}</span>
                    <span class="diagrams-row-meta">
                      <code>{d.category_path}</code>
                      · {fmtDate(d.updated_at)}
                    </span>
                  </span>
                  {#if d.snapshot_count > 0}
                    <span class="diagrams-row-badge" title="{d.snapshot_count} snapshots">
                      {d.snapshot_count}
                    </span>
                  {/if}
                </button>
                <label
                  class="ps-toggle ps-toggle-inline"
                  title={d.enabled ? 'Disable diagram' : 'Enable diagram'}
                >
                  <input
                    type="checkbox"
                    checked={d.enabled}
                    onchange={() => toggleDiagram(d)}
                    aria-label="Enable {d.diagram_name}"
                  />
                  <span class="ps-toggle-slider"></span>
                </label>
                <button
                  class="ps-btn-link diagrams-row-del"
                  onclick={() => unregister(d)}
                  aria-label="Unregister {d.diagram_name}"
                >×</button>
              </li>
            {/each}
          </ul>
        {/if}

        {#if !livePushOk && pollTimer != null}
          <p class="ps-hint diagrams-live-badge">
            Live updates pending — falling back to 5s polling.
          </p>
        {/if}
      </aside>

      <!-- ─── Right pane: preview + actions ───────────────────────────── -->
      <main class="diagrams-preview" aria-label="Diagram preview">
        {#if !selected}
          <p class="ps-empty">Select a diagram on the left to preview.</p>
        {:else}
          <div class="diagrams-preview-toolbar">
            <strong>{selected.inferred_title ?? selected.diagram_name}</strong>
            <span class="ps-tag diagrams-tag-kind">
              {selected.diagram_kind ?? selected.diagram_type}
            </span>
            <span class="diagrams-preview-spacer"></span>
            <button class="ps-btn-link" onclick={openInEditor} title="Open file in OS default editor">
              Open in editor
            </button>
            <button
              class="ps-btn-link"
              onclick={saveSnapshot}
              disabled={creatingSnapshot}
            >
              {creatingSnapshot ? 'Saving…' : 'Save snapshot'}
            </button>
            {#if selected.diagram_type === 'mermaid'}
              <button
                class="ps-btn-link"
                onclick={exportSvg}
                disabled={!previewSvg}
                title="Save the rendered SVG to disk"
              >
                Export SVG
              </button>
            {/if}
          </div>

          <div class="diagrams-preview-body">
            {#if selected.diagram_type === 'mermaid'}
              {#if previewError}
                <pre class="diagrams-preview-error">{previewError}</pre>
              {:else if previewSvg}
                <!-- Mermaid output is trusted: rendered with
                     securityLevel='strict' which sanitises user input
                     and disallows raw HTML. -->
                <div class="diagrams-svg-host">
                  {@html previewSvg}
                </div>
              {:else}
                <p class="ps-loading">Rendering…</p>
              {/if}
            {:else}
              <div class="diagrams-excalidraw-placeholder">
                <p>Excalidraw embedded editor lands in Phase 2.</p>
                <button class="ps-btn-link" onclick={openInEditor}>
                  Open in browser / OS editor
                </button>
              </div>
            {/if}
          </div>

          <!-- Snapshot timeline. role="toolbar" lets keyboard users
               focus the individual snapshot buttons via Tab; the
               container itself is non-focusable on purpose so we don't
               add an extra Tab stop. -->
          <div
            class="diagrams-snapshot-strip"
            role="toolbar"
            aria-label="Snapshot timeline"
          >
            {#if snapshots.length === 0}
              <span class="ps-hint">No snapshots yet.</span>
            {:else}
              {#each snapshots as snap (snap.id)}
                <button
                  class="diagrams-snap"
                  onclick={() => restoreSnapshot(snap)}
                  title="Restore snapshot from {fmtDate(snap.created_at)}"
                  aria-label="Restore snapshot {snap.label ?? snap.trigger} from {fmtDate(snap.created_at)}"
                >
                  <span class="diagrams-snap-trigger">{snap.trigger}</span>
                  <span class="diagrams-snap-label">{snap.label ?? '—'}</span>
                  <span class="diagrams-snap-time">{fmtDate(snap.created_at)}</span>
                </button>
              {/each}
            {/if}
          </div>
        {/if}
      </main>
    </div>
  {/if}
</section>

<style>
  .ps-tab { padding: 16px; }
  .ps-tab-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .ps-tab-header h3 { font-size: 16px; margin: 0; }
  .ps-loading, .ps-empty { color: #888; padding: 24px; text-align: center; }
  .ps-empty-hint { color: #aaa; font-size: 12px; padding: 0 0 16px; max-width: 480px; margin: 0 auto; }
  .ps-hint { font-size: 11px; color: #888; margin: 0; }
  .ps-hint code { background: rgba(255,255,255,0.06); padding: 0 4px; border-radius: 2px; }
  .ps-disabled-state { text-align: center; padding: 32px 16px; }
  .ps-warning {
    background: rgba(245,179,66,0.12); color: #f5b342;
    padding: 8px 12px; border-radius: 4px; font-size: 12px;
    margin-bottom: 12px;
  }

  .ps-module-toggle {
    display: flex; align-items: center; gap: 8px;
    font-size: 12px; color: #aaa; cursor: pointer;
  }

  .ps-btn-primary {
    background: rgb(0,191,166); border: none; color: #000;
    padding: 4px 12px; border-radius: 4px; cursor: pointer;
    font-size: 12px; font-weight: 600;
  }
  .ps-btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .ps-btn-link {
    background: none; border: 1px solid transparent; color: #6cf;
    cursor: pointer; font-size: 12px; padding: 4px 8px; border-radius: 4px;
  }
  .ps-btn-link:hover { background: rgba(255,255,255,0.04); }
  .ps-btn-link:disabled { opacity: 0.4; cursor: not-allowed; }

  .ps-form {
    background: rgba(255,255,255,0.04); padding: 12px;
    border-radius: 6px; margin-bottom: 12px;
    display: flex; flex-direction: column; gap: 8px;
  }
  .ps-form-row { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: #888; }
  .ps-form-row input {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12);
    color: inherit; padding: 5px 8px; border-radius: 4px; font-size: 12px;
  }

  /* ── Split-pane ──────────────────────────────────────────────────────── */
  .diagrams-split {
    display: grid;
    grid-template-columns: minmax(260px, 1fr) minmax(0, 2fr);
    gap: 12px;
    align-items: stretch;
  }
  .diagrams-split.dimmed { opacity: 0.6; pointer-events: none; }

  /* ── Left pane ───────────────────────────────────────────────────────── */
  .diagrams-list {
    background: rgba(255,255,255,0.02);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 6px;
    padding: 10px;
    min-height: 320px;
    display: flex; flex-direction: column;
  }
  .diagrams-list-header { display: flex; justify-content: flex-end; margin-bottom: 8px; }
  .diagrams-rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .diagrams-row-wrapper {
    display: flex; align-items: stretch; gap: 4px;
    background: rgba(255,255,255,0.03);
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 2px 4px 2px 0;
  }
  .diagrams-row-wrapper:hover { background: rgba(255,255,255,0.06); }
  .diagrams-row-wrapper.active {
    border-color: rgb(0,191,166);
    background: rgba(0,191,166,0.08);
  }
  .diagrams-row-wrapper.disabled-row { opacity: 0.5; }
  .diagrams-row {
    flex: 1;
    background: none;
    border: none;
    color: inherit;
    text-align: left;
    padding: 6px 8px;
    border-radius: 4px;
    cursor: pointer;
    display: grid;
    grid-template-columns: 22px 1fr auto;
    gap: 8px;
    align-items: center;
    font-size: 12px;
  }
  .diagrams-row-icon {
    width: 22px; height: 22px; border-radius: 4px;
    display: inline-flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 11px;
  }
  .diagrams-row-icon-mermaid { background: rgba(255,82,82,0.20); color: #ff8a8a; }
  .diagrams-row-icon-excalidraw { background: rgba(123,95,255,0.20); color: #c4b3ff; }
  .diagrams-row-main { display: flex; flex-direction: column; min-width: 0; }
  .diagrams-row-name {
    font-weight: 600;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .diagrams-row-meta { color: #888; font-size: 10px; }
  .diagrams-row-meta code { font-family: ui-monospace, monospace; }
  .diagrams-row-badge {
    background: rgba(0,191,166,0.20); color: #6fe7d2;
    padding: 1px 6px; border-radius: 8px; font-size: 10px;
  }
  .diagrams-row-del {
    color: #f99 !important;
    font-size: 14px;
    line-height: 1;
    align-self: center;
  }
  .diagrams-live-badge { margin-top: 8px; }

  /* ── Right pane ──────────────────────────────────────────────────────── */
  .diagrams-preview {
    background: rgba(255,255,255,0.02);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 6px;
    display: flex; flex-direction: column;
    min-height: 320px;
  }
  .diagrams-preview-toolbar {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 12px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    font-size: 12px;
  }
  .diagrams-preview-spacer { flex: 1; }
  .diagrams-preview-body {
    flex: 1; padding: 16px; overflow: auto;
  }
  .diagrams-preview-error {
    background: rgba(255,82,82,0.10); color: #ff8a8a;
    padding: 10px; border-radius: 4px;
    white-space: pre-wrap; font-family: ui-monospace, monospace; font-size: 11px;
  }
  .diagrams-svg-host { display: flex; justify-content: center; }
  .diagrams-svg-host :global(svg) { max-width: 100%; height: auto; }
  .diagrams-excalidraw-placeholder {
    text-align: center; padding: 24px;
    color: #aaa; font-size: 12px;
  }

  /* ── Snapshot timeline ──────────────────────────────────────────────── */
  .diagrams-snapshot-strip {
    display: flex; gap: 6px; overflow-x: auto;
    padding: 10px 12px;
    border-top: 1px solid rgba(255,255,255,0.06);
    min-height: 50px;
    align-items: center;
  }
  .diagrams-snapshot-strip:focus { outline: 1px solid rgb(0,191,166); outline-offset: -1px; }
  .diagrams-snap {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.10);
    color: inherit;
    padding: 4px 8px;
    border-radius: 4px;
    cursor: pointer;
    display: flex; flex-direction: column; gap: 2px;
    font-size: 10px; min-width: 80px;
  }
  .diagrams-snap:hover { background: rgba(0,191,166,0.10); border-color: rgba(0,191,166,0.4); }
  .diagrams-snap-trigger { color: #888; font-size: 9px; }
  .diagrams-snap-label { color: #ccc; }
  .diagrams-snap-time { color: #666; font-size: 9px; }

  /* ── Toggle (shared with AgentsTab pattern) ─────────────────────────── */
  .ps-toggle { position: relative; display: inline-block; width: 32px; height: 16px; cursor: pointer; }
  .ps-toggle-inline { align-self: center; }
  .ps-toggle input { opacity: 0; width: 0; height: 0; }
  .ps-toggle-slider {
    position: absolute; inset: 0; background: rgba(255,255,255,0.15);
    border-radius: 8px; transition: background 0.15s ease;
  }
  .ps-toggle-slider::before {
    content: ''; position: absolute; top: 2px; left: 2px;
    width: 12px; height: 12px; background: #ddd; border-radius: 50%;
    transition: transform 0.15s ease;
  }
  .ps-toggle input:checked + .ps-toggle-slider { background: rgb(0,191,166); }
  .ps-toggle input:checked + .ps-toggle-slider::before { transform: translateX(16px); }

  .ps-tag {
    font-size: 10px; padding: 1px 6px; border-radius: 8px;
    background: rgba(255,255,255,0.08); color: #ccc;
  }
  .diagrams-tag-kind { background: rgba(123,95,255,0.15); color: #c4b3ff; }
</style>
