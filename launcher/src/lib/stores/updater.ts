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
//
// v0.2.100 (WP-08, AD-1): `run(kind)` is the ONE update action. Every GUI
// control that updates the orchestrator — the badge, the Updates page's
// "Update now", the tray (via the Updates page), Continue/Finish Update, the
// divergence modal's Merge/Rebase, the conflict modal's Continue — calls
// `updater.run(<kind>)`, which brackets `run_orchestrator_update` with the
// one progress overlay (`beginOp`/`endOp`) and routes every failure through
// the one `routeUpdateError`. Restart is `runRestart()`; a check is
// `manualCheck()`. No component invokes an update command directly
// (pinned by `update-invoke-census.test.ts`).

import { writable, get } from 'svelte/store';
import { invoke, tauriAvailable } from '$lib/tauri';
import { orchestrator, cancelScheduledRetry, renderCheck, checkError } from './orchestrator';
// v0.2.93 (field incident 2026-09-07): the progress overlay is opened/closed
// from HERE (beginOp / endOp) so every update-class operation — not only the
// badge's four — drives the one live indicator.
import { ui } from './ui';
// v0.2.97: an update that finishes WITHOUT restarting the launcher still left
// the running model gateway on the old code — ask, the same way the layout
// asks at launcher start. Read-only; the modal restarts nothing by itself.
import { gatewayFreshness } from './gateway-freshness';
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
  | 'reset'
  | 'keep_local'
  | 'accept_upstream'
  | 'abort';

/**
 * v0.2.100 (WP-08, AD-1): the `kind` argument of the ONE backend update
 * command `run_orchestrator_update`. Mirrors Rust `UpdateKindDto`
 * (`commands/update_run.rs`) — the string values are the wire format.
 * Distinct from {@link UpdateKind}, which is the BADGE state.
 */
export type UpdateRunKind = 'PullFf' | 'Merge' | 'Rebase' | 'Resume' | 'ApplyOnly' | 'ResetHard';

/** Every {@link UpdateRunKind}, for exhaustive tests. */
export const UPDATE_RUN_KINDS: readonly UpdateRunKind[] = [
  'PullFf',
  'Merge',
  'Rebase',
  'Resume',
  'ApplyOnly',
  'ResetHard',
];

/** Mirrors Rust `UpdateOutcome` — what `run_orchestrator_update` resolves to
 *  when the launcher did not restart mid-call. */
export interface UpdateOutcome {
  kind: UpdateRunKind;
  head_before: string | null;
  head_after: string | null;
  install_py_ran: boolean;
  restarted: boolean;
  log_path: string | null;
  /** One-line summary for the user (WP-03a contract). */
  message?: string;
  /** The phase ledger (`update_run.rs::PhaseRecord`). */
  phases?: { phase: string; status: 'done' | 'skipped' | 'failed'; detail: string | null }[];
  /** `ResetHard` only: where the discarded local work was saved
   *  (`update_run.rs::ResetBackup`, owner ruling F-W2-03). */
  reset_backup?: ResetBackup;
}

/** Mirrors Rust `update_run::ResetBackup`. */
export interface ResetBackup {
  /** `vco-backup/<stamp>` — the pre-reset HEAD. */
  branch: string;
  /** `vco-backup/<stamp>-wip` — the uncommitted + untracked changes. */
  uncommitted_branch: string | null;
  /** `<vct_root>/backups/orchestrator-reset-<stamp>.bundle`; null when there
   *  was nothing local to save. */
  bundle: string | null;
  local_commits: number;
}

/** Which overlay op (title / running message) a run kind drives. */
export function opForRunKind(kind: UpdateRunKind): UpdateOpKind {
  switch (kind) {
    case 'PullFf':
      return 'update';
    case 'ApplyOnly':
      return 'install';
    case 'Resume':
      return 'resume';
    case 'Merge':
      return 'merge';
    case 'Rebase':
      return 'rebase';
    case 'ResetHard':
      return 'reset';
  }
}

