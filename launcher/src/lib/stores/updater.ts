// Orchestrator update detection + dismissal state.
//
// `check_for_updates` is already called by the orchestrator store on every
// `checkStatus()`. This store layers on top: tracks the version detected
// and the timestamp the user last saw a notification, so we don't re-toast
// on every render.
//
// v0.2.16 (W4 / 0.5): the underlying Rust command now returns a full
// `UpdateStatus` struct with three independent flags
// (remote_ahead / install_stale / binary_stale). We render priority-
// based UX in `UpdateBadge.svelte`. The `dismiss-until-version-bump`
// behaviour preserved here applies to the highest-priority pending
// state; once resolved (e.g. install_stale → user clicks Install
// Update → manifest.version catches up to source.version → flag goes
// false) the banner auto-dismisses on the next `checkStatus()` poll.

import { writable, get } from 'svelte/store';
import { invoke, tauriAvailable } from '$lib/tauri';
import { orchestrator, cancelScheduledRetry, renderCheck, checkError } from './orchestrator';
// v0.2.93 (field incident 2026-09-07): the progress overlay is opened/closed
// from HERE (beginOp / endOp) so every update-class operation — not only the
// badge's four — drives the one live indicator.
import { ui } from './ui';
import {
  parseTaggedErrorPayload,
  parseOrchestratorConflictError,
  errorText,
  type OrchestratorConflictPayload,
} from '$lib/tauri-error-payload';
// M-P1-5: scope the seen-version flag by install_root so two clones
// on the same machine maintain independent dismissal state. The
// helper transparently migrates the legacy unscoped key on first
// scoped read.
import {
  getInstallScopedFlag,
  setInstallScopedFlag,
  clearInstallScopedFlag,
} from './install-state-store';
import type { InstallHealth } from '$lib/types/launcher';

const SEEN_KEY = 'vct.update.seen_version';

// Lazy-resolved install_root cache. The updater store is created at
// module-load time (before the first `check_install_health` round-
// trip), so we cannot synchronously know the install_root. Instead we
// resolve it lazily on first read/write and reuse the cached value.
// `null` is a sentinel for "resolved, but unknown" (dev mode); the
// store helpers map that to the "unknown" bucket which still beats
// the cross-clone leak of the pre-v0.2.53 unscoped key.
let cachedInstallRoot: string | null | undefined = undefined;

async function resolveInstallRoot(): Promise<string | null> {
  if (cachedInstallRoot !== undefined) return cachedInstallRoot;
  if (!tauriAvailable()) {
    cachedInstallRoot = null;
    return null;
  }
  try {
    const h = await invoke<InstallHealth>('check_install_health');
    cachedInstallRoot = h.install_root ?? null;
  } catch {
    cachedInstallRoot = null;
  }
  return cachedInstallRoot;
}

/** v0.2.16 (W4 / 0.5): which of the update signals to render.
 *  Priority order (v0.2.93):
 *    'merge_in_progress' > 'merge_resolved_incomplete' > 'binary_stale'
 *    > 'install_stale' > 'remote_ahead'.
 *  `merge_in_progress` (v0.2.93, field incident 2026-09-07) is HIGHEST:
 *  the clone is literally mid-merge (`.git/MERGE_HEAD` present) — nothing
 *  else can proceed until the conflict is resolved or aborted.
 *  `merge_resolved_incomplete` is next because every other flag is
 *  meaningless until install.py finishes against the freshly-merged
 *  source: a binary refresh against a non-installed source would ship a
 *  launcher that doesn't match its own manifest.
 *  `null` when no signal is true. */
export type UpdateKind =
  | 'merge_in_progress'
  | 'merge_resolved_incomplete'
  | 'binary_stale'
  | 'install_stale'
  | 'remote_ahead'
  | null;

/**
 * v0.2.93: every update-class operation the progress overlay can be
 * driving. `beginOp(kind)` / `endOp(err?)` bracket each one. The badge's
 * four (update / install / restart / resume) plus the divergence modal's
 * merge / rebase and the conflict modal's keep_local / accept_upstream /
 * abort — the ops that, before v0.2.93, ran with NO live indicator at all
 * (the field incident: a merge that hit a conflict looked like a hang).
 */
export type UpdateOpKind =
  | 'update'
  | 'install'
  | 'restart'
  | 'resume'
  | 'merge'
  | 'rebase'
  | 'keep_local'
  | 'accept_upstream'
  | 'abort';

