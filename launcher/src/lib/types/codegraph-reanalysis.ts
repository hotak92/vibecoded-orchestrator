// SPDX-License-Identifier: AGPL-3.0-or-later
// TS shapes for `reanalyze_code_graph` (v0.2.18, Plan C).
//
// Mirror of the Rust types in
// `launcher/src-tauri/src/commands/codegraph_reanalyze.rs` and the
// Python JSON-progress output in `templates/scripts/analyze_code_graph.py`
// (the `--json-progress` flag). Keep all three in sync — drift causes
// silent deserialise failures at the Tauri boundary.

/**
 * Per-file progress event payload. Emitted on the `vct-reanalysis-progress`
 * Tauri event while the Python analyzer subprocess streams its
 * `--json-progress` lines. The Svelte modal binds the progress bar to
 * `progress` ∈ [0, 1] and displays `message` + `file` as sub-text.
 */
export interface ReanalysisProgress {
  /** Project name being analyzed (echoed back so the UI can sanity-check
   *  the event is for the modal it's showing). */
  project: string;
  /** `--language` flag value, or empty string for full-multi-language. */
  language: string;
  /** Fractional progress in [0, 1]. The analyzer clamps server-side; the
   *  UI clamps again for robustness against float drift. */
  progress: number;
  /** Human-readable sub-text: e.g. `"Analyzing src/foo.py"`. */
  message: string;
  /** Repo-relative POSIX path of the file currently being analyzed.
   *  Empty when the final emit fires after the dispatch loop. */
  file: string;
  /** Canonical language ID for the current file (`"python"`, `"go"`, …).
   *  Empty when the final emit fires. */
  lang: string;
}

/**
 * Final report returned by `reanalyze_code_graph`. The modal switches
 * from "running" to "complete" state when this resolves.
 */
export interface ReanalysisReport {
  files_analyzed: number;
  files_skipped: number;
  modules: number;
  classes: number;
  functions: number;
  apis: number;
  /** Files where one or more `_dedup_insert` calls failed. Non-zero means
   *  the code graph for this project is missing data (exit code 4 in the
   *  underlying analyzer, mapped to a stderr-rich error string by the
   *  Tauri command). */
  insert_errors: number;
  /** Number of orphan rows the prune pass deleted. Always present —
   *  Re-analyze always passes `--prune-stale`. */
  stale_pruned: number;
  /** Empty string when no `--language` filter was set (full multi-
   *  language re-walk). Non-empty when a single-language run. */
  language: string;
  /** Whether `--prune-stale` was active for this run. Always true for
   *  Re-analyze (the button always passes it); kept in the shape so the
   *  UI can render the "stale pruned" count contextually. */
  prune_stale: boolean;
}
