// Project selector state.
//
// Backs Screen 1 (project selector in MenuBar). Selected project ID is
// persisted to localStorage as `vct.selected_project_id`. Survives reloads.

import { writable, derived, get } from 'svelte/store';
import { invoke, tauriAvailable } from '$lib/tauri';
import type {
  ProjectView,
  ProjectHost,
  SwitchHostResult,
  CreateProjectResult,
  RenameProjectResult,
  UpdateProjectResult,
  UpdateAllOptions,
  UpdateAllReport,
  UnregisterOptions,
  UnregisterReport,
} from '$lib/types/launcher';
import { toast } from '$lib/stores/toast';
import { isErrorWarning } from '$lib/warning-severity';

const SELECTED_KEY = 'vct.selected_project_id';

interface ProjectsState {
  projects: ProjectView[];
  selectedId: string | null;
  loading: boolean;
  error: string | null;
}

function loadSelected(): string | null {
  if (typeof localStorage === 'undefined') return null;
  return localStorage.getItem(SELECTED_KEY);
}

function saveSelected(id: string | null) {
  if (typeof localStorage === 'undefined') return;
  if (id) localStorage.setItem(SELECTED_KEY, id);
  else localStorage.removeItem(SELECTED_KEY);
}

function createProjectsStore() {
  const { subscribe, update } = writable<ProjectsState>({
    projects: [],
    selectedId: loadSelected(),
    loading: false,
    error: null,
  });

  return {
    subscribe,

    /** Load projects from backend. Resolves stale selection if the saved
     * id no longer exists. */
    async load(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, loading: true, error: null }));
      try {
        const projects = await invoke<ProjectView[]>('list_projects_v2');
        update((s) => {
          let selectedId = s.selectedId;
          if (selectedId && !projects.find((p) => p.id === selectedId)) {
            selectedId = null;
            saveSelected(null);
          }
          // If nothing selected and there's exactly one project, auto-select.
          if (!selectedId && projects.length === 1) {
            selectedId = projects[0].id;
            saveSelected(selectedId);
          }
          return { ...s, projects, selectedId, loading: false };
        });
      } catch (e) {
        update((s) => ({
          ...s,
          loading: false,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    select(id: string | null) {
      saveSelected(id);
      update((s) => ({ ...s, selectedId: id }));
    },

    // v0.2.63: `safe_add` is the per-add "Safe add" flag (default false → no
    // behaviour change). When true, VCO won't merge its config into the
    // project's sensitive, often-committed project-root `.env` — it writes a
    // `.env.vco.reference` sidecar + a deferral instead, and keeps VCO files
    // out of the repo's commits via local-only `.git/info/exclude`.
    async create(
      name: string,
      folder_path: string,
      host: ProjectHost,
      safe_add: boolean = false,
    ): Promise<ProjectView> {
      // BLOCKER-2 (2026-05-01): the Rust command returns
      // CreateProjectResult { project, warnings }, not a bare ProjectView.
      // Pre-fix this generic was <ProjectView> — `result.id` was undefined
      // and the wrapper object was being stored as if it were a project.
      const result = await invoke<CreateProjectResult>('create_project_v2', {
        req: { name, folder_path, host, safe_add },
      });
      const project = result.project;
      for (const w of result.warnings) toast.error(w);
      update((s) => ({
        ...s,
        projects: [...s.projects, project],
        selectedId: project.id,
        error: null,
      }));
      saveSelected(project.id);
      return project;
    },

    /**
     * PR 5 (2026-05-01): re-run the bundle install in update mode against
     * an existing project. Picks up newly-shipped orchestrator files
     * (hooks, scripts, agents, skills, settings, infrastructure) WITHOUT
     * overwriting user customizations.
     *
     * On success: toasts a one-line summary ("N updated, M preserved") +
     * info/error toasts for every entry in `result.warnings`. The toast
     * stream surfaces deferral writes (schema migration required, user-
     * modified files preserved) so the user knows what to action next.
     *
     * On hard env failure (project missing, folder gone): the invoke
     * itself throws — we re-throw to the caller so it can render a
     * fatal error UI.
     */
    async update(id: string): Promise<UpdateProjectResult> {
      const result = await invoke<UpdateProjectResult>('update_project_v2', {
        projectId: id,
      });

      // One-line summary toast: choose info/success based on whether
      // anything actually changed (created + overwritten + always_overwritten > 0).
      const s = result.summary;
      const shipped = s.created + s.overwritten + s.always_overwritten;
      if (shipped === 0 && s.preserved === 0 && s.errors_count === 0) {
        toast.success('Project bundle already up to date.');
      } else {
        const parts: string[] = [];
        if (s.created > 0) parts.push(`${s.created} created`);
        if (s.overwritten > 0) parts.push(`${s.overwritten} updated`);
        if (s.always_overwritten > 0) parts.push(`${s.always_overwritten} always-updated`);
        if (s.preserved > 0) parts.push(`${s.preserved} user-modifications preserved`);
        if (s.errors_count > 0) parts.push(`${s.errors_count} errors`);
        const line = parts.length > 0 ? parts.join(', ') : 'no changes';
        if (s.errors_count > 0) {
          toast.error(`Bundle update finished: ${line}.`);
        } else {
          toast.success(`Bundle update finished: ${line}.`);
        }
      }

      // Stream every warning as its own toast so the user sees the
      // deferral pointers (schema migration required, user-modified
      // files preserved). Most are info-level conditions, not blocking
      // failures — but the project owner should action them.
      //
      // v0.2.70 (A2-NIT1): route by severity instead of red-for-all. The
      // synchronous `UpdateProjectResult.warnings` is a plain string list,
      // so we classify by content via `isErrorWarning` (must-match
      // `project_setup.rs::classify_warning`). Genuine failures → red error;
      // informational notices (e.g. "additive schema migration auto-applied
      // … vectors preserved", "N file(s) preserved") → amber info, so a
      // SUCCESS message no longer mis-signals as an error.
      for (const w of result.warnings) {
        if (isErrorWarning(w)) toast.error(w);
        else toast.info(w);
      }

      // Refresh the cached project view (e.g. updated_at bumped via
      // db.log_change).
      update((s) => ({
        ...s,
        projects: s.projects.map((p) => (p.id === id ? result.project : p)),
      }));

      return result;
    },

    /**
     * 0.2.x backlog #4 (2026-05-10): iterate every registered project
     * and run `update_project_v2` sequentially. Power-user shortcut for
     * users who don't want to click Update N times.
     *
     * Sequential (not fan-out): keeps the UX understandable + lets the
     * GUI stream per-project progress cleanly. The `opts.stop_on_error`
     * flag (default true) controls whether the iteration halts at the
     * first hard failure or continues through every project. Returns
     * the aggregate `UpdateAllReport`; the modal renders it.
     *
     * On hard env failure (DB query, etc.), this re-throws the original
     * Tauri error so the caller can render a fatal-error UI. Per-project
     * failures are NOT thrown — they land in `report.updated[]` with
     * `status="failed"` and the caller surfaces them as needed.
     */
    async updateAll(opts: UpdateAllOptions | null = null): Promise<UpdateAllReport> {
      const report = await invoke<UpdateAllReport>('update_all_projects', { opts });

      // Refresh local cache for every successfully-updated project — the
      // bumped `updated_at` lives in `report.updated[i].summary` only on
      // the server side; the cheapest UI sync is just to re-call list().
      // Call the store via the module-level singleton instead of `this`:
      // `this` inside an object-literal method has no inferred type under
      // strict TS, which breaks the return-type inference of the whole
      // object and propagates as a "Cannot use 'state' as a store" error
      // on every `$projects` use site downstream.
      if (report.total_succeeded > 0) {
        try {
          await projects.load();
        } catch {
          // Non-fatal: the per-project info was already returned in the
          // report; a stale cache here just means the next manual refresh
          // will pick it up.
        }
      }

      return report;
    },

    async rename(id: string, newName: string): Promise<ProjectView> {
      // HIGH-7 (2026-05-01): rename now returns RenameProjectResult so env
      // refresh failures surface as warnings instead of disappearing into
      // eprintln on the Rust side.
      const result = await invoke<RenameProjectResult>('rename_project_v2', {
        id,
        newName,
      });
      const updated = result.project;
      for (const w of result.warnings) toast.error(w);
      update((s) => ({
        ...s,
        projects: s.projects.map((p) => (p.id === id ? updated : p)),
      }));
      return updated;
    },

    /**
     * MEDIUM-1 (refactored 2026-05-01): toggle the project's
     * SHARED_KG_WRITE_DISABLED setting. Asymmetric model — gates WRITES
     * to the cross-project shared KG only; reads remain unconditional.
     *
     * Persists to DB AND refreshes the 3 launcher-owned env surfaces
     * (.vscode/settings.json, .claude/env, .claude/settings.json) so the
     * new value takes effect without a relaunch. The Svelte UI control
     * is a follow-up — this is just the wiring.
     */
    async setSharedKgWriteDisabled(id: string, writeDisabled: boolean): Promise<ProjectView> {
      const result = await invoke<RenameProjectResult>('set_shared_kg_write_disabled', {
        projectId: id,
        writeDisabled,
      });
      const updated = result.project;
      for (const w of result.warnings) toast.error(w);
      update((s) => ({
        ...s,
        projects: s.projects.map((p) => (p.id === id ? updated : p)),
      }));
      return updated;
    },

    /**
     * Deprecated alias of `setSharedKgWriteDisabled`. Kept for ~3 releases
     * (target removal: 2026-08) so any UI code still calling the old name
     * keeps working through the rename. Emits a console warning per call.
     */
    async setSharedKgOptOut(id: string, optOut: boolean): Promise<ProjectView> {
      console.warn(
        '[vct] setSharedKgOptOut is deprecated — use setSharedKgWriteDisabled. ' +
        'The toggle now gates WRITES only; reads of the shared KG are always on.',
      );
      return this.setSharedKgWriteDisabled(id, optOut);
    },

    /**
     * v0.2.46 Decision B — toggle the project's SHARED_KG_READ_DISABLED
     * setting. Symmetric mirror of `setSharedKgWriteDisabled`: when
     * `true`, the MCP's `_kg_collections_to_search` drops the shared
     * collection from the hybrid_search / semantic_graph_search fan-out
     * for this project. Pre-v0.2.46 the read path was unconditional;
     * v0.2.46 lets users opt OUT explicitly while keeping default ON.
     *
     * Persists to DB AND refreshes the 2 launcher-owned env surfaces
     * (.claude/env, .claude/settings.json) so the new value takes
     * effect without a relaunch.
     */
    async setSharedKgReadDisabled(id: string, readDisabled: boolean): Promise<ProjectView> {
      const result = await invoke<RenameProjectResult>('set_shared_kg_read_disabled', {
        projectId: id,
        readDisabled,
      });
      const updated = result.project;
      for (const w of result.warnings) toast.error(w);
      update((s) => ({
        ...s,
        projects: s.projects.map((p) => (p.id === id ? updated : p)),
      }));
      return updated;
    },

    async switchHost(id: string, newHost: ProjectHost): Promise<SwitchHostResult> {
      const result = await invoke<SwitchHostResult>('switch_project_host_v2', {
        id,
        newHost,
      });
      update((s) => ({
        ...s,
        projects: s.projects.map((p) => (p.id === id ? result.project : p)),
      }));
      return result;
    },

    /**
     * Unregister a project from the launcher.
     *
     * 2026-05-06: replaced the old `deleteFolder: boolean` (always
     * ignored — launcher never touched the user's folder) with a richer
     * `UnregisterOptions` object that lets the user pick:
     *   - `purgeLauncherFiles` (default true): surgical removal of
     *     launcher-managed files (.claude/hooks, .claude/scripts,
     *     infrastructure compose YAMLs) + canonical env-key strip.
     *     User content (agents/skills/CONTEXT_STATE/CLAUDE.md/source
     *     code/user-added .env keys) is preserved.
     *   - `purgeCollections` (default false): drop the project's OWN
     *     Weaviate collections. Shared collections never touched.
     *
     * Returns the `UnregisterReport` with counts the UI can toast.
     * `options=null` (or omitted) → backend defaults apply.
     */
    async delete(
      id: string,
      options: UnregisterOptions | null = null,
    ): Promise<UnregisterReport> {
      const report = await invoke<UnregisterReport>('delete_project_v2', {
        id,
        options,
      });
      update((s) => {
        const projects = s.projects.filter((p) => p.id !== id);
        let selectedId = s.selectedId;
        if (selectedId === id) {
          selectedId = projects.length > 0 ? projects[0].id : null;
          saveSelected(selectedId);
        }
        return { ...s, projects, selectedId };
      });
      return report;
    },

    clearError() {
      update((s) => ({ ...s, error: null }));
    },
  };
}

export const projects = createProjectsStore();

// Defect B (v0.2.68): wire the serialized add-queue's create-invoke. The
// `project-setup` store owns the queue (so rapid adds don't race the
// synchronous DB/env phase) + the global progress banner; it calls back into
// `projects.create` for each dequeued add. Import is one-directional
// (project-setup imports only types/toast/tauri, never projects), so no
// circular dependency. Registered once at module load.
import { projectSetup } from '$lib/stores/project-setup';
projectSetup.setCreateFn(async (req) => {
  // `projects.create` performs the FAST `create_project_v2` invoke, updates
  // the store, and toasts the (now sync-phase-only) warnings. It returns the
  // ProjectView; we re-wrap into the CreateProjectResult shape the queue
  // expects (warnings already toasted inside create, so an empty list here is
  // fine — the heavy-phase warnings arrive on the setup-progress event).
  const project = await projects.create(
    req.name,
    req.folder_path,
    req.host,
    req.safe_add,
  );
  return { project, warnings: [] };
});

export const selectedProject = derived(projects, ($p) =>
  $p.selectedId ? $p.projects.find((pr) => pr.id === $p.selectedId) ?? null : null,
);

/** Convenience: get the current selected ID without subscribing. */
export function getSelectedProjectId(): string | null {
  return get(projects).selectedId;
}