/**
 * v0.2.23 (B4 / D19): structured payload returned by `update_orchestrator`
 * when `git pull --ff-only` fails because the local clone has diverged
 * from upstream. Mirrors Rust `serialize_orchestrator_non_ff_error`.
 *
 * When set, `UpdateBadge.svelte` renders `OrchestratorUpdateDivergenceModal`
 * instead of a raw error toast — the user picks Merge / Rebase / Cancel.
 *
 * v0.2.27: the Rust side splits the file list into two categories so the
 * UI can render them as separate sections. `local_only_files` are paths
 * that exist on the local clone but NOT on upstream (e.g. user-added
 * `other_projects_knowledge/*` — these are not merge blockers). The
 * `diverged_files` list is reserved for paths where BOTH sides have
 * changes that need to be reconciled. The Rust split lands in a separate
 * commit; the modal degrades gracefully if `local_only_files` is absent.
 */
export type OrchestratorNonFfPayload = {
  event: 'orchestrator_update_non_ff';
  branch: string;
  local_sha: string | null;
  remote_sha: string | null;
  diverged_files: string[];
  git_stderr: string;
  /** v0.2.27: paths only present on the local clone (additive, no merge
   *  required). Optional — pre-v0.2.27 Rust returns undefined; the modal
   *  treats the whole `diverged_files` list as diverging in that case. */
  local_only_files?: string[];
  /** v0.2.93: paths changed ONLY upstream (will merge cleanly). With this
   *  split `diverged_files` becomes the true both-sides intersection.
   *  Optional — pre-v0.2.93 Rust omits both; the modal then shows the
   *  intersection badge only. */
  upstream_only_files?: string[];
  /** v0.2.93: `upstream_only_files.length` as reported by Rust (the list
   *  itself may be truncated for very large diffs). */
  upstream_only_count?: number;
};

/**
 * v0.2.88 (DEFECT 1 / FIELD DEFECT): the enriched untracked-collision payload
 * from `update_orchestrator` when its inline pull aborts with "untracked
 * working tree files would be overwritten by merge". Mirrors Rust
 * `serialize_untracked_collision_resolvable_error`.
 *
 * The pre-fix path routed this to the conflict modal with an EMPTY file list
 * (dead-end). Now the parsed collision set is carried, split into byte-identical
 * (safe delete) + divergent (backup-then-delete), and the modal offers a single
 * "Resolve & retry" button (`resolve_untracked_collision_and_retry`).
 */
export type OrchestratorUntrackedCollisionResolvablePayload = {
  event: 'orchestrator_untracked_collision';
  operation: 'merge' | 'rebase';
  branch: string;
  /** Present ONLY on the resolvable POST-pull variant. The pre-pull leave-alone
   *  variant (v0.2.78) omits it — the modal degrades to the informational view. */
  resolvable?: boolean;
  identical_files?: string[];
  divergent_files: string[];
  git_stderr?: string;
};

/**
 * v0.2.88 (DEFECT 2 / FIELD DEFECT): the merge SUCCEEDED but the `--autostash`
 * pop of the user's local WIP conflicted. Distinct from a merge failure — the
 * update's merge is done; only restoring local changes clashed. The user's
 * changes are safe in the git stash. Mirrors Rust
 * `serialize_autostash_pop_conflict_error`.
 */
export type OrchestratorAutostashPopConflictPayload = {
  event: 'orchestrator_autostash_pop_conflict';
  branch: string;
  conflicted_files: string[];
  git_stderr: string;
};