/**
 * v0.2.23 (B4 / D19): structured payload an update run returns (v0.2.100:
 * `run_orchestrator_update`'s `NonFastForward`, flattened with this `event`)
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
 * from an update run when its inline pull aborts with "untracked
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
  /** v0.2.100: `update` when the collision stopped a plain update pull. */
  operation: 'merge' | 'rebase' | 'update';
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
  /** v0.2.100 (WP-08, L3-F03): the last update-class op FAILED. This — not
   *  `error`'s truthiness — is the failure signal: a failure whose backend
   *  text was empty used to leave `error === ""`, which is falsy, so the
   *  overlay celebrated "Update complete". `endOp` also guarantees `error`
   *  is non-empty whenever `failed` is true. */
  failed: boolean;
  dismissed: boolean;
  /** v0.2.23 (B4 / D19): when non-null, render the divergence modal
   *  instead of the popover error. Cleared by the modal's onClose.
   *  v0.2.93: the modal is mounted in `+layout.svelte` (root stacking
   *  context), keyed on this field — no longer inside UpdateBadge. */
  nonFf: OrchestratorNonFfPayload | null;
  /** v0.2.93 (field incident 2026-09-07): when non-null, render the
   *  merge/rebase conflict modal (hoisted to `+layout.svelte`). Set by the
   *  divergence modal when merge/rebase returns the
   *  `orchestrator_update_conflict` payload, by `run(kind)` when the inline
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
  // source. Re-entering via the `Resume` run kind is the ONLY
  // correct next step.
  //
  // Then: remote > binary > install.
  // - remote_ahead ABOVE binary_stale (v0.2.100, WP-08): a newer binary on
  //   disk used to HIDE a pending remote update — the user restarted, the
  //   new binary came up, and the remote update was still waiting behind a
  //   badge that had shown the wrong thing. The update run ends by
  //   relaunching the dist binary, so it subsumes the restart; the popover
  //   says so when both are true (`binaryAlsoStale` in `badgeCopyFor`).
  // - binary_stale above install_stale: restart is fastest + a newer
  //   binary can change every other code path.
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
  if (status.remote_ahead) return 'remote_ahead';
  if (status.binary_stale) return 'binary_stale';
  if (status.install_stale) return 'install_stale';
  return null;
}

/**
 * v0.2.100 (WP-08): what the badge's (and the Updates page's) primary
 * button does for a badge kind. ONE table — the badge and the page cannot
 * disagree about which action a state offers.
 */
export type BadgeAction =
  | { type: 'run'; kind: UpdateRunKind }
  | { type: 'restart' }
  | { type: 'resolve_conflict' };

export function actionForKind(kind: UpdateKind): BadgeAction | null {
  switch (kind) {
    case 'merge_in_progress':
      return { type: 'resolve_conflict' };
    case 'merge_resolved_incomplete':
      return { type: 'run', kind: 'Resume' };
    case 'remote_ahead':
      return { type: 'run', kind: 'PullFf' };
    case 'binary_stale':
      return { type: 'restart' };
    case 'install_stale':
      return { type: 'run', kind: 'ApplyOnly' };
    default:
      return null;
  }
}

/** The status fields {@link badgeCopyFor} reads (a subset of the
 *  orchestrator store's `UpdateStatus`, kept structural so this stays a
 *  pure, unit-testable function). */
export interface BadgeCopyStatus {
  binary_stale?: boolean;
  resume_operation?: string;
  resume_branch?: string;
  source_version?: string;
  installed_version?: string;
  running_version?: string;
  on_disk_binary_version?: string;
}

export interface BadgeCopy {
  title: string;
  desc: string;
  buttonLabel: string;
  action: BadgeAction | null;
  /** remote_ahead AND binary_stale: the run also loads the newer binary. */
  binaryAlsoStale: boolean;
}

/**
 * v0.2.100 (WP-08, L3-F11): the ONE wording table, keyed on the badge kind.
 * Moved out of `UpdateBadge.svelte` so the Updates page renders the same
 * title / description / button for the same state.
 */
