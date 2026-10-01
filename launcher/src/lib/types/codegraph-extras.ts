// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.47: TypeScript mirrors of Rust types in
//   launcher/src-tauri/src/commands/project_codegraph_extras.rs
//   vct-launcher-core/src/db/codegraph_extras.rs
//
// Project-extra codegraph paths let a project index read-only
// reference folders into its own codegraph collection without
// making them launcher projects. See
// .claude/context/plans/v0.2.47-project-extra-codegraph-paths-2026-06-05.md
// and knowledge/concepts/project-extra-codegraph-paths-2026-06-05.md.

/**
 * One row from `project_codegraph_extra_paths`. `last_indexed_at` and
 * `last_indexed_commit` are NULL until the path has been analyzed at
 * least once.
 */
export interface ExtraPath {
  project_id: string;
  /** Absolute, canonicalised at add-time. Trailing separator stripped. */
  path: string;
  /** Optional UI label; falls back to basename when null. */
  label: string | null;
  /** Unix millis when the row was inserted. */
  added_at: number;
  /** Unix millis of the most recent successful analyze. NULL until first. */
  last_indexed_at: number | null;
  /** Git SHA at the most recent analyze, or NULL for non-git extras. */
  last_indexed_commit: string | null;
  /** 1/true = analyze visits this path; 0/false = soft-disabled. */
  enabled: boolean;
  /** Server-side derived label = label ?? basename(path). */
  display_label: string;
  /**
   * v0.2.100 W5R-04: the path's CURRENT repo HEAD at list time, or null
   * (non-git path / git unavailable). Optional: absent on rows built by
   * `add_*` before a list refresh.
   */
  head_commit?: string | null;
  /**
   * v0.2.100 W5R-04: HEAD is known and differs from `last_indexed_commit`
   * (the one Rust rule `extra_path_is_stale`). The Stop hook re-indexes such
   * a path automatically; the panel badges it until then.
   */
  stale?: boolean;
}

/** Badge shown on a stale extra path, or null when none applies. */
export interface ExtraPathStaleBadge {
  text: string;
  title: string;
}

/**
 * The panel's "stale" badge for a row (W5R-04). Decided by the server-side
 * `stale` flag — this only words it. Disabled rows get no badge (nothing
 * re-indexes them, and the "disabled" badge already says why).
 */
export function extraPathStaleBadge(row: ExtraPath): ExtraPathStaleBadge | null {
  if (!row.enabled || !row.stale) return null;
  const head = (row.head_commit ?? '').slice(0, 8);
  const last = row.last_indexed_commit ? row.last_indexed_commit.slice(0, 8) : 'never';
  return {
    text: 'stale',
    title:
      `The checkout is at ${head || 'a newer commit'} but the code graph was indexed at ${last}. ` +
      'It is re-indexed automatically at the end of a session turn (at most hourly), ' +
      'or now with Sync.',
  };
}

/**
 * Minimal project reference returned by the disambiguation branch of
 * `add_project_codegraph_extra_path` — only the fields the modal needs
 * to render the "Add as project / Add as path anyway" prompt.
 */
export interface ProjectMeta {
  id: string;
  name: string;
  slug: string;
  folder_path: string;
}

/**
 * Two-variant response from `add_project_codegraph_extra_path`.
 *
 * `action: "added"` — the row was persisted; GUI proceeds to auto-sync.
 * `action: "disambiguation_required"` — the path is the root of an
 *   existing launcher project. GUI must render the disambiguation
 *   modal and either grant access-matrix (NOT re-call add) or re-call
 *   add with `force: true`.
 */
export type AddExtraPathResult =
  | { action: 'added'; row: ExtraPath }
  | {
      action: 'disambiguation_required';
      existing_project: ProjectMeta;
      path: string;
    };

/**
 * Per-path or whole-project analyze outcome. `entities_indexed` is the
 * count of rows visited (CodeFunction + CodeClass + CodeModule + ...);
 * the launcher uses it for the post-sync toast. `project_codegraph_prefix`
 * is the Weaviate collection prefix the analyzer wrote into.
 */
export interface SyncOutcome {
  files_scanned: number;
  entities_indexed: number;
  duration_ms: number;
  project_codegraph_prefix: string;
}