interface UpdaterState {
  available: boolean;
  /** v0.2.16: which signal is currently being rendered. Drives copy +
   *  action button choice in `UpdateBadge.svelte`. */
  kind: UpdateKind;
  /** The version we detected as available — kept opaque (Rust doesn't
   * expose the new version yet, only a boolean). When the boolean
   * transitions false→true we re-show. */
  lastSeenVersion: string | null;
  /** An update-class operation is in flight (bracketed by `beginOp` /
   *  `endOp`). Drives the progress overlay's running animation. */
  updating: boolean;
  /** v0.2.93: which operation `updating` refers to — the CURRENT op while
   *  `updating` is true, and the MOST RECENT one afterwards (deliberately
   *  not cleared by `endOp`, so the overlay's title stays stable through
   *  its completed / failed phases). `beginOp` overwrites it. */
  op: UpdateOpKind | null;
  error: string | null;
  dismissed: boolean;
  /** v0.2.23 (B4 / D19): when non-null, render the divergence modal
   *  instead of the popover error. Cleared by the modal's onClose.
   *  v0.2.93: the modal is mounted in `+layout.svelte` (root stacking
   *  context), keyed on this field — no longer inside UpdateBadge. */
  nonFf: OrchestratorNonFfPayload | null;
  /** v0.2.93 (field incident 2026-09-07): when non-null, render the
   *  merge/rebase conflict modal (hoisted to `+layout.svelte`). Set by the
   *  divergence modal when merge/rebase returns the
   *  `orchestrator_update_conflict` payload, by `runUpdate` when the inline
   *  pull does, and by `openPendingConflict` (the badge's "stalled merge"
   *  action). Cleared by the modal's onClose. */
  conflict: OrchestratorConflictPayload | null;
  /** v0.2.88 (DEFECT 1): when non-null, render the untracked-collision modal
   *  (Resolve & retry) instead of a raw error toast. Cleared by onClose. */
  untrackedCollision: OrchestratorUntrackedCollisionResolvablePayload | null;
  /** v0.2.88 (DEFECT 2): when non-null, render the autostash-pop-conflict modal
   *  (keep updated / keep local) instead of mislabeling it a merge failure. */
  autostashPop: OrchestratorAutostashPopConflictPayload | null;
  /** v0.2.83 (WP-A2 / D6): a `manualCheck()` (RightSidebar "Check Update"
   *  button, or the badge's "Retry now") is in flight. Drives the button's
   *  "Checking…" label so the user gets honest feedback instead of the old
   *  setTimeout fake. Distinct from `updating`, which means an actual
   *  install/update/restart is running. */
  checking: boolean;
  /** v0.2.83 (WP-A2 / D3): the last `check_for_updates` could NOT determine
   *  remote state (`remote_check.state === 'unknown'`) AND there is no real
   *  pending update to show (kind === null). When true, `UpdateBadge` renders
   *  the amber "couldn't check, retrying" state instead of nothing — the badge
   *  must NEVER silently imply "up to date" when the check actually failed.
   *  Derived in `syncFromOrchestrator()` from the orchestrator store. */
  remoteCheckFailed: boolean;
  /** v0.2.83 (WP-A2 / D3): the concise error/stage label from the failed
   *  remote check (`checkError(updateStatus.remote_check)`), surfaced in the
   *  amber popover copy. Null when the check succeeded or is not applicable. */
  remoteCheckError: string | null;
}

// Synchronous loadSeen for the initial store value. When the lazy
// install_root resolution has not yet completed, we read whatever the
// store helper sees for the "unknown" bucket — which transparently
// migrates the legacy key. The first async store action that observes
// install_root (refresh / dismiss) then rewrites under the scoped key
// AND clears the unknown bucket.
function loadSeen(): string | null {
  return getInstallScopedFlag(SEEN_KEY, cachedInstallRoot ?? null);
}

async function saveSeen(v: string | null) {
  const root = await resolveInstallRoot();
  if (v) {
    setInstallScopedFlag(SEEN_KEY, root, v);
  } else {
    clearInstallScopedFlag(SEEN_KEY, root);
  }
}

/**
 * Which badge kind a `check_for_updates` result renders. Exported (v0.2.93)
 * so the priority order is pinned by a test instead of only by prose.
 */
export function pickKind(status: {
  remote_ahead: boolean;
  install_stale: boolean;
  binary_stale: boolean;
  merge_resolved_incomplete?: boolean;
  merge_in_progress?: boolean;
} | null): UpdateKind {
  if (!status) return null;
  // v0.2.93 (field incident 2026-09-07): merge_in_progress beats EVERYTHING.
  // The clone is mid-merge (`.git/MERGE_HEAD` present) — a stalled conflict
  // the launcher lost track of (restart while the modal was up, or the modal
  // never rendered). Resolving or aborting it is the only possible next step;
  // resume / restart / install against a conflicted tree are all wrong.
  if (status.merge_in_progress) return 'merge_in_progress';
  // v0.2.51 Bug A: merge_resolved_incomplete is next. When a prior
  // conflict-resolution path was abandoned, every downstream signal is
  // misleading until install.py finishes against the freshly-merged
  // source. Re-entering via resume_orchestrator_update is the ONLY
  // correct next step.
  //
  // Then: binary > remote > install (v0.2.93 — see below).
  // - binary_stale wins because restart is fastest + a newer binary can
  //   change every other code path.
  // - remote_ahead ABOVE install_stale (v0.2.93, field incident 2026-09-08):
  //   a half-finished install previously masked the only action that PULLS
  //   (`apply_pending_install` runs install.py from the tree as it stands),
  //   so a user whose update died mid-install could never reach a newer
  //   release through the badge — Resume re-ran the old installer forever.
  //   The update flow INCLUDES the install (its post-pull tail), so when the
  //   remote is ahead it is strictly the better offer; install_stale alone
  //   (source ahead with no remote update, e.g. a shell pull) keeps the
  //   install action.
  if (status.merge_resolved_incomplete) return 'merge_resolved_incomplete';
  if (status.binary_stale) return 'binary_stale';
  if (status.remote_ahead) return 'remote_ahead';
  if (status.install_stale) return 'install_stale';
  return null;
}

