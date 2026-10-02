// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100 - view-model for the interrupted-project-move banner.
//
// `list_live_project_moves_v2` returns the `project_moves` rows still live
// (`running` / `flipped`). A launcher restart or crash mid-move leaves one
// behind, and the two statuses need OPPOSITE sentences - which is why the
// command returns the status and not a boolean:
//
//   running  - interrupted BEFORE the flip. Nothing in `projects` changed; the
//              project still works at its old folder. Tell the user it was NOT
//              moved (scary wording here would be wrong).
//   flipped  - interrupted AFTER the flip. The project already lives at the
//              destination and reconciliation is still owed. Tell the user
//              where it is now and how to finish.
//
// Pure (no store, no Tauri) so both variants are real asserts under node.

/** Wire shape of a `project_moves` row (Rust `ProjectMoveRow`). */
export interface LiveProjectMove {
  id: string;
  project_id: string;
  src: string;
  dst: string;
  status: string;
  error: string | null;
  started_at: number;
  flipped_at: number | null;
  finished_at: number | null;
}

export type MoveBannerKind = 'not_moved' | 'finish_owed';

export interface MoveBannerItem {
  moveId: string;
  kind: MoveBannerKind;
  title: string;
  detail: string;
  /** The exact command that finishes a `flipped` move (null for `running`). */
  command: string | null;
}

/** Single-quote a path for a POSIX shell (the printed command is shipped code). */
export function shellQuote(path: string): string {
  return `'${path.replace(/'/g, `'\\''`)}'`;
}

/** The verify/finish command, same flags the move engine prints itself. */
export function finishMoveCommand(dst: string): string {
  return `vco project move --verify --folder ${shellQuote(dst)}`;
}

/**
 * One banner item per live move; rows with any other status (a completed or
 * failed row is history, not an interruption) are ignored, as are items the
 * user already dismissed this session.
 */
export function buildMoveBannerItems(
  moves: LiveProjectMove[],
  projectName: (projectId: string) => string,
  dismissed: ReadonlySet<string> = new Set(),
): MoveBannerItem[] {
  const items: MoveBannerItem[] = [];
  for (const m of moves) {
    if (dismissed.has(m.id)) continue;
    const name = projectName(m.project_id);
    if (m.status === 'running') {
      items.push({
        moveId: m.id,
        kind: 'not_moved',
        title: `Move of ${name} was interrupted - it was not moved`,
        detail: `${name} still works at ${m.src}. Nothing was changed, so you can move it again whenever you like.`,
        command: null,
      });
    } else if (m.status === 'flipped') {
      items.push({
        moveId: m.id,
        kind: 'finish_owed',
        title: `Move of ${name} needs finishing`,
        detail: `${name} already lives at ${m.dst}, but the last clean-up step did not finish. Run the command below to complete it.`,
        command: finishMoveCommand(m.dst),
      });
    }
  }
  return items;
}
