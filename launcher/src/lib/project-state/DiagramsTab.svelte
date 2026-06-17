<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  // Phase 1 of the diagrams-integration plan (2026-05-24): launcher tab
  // that lists project diagrams (Mermaid + Excalidraw), previews them,
  // and exposes snapshot + access controls.
  //
  // Backend wiring (v0.2.34 Agent D): all `invoke<...>` calls below
  // hit real Tauri commands — `read_project_diagram_source`,
  // `write_text_file`, `resolve_project_path`, and
  // `subscribe_to_diagram_changes` were the four missing commands
  // (added in v0.2.34); the rest were in v0.2.33.
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
  import ExcalidrawEditor from './ExcalidrawEditor.svelte';
  import MermaidEditor from './MermaidEditor.svelte';

  // ─── Props ────────────────────────────────────────────────────────────
  let { projectId }: { projectId: string } = $props();

  // ─── Diagram-name validation (v0.2.61) ──────────────────────────────────
  // A diagram name becomes a FILENAME at
  // `.claude/diagrams/<category>/<name>.<ext>` and must round-trip through
  // the auto-register path parser (`AUTO_REGISTER_PATH_RE` below). The DB +
  // MCP impose NO name pattern (diagram_name is a free-form column), so the
  // only real constraints are path/parser safety:
  //   - must NOT contain `/` (path separator) or `.` (extension delimiter),
  //   - must NOT contain whitespace or control chars,
  //   - must start + end with an alphanumeric or `_` (no leading/trailing
  //     `-`, no surprises for the parser).
  // Everything else a normal filename allows is fine — including `_` and
  // mixed case. (Previously this was lowercase-kebab-only `[a-z0-9][a-z0-9-]*`,
  // which rejected perfectly safe names like `my_diagram`.)
  const DIAGRAM_NAME_RE = /^[A-Za-z0-9_][A-Za-z0-9_-]*$/;
  // The human-facing rule, kept in sync with DIAGRAM_NAME_RE. Shown on reject.
  const DIAGRAM_NAME_RULE =
    'Letters, numbers, hyphen (-) and underscore (_) only; must start/end with a letter, number, or underscore. No spaces, slashes, or dots.';
  function isValidDiagramName(name: string): boolean {
    return DIAGRAM_NAME_RE.test(name);
  }

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
      // Defensive fallback — `is_project_module_active` is wired since
      // v0.2.33 and v0.2.34 added a backfill for orchestrator-bundled
      // modules so an absent row resolves to `true` rather than `false`.
      // This branch now only fires on Tauri-IPC-level errors.
      console.warn('[diagrams] is_project_module_active failed:', e);
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
  // 5-second polling fallback. Kicks in if `diagram-changed` events
  // don't arrive within 10s of mount — soft-fail safety net for the
  // case where `subscribe_to_diagram_changes` short-circuited on a
  // watcher init error (read-only volume, permission denied, etc).
  // The Tauri command always returns Ok so the frontend can't tell
  // setup failed; the 10s probe is how we self-heal to polling.
  // pollTimer is `$state` so the `Live updates pending` badge
  // re-renders when it transitions null → number; the handle itself
  // never gets used reactively, only the truthiness of "are we polling?".
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

  // ─── Draft state: "Draw new" inline editor (v0.2.35 Agent L) ─────────
  // The DiagramsTab originally accepted only externally-created diagrams
  // (the "+ Add diagram" modal registers a file path; the user creates
  // the content elsewhere). v0.2.35 adds two affordances for creating
  // diagrams directly from the launcher:
  //   1. "Draw new Mermaid" → inline MermaidEditor (textarea + preview).
  //   2. "Draw new Excalidraw" → embedded ExcalidrawEditor with empty
  //      initial scene.
  // The draft persists in component state until the user clicks "Save
  // as new", at which point we prompt for name + category, then call
  // register_project_diagram + write_text_file (both Tauri commands
  // shipped by Agent D in v0.2.34).
  //
  // While drafting, the right pane is fully owned by the draft editor
  // (the registered-diagram preview is hidden). Cancel discards the
  // draft. Selecting a registered diagram cancels the draft.
  let draftingType = $state<DiagramType | null>(null);
  let draftMermaidSource = $state<string>('');
  let draftExcalidrawSource = $state<string>('');
  let showSaveDraftDialog = $state(false);
  let draftSaveName = $state<string>('');
  let draftSaveCategory = $state<string>('');
  let savingDraft = $state(false);
  // Drag-drop import zone state — single boolean for visual hover/active.
  let dropZoneActive = $state(false);

  function startDrawing(type: DiagramType) {
    // Cancel any current selection so the right pane shows the draft.
    selectedId = null;
    draftingType = type;
    if (type === 'mermaid') {
      draftMermaidSource = '';
    } else {
      draftExcalidrawSource = '';
    }
  }

  function cancelDraft() {
    if (draftingType === null) return;
    if (
      (draftingType === 'mermaid' && draftMermaidSource.trim()) ||
      (draftingType === 'excalidraw' && draftExcalidrawSource.trim())
    ) {
      if (!confirm('Discard the draft? Unsaved content will be lost.')) return;
    }
    draftingType = null;
    draftMermaidSource = '';
    draftExcalidrawSource = '';
    showSaveDraftDialog = false;
  }

  function openSaveDraftDialog() {
    if (!draftingType) return;
    // Pre-fill with safe defaults the user can edit.
    draftSaveName = '';
    draftSaveCategory = '';
    showSaveDraftDialog = true;
  }

  async function saveDraft() {
    if (!draftingType) return;
    const name = draftSaveName.trim();
    const category = draftSaveCategory.trim();
    if (!name) {
      toast.error('Diagram name required');
      return;
    }
    if (!category) {
      toast.error('Category path required (e.g. gui/auth)');
      return;
    }
    if (!isValidDiagramName(name)) {
      toast.error(`Invalid name. ${DIAGRAM_NAME_RULE}`);
      return;
    }
    if (!/^[a-z0-9][a-z0-9/-]*[a-z0-9]$/.test(category)) {
      toast.error('Category path must be kebab segments separated by /');
      return;
    }
    const ext = draftingType === 'mermaid' ? 'mmd' : 'excalidraw';
    const filePath = `.claude/diagrams/${category}/${name}.${ext}`;
    const contents =
      draftingType === 'mermaid' ? draftMermaidSource : draftExcalidrawSource;
    savingDraft = true;
    try {
      // 1. Register the diagram row (file_path is project-relative).
      const row = await invoke<DiagramRow>('register_project_diagram', {
        projectId,
        req: {
          diagram_name: name,
          diagram_type: draftingType,
          file_path: filePath,
          category_path: category,
        },
      });
      // 2. Resolve to absolute path + write contents atomically.
      const absPath = await invoke<string>('resolve_project_path', {
        projectId,
        relPath: filePath,
      });
      await invoke('write_text_file', { path: absPath, contents });
      toast.success(`Saved ${name}`);
      // 3. Reset draft + reload list + select the new row.
      draftingType = null;
      draftMermaidSource = '';
      draftExcalidrawSource = '';
      showSaveDraftDialog = false;
      await load();
      selectedId = row.id;
    } catch (e) {
      toast.error(e);
    } finally {
      savingDraft = false;
    }
  }

  // ─── Drag-drop file import (v0.2.35 Agent L) ──────────────────────────
  // The empty-state of the registry now exposes a drop zone — the user
  // can drag a `.mmd` / `.excalidraw` file from their file manager onto
  // it and the launcher (a) reads the file content, (b) infers the type
  // from extension, (c) prompts for name + category, (d) calls the same
  // register + write_text_file flow as `saveDraft` above.
  //
  // Why this is a soft-fail-only path: Tauri's webview exposes the
  // browser `File` API on drop events, so we can read the dropped file
  // entirely client-side. If the drop has multiple files we only accept
  // the first; if the extension isn't recognised we toast an error.
  //
  // After reading, we stash the content into the draft slots and open
  // the save dialog — so the drop flow funnels into the same persistence
  // path as `saveDraft` (register + write_text_file). No extra commands.
  function onDragOver(e: DragEvent) {
    e.preventDefault();
    dropZoneActive = true;
  }
  function onDragLeave(_e: DragEvent) {
    dropZoneActive = false;
  }
  async function onDrop(e: DragEvent) {
    e.preventDefault();
    dropZoneActive = false;
    const files = e.dataTransfer?.files;
    if (!files || files.length === 0) return;
    const file = files[0];
    const lower = file.name.toLowerCase();
    let type: DiagramType | null = null;
    if (lower.endsWith('.mmd')) type = 'mermaid';
    else if (lower.endsWith('.excalidraw')) type = 'excalidraw';
    else {
      toast.error(
        `Unsupported file type: ${file.name}. Drop a .mmd or .excalidraw file.`,
      );
      return;
    }
    try {
      const text = await file.text();
      // Pre-fill the save dialog with the file's basename (stripped of
      // extension) — user can override before confirming.
      const base = file.name.replace(/\.(mmd|excalidraw)$/i, '');
      draftSaveName = base.toLowerCase().replace(/[^a-z0-9-]+/g, '-')
        .replace(/^-+|-+$/g, '');
      draftSaveCategory = '';
      showSaveDraftDialog = true;
      // Stash the pending content into the draft slot so `saveDraft` can
      // pick it up without rewriting the persistence path.
      draftingType = type;
      if (type === 'mermaid') draftMermaidSource = text;
      else draftExcalidrawSource = text;
    } catch (err) {
      toast.error(err);
    }
  }

  // ─── v0.2.36 Agent R — vendored visual editor opener ────────────────
  // Distinct from `startDrawing` (which opens the in-tab inline editor).
  // `openVisualEditor` calls the backend's `open_diagrams_editor` Tauri
  // command, which:
  //   1. Creates an empty file under `.claude/diagrams/visual-draft/`
  //      so the file watcher has a target.
  //   2. Lazy-starts the local diagrams-editor HTTP server.
  //   3. Opens the vendored Mermaid (or Excalidraw bridge) page in the
  //      user's DEFAULT BROWSER — NOT in the Tauri WebView, which has
  //      Wayland+webkit2gtk rendering bugs for both libraries.
  //
  // Auto-register on first non-blank save is handled by the existing
  // `diagram-changed` event handler below — when an `edit` payload
  // arrives for an UNREGISTERED file under `.claude/diagrams/`, we
  // call `register_project_diagram` silently so the file appears in
  // the registry without a second user action. See the watcher event
  // handler in `subscribeToChanges` for the auto-register branch.
  let openingVisualEditor = $state(false);
  async function openVisualEditor(type: DiagramType) {
    // Prompt the user for a name; the rest of the file shape is
    // hard-coded to `.claude/diagrams/visual-draft/<name>.<ext>`. The
    // user can re-organise via the normal "+ Add diagram" flow once
    // the file is registered (or unregister + re-register elsewhere).
    const raw = window.prompt(
      `Name for the new ${type === 'mermaid' ? 'Mermaid' : 'Excalidraw'} diagram?\n` +
        `(e.g. "login-flow" or "my_diagram"; saved under .claude/diagrams/visual-draft/)`,
    );
    if (raw === null) return;
    const name = raw.trim();
    if (!name) {
      toast.error('Name cannot be empty');
      return;
    }
    if (!isValidDiagramName(name)) {
      toast.error(`Invalid name. ${DIAGRAM_NAME_RULE}`);
      return;
    }
    openingVisualEditor = true;
    try {
      const url = await invoke<string>('open_diagrams_editor', {
        projectId,
        diagramType: type,
        name,
      });
      toast.info(`Opening ${type} editor in your default browser…`);
      console.info('[diagrams] open_diagrams_editor →', url);
    } catch (e) {
      toast.error(e);
    } finally {
      openingVisualEditor = false;
    }
  }

  // Track which files we've already tried to auto-register so we don't
  // hammer the backend on every save burst. Keys are the relative file
  // path the watcher resolves; entries live for the component lifetime.
  const autoRegisterTried = new Set<string>();

  async function tryAutoRegister(diagramId: number, relPath: string | null) {
    // The watcher emits `diagram_id: -1` when it can't resolve the
    // path to a registered row — that's the auto-register signal.
    if (diagramId !== -1) return;
    if (!relPath) return;
    if (autoRegisterTried.has(relPath)) return;
    autoRegisterTried.add(relPath);

    // Path shape: .claude/diagrams/<category-path>/<name>.<ext>
    // Name charset MUST stay in lockstep with DIAGRAM_NAME_RE (the
    // creation-time validator) so any name we let the user create also
    // round-trips through this on-disk auto-register parser.
    const m = relPath.match(
      /^\.claude\/diagrams\/(.+)\/([A-Za-z0-9_][A-Za-z0-9_-]*)\.(mmd|excalidraw)$/,
    );
    if (!m) {
      console.warn('[diagrams] auto-register: skipping unsupported path shape:', relPath);
      return;
    }
    const category = m[1];
    const name = m[2];
    const type: DiagramType = m[3] === 'mmd' ? 'mermaid' : 'excalidraw';
    try {
      await invoke('register_project_diagram', {
        projectId,
        req: {
          diagram_name: name,
          diagram_type: type,
          file_path: relPath,
          category_path: category,
        },
      });
      toast.info(`Auto-registered ${name} (${type})`);
      await load();
    } catch (e) {
      // Soft-fail: maybe the row already exists (race with a previous
      // save event) — list_project_diagrams will reflect reality on
      // the next refresh. Don't toast on UNIQUE constraint errors.
      const msg = String(e);
      if (!/unique|already exists/i.test(msg)) {
        console.warn('[diagrams] auto-register failed:', e);
      }
    }
  }

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
    if (!isValidDiagramName(name)) {
      toast.error(`Invalid name. ${DIAGRAM_NAME_RULE}`);
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

  // ─── Excalidraw embedded editor state (Phase 2, 2026-05-25) ──────────
  // The embedded React-in-Svelte editor mounts via ExcalidrawEditor.svelte
  // when the selected diagram is .excalidraw AND the runtime environment
  // doesn't trip the Wayland+webkit2gtk fallback. The fallback path
  // shows an "Open externally" prompt instead of an inline editor — see
  // docs/EXCALIDRAW_WAYLAND_TEST.md for the threshold + rationale.
  //
  // `excalidrawSource` is the JSON string read from disk; passed into
  // the editor as `initialSceneJson`. The editor calls back with the
  // serialised scene on every (debounced 300ms) change.
  let excalidrawSource = $state<string>('');
  let excalidrawLoading = $state(false);
  let excalidrawWaylandFallback = $state<boolean | null>(null); // null = unchecked
  let excalidrawExportSvg = $state<(() => Promise<string | null>) | null>(null);

  async function detectExcalidrawFallback(): Promise<boolean> {
    // Wayland + webkit2gtk has documented canvas latency / pointer
    // event issues for Excalidraw (plan §4 Risk 5, docs/
    // EXCALIDRAW_WAYLAND_TEST.md). Two signals trigger the fallback:
    //   1. Tauri exposes XDG_SESSION_TYPE via an env-read command
    //      (best signal; only fires when the launcher backend confirms
    //      Wayland is the active session type).
    //   2. Navigator UA contains "WebKit" (covers webkit2gtk webview,
    //      Safari, and Tauri's macOS WebKit — macOS is fine, but the
    //      env-read signal disambiguates).
    //
    // We require BOTH signals before falling back so we don't
    // accidentally disable the embed for Safari users testing the
    // launcher in a browser preview (rare but possible).
    try {
      const ua = (typeof navigator !== 'undefined' && navigator.userAgent) || '';
      const hasWebkit = /WebKit/i.test(ua);
      let sessionType = '';
      try {
        sessionType = await invoke<string>('read_env_var', {
          name: 'XDG_SESSION_TYPE',
        });
      } catch {
        // Backend command not present → skip the env check; fall back
        // only on a webview confirmation later, never silently.
        sessionType = '';
      }
      const isWayland = sessionType.toLowerCase() === 'wayland';
      return hasWebkit && isWayland;
    } catch {
      return false;
    }
  }

  async function loadExcalidrawSource() {
    if (!selected) {
      excalidrawSource = '';
      return;
    }
    excalidrawLoading = true;
    try {
      const txt = await readFile(selected.file_path);
      excalidrawSource = txt;
    } catch (e) {
      // File doesn't exist yet (just-registered diagram with no
      // starter content) — boot empty.
      console.info('[diagrams] excalidraw source unreadable, booting empty:', e);
      excalidrawSource = '';
    } finally {
      excalidrawLoading = false;
    }
  }

  async function saveExcalidrawSource(sceneJsonString: string) {
    if (!selected) return;
    // Resolve relative file_path → absolute (the existing helper
    // `resolve_project_path` already does this for Open in editor).
    try {
      const absPath = await invoke<string>('resolve_project_path', {
        projectId,
        relPath: selected.file_path,
      });
      await invoke('write_text_file', {
        path: absPath,
        contents: sceneJsonString,
      });
      // The PostToolUse hook on Write(.claude/diagrams/**) will fire
      // the indexer; we don't need to invoke it here. The live-push
      // subscription will refresh `diagrams` in turn.
    } catch (e) {
      // Surface a single toast per session-of-failures rather than
      // one per debounced save; the editor will retry on next change.
      console.warn('[diagrams] excalidraw save failed:', e);
      toast.error(e);
    }
  }

  async function exportExcalidrawSvg() {
    if (!selected || selected.diagram_type !== 'excalidraw') return;
    if (!excalidrawExportSvg) return;
    try {
      const svg = await excalidrawExportSvg();
      if (!svg) {
        toast.error('Excalidraw export returned nothing — try again.');
        return;
      }
      const { save } = await import('@tauri-apps/plugin-dialog');
      const path = await save({
        defaultPath: `${selected.diagram_name}.svg`,
        filters: [{ name: 'SVG', extensions: ['svg'] }],
      });
      if (!path) return;
      await invoke('write_text_file', { path, contents: svg });
      toast.success('SVG exported');
    } catch (e) {
      toast.error(e);
    }
  }

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
    // Backend resolves `relPath` against the project root and
    // enforces the `.claude/diagrams/` scoped boundary (see
    // `commands/diagrams_cmd.rs::read_project_diagram_source`).
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
      // Excalidraw embedded editor (Phase 2, 2026-05-25). The actual
      // canvas lives in <ExcalidrawEditor>; this branch just loads the
      // on-disk JSON source so the editor's `initialSceneJson` prop is
      // current. The Mermaid-specific `previewSvg`/`previewError` are
      // unused for excalidraw rows.
      previewSvg = '';
      previewError = null;
      await loadExcalidrawSource();
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
            // v0.2.36 Agent R: auto-register-on-first-edit. The watcher
            // sets `diagram_id: -1` when the file isn't in the registry;
            // we call `register_project_diagram` silently to bring it
            // under management. tryAutoRegister is a no-op when the
            // file is already known or the path shape doesn't match.
            void tryAutoRegister(payload.diagram_id, payload.file_path);
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
      // The backend command returns Ok even on watcher init failure
      // (soft-fail design — see `commands/diagram_watcher.rs`), so we
      // only reach this branch on a Tauri-IPC-level error. Polling
      // fallback keeps the feature usable.
      console.warn('[diagrams] subscribe_to_diagram_changes failed:', e);
      pollTimer = setInterval(() => {
        void load();
      }, 5000);
    }
  }

  // ─── Lifecycle ───────────────────────────────────────────────────────
  onMount(async () => {
    await loadModuleState();
    // Run Wayland detection in parallel with the module-state load —
    // it's a single env-var read so cost is negligible, but we want
    // the result settled before the user clicks on an excalidraw row.
    void detectExcalidrawFallback().then((flag) => {
      excalidrawWaylandFallback = flag;
      if (flag) {
        console.warn(
          '[diagrams] Wayland+webkit2gtk detected — Excalidraw ' +
          'embedded editor disabled, will fall back to Open externally. ' +
          'See docs/EXCALIDRAW_WAYLAND_TEST.md',
        );
      }
    });
    if (moduleActive) {
      await load();
      await subscribeToChanges();
    }
  });

  $effect(() => {
    // Re-render preview + reload snapshots when the selection changes.
    if (selected) {
      // v0.2.35 Agent L: selecting a registered diagram implicitly
      // cancels any in-progress draft. We don't `confirm()` here
      // because the parent's selection click is intentional UX (the
      // user-explicit `cancelDraft` button covers the unsaved-warning
      // path).
      if (draftingType !== null) {
        draftingType = null;
        showSaveDraftDialog = false;
      }
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
        <!-- v0.2.35 Agent L: three creation affordances live here.
             "+ Add diagram" registers an externally-created file (legacy
             v0.2.34 flow). "Draw Mermaid (text)" / "Draw Mermaid (visual)"
             / "Draw Excalidraw (visual)" each open a different editor:
             - text: inline textarea + preview (still works; useful for
               code-first users).
             - visual: opens the vendored editor in the user's default
               browser via the local diagrams-editor HTTP server
               (v0.2.36 Agent R rework). Replaces the previously-broken
               embedded Excalidraw editor that rendered as enormous
               icons in the Tauri WebView on Wayland+webkit2gtk. -->
        <div class="diagrams-list-header">
          <button
            class="ps-btn-primary"
            onclick={() => (showAdd = !showAdd)}
            aria-expanded={showAdd}
          >
            {showAdd ? 'Cancel' : '+ Add diagram'}
          </button>
          <button
            class="ps-btn-secondary"
            onclick={() => startDrawing('mermaid')}
            title="Draft a new Mermaid diagram inline (textarea + live preview); save auto-registers it."
          >
            Draw Mermaid (text)
          </button>
          <button
            class="ps-btn-secondary"
            onclick={() => openVisualEditor('mermaid')}
            disabled={openingVisualEditor}
            title="Open a vendored Mermaid visual editor in your default browser. Save auto-registers it."
          >
            Draw Mermaid (visual)
          </button>
          <button
            class="ps-btn-secondary"
            onclick={() => openVisualEditor('excalidraw')}
            disabled={openingVisualEditor}
            title="Open the Excalidraw workflow page in your default browser (draw at excalidraw.com, export, then drag the file into the Diagrams tab)."
          >
            Draw Excalidraw
          </button>
        </div>

        {#if showAdd}
          <!-- v0.2.35 (a11y, Agent O): inline form was role="dialog" but
               it's an in-place expandable form, NOT a modal dialog. SR
               users get a misleading "dialog" announcement otherwise.
               role="group" with aria-labelledby pointing to a labelling
               heading correctly communicates "this is a grouped form
               control set" without the modal semantics. -->
          <div
            class="ps-form"
            role="group"
            aria-labelledby="add-diagram-form-heading"
          >
            <h5 id="add-diagram-form-heading" class="sr-only">Add diagram form</h5>
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
              Letters, numbers, <code>-</code> and <code>_</code>. Start/end
              with a letter, number, or <code>_</code>. No spaces, slashes, or
              dots.
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
          <!-- v0.2.35 Agent L: empty-state drop zone. Drag a .mmd or
               .excalidraw file from the file manager → reads content
               client-side via the browser File API → opens the save
               dialog pre-filled with the file's basename. Drag-drop
               here is the third creation entry point (after "+ Add
               diagram" and "Draw new"). -->
          <!-- v0.2.35 (a11y, Agent O): drop zone announces dragover/leave
               state via aria-live so SR users hear the "Release to
               import…" prompt when a draggable enters the zone. Polite
               so it doesn't interrupt; the visual border change already
               serves sighted users. -->
          <div
            class="diagrams-empty-state"
            class:drop-active={dropZoneActive}
            ondragover={onDragOver}
            ondragleave={onDragLeave}
            ondrop={onDrop}
            role="region"
            aria-label="Drop diagram files here to import"
          >
            <p class="ps-empty">
              No diagrams registered. Use <code>+ Add diagram</code>,
              <code>Draw Mermaid</code> / <code>Draw Excalidraw</code>,
              or drop a <code>.mmd</code> / <code>.excalidraw</code> file here.
            </p>
            <p class="diagrams-drop-hint" aria-live="polite">
              {dropZoneActive
                ? 'Release to import…'
                : 'Drop file to import'}
            </p>
          </div>
        {:else}
          <!-- v0.2.35 (a11y, Agent O): listbox semantics require direct
               role="option" children. The <li> wrappers exist for
               layout (they pair the row activator with a toggle + delete
               button); marking them role="none" tells AT to skip the
               list wrapper and treat the inner role="option" button as
               the option itself. The sibling toggle/delete buttons stay
               focusable via Tab even though they aren't options
               (assistive tech handles this via the standard listbox
               pattern + tabindex). -->
          <ul class="diagrams-rows" role="listbox" aria-label="Diagrams">
            {#each diagrams as d (d.id)}
              <li
                class="diagrams-row-wrapper"
                class:active={selectedId === d.id}
                class:disabled-row={!d.enabled}
                role="none"
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
        {#if draftingType !== null}
          <!-- v0.2.35 Agent L: draft state. Right pane is owned by the
               inline editor (MermaidEditor or ExcalidrawEditor) until
               the user clicks "Save as new" (opens the name/category
               dialog) or "Cancel" (discards the draft). -->
          <div class="diagrams-preview-toolbar">
            <strong>Drafting new {draftingType} diagram</strong>
            <span class="ps-tag diagrams-tag-kind">draft</span>
            <span class="diagrams-preview-spacer"></span>
            <button
              class="ps-btn-primary"
              onclick={openSaveDraftDialog}
              disabled={savingDraft}
              title="Register this draft as a new project diagram and write the file."
            >
              Save as new
            </button>
            <button class="ps-btn-link" onclick={cancelDraft}>Cancel</button>
          </div>
          <div class="diagrams-preview-body">
            {#if draftingType === 'mermaid'}
              <MermaidEditor bind:source={draftMermaidSource} diagramName="draft" />
            {:else if excalidrawWaylandFallback}
              <div class="diagrams-excalidraw-placeholder">
                <p>
                  Embedded Excalidraw editor disabled on Wayland +
                  webkit2gtk (known canvas latency / pointer issue).
                </p>
                <p class="ps-hint">
                  See <code>docs/EXCALIDRAW_WAYLAND_TEST.md</code> for the
                  test recipe and reproduction steps. Cancel the draft and
                  use an external editor instead.
                </p>
              </div>
            {:else}
              <ExcalidrawEditor
                diagramName="draft"
                initialSceneJson={draftExcalidrawSource}
                onSave={async (json) => {
                  draftExcalidrawSource = json;
                }}
              />
            {/if}
          </div>

          {#if showSaveDraftDialog}
            <!-- v0.2.35 (a11y, Agent O): "Modal-ish" inline form, not a
                 true modal — focus isn't trapped, backdrop doesn't
                 block. role="dialog" was misleading; switched to
                 role="group" with aria-labelledby pointing to the
                 visible heading so SR users get the same name without
                 the false-modal announcement. -->
            <div class="diagrams-save-dialog" role="group" aria-labelledby="save-draft-heading">
              <h4 id="save-draft-heading">Save draft as new diagram</h4>
              <label class="ps-form-row">
                <span>Name</span>
                <input
                  bind:value={draftSaveName}
                  placeholder="login-form"
                  aria-label="Diagram name"
                />
              </label>
              <label class="ps-form-row">
                <span>Category path</span>
                <input
                  bind:value={draftSaveCategory}
                  placeholder="gui/auth"
                  aria-label="Category path"
                />
              </label>
              <p class="ps-hint">
                Lowercase-kebab name; multi-level category path. File will
                be written to
                <code>
                  .claude/diagrams/{draftSaveCategory || '<category>'}/{draftSaveName || '<name>'}.{draftingType === 'mermaid' ? 'mmd' : 'excalidraw'}
                </code>.
              </p>
              <div class="diagrams-save-dialog-actions">
                <button
                  class="ps-btn-primary"
                  onclick={saveDraft}
                  disabled={savingDraft}
                >
                  {savingDraft ? 'Saving…' : 'Save'}
                </button>
                <button
                  class="ps-btn-link"
                  onclick={() => (showSaveDraftDialog = false)}
                >
                  Cancel
                </button>
              </div>
            </div>
          {/if}
        {:else if !selected}
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
            {:else if selected.diagram_type === 'excalidraw' && !excalidrawWaylandFallback}
              <button
                class="ps-btn-link"
                onclick={exportExcalidrawSvg}
                disabled={!excalidrawExportSvg}
                title="Export the Excalidraw scene to SVG"
              >
                Export SVG
              </button>
            {/if}
          </div>

          <div class="diagrams-preview-body">
            {#if selected.diagram_type === 'mermaid'}
              {#if previewError}
                <!-- v0.2.35 (a11y, Agent O): render errors are async,
                     not user-triggered — role="alert" interrupts and
                     announces immediately so SR users hear about syntax
                     errors as they type. -->
                <pre class="diagrams-preview-error" role="alert">{previewError}</pre>
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
              <!-- Excalidraw branch (Phase 2, 2026-05-25). Three states:
                   1. fallback triggered (Wayland+webkit2gtk) → "Open
                      externally" prompt. Doc reference baked into the
                      info text so the user can find the test recipe.
                   2. source loading → spinner.
                   3. ready → embedded editor mounted. -->
              {#if excalidrawWaylandFallback}
                <div class="diagrams-excalidraw-placeholder">
                  <p>
                    Embedded Excalidraw editor disabled on Wayland +
                    webkit2gtk (known canvas latency / pointer issue).
                  </p>
                  <p class="ps-hint">
                    See <code>docs/EXCALIDRAW_WAYLAND_TEST.md</code> for the
                    test recipe and reproduction steps.
                  </p>
                  <button class="ps-btn-link" onclick={openInEditor}>
                    Open in OS default editor
                  </button>
                </div>
              {:else if excalidrawLoading}
                <p class="ps-loading">Loading scene…</p>
              {:else}
                <ExcalidrawEditor
                  diagramName={selected.diagram_name}
                  initialSceneJson={excalidrawSource}
                  onSave={saveExcalidrawSource}
                  bind:exportSvgFn={excalidrawExportSvg}
                />
              {/if}
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

  /* ── v0.2.35 Agent L: Draw-new buttons + drop zone + save dialog ─── */
  .diagrams-list-header {
    flex-wrap: wrap;
    gap: 6px;
    justify-content: flex-start;
  }
  .ps-btn-secondary {
    background: rgba(123, 95, 255, 0.20);
    border: 1px solid rgba(123, 95, 255, 0.40);
    color: #c4b3ff;
    padding: 4px 10px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 11px;
    font-weight: 500;
  }
  .ps-btn-secondary:hover {
    background: rgba(123, 95, 255, 0.30);
  }
  .ps-btn-secondary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .diagrams-empty-state {
    padding: 20px;
    border: 2px dashed rgba(255, 255, 255, 0.15);
    border-radius: 6px;
    transition: all 0.15s ease;
    text-align: center;
  }
  .diagrams-empty-state.drop-active {
    border-color: rgb(0, 191, 166);
    background: rgba(0, 191, 166, 0.08);
  }
  .diagrams-drop-hint {
    margin-top: 12px;
    color: #888;
    font-size: 11px;
    font-style: italic;
  }
  .diagrams-empty-state.drop-active .diagrams-drop-hint {
    color: rgb(0, 191, 166);
    font-weight: 600;
    font-style: normal;
  }
  .diagrams-save-dialog {
    margin: 12px;
    padding: 12px;
    background: rgba(0, 0, 0, 0.4);
    border: 1px solid rgba(0, 191, 166, 0.40);
    border-radius: 6px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .diagrams-save-dialog h4 {
    margin: 0;
    font-size: 13px;
    color: #c4b3ff;
  }
  .diagrams-save-dialog-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 4px;
  }

  /* v0.2.35 (a11y, Agent O): visually-hidden heading used for the
     role="group" form labelling pattern. Standard WCAG hide-from-sighted
     /show-to-AT helper. */
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
</style>