/**
 * P2-M7 (v0.2.91 wave 5): whether a `merge_resolved_incomplete` resume is
 * the autostash-pop story — the merge itself SUCCEEDED and only restoring
 * the user's uncommitted local changes (`git --autostash` pop) conflicted —
 * versus a plain halted-at-a-real-conflict resume that was resolved
 * outside the launcher. These are different states with different
 * remediation (see `UPDATE_DEFERRED.md`'s per-file steps for the former).
 *
 * SSOT for that branch: `UpdateBadge.svelte`'s popover copy ("Finish
 * Update" vs "Continue Update") and `titleForUpdateKind` below (used by
 * `OrchestratorUpdateProgressModal`'s overlay title) both call this
 * instead of each re-deriving `resumeOperation === 'autostash-pop'`
 * independently.
 */
export function isAutostashPopResume(resumeOperation?: string | null): boolean {
  return resumeOperation === 'autostash-pop';
}

/**
 * P2-M7: in-progress overlay title for `OrchestratorUpdateProgressModal`,
 * per update kind. Extracted here (rather than left as a second inline
 * switch in the modal) because `updater.ts` already owns `UpdateKind` and
 * its priority ordering, and because the modal's previous copy of this
 * switch had no `merge_resolved_incomplete` arm at all — it silently fell
 * through to the generic "Updating orchestrator", mistitling the one
 * resume path the project's CLAUDE.md says must be surfaced prominently.
 *
 * `resumeOperation` only matters for `merge_resolved_incomplete`; every
 * other kind ignores it.
 */
export function titleForUpdateKind(kind: UpdateKind, resumeOperation?: string | null): string {
  switch (kind) {
    case 'merge_in_progress':
      return 'Resolving merge conflict';
    case 'merge_resolved_incomplete':
      return isAutostashPopResume(resumeOperation) ? 'Finishing update' : 'Resuming update';
    case 'binary_stale':
      return 'Restarting launcher';
    case 'install_stale':
      return 'Installing update';
    case 'remote_ahead':
      return 'Updating orchestrator';
    default:
      return 'Updating orchestrator';
  }
}

/**
 * v0.2.93: overlay title for the operation actually in flight. The badge
 * kind alone can't title a merge / rebase / keep-local / accept-upstream /
 * abort (those start from a modal, not from a badge kind), so the op wins
 * and the kind-based title is the fallback for the badge's own actions.
 */
export function titleForUpdateOp(
  op: UpdateOpKind | null,
  kind: UpdateKind,
  resumeOperation?: string | null,
): string {
  switch (op) {
    case 'update':
      return 'Updating orchestrator';
    case 'install':
      return 'Installing update';
    case 'restart':
      return 'Restarting launcher';
    case 'resume':
      return titleForUpdateKind('merge_resolved_incomplete', resumeOperation);
    case 'merge':
      return 'Merging upstream changes';
    case 'rebase':
      return 'Rebasing onto upstream';
    case 'keep_local':
      return 'Keeping local versions';
    case 'accept_upstream':
      return 'Accepting upstream versions';
    case 'abort':
      return 'Aborting merge';
    default:
      return titleForUpdateKind(kind, resumeOperation);
  }
}

/**
 * v0.2.93: what the overlay says while an op runs and NO `install_progress`
 * event has arrived yet. The git-phase ops (merge / rebase / abort) emit no
 * progress at all, so without this the overlay would sit on a bare
 * "Working…" — still better than the pre-v0.2.93 nothing, but the user
 * should be told what is actually happening.
 */
export function runningMessageForUpdateOp(op: UpdateOpKind | null): string {
  switch (op) {
    case 'merge':
      return 'Running git merge with upstream…';
    case 'rebase':
      return 'Running git rebase onto upstream…';
    case 'keep_local':
      return 'Checking out your versions and continuing the update…';
    case 'accept_upstream':
      return 'Checking out upstream versions and continuing the update…';
    case 'abort':
      return 'Restoring the working tree…';
    case 'restart':
      return 'Re-launching…';
    default:
      return 'Working…';
  }
}

/**
 * v0.2.93: whether the op ends in a launcher restart on success (so the
 * overlay's hint can say so) — `abort` is the one op that doesn't.
 */
export function updateOpRestartsOnSuccess(op: UpdateOpKind | null): boolean {
  return op !== 'abort';
}

/**
 * v0.2.23 (B4 / D19): try to parse a Tauri error as a non-FF divergence
 * payload from `update_orchestrator`. Returns null on any other shape.
 */
