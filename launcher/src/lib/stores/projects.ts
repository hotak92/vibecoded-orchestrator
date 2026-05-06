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
  UnregisterOptions,
  UnregisterReport,
} from '$lib/types/launcher';
import { toast } from '$lib/stores/toast';

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

    async create(name: string, folder_path: string, host: ProjectHost): Promise<ProjectView> {
      // BLOCKER-2 (2026-05-01): the Rust command returns
      // CreateProjectResult { project, warnings }, not a bare ProjectView.
      // Pre-fix this generic was <ProjectView> — `result.id` was undefined
      // and the wrapper object was being stored as if it were a project.
      const result = await invoke<CreateProjectResult>('create_project_v2', {
        req: { name, folder_path, host },
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
      // files preserved). They're info-level conditions, not blocking
      // failures — but the project owner should action them.
      for (const w of result.warnings) toast.error(w);

      // Refresh the cached project view (e.g. updated_at bumped via
      // db.log_change).
      update((s) => ({
        ...s,
        projects: s.projects.map((p) => (p.id === id ? result.project : p)),
      }));

      return result;
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

export const selectedProject = derived(projects, ($p) =>
  $p.selectedId ? $p.projects.find((pr) => pr.id === $p.selectedId) ?? null : null,
);

/** Convenience: get the current selected ID without subscribing. */
export function getSelectedProjectId(): string | null {
  return get(projects).selectedId;
}
