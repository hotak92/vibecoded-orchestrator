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
     * MEDIUM-1 (2026-05-01): toggle the project's SHARED_KG_OPT_OUT setting.
     * Persists to DB AND refreshes the 3 launcher-owned env surfaces
     * (.vscode/settings.json, .claude/env, .claude/settings.json) so the new
     * value takes effect without a relaunch. The Svelte UI control is a
     * follow-up — this is just the wiring.
     */
    async setSharedKgOptOut(id: string, optOut: boolean): Promise<ProjectView> {
      const result = await invoke<RenameProjectResult>('set_shared_kg_opt_out', {
        projectId: id,
        optOut,
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

    async delete(id: string, deleteFolder: boolean = false): Promise<void> {
      await invoke<void>('delete_project_v2', { id, deleteFolder });
      update((s) => {
        const projects = s.projects.filter((p) => p.id !== id);
        let selectedId = s.selectedId;
        if (selectedId === id) {
          selectedId = projects.length > 0 ? projects[0].id : null;
          saveSelected(selectedId);
        }
        return { ...s, projects, selectedId };
      });
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