function parseNonFfError(raw: unknown): OrchestratorNonFfPayload | null {
  // v0.2.93: tolerant shared parser (leading whitespace / `Error: ` label
  // / Error instance). The old `startsWith('{')` check is the exact shape
  // that hid the 2026-09-07 conflict payload.
  return parseTaggedErrorPayload<OrchestratorNonFfPayload>(
    raw,
    'event',
    'orchestrator_update_non_ff',
  );
}

/**
 * v0.2.88 (DEFECT 1): parse a Tauri error as the enriched untracked-collision
 * payload. Returns null for any other shape.
 */
function parseUntrackedCollisionError(
  raw: unknown
): OrchestratorUntrackedCollisionResolvablePayload | null {
  return parseTaggedErrorPayload<OrchestratorUntrackedCollisionResolvablePayload>(
    raw,
    'event',
    'orchestrator_untracked_collision',
  );
}

/**
 * v0.2.88 (DEFECT 2): parse a Tauri error as the autostash-pop-conflict payload.
 * Returns null for any other shape.
 */
function parseAutostashPopError(
  raw: unknown
): OrchestratorAutostashPopConflictPayload | null {
  return parseTaggedErrorPayload<OrchestratorAutostashPopConflictPayload>(
    raw,
    'event',
    'orchestrator_autostash_pop_conflict',
  );
}