export function badgeCopyFor(
  kind: UpdateKind,
  us: BadgeCopyStatus | null,
  fallbackVersion = '',
): BadgeCopy {
  const action = actionForKind(kind);
  switch (kind) {
    case 'merge_in_progress':
      // v0.2.93 (field incident 2026-09-07): the clone is mid-merge
      // (`.git/MERGE_HEAD` present) and the launcher has no conflict payload
      // in memory. Nothing else can run against a conflicted tree.
      return {
        title: 'Update stopped at a merge conflict — resolve it',
        desc:
          `An orchestrator ${us?.resume_operation || 'merge'} on ` +
          `\`${us?.resume_branch || 'main'}\` stopped at a conflict and the ` +
          `clone is still mid-merge. Nothing else can update until you ` +
          `resolve it (keep local / accept upstream) or abort it. Click ` +
          `Resolve conflict to reopen the resolution dialog.`,
        buttonLabel: 'Resolve conflict',
        action,
        binaryAlsoStale: false,
      };
    case 'merge_resolved_incomplete': {
      const op = us?.resume_operation || 'update';
      const branch = us?.resume_branch || 'main';
      // v0.2.88 (MINOR-9): the autostash-pop sentinel reuses this kind but
      // its story differs — the merge SUCCEEDED and only restoring local WIP
      // clashed.
      if (isAutostashPopResume(op)) {
        return {
          title: 'Finish Update',
          desc:
            `A recent update on \`${branch}\` merged successfully, but ` +
            `restoring your uncommitted local changes (git \`--autostash\` ` +
            `pop) conflicted, so \`install.py --update\` and the binary ` +
            `refresh haven't run — last_installed_version is still ` +
            `v${us?.installed_version || '?'} while source is ` +
            `v${us?.source_version || '?'}. Resolve the conflicted file(s) ` +
            `(the update wrote the exact per-file steps to ` +
            `\`.claude/context/UPDATE_DEFERRED.md\`), then click Finish ` +
            `Update.`,
          buttonLabel: 'Finish Update',
          action,
          binaryAlsoStale: false,
        };
      }
      return {
        title: 'Continue Update',
        desc:
          `A previous orchestrator ${op} on \`${branch}\` was halted at a ` +
          `conflict and resolved outside the launcher. The source is merged ` +
          `but \`install.py --update\` and the binary refresh never ran — ` +
          `last_installed_version is still v${us?.installed_version || '?'} ` +
          `while source is v${us?.source_version || '?'}. Click Continue ` +
          `Update to finish the install.`,
        buttonLabel: 'Continue Update',
        action,
        binaryAlsoStale: false,
      };
    }
    case 'remote_ahead':
      return {
        title: 'Update available',
        desc: us
          ? `A new version of the orchestrator is available on the remote. Current: v${us.installed_version || us.source_version || fallbackVersion || 'unknown'}.`
          : 'A new version of the orchestrator is available.',
        buttonLabel: 'Fetch + Install',
        action,
        binaryAlsoStale: us?.binary_stale === true,
      };
    case 'binary_stale':
      return {
        title: 'Restart Launcher',
        // L3-F11: say WHY there is a newer binary, not only that there is one.
        desc: us
          ? `An update refreshed the launcher binary on disk (v${us.on_disk_binary_version}), but this window is still running v${us.running_version}. Restart to load it.`
          : 'An update refreshed the launcher binary on disk. Restart to load it.',
        buttonLabel: 'Restart Launcher',
        action,
        binaryAlsoStale: false,
      };
    case 'install_stale': {
      // v0.2.60: distinguish a fresh apply from RESUMING a half-finished
      // install (installed_version present but behind the source).
      const priorInstall = !!us?.installed_version;
      return {
        title: priorInstall ? 'Resume Update' : 'Install Update',
        desc: us
          ? priorInstall
            ? `A previous update to v${us.source_version} did not finish (last completed install: v${us.installed_version}). Click Resume Update to apply the rest.`
            : `v${us.source_version} is on disk. Click Install Update to apply.`
          : 'Source is newer than the last successful install. Click to apply.',
        buttonLabel: priorInstall ? 'Resume Update' : 'Install Update',
        action,
        binaryAlsoStale: false,
      };
    }
    default:
      return {
        title: 'Up to date',
        desc: 'No pending updates detected.',
        buttonLabel: '',
        action: null,
        binaryAlsoStale: false,
      };
  }
}

/**
 * v0.2.100 (WP-08): the root-ledger deferral condition ids an UPDATE BADGE /
 * restart action resolves. `DeferralBadge` uses this to point at the update
 * badge instead of only at the ledger (L3-F11: two badges surfacing one
 * unfinished update). Each id is registered in
 * `vco_lib/deferral_conditions.toml`.
 */