function createUpdaterStore() {
  const { subscribe, update } = writable<UpdaterState>({
    available: false,
    kind: null,
    lastSeenVersion: loadSeen(),
    updating: false,
    op: null,
    error: null,
    dismissed: false,
    nonFf: null,
    conflict: null,
    untrackedCollision: null,
    autostashPop: null,
    checking: false,
    remoteCheckFailed: false,
    remoteCheckError: null,
  });

  // Local implementation shared by the public `syncFromOrchestrator` method
  // and `manualCheck`. Kept as a plain function (not a `this.`-method call)
  // so it's robust against `this`-binding — an internal caller never has to
  // worry about how the method was invoked.
  function doSync() {
    const o = get(orchestrator);
    const installed = o.status === 'installed' || o.status === 'updating';
    if (!installed) {
      update((s) => ({
        ...s,
        available: false,
        kind: null,
        // Not installed ⇒ no remote to check; clear the amber state too.
        remoteCheckFailed: false,
        remoteCheckError: null,
      }));
      return;
    }
    const kind = pickKind(o.updateStatus);
    // v0.2.83 (WP-A2 / D3 + N-4): the amber "couldn't check for updates" state
    // is ONLY meaningful when there is no real pending update to surface. If a
    // real `kind` is active (remote_ahead / install_stale / …), that takes
    // precedence and the amber state is suppressed — a stale failed check from
    // a prior poll must not paint amber over a genuine update badge.
    //
    // N-4: derive from the orchestrator store's EXPLICIT `lastCheckFailed`, NOT
    // from the live `updateStatus`. The old `!!us && <probe failed>`
    // derivation rendered NOTHING when the check itself soft-failed to a null
    // `updateStatus` (the command errored) — the exact silent gap N-4 closes.
    // `lastCheckFailed` is `true` for BOTH a null-status completed check AND an
    // an `unknown` remote_check; `null` before the first completed
    // check (so no amber flash during startup); `false` on success.
    const us = o.updateStatus;
    const remoteCheckFailed = o.lastCheckFailed === true && kind === null;
    const remoteCheckError = remoteCheckFailed
      ? checkError(us?.remote_check)
      : null;
    if (kind !== null) {
      // v0.2.16 (W4): the dismissal marker now keys on
      // `<kind>:<version-snapshot>` so dismissing one kind (e.g.
      // install_stale@0.2.15) doesn't suppress a later kind
      // (binary_stale@0.2.16). Version snapshot is the current
      // installed version; flipping kinds OR upgrading versions
      // re-shows the badge.
      const versionSnapshot = us
        ? `${us.source_version}|${us.installed_version}|${us.on_disk_binary_version}|${us.running_version}`
        : (o.version || '');
      const marker = `${kind}:${versionSnapshot}`;
      update((s) => ({
        ...s,
        available: true,
        kind,
        dismissed: s.lastSeenVersion === marker ? s.dismissed : false,
        // A real update takes precedence — never paint amber over it.
        remoteCheckFailed,
        remoteCheckError,
      }));
    } else {
      update((s) => ({
        ...s,
        available: false,
        kind: null,
        dismissed: false,
        remoteCheckFailed,
        remoteCheckError,
      }));
    }
  }

  /**
   * v0.2.93 (field incident 2026-09-07): ONE entry point for "an
   * update-class operation is starting". Sets `updating` + `op`, clears
   * any stale error, resets the orchestrator's last `install_progress`
   * snapshot (so the overlay starts at 0%, not at the previous op's
   * "done 100%"), and opens the blocking progress overlay. Callers:
   * runUpdate / applyPendingInstall / resumeUpdate / runRestart here, the
   * divergence modal's runMerge / runRebase, and the conflict modal's
   * keep-local / accept-upstream / continue / abort handlers.
   *
   * Deliberately does NOT touch the decision-modal payload fields
   * (`nonFf` / `conflict` / …): a merge started FROM the divergence modal
   * must keep `nonFf` set, or the modal that launched it unmounts
   * mid-flight (the second half of the incident).
   */
  function beginOp(kind: UpdateOpKind) {
    orchestrator.resetProgress();
    update((s) => ({ ...s, updating: true, op: kind, error: null }));
    ui.openOrchestratorUpdateProgress();
  }

  /**
   * v0.2.93: the matching "operation ended" call. `err` undefined/null ⇒
   * success (or a hand-over to a decision modal — set the payload field
   * BEFORE calling endOp so the overlay's falling edge sees it and closes
   * instead of celebrating "Update complete"). Any other value ⇒ the
   * overlay renders its FAILED state with the error text + Dismiss.
   * `op` is left as-is so the overlay's title stays stable in its
   * completed / failed phases.
   */
  function endOp(err?: unknown) {
    const error = err === undefined || err === null ? null : errorText(err);
    update((s) => ({ ...s, updating: false, error }));
  }

  return {
    subscribe,

    /** v0.2.93: see the inner `beginOp` — public surface for the modals. */
    beginOp(kind: UpdateOpKind) {
      beginOp(kind);
    },

    /** v0.2.93: see the inner `endOp` — public surface for the modals. */
    endOp(err?: unknown) {
      endOp(err);
    },

    /** Pull update status from the orchestrator store. Re-shows the toast
     * if the underlying version changed since the last dismissal. */
    syncFromOrchestrator() {
      doSync();
    },

    /**
     * v0.2.83 (WP-A2 / D6): the ONE real update-check entry point behind
     * every manual "check for updates" surface — RightSidebar's "Check
     * Update" button (which used to be a setTimeout fake, A-RC5) and the
     * UpdateBadge amber-state "Retry now" button. Runs the actual backend
     * check and reports the outcome so the caller can render honest copy.
     *
     * Contract:
     *   - browser mode (no Tauri) ⇒ 'check_failed' (nothing to check);
     *   - sets `checking: true` for the duration (drives the button label);
     *   - awaits `orchestrator.checkStatus()` — after A-F3 this never throws,
     *     so we don't need a try/catch here; a failed backend probe surfaces
     *     as a null updateStatus or an `unknown` remote_check, both handled;
     *   - reads the freshly-updated orchestrator store: a null updateStatus
     *     OR an `unknown` remote_check ⇒ 'check_failed' (we couldn't determine
     *     remote state — never report 'up_to_date' in that case). A
     *     `not_applicable` remote_check is NOT a failure: there is no remote
     *     on this install, so the other signals decide;
     *   - syncs our derived state; a real pending update (kind !== null) ⇒
     *     un-dismiss the badge so it re-shows even if previously dismissed,
     *     and report 'available';
     *   - otherwise ⇒ 'up_to_date'.
     *
     * A manual check also cancels any pending remote-check retry (D3
     * single-flight "cancel + replace") — the checkStatus() it runs will
     * re-arm the episode if the remote is still unreachable, or reset it on
     * success.
     */
    async manualCheck(): Promise<'available' | 'up_to_date' | 'check_failed'> {
      if (!tauriAvailable()) {
        // No backend to ask. Surface honestly; do NOT touch store state
        // beyond clearing any leftover `checking` flag (there won't be one,
        // but keep the invariant that checking is false when idle).
        update((s) => ({ ...s, checking: false }));
        return 'check_failed';
      }
      // Cancel + replace the scheduled retry: the checkStatus() below is the
      // fresh attempt, and it will re-schedule (fail) or reset (success).
      cancelScheduledRetry();
      update((s) => ({ ...s, checking: true }));
      try {
        await orchestrator.checkStatus();
      } finally {
        update((s) => ({ ...s, checking: false }));
      }
      const o = get(orchestrator);
      const us = o.updateStatus;
      // Couldn't determine remote state ⇒ honest 'check_failed'.
      // v0.2.92 (WP-13): driven by the tri-state. `unknown` is a failure;
      // `not_applicable` is not (nothing to check here, so `pickKind` below
      // decides from install_stale / binary_stale); a null status (the command
      // itself soft-failed) is a failure for the same reason `unknown` is.
      // The old "a MISSING field is healthy" branch is gone — see
      // `orchestrator.ts::renderCheck`.
      if (us === null || renderCheck(us.remote_check) === 'unknown') {
        // Refresh derived state (paints the amber remote-check-failed badge
        // when appropriate) before reporting.
        doSync();
        return 'check_failed';
      }
      doSync();
      const kind = pickKind(us);
      if (kind !== null) {
        // Un-dismiss so a previously-dismissed badge re-shows on an explicit
        // user-initiated check (they asked; show them the answer).
        update((s) => ({ ...s, dismissed: false }));
        return 'available';
      }
      return 'up_to_date';
    },

    dismiss() {
      const o = get(orchestrator);
      const kind = pickKind(o.updateStatus);
      const us = o.updateStatus;
      const versionSnapshot = us
        ? `${us.source_version}|${us.installed_version}|${us.on_disk_binary_version}|${us.running_version}`
        : (o.version || '');
      const marker = `${kind ?? 'none'}:${versionSnapshot}`;
      // Fire-and-forget: the store mutation MUST stay synchronous
      // (UpdateBadge depends on it for derived state), and the
      // localStorage write is best-effort anyway.
      void saveSeen(marker);
      update((s) => ({
        ...s,
        dismissed: true,
        lastSeenVersion: marker,
      }));
    },

    /** Resolve `remote_ahead` — git pull + install.py --update. */
    async runUpdate(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({
        ...s,
        nonFf: null,
        conflict: null,
        untrackedCollision: null,
        autostashPop: null,
      }));
      beginOp('update');
      try {
        await orchestrator.update_orchestrator();
        update((s) => ({
          ...s,
          available: false,
          kind: null,
          dismissed: false,
        }));
        endOp();
        // Re-check to refresh the new install/binary state.
        await orchestrator.checkStatus();
      } catch (e) {
        // v0.2.23 (B4 / D19): detect divergence. The error string is
        // the raw Tauri Err payload; the orchestrator store wraps it as
        // an Error so we unwrap before parsing.
        const raw = errorText(e);
        const nff = parseNonFfError(raw);
        // v0.2.88 (DEFECT 1 + DEFECT 2): the inline update pull can now surface
        // TWO more structured, actionable events. Route each to its own modal
        // instead of a dead-end toast.
        const collision = parseUntrackedCollisionError(raw);
        const pop = parseAutostashPopError(raw);
        // v0.2.93: the inline pull's auto-merge can also stop at a real
        // conflict — route it to the (hoisted) conflict modal.
        const conf = parseOrchestratorConflictError(raw);
        // ORDER MATTERS: set the decision-modal payload FIRST, then endOp,
        // so the overlay's falling edge sees the hand-over and closes
        // instead of holding at "Update complete 100%".
        if (collision) {
          update((s) => ({ ...s, untrackedCollision: collision }));
          endOp();
        } else if (pop) {
          update((s) => ({ ...s, autostashPop: pop }));
          endOp();
        } else if (conf) {
          update((s) => ({ ...s, conflict: conf }));
          endOp();
        } else if (nff) {
          // Surface the modal instead of a toast — the user has a real
          // choice to make (merge vs rebase vs cancel) and the raw
          // git stderr is unactionable.
          update((s) => ({ ...s, nonFf: nff }));
          endOp();
        } else {
          endOp(raw);
        }
      }
    },

    /**
     * v0.2.23 (B4 / D19): dismiss the divergence modal. Called by the
     * modal component's onClose after the user picks an action (or
     * cancels). Clearing `nonFf` removes the modal from the DOM.
     */
    dismissNonFf() {
      update((s) => ({ ...s, nonFf: null }));
    },

    /**
     * v0.2.93 (field incident 2026-09-07): a merge/rebase (started from the
     * divergence modal, or the inline pull) stopped at a real conflict.
     * Hands over to the conflict modal mounted in `+layout.svelte`. Clears
     * `nonFf` — the divergence modal's job is done — so the two modals are
     * never up at once.
     */
    setConflict(payload: OrchestratorConflictPayload) {
      update((s) => ({ ...s, conflict: payload, nonFf: null, error: null }));
    },

    /** v0.2.93: dismiss the conflict modal (its onClose). */
    dismissConflict() {
      update((s) => ({ ...s, conflict: null }));
    },

    /**
     * v0.2.93 (D): the badge's `merge_in_progress` action. The backend
     * reports the clone is mid-merge but the launcher has no payload in
     * memory (restart, or the modal never rendered). Ask Rust for the
     * pending conflict payload (`get_pending_conflict_payload` — same JSON
     * shape as the `orchestrator_update_conflict` Err) and open the
     * hoisted conflict modal with it. Not an update-class op (a read), so
     * no overlay; a failure is surfaced as the badge's popover error.
     */
    async openPendingConflict(): Promise<void> {
      if (!tauriAvailable()) return;
      const o = get(orchestrator);
      try {
        const raw = await invoke<string>('get_pending_conflict_payload', {
          path: o.installPath,
        });
        const conf = parseOrchestratorConflictError(raw);
        if (!conf) {
          update((s) => ({
            ...s,
            error: `Could not read the pending merge conflict: ${String(raw)}`,
          }));
          return;
        }
        update((s) => ({ ...s, conflict: conf, nonFf: null, error: null }));
      } catch (e) {
        update((s) => ({ ...s, error: errorText(e) }));
      }
    },

    /** v0.2.88 (DEFECT 1): dismiss the untracked-collision modal. */
    dismissUntrackedCollision() {
      update((s) => ({ ...s, untrackedCollision: null }));
    },

    /** v0.2.88 (DEFECT 2): dismiss the autostash-pop-conflict modal. */
    dismissAutostashPop() {
      update((s) => ({ ...s, autostashPop: null }));
    },

    /**
     * v0.2.16 (W4 / 0.5): resolve `install_stale` — install.py --update
     * only, no git pull. Source is already current; this just refreshes
     * `.claude/` and bumps state/install-manifest.json::version.
     */
    async applyPendingInstall(): Promise<void> {
      if (!tauriAvailable()) return;
      beginOp('install');
      try {
        await orchestrator.apply_pending_install();
        update((s) => ({
          ...s,
          available: false,
          kind: null,
          dismissed: false,
        }));
        endOp();
        // Re-check so install_stale clears + any new flags surface.
        await orchestrator.checkStatus();
      } catch (e) {
        endOp(e);
      }
    },

    /**
     * v0.2.51 (Bug A): resolve `merge_resolved_incomplete` — call the new
     * `resume_orchestrator_update` Tauri command, which verifies the
     * working tree is clean (no leftover conflict markers, no in-flight
     * merge state) and then re-enters the post-merge tail of
     * `update_orchestrator` (install.py --update + binary refresh +
     * auto-restart). The Rust side audit-logs `update_orchestrator_resumed`
     * for forensic clarity.
     *
     * On success the launcher auto-restarts mid-call; in practice we
     * rarely reach the success branch here. Errors surface as toast +
     * popover error string (the user can see e.g. "found unresolved
     * conflict markers in N files").
     */
    async resumeUpdate(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, nonFf: null }));
      beginOp('resume');
      try {
        const o = get(orchestrator);
        await invoke('resume_orchestrator_update', { path: o.installPath });
        update((s) => ({
          ...s,
          available: false,
          kind: null,
          dismissed: false,
          nonFf: null,
        }));
        endOp();
        // Re-check so merge_resolved_incomplete clears + any newer flags
        // (binary_stale typically — the swap just landed) surface.
        await orchestrator.checkStatus();
      } catch (e) {
        endOp(e);
        // v0.2.88 (DEFECT 3): the resume can honestly report "nothing to resume
        // but a real update is still pending" (the fake-100% field bug's fix).
        // Re-check status so the UpdateBadge re-shows the genuine pending update
        // and the user is routed back to the normal update, not left thinking
        // the resume "did nothing" silently.
        try {
          await orchestrator.checkStatus();
        } catch {
          // Best-effort: a failed re-check leaves the honest error visible.
        }
      }
    },

    /**
     * v0.2.16 (W4 / 0.5): resolve `binary_stale` — re-exec the on-disk
     * launcher binary. The Rust `restart_launcher` command spawns the
     * new binary detached and exits the current process; the user sees
     * the launcher window blank for ~1s then come back at the new
     * version.
     */
    async runRestart(): Promise<void> {
      if (!tauriAvailable()) return;
      beginOp('restart');
      try {
        const o = get(orchestrator);
        await invoke('restart_launcher', { installRoot: o.installPath });
        // Note: in practice we never reach here — restart_launcher
        // exits the process. Kept defensively in case of failures
        // (e.g. binary missing) so the spinner clears.
        update((s) => ({
          ...s,
          available: false,
          kind: null,
          dismissed: false,
        }));
        endOp();
      } catch (e) {
        endOp(e);
      }
    },

    clearError() {
      update((s) => ({ ...s, error: null }));
    },
  };
}

export const updater = createUpdaterStore();