export const UPDATE_BADGE_DEFERRAL_IDS: ReadonlySet<string> = new Set([
  'update_resume_required',
  'update_install_phase_failed',
  'launcher_update_diverged',
  'launcher_update_post_pull_unverified',
  'launcher_restart_required',
]);

/** How many of `conditionIds` the update badge resolves. */
export function updateDeferralCount(conditionIds: readonly string[]): number {
  return conditionIds.filter((c) => UPDATE_BADGE_DEFERRAL_IDS.has(c)).length;
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
    case 'reset':
      return 'Resetting to upstream';
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
    case 'reset':
      return 'Resetting the clone to the upstream branch…';
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
 * payload (legacy `event` tag). Returns null on any other shape.
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

// ---------------------------------------------------------------------------
// v0.2.100 (WP-08, AD-1 + L3-F02/F06): ONE error router for every update run.
// ---------------------------------------------------------------------------

/** Where a failed update run goes: one of the four decision modals (hoisted
 *  in `+layout.svelte`, keyed on the store fields of the same name), or the
 *  overlay's failed state with ONE message. */
export type RoutedUpdateError =
  | { to: 'nonFf'; payload: OrchestratorNonFfPayload }
  | { to: 'untrackedCollision'; payload: OrchestratorUntrackedCollisionResolvablePayload }
  | { to: 'autostashPop'; payload: OrchestratorAutostashPopConflictPayload }
  | { to: 'conflict'; payload: OrchestratorConflictPayload }
  | { to: 'failed'; message: string; errorKind: string | null };

export type UpdateRoute = RoutedUpdateError['to'];

/** Shown when a failure carried no text at all (L3-F02/F03). The same words
 *  the Rust producer uses for an empty `Raw` error
 *  (`update_failure.rs`; fixture row `raw_double_prefixed_empty`). */
export const EMPTY_FAILURE_TEXT = 'The update failed and reported no reason.';

const FAILURE_PREFIX_RE = /^\s*(?:error:\s*)?update failed\s*:?\s*/i;

/**
 * v0.2.100 (WP-08, L3-F02): the text the overlay shows for a failure. The
 * backend contract is "never empty, never prefixed", but the overlay renders
 * its own "failed" heading, so any leading `Update failed:` (repeated or not,
 * any case) is stripped here and an empty reason becomes `fallback`
 * ({@link EMPTY_FAILURE_TEXT} by default). The case table is shared with the
 * Rust producer: `tests/fixtures/update_failure_messages.json`
 * (`normalise_cases`).
 * The result is never empty and never starts with "Update failed".
 */
export function normalizeFailureText(raw: unknown, fallback: string = EMPTY_FAILURE_TEXT): string {
  let s = raw === undefined || raw === null ? '' : errorText(raw);
  let prev: string | null = null;
  while (prev !== s) {
    prev = s;
    s = s.replace(FAILURE_PREFIX_RE, '');
  }
  s = s.trim();
  return s.length > 0 ? s : fallback;
}

/** The typed-error kinds `run_orchestrator_update` rejects with (Rust
 *  `UpdateSurfaceError`, serialised as `{kind, message, ...payload}`). */
const SURFACE_ERROR_KINDS = new Set([
  'Refused',
  'NonFastForward',
  'UntrackedCollision',
  'AutostashPop',
  'Conflict',
  'InstallFailed',
  'Raw',
]);

function parseJsonObject(raw: unknown): Record<string, unknown> | null {
  const text = raw instanceof Error ? raw.message : raw;
  if (text && typeof text === 'object' && !Array.isArray(text)) {
    return text as Record<string, unknown>;
  }
  if (typeof text !== 'string') return null;
  const s = text.trim();
  const brace = s.indexOf('{');
  if (brace < 0) return null;
  for (const candidate of brace === 0 ? [s] : [s, s.slice(brace)]) {
    try {
      const v: unknown = JSON.parse(candidate);
      if (v && typeof v === 'object' && !Array.isArray(v)) return v as Record<string, unknown>;
    } catch {
      // not JSON
    }
  }
  return null;
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [];
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function strOrNull(v: unknown): string | null {
  return typeof v === 'string' ? v : null;
}

/** The typed error's payload fields: nested under `payload`, or flattened
 *  beside `kind`/`message` — both are accepted. */
function surfaceFields(obj: Record<string, unknown>): Record<string, unknown> {
  const nested = obj.payload;
  if (nested && typeof nested === 'object' && !Array.isArray(nested)) {
    return { ...obj, ...(nested as Record<string, unknown>) };
  }
  return obj;
}

function failedFromSurface(obj: Record<string, unknown>, kind: string): RoutedUpdateError {
  const f = surfaceFields(obj);
  let message = normalizeFailureText(str(f.message) || str(f.reason));
  const logPath = str(f.log_path);
  if (logPath && !message.includes(logPath)) message = `${message} (log: ${logPath})`;
  return { to: 'failed', message, errorKind: kind };
}

/** Route a typed `{kind, message, ...}` rejection; null if `obj` is not one. */
function routeSurfaceError(obj: Record<string, unknown>): RoutedUpdateError | null {
  const kind = obj.kind;
  if (typeof kind !== 'string' || !SURFACE_ERROR_KINDS.has(kind)) return null;
  const f = surfaceFields(obj);
  switch (kind) {
    case 'NonFastForward':
      if (!str(f.branch)) return failedFromSurface(obj, kind);
      return {
        to: 'nonFf',
        payload: {
          event: 'orchestrator_update_non_ff',
          branch: str(f.branch),
          local_sha: strOrNull(f.local_sha),
          remote_sha: strOrNull(f.remote_sha),
          diverged_files: strList(f.diverged_files),
          git_stderr: str(f.git_stderr),
          local_only_files: Array.isArray(f.local_only_files) ? strList(f.local_only_files) : undefined,
          upstream_only_files: Array.isArray(f.upstream_only_files)
            ? strList(f.upstream_only_files)
            : undefined,
          upstream_only_count:
            typeof f.upstream_only_count === 'number' ? f.upstream_only_count : undefined,
        },
      };
    case 'UntrackedCollision':
      if (!Array.isArray(f.divergent_files) && !Array.isArray(f.identical_files)) {
        return failedFromSurface(obj, kind);
      }
      return {
        to: 'untrackedCollision',
        payload: {
          event: 'orchestrator_untracked_collision',
          operation: f.operation === 'rebase' || f.operation === 'update' ? f.operation : 'merge',
          branch: str(f.branch),
          resolvable: typeof f.resolvable === 'boolean' ? f.resolvable : undefined,
          identical_files: Array.isArray(f.identical_files) ? strList(f.identical_files) : undefined,
          divergent_files: strList(f.divergent_files),
          git_stderr: typeof f.git_stderr === 'string' ? f.git_stderr : undefined,
        },
      };
    case 'AutostashPop':
      if (!Array.isArray(f.conflicted_files)) return failedFromSurface(obj, kind);
      return {
        to: 'autostashPop',
        payload: {
          event: 'orchestrator_autostash_pop_conflict',
          branch: str(f.branch),
          conflicted_files: strList(f.conflicted_files),
          git_stderr: str(f.git_stderr),
        },
      };
    case 'Conflict':
      if (!Array.isArray(f.conflicted_files)) return failedFromSurface(obj, kind);
      return {
        to: 'conflict',
        payload: {
          event: 'orchestrator_update_conflict',
          operation: f.operation === 'rebase' ? 'rebase' : 'merge',
          branch: str(f.branch),
          conflicted_files: strList(f.conflicted_files),
          git_stderr: str(f.git_stderr),
        },
      };
    default:
      // Refused / InstallFailed / Raw → the overlay's failed state.
      return failedFromSurface(obj, kind);
  }
}

/**
 * v0.2.100 (WP-08): route ANY update-run rejection. Order:
 *   1. the typed `run_orchestrator_update` error (`{kind, message, ...}`);
 *   2. the legacy `event`-tagged payloads (`orchestrator_update_non_ff`,
 *      `orchestrator_untracked_collision`, `orchestrator_autostash_pop_conflict`,
 *      `orchestrator_update_conflict`) — still produced by the conflict
 *      modal's keep-local / accept-upstream commands and nested inside a
 *      typed error's payload by older backends;
 *   3. anything else → `failed` with the normalised, never-empty message.
 * Pure; never throws.
 */
export function routeUpdateError(raw: unknown): RoutedUpdateError {
  const obj = parseJsonObject(raw);
  if (obj) {
    const typed = routeSurfaceError(obj);
    if (typed) return typed;
  }
  const collision = parseUntrackedCollisionError(raw);
  if (collision) return { to: 'untrackedCollision', payload: collision };
  const pop = parseAutostashPopError(raw);
  if (pop) return { to: 'autostashPop', payload: pop };
  const conf = parseOrchestratorConflictError(raw);
  if (conf) return { to: 'conflict', payload: conf };
  const nff = parseNonFfError(raw);
  if (nff) return { to: 'nonFf', payload: nff };
  return { to: 'failed', message: normalizeFailureText(raw), errorKind: null };
}

/**
 * v0.2.100 (W3-FIX): what a recovery modal does with the route `failOp`
 * applied to its command's rejection. The resolve-and-continue commands
 * (`resolve_autostash_pop_and_retry`, `resolve_untracked_collision_and_retry`)
 * finish through the ONE update pipeline, so they reject with the same JSON
 * contract as `run_orchestrator_update` (`{kind, message, ...}`) — rendering
 * that raw (`${e}`) showed the JSON blob. Pure; never throws.
 *   - `failed` → `inline` is `<label>: <message>`, the message rendered ONCE
 *     and unprefixed (the same normalised text the overlay shows);
 *   - a decision-modal route → no inline text; the store has opened that
 *     modal, so `closeSelf` asks the caller to step aside — unless the route
 *     reopens the caller's OWN modal (`self`) with the fresh payload.
 */
export function modalFailure(
  routed: RoutedUpdateError,
  label: string,
  self: UpdateRoute,
): { inline: string | null; closeSelf: boolean } {
  if (routed.to === 'failed') return { inline: `${label}: ${routed.message}`, closeSelf: false };
  return { inline: null, closeSelf: routed.to !== self };
}

/** What `updater.run` resolves to (it never rejects). */
export type UpdateRunResult =
  | { ok: true; outcome: UpdateOutcome | null }
  | { ok: false; routed: RoutedUpdateError };

export interface UpdateRunOptions {
  /** Routes the CALLER renders itself; the store leaves those payloads
   *  unset and only closes the overlay. The divergence modal keeps its
   *  inline untracked-collision view this way. */
  handleLocally?: readonly UpdateRoute[];
}

function createUpdaterStore() {
  const { subscribe, update } = writable<UpdaterState>({
    available: false,
    kind: null,
    lastSeenVersion: loadSeen(),
    updating: false,
    op: null,
    error: null,
    failed: false,
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
   * `run(kind)` / `runRestart` here, and the conflict modal's keep-local /
   * accept-upstream / abort handlers (git operations that are not update
   * kinds; they end through `failOp` / `endOp`).
   *
   * Deliberately does NOT touch the decision-modal payload fields
   * (`nonFf` / `conflict` / …): a merge started FROM the divergence modal
   * must keep `nonFf` set, or the modal that launched it unmounts
   * mid-flight (the second half of the incident).
   */
  function beginOp(kind: UpdateOpKind) {
    orchestrator.resetProgress();
    update((s) => ({ ...s, updating: true, op: kind, error: null, failed: false }));
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
    // v0.2.100 (L3-F03): failure is `err !== undefined/null`, INCLUDING an
    // empty string — which used to read as success. The text is normalised
    // so it is never empty and never double-prefixed.
    const failed = err !== undefined && err !== null;
    const error = failed ? normalizeFailureText(err) : null;
    update((s) => ({ ...s, updating: false, error, failed }));
  }

  /** Apply a routed failure: set the decision-modal payload FIRST (unless the
   *  caller renders it itself), then end the op, so the overlay's falling
   *  edge sees the hand-over and closes instead of celebrating. */
  function applyRouted(routed: RoutedUpdateError, local: readonly UpdateRoute[]) {
    if (routed.to === 'failed') {
      endOp(routed.message);
      return;
    }
    if (!local.includes(routed.to)) {
      switch (routed.to) {
        case 'untrackedCollision':
          update((s) => ({ ...s, untrackedCollision: routed.payload, nonFf: null }));
          break;
        case 'autostashPop':
          update((s) => ({ ...s, autostashPop: routed.payload, nonFf: null }));
          break;
        case 'conflict':
          // Same rule as `setConflict`: the divergence modal's job is done.
          update((s) => ({ ...s, conflict: routed.payload, nonFf: null }));
          break;
        case 'nonFf':
          update((s) => ({ ...s, nonFf: routed.payload }));
          break;
      }
    }
    endOp();
  }

  const api = {
    subscribe,

    /** v0.2.93: see the inner `beginOp` — public surface for the modals. */
    beginOp(kind: UpdateOpKind) {
      beginOp(kind);
    },

    /** v0.2.93: see the inner `endOp` — public surface for the modals. */
    endOp(err?: unknown) {
      endOp(err);
    },

    /**
     * v0.2.100 (WP-08, L3-F06): end a modal-launched op that FAILED, routing
     * the rejection exactly as `run` does (a structured payload opens its
     * decision modal; anything else is the overlay's failed state with one
     * normalised message). Returns the route so the caller can mirror it.
     */
    failOp(raw: unknown): RoutedUpdateError {
      const routed = routeUpdateError(raw);
      applyRouted(routed, []);
      return routed;
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

    /**
     * v0.2.100 (WP-08, AD-1): THE update action. `beginOp` → the one backend
     * command `run_orchestrator_update({kind})` → `routeUpdateError` → `endOp`.
     * Resolves (never rejects) to what happened so a modal that started the
     * run can keep its own retry state; the store has already routed the
     * failure to a decision modal or to the overlay's failed state.
     *
     * On success the backend usually relaunches the launcher mid-call, so the
     * success branch mostly runs only when it did not (e.g. `restarted:
     * false`); it clears the badge state and re-checks.
     */
    async run(kind: UpdateRunKind, opts: UpdateRunOptions = {}): Promise<UpdateRunResult> {
      if (!tauriAvailable()) {
        return { ok: false, routed: { to: 'failed', message: 'The launcher backend is not available.', errorKind: null } };
      }
      // A fresh run supersedes any decision payload EXCEPT the one whose
      // modal launched it (a Merge/Rebase started from the divergence modal
      // must keep `nonFf`, or that modal unmounts mid-flight).
      update((s) => ({
        ...s,
        // ResetHard is started from the divergence modal too (F-W2-03).
        nonFf: kind === 'Merge' || kind === 'Rebase' || kind === 'ResetHard' ? s.nonFf : null,
        conflict: kind === 'Resume' ? s.conflict : null,
        untrackedCollision: null,
        autostashPop: null,
      }));
      beginOp(opForRunKind(kind));
      let outcome: UpdateOutcome | null;
      try {
        outcome = await orchestrator.runUpdate(kind);
      } catch (e) {
        const routed = routeUpdateError(e);
        applyRouted(routed, opts.handleLocally ?? []);
        // A refused / failed run can leave a DIFFERENT pending state (e.g.
        // "nothing to resume but a real update is pending") — re-check so the
        // badge shows the genuine one.
        await orchestrator.checkStatus();
        return { ok: false, routed };
      }
      // A successful Merge/Rebase ends the divergence modal's job: clear
      // `nonFf` BEFORE `endOp` so the overlay's falling edge reads success,
      // not a hand-over. (A successful Resume keeps `conflict`: the conflict
      // modal shows its own success line and closes itself. A successful
      // ResetHard keeps `nonFf` the same way: the divergence modal names
      // where the discarded commits were saved, then the user closes it.)
      update((s) => ({
        ...s,
        available: false,
        kind: null,
        dismissed: false,
        nonFf: kind === 'Merge' || kind === 'Rebase' ? null : s.nonFf,
      }));
      endOp();
      await orchestrator.checkStatus();
      void gatewayFreshness.check();
      return { ok: true, outcome };
    },

    /**
     * v0.2.100 (WP-08): perform a badge action (from {@link actionForKind}).
     * The badge and the Updates page both call this, so a state offers the
     * same action on both surfaces.
     */
    async perform(action: BadgeAction | null): Promise<void> {
      if (!action) return;
      switch (action.type) {
        case 'run':
          await api.run(action.kind);
          return;
        case 'restart':
          await api.runRestart();
          return;
        case 'resolve_conflict':
          await api.openPendingConflict();
          return;
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
      update((s) => ({ ...s, error: null, failed: false }));
    },
  };
  // `api.<method>` (never `this.`) inside the methods, so a method passed as a
  // bare callback (`onclick={updater.perform}`) still works.
  return api;
}

export const updater = createUpdaterStore();
