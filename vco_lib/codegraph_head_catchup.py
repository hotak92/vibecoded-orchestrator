# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""HEAD-diff catch-up for the PRIMARY code-graph root (v0.2.105 #512A).

Why this exists (field report, 2026-10-29): a project's code graph went SIX
DAYS stale. The binding's ``last_analyzed_commit`` stayed at a commit from
10-23 while HEAD moved 1,070 commits ahead, because the only thing that ever
queued a file was the Edit/Write hook — and it queues only edits made through
the Edit tool in the MAIN checkout (worktree edits are dropped on purpose,
FIX-A'). Code that lands through a ``git merge`` of lane branches, a
``git pull``, a commit made from a shell, or a GUI merge is never queued, so
the graph silently stops tracking the repository.

The catch-up closes that gap WITHOUT installing git hooks into user repos:

1. read this project's recorded ``last_analyzed_commit`` (hub ``/config``,
   ``code_graph_last_analyzed_commit``) and the main checkout's HEAD;
2. equal → nothing to do (the leave-alone case);
3. behind, and the recorded commit is an ANCESTOR of HEAD → the files in
   ``git diff --name-only <recorded>..HEAD`` are filtered exactly the way the
   drain and the analyzer filter (code extension, no transient
   ``.claude/state`` scratch, existing files only) and analyzed in ONE scoped
   ``--only-files-from`` run — the drain's own batched shape. A FAILED diff
   (timeout, git error) records nothing: failure is distinct from empty;
4. behind by MORE than ``--max-files`` → the same efficient path the analyzer
   already has: one ``--incremental --since-commit <recorded>`` walk (which
   also prunes the entities of deleted/renamed files);
5. the recorded commit is MISSING or is NOT an ancestor (a rewritten history)
   → ONE full (non-incremental) analyzer walk, then record HEAD — the only
   converging shape: an ``--incremental HEAD~1..HEAD`` walk indexed one
   commit, recorded nothing and re-ran every interval forever;
6. the analysis succeeded → record HEAD through the hub route
   ``POST /api/v1/projects/{id}/codegraph/analyzed`` (launcher.db keeps its
   single writer: this module never opens it), presenting the PROJECT-SCOPED
   ``hub.token.<project_id>`` that route requires. A failed run records
   NOTHING, so the next eligible catch-up retries the same diff. A run the
   analyzer SOFT-SKIPPED (no embedding backend → "Code-graph analysis
   skipped", exit 0) counts as FAILED, never as success — the check lives in
   :mod:`vco_lib.codegraph_analyzer_outcome`, one home shared with the resync
   driver and every other ``run_analyzer`` caller.

7. a walk that keeps failing (a full walk past the timeout, a hub that keeps
   refusing the recording) does not restart on every Stop after
   :data:`BACKOFF_AFTER_FAILURES` consecutive failures: the next attempt is
   skipped (``backoff``) until a doubling window elapses. Any recorded outcome
   clears the count.

A project with no ENABLED code-graph binding is skipped entirely (the
``code_graph_bound`` config field): the hub serves a name-derived fallback
prefix even for an unbound project, and the catch-up must never create
collections on an unbound project's behalf.

The trigger lives in the Stop hook (``templates/hooks/stop-codegraph-drain.
{sh,ps1}``), which spawns this module DETACHED at most once per
``VCO_CODEGRAPH_HEAD_CATCHUP_INTERVAL_SECONDS`` (default 600) — a Stop hook
must never block the turn, and a merge produces no queue entry, so the check
must run whether or not the turn queued anything.

Deliberately NOT covered: a session whose checkout is a linked git WORKTREE
(``worktree_session``). Worktree edits are dropped from the index on purpose
(FIX-A'), worktree lanes share the main repo's commit graph, and stamping the
binding from a worktree HEAD would advance it to a commit the main checkout
does not have. The main checkout's own session runs the catch-up.

Every decision is one line in
``<project>/.claude/logs/codegraph_head_catchup.log`` (size-capped). Exit
status is always 0.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from vco_lib.codegraph_extras_refresh import (
    AnalyzerResult,
    RunLog,
    run_analyzer,
)
from vco_lib.git_meta import git_head_sha, run_git
from vco_lib.paths import looks_like_orchestrator_root

#: Above this many changed files one scoped analyzer run is the wrong shape —
#: hand the diff to the analyzer's own ``--incremental --since-commit`` walk
#: instead of a per-turn batch (an argv + list file of thousands of paths).
DEFAULT_MAX_DIFF_FILES = 2000
#: Hard ceiling on one analyzer run — the same 30 min the drain/extras use.
DEFAULT_TIMEOUT_SECONDS = 1800
#: Wall-clock ceiling for the ONE ``git diff --name-only`` call. The module's
#: other git calls are plumbing and keep ``git_meta``'s 5 s default, but a
#: diff over a thousand-commit range on a slow disk or Windows can outgrow
#: it; this module runs detached, so a generous ceiling costs no turn time.
DIFF_TIMEOUT_SECONDS = 60
LOG_NAME = "codegraph_head_catchup.log"

#: v0.2.105 fix round (review LOW): the Stop hook spawns this module at most
#: once per interval (600 s), and a walk that CANNOT converge — a full walk
#: past ``DEFAULT_TIMEOUT_SECONDS`` on a huge tree, or a recording the hub keeps
#: refusing — used to restart on every Stop forever, each one holding the
#: project's walk claim (a GUI build then reads "already running"). After this
#: many CONSECUTIVE failed runs the module backs off: the next attempt is
#: skipped (``"backoff"``) until the window elapses, which doubles per
#: additional failure up to :data:`BACKOFF_MAX_SECONDS`. Any recorded outcome
#: resets the count, so a healthy project never sees the gate.
BACKOFF_AFTER_FAILURES = 3
#: First backoff window once :data:`BACKOFF_AFTER_FAILURES` is reached.
BACKOFF_BASE_SECONDS = 3600
#: Ceiling of the doubling window.
BACKOFF_MAX_SECONDS = 6 * 3600

#: The drain's extension gate, byte for byte
#: (``templates/hooks/stop-codegraph-drain.sh :: CODE_RE``). MUST MATCH.
CODE_RE = re.compile(
    r"\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|json|ini|tweakers|sh|bash)$"
)


# ── Share the drain's / extras' interpreter + analyzer runner ─────────────
# ``run_analyzer`` / ``AnalyzerResult`` / ``RunLog`` are imported from
# ``vco_lib.codegraph_extras_refresh``: one home for "run the analyzer, parse
# its final report, decide ok/failed" and for the size-capped decision log.
# The argv builders below ARE specific to this catch-up and MUST match the
# drain's own spawn line (see their docstrings).


def path_key(path: str) -> str:
    """md5 of a path — the key the drain's per-root lock uses
    (``codegraph_drain_root_<md5>.lock``), so a catch-up run serialises with a
    drain batch over the same checkout. Callers pass the CANONICAL root
    (git-resolved realpath), which is what the drain keys on — not the
    session's spelling of it."""
    return hashlib.md5(path.encode("utf-8")).hexdigest()  # noqa: S324 — a key, not security


# ── Git plumbing (one home: vco_lib.git_meta) ─────────────────────────────


def git_head(root: str) -> Optional[str]:
    """FULL SHA of ``root``'s HEAD, or None (not a repo / no commits / git
    missing)."""
    return git_head_sha(Path(root))


def is_ancestor(root: str, ancestor: str, descendant: str) -> bool:
    """True iff ``ancestor`` is reachable from ``descendant``.

    ``rc is None`` (git not runnable) and any non-zero exit — including the
    ``128`` git returns for an UNKNOWN object — are both "not provably an
    ancestor": the caller then takes the conservative
    ``--incremental`` fallback, never a destructive action.
    """
    rc, _out, _err = run_git(
        Path(root), ["merge-base", "--is-ancestor", ancestor, descendant]
    )
    return rc == 0


def diff_paths(root: str, old: str, new: str) -> Optional[list[str]]:
    """``git diff --name-only -z <old>..<new>`` → repo-relative paths.

    NUL-separated (``-z``) so a path holding a space or a quote survives
    verbatim. ``None`` on any git failure (non-zero exit, timeout, git not
    runnable) — FAILURE is distinct from an empty diff: an empty list means
    "nothing changed", while ``None`` means "cannot see the range", and the
    caller must record nothing rather than advance the baseline over a diff
    it could not read (review M2: a timed-out 1,070-commit diff used to read
    as "no changes" and silently stamped HEAD).
    """
    rc, out, _err = run_git(
        Path(root),
        ["diff", "--name-only", "-z", f"{old}..{new}"],
        timeout=DIFF_TIMEOUT_SECONDS,
    )
    if rc != 0:
        return None
    return [p for p in out.split("\0") if p]


def canonical_repo_root(root: str) -> Optional[str]:
    """The MAIN checkout root a linked worktree shares, or None.

    Python twin of ``templates/hooks/_lib/canonical-repo-root.sh``
    (``git rev-parse --path-format=absolute --git-common-dir`` → dirname →
    strip ``/.git`` → normalise). Used ONLY to tell a worktree session from
    the main checkout; a resolution failure is read as "not a worktree".
    """
    repo = Path(root)
    rc, common, _err = run_git(
        repo, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    )
    if rc != 0 or not common:
        rc, common, _err = run_git(repo, ["rev-parse", "--git-common-dir"])
        if rc != 0 or not common:
            return None
        if not os.path.isabs(common):
            common = os.path.join(str(repo), common)
    base = common.rstrip("/")
    if base.endswith("/.git"):
        base = base[: -len("/.git")]
    try:
        resolved = Path(base).resolve()
    except OSError:
        return None
    return str(resolved) if resolved.is_dir() else None


# ── The drain's argv shapes ───────────────────────────────────────────────


def index_dot_claude_flag(root: str) -> str:
    """``--index-dot-claude`` for the orchestrator clone, else
    ``--no-index-dot-claude``.

    MUST MATCH ``stop-codegraph-drain.sh :: _drain_index_dot_claude``'s
    fallback branch. (Its hub-resolver branch reads a
    ``code_graph_index_dot_claude`` field the resolver does not serve today,
    so the shell hook always lands on the same root-autodetect heuristic —
    ``vco_lib.paths.looks_like_orchestrator_root`` is the ONE home for it,
    shared with the analyzer's own default.)
    """
    return (
        "--index-dot-claude"
        if looks_like_orchestrator_root(Path(root))
        else "--no-index-dot-claude"
    )


def build_scoped_argv(
    root: str, prefix: str, list_file: str, dot_flag: str
) -> list[str]:
    """Analyzer argv (after the script) for ONE scoped batch over a file list.

    MUST MATCH the drain's detached spawn:
    ``"$1" "$2" "$3" --project "$4" --only-files-from "$5"
    --canonical-source "$3" $6`` — same flags, same order.
    """
    return [
        root,
        "--project",
        prefix,
        "--only-files-from",
        list_file,
        "--canonical-source",
        root,
        dot_flag,
    ]


def build_incremental_argv(
    root: str, prefix: str, since: Optional[str], dot_flag: str
) -> list[str]:
    """Analyzer argv for the analyzer's own incremental walk.

    ``since`` set → restrict to ``<since>..HEAD`` (and prune the entities of
    deleted/renamed files, ``--since-commit`` is what enables that prune).
    ``since`` None → the analyzer's default ``HEAD~1..HEAD`` bound.

    ``--canonical-source`` is deliberately ABSENT: the analyzer REFUSES it
    (exit 1) unless a single-file/``--only-files-from`` mode is in force.
    """
    argv = [root, "--project", prefix, "--incremental"]
    if since:
        argv += ["--since-commit", since]
    return argv + [dot_flag]


def build_full_argv(root: str, prefix: str, dot_flag: str) -> list[str]:
    """Analyzer argv for ONE full (non-incremental) walk of ``root``.

    The converging fallback for a baseline that cannot be diffed (no
    recorded commit, or a rewritten history): re-index everything at HEAD,
    then record HEAD — the only route that PROVES the new baseline. An
    ``--incremental HEAD~1..HEAD`` walk here (the pre-fix shape) indexed one
    commit's worth of files, recorded nothing and re-ran every interval
    forever (review M3).
    """
    return [root, "--project", prefix, dot_flag]


# ── Recording the new baseline (hub = the single launcher.db writer) ─────


def record_analyzed_commit(project_id: str, commit: str) -> tuple[bool, str]:
    """POST the new baseline to the hub
    (``/projects/{id}/codegraph/analyzed`` → advance-only stamp).

    That route is PROJECT-SCOPED (v0.2.105 fix round, same gate as ``/env``
    and ``/config``): it accepts only ``hub.token.<project_id>``, so the
    project id is passed through for the bearer — the global ``hub.token``
    is refused there.
    """
    from vco_lib import project_config

    try:
        resp = project_config.hub_post_json(
            f"projects/{project_id}/codegraph/analyzed",
            {"commit": commit},
            project_id=project_id,
        )
    except project_config.ResolverError as exc:
        return False, f"hub unreachable: {exc}"
    if resp.status_code == 200:
        return True, "recorded"
    return False, f"hub answered {resp.status_code}: {resp.text[:200]}"


# ── The driver ───────────────────────────────────────────────────────────


@dataclass
class Deps:
    """Injection seam for tests (defaults are the real implementations)."""

    resolve: Callable[[str], object]
    git_head: Callable[[str], Optional[str]] = git_head
    is_ancestor: Callable[[str, str, str], bool] = is_ancestor
    diff_paths: Callable[[str, str, str], Optional[list[str]]] = diff_paths
    run_analyzer: Callable[..., AnalyzerResult] = run_analyzer
    record: Callable[[str, str], tuple[bool, str]] = record_analyzed_commit
    canonical_root: Callable[[str], Optional[str]] = canonical_repo_root
    #: Wall clock for the backoff window (review LOW) — a test moves it.
    now: Callable[[], float] = time.time


def _str_or_none(value: object) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def indexable_files(root: str, rel_paths: Sequence[str]) -> tuple[list[str], int]:
    """``(absolute paths to index, count of vanished code files)``.

    Same rules the drain applies per queued line, in the same order:
    a code extension (``CODE_RE``), no transient ``.claude/state`` scratch, and
    the file must still exist. A path that no longer exists is counted, never
    queued — the brief's "existing, indexable files only". Deletions are the
    incremental walk's job (it prunes by ``--since-commit``), not this batch's.
    """
    base = Path(root)
    keep: list[str] = []
    vanished = 0
    for rel in rel_paths:
        if not CODE_RE.search(rel):
            continue
        if "/.claude/state/" in f"/{rel.replace(os.sep, '/')}":
            continue
        candidate = base / rel
        try:
            if not candidate.is_file():
                vanished += 1
                continue
        except OSError:
            vanished += 1
            continue
        keep.append(str(candidate))
    return keep, vanished


def _acquire_lock(state_dir: Path, key: str) -> Optional[Path]:
    """Atomic mkdir per-root lock (the drain's own lock name). None = busy."""
    lock = state_dir / f"codegraph_drain_root_{key}.lock"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        lock.mkdir()
    except FileExistsError:
        return None
    except OSError:
        return None
    try:
        (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    return lock


def _release_lock(lock: Optional[Path]) -> None:
    if lock is None:
        return
    try:
        (lock / "pid").unlink(missing_ok=True)
        lock.rmdir()
    except OSError:
        pass


# ── Backoff after consecutive failures (review LOW) ───────────────────────
#
# One file per project under the state dir:
# ``{"failures": N, "retry_at": <epoch>}``. Written on every FAILED run
# (``analyzer_failed`` / ``record_failed``), removed on a recorded one. Every
# read/write soft-fails: an unreadable or unwritable state file must never
# turn a catch-up into an error (a lost file merely retries at the ordinary
# interval).


def backoff_state_path(state_dir: Path, project_id: str) -> Path:
    """Where this project's consecutive-failure state lives."""
    return state_dir / f"codegraph_head_catchup_backoff_{path_key(project_id)}.json"


def _read_backoff(path: Path) -> tuple[int, float]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, 0.0
    if not isinstance(raw, dict):
        return 0, 0.0
    try:
        failures = int(raw.get("failures", 0) or 0)
        retry_at = float(raw.get("retry_at", 0) or 0)
    except (TypeError, ValueError):
        return 0, 0.0
    return max(0, failures), max(0.0, retry_at)


def backoff_window_seconds(failures: int) -> int:
    """The cooling window after ``failures`` consecutive failed runs.

    ``0`` below :data:`BACKOFF_AFTER_FAILURES` (the ordinary interval still
    applies); then :data:`BACKOFF_BASE_SECONDS`, doubling per further failure,
    capped at :data:`BACKOFF_MAX_SECONDS`.
    """
    if failures < BACKOFF_AFTER_FAILURES:
        return 0
    doublings = failures - BACKOFF_AFTER_FAILURES
    return min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** doublings))


def _write_backoff(state_dir: Path, project_id: str, failures: int,
                   retry_at: float) -> None:
    path = backoff_state_path(state_dir, project_id)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"failures": failures, "retry_at": retry_at}),
            encoding="utf-8",
        )
    except OSError:
        pass  # a lost counter only means "retry at the ordinary interval"


def _clear_backoff(state_dir: Path, project_id: str) -> None:
    try:
        backoff_state_path(state_dir, project_id).unlink(missing_ok=True)
    except OSError:
        pass


def _note_failure(
    state_dir: Path, project_id: str, log: Callable[[str], None],
    label: str, now: float,
) -> None:
    """Count one failed run; once the threshold is reached, log the window
    the next attempt must wait for (the gate reads it from the file)."""
    path = backoff_state_path(state_dir, project_id)
    failures, _retry_at = _read_backoff(path)
    failures += 1
    window = backoff_window_seconds(failures)
    _write_backoff(state_dir, project_id, failures, now + window)
    if window:
        log(
            f"backoff {label}: {failures} consecutive failed catch-up run(s) "
            f"for project={project_id}; the next attempt is skipped for "
            f"{window}s (capped at {BACKOFF_MAX_SECONDS}s) — the baseline is "
            "unchanged and nothing is recorded until then"
        )


def _backoff_remaining(state_dir: Path, project_id: str, now: float) -> float:
    """Seconds the next attempt must still wait (``0`` = run now)."""
    failures, retry_at = _read_backoff(backoff_state_path(state_dir, project_id))
    if backoff_window_seconds(failures) == 0:
        return 0.0
    return max(0.0, retry_at - now)


def _run_walk(
    project_root: str,
    prefix: str,
    head: str,
    since: Optional[str],
    *,
    full: bool,
    lock_root: str,
    analyzer: str,
    python: str,
    state_dir: Path,
    log: Callable[[str], None],
    timeout: int,
    deps: Deps,
    label: str,
) -> AnalyzerResult:
    """One analyzer walk (incremental with ``since``, or full) under the
    per-root lock AND the project's single-flight walk claim (so it never
    twins a launcher/driver walk of the same project — the 2026-10-14 field
    defect).

    ``lock_root`` is the CANONICAL root (git-resolved realpath) the drain
    keys its own per-root lock on, so a catch-up walk serialises with a
    drain batch over the same checkout even when this session spelled the
    root differently (symlink, trailing-dot form) — review LOW.
    """
    from vco_lib.codegraph_walk import WalkAlreadyRunning, walk_claim

    key = path_key(lock_root)
    lock = _acquire_lock(state_dir, key)
    if lock is None:
        log(f"{label}: busy — another analyzer run holds the root lock; nothing recorded")
        return AnalyzerResult(False, "busy")
    try:
        try:
            with walk_claim(prefix):
                if full:
                    argv = build_full_argv(
                        project_root, prefix, index_dot_claude_flag(project_root)
                    )
                    range_desc = f"full walk → {head[:12]}"
                else:
                    argv = build_incremental_argv(
                        project_root, prefix, since, index_dot_claude_flag(project_root)
                    )
                    range_desc = f"{since or 'HEAD~1'}..{head[:12]}"
                log(f"{label}: walk {project_root} prefix={prefix} {range_desc}")
                return deps.run_analyzer(python, analyzer, argv, timeout)
        except WalkAlreadyRunning:
            log(
                f"{label}: busy — another code-graph walk holds the single-flight "
                "claim for this prefix; nothing recorded"
            )
            return AnalyzerResult(False, "busy")
    finally:
        _release_lock(lock)


def catchup(
    project_root: str,
    analyzer: str,
    python: str,
    state_dir: Path,
    log: Callable[[str], None],
    *,
    max_files: int = DEFAULT_MAX_DIFF_FILES,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    deps: Optional[Deps] = None,
) -> str:
    """Bring the PRIMARY root's code graph up to HEAD. Returns the outcome
    (one of ``up_to_date``, ``no_git_head``, ``no_config``, ``not_bound``,
    ``worktree_session``, ``diff_failed``, ``busy``, ``backoff``,
    ``analyzer_failed``, ``record_failed``, ``recorded``). Never raises."""
    if deps is None:
        from vco_lib import project_config

        deps = Deps(resolve=project_config.resolve)

    root = str(Path(project_root))
    try:
        cfg = deps.resolve(root)
    except Exception as exc:  # noqa: BLE001 — hub down/unregistered: nothing to do
        log(f"skip project={root}: config unavailable ({exc})")
        return "no_config"
    project_id = str(getattr(cfg, "project_id", "") or "")
    prefix = str(getattr(cfg, "code_graph_collection_prefix", "") or "")
    recorded = _str_or_none(getattr(cfg, "code_graph_last_analyzed_commit", None))
    if not project_id or not prefix:
        log(f"skip project={root}: no project id / code-graph prefix")
        return "no_config"
    # The binding's enabled gate (review M3): /config serves a NAME-derived
    # fallback prefix even for a project with NO binding row, so a prefix
    # alone proves nothing. A project that is not bound (no row, or the row
    # is disabled) must never get code-graph collections created on its
    # behalf — and never get its (nonexistent) baseline stamped.
    bound = bool(getattr(cfg, "code_graph_bound", False))
    if not bound:
        log(
            f"skip project={root}: no enabled code-graph binding — the "
            "catch-up never creates collections for an unbound project"
        )
        return "not_bound"

    head = deps.git_head(root)
    if not head:
        log(f"skip project={root}: no git HEAD (not a repo, no commits, or git missing)")
        return "no_git_head"
    if recorded and recorded == head:
        # Nothing to converge, so any earlier failure chain is over: drop the
        # backoff state (review LOW) so the NEXT real difference starts fresh.
        _clear_backoff(state_dir, project_id)
        return "up_to_date"

    canonical = deps.canonical_root(root)
    if canonical and Path(canonical) != Path(root).resolve():
        log(
            f"skip project={root}: linked worktree of {canonical} — the main "
            "checkout's own session owns this catch-up (FIX-A')"
        )
        return "worktree_session"
    # The lock key follows the drain's canonical root (same resolution), so
    # the catch-up and the drain serialise however the root was spelled.
    lock_root = canonical or root

    # Review LOW: the Stop hook spawns this module once per interval (600 s),
    # and a walk that CANNOT converge (a full walk past the timeout on a huge
    # tree, a recording the hub keeps refusing) used to restart on every Stop
    # forever — each run holding the project's walk claim. After
    # BACKOFF_AFTER_FAILURES consecutive failed runs, skip until the doubling
    # window elapses. Any recorded outcome (or an up-to-date tree) clears it.
    remaining = _backoff_remaining(state_dir, project_id, deps.now())
    if remaining > 0:
        log(
            f"backoff project={project_id}: {int(remaining) + 1}s left before "
            "the next attempt after consecutive failed catch-up run(s); the "
            "baseline is unchanged — skipping this one"
        )
        return "backoff"

    def _recorded() -> str:
        _clear_backoff(state_dir, project_id)
        return "recorded"

    def _failed(outcome: str, label: str) -> str:
        _note_failure(state_dir, project_id, log, label, deps.now())
        return outcome

    def _result(ok: bool, label: str) -> str:
        return _recorded() if ok else _failed("record_failed", label)

    if not recorded:
        log(
            f"no recorded commit for project={project_id} prefix={prefix} — "
            "running ONE full analyzer walk, then recording HEAD (the "
            "converging fallback: an unprovable baseline is re-proven, not "
            "advanced blind)"
        )
        res = _run_walk(
            root, prefix, head, None, full=True, lock_root=lock_root,
            analyzer=analyzer, python=python, state_dir=state_dir, log=log,
            timeout=timeout, deps=deps, label="no_recorded",
        )
        if res.reason == "busy":
            return "busy"
        if not res.ok:
            log(
                f"no_recorded: FAILED project={project_id} full walk at "
                f"{head[:12]}: {res.reason}; baseline unchanged"
            )
            return _failed("analyzer_failed", "no_recorded")
        ok, why = deps.record(project_id, head)
        log(
            f"{'recorded' if ok else 'NOT RECORDED'} project={project_id} "
            f"via full walk (no prior baseline) at {head[:12]}: {why}",
        )
        return _result(ok, "no_recorded")

    if not deps.is_ancestor(root, recorded, head):
        log(
            f"recorded commit {recorded[:12]} is NOT an ancestor of HEAD "
            f"{head[:12]} (rewritten history or unknown object) — running ONE "
            "full analyzer walk, then recording HEAD (the converging fallback: "
            "a baseline that cannot be diffed is re-proven, not advanced blind)"
        )
        res = _run_walk(
            root, prefix, head, None, full=True, lock_root=lock_root,
            analyzer=analyzer, python=python, state_dir=state_dir, log=log,
            timeout=timeout, deps=deps, label="not_ancestor",
        )
        if res.reason == "busy":
            return "busy"
        if not res.ok:
            log(
                f"not_ancestor: FAILED project={project_id} full walk at "
                f"{head[:12]}: {res.reason}; baseline unchanged"
            )
            return _failed("analyzer_failed", "not_ancestor")
        ok, why = deps.record(project_id, head)
        log(
            f"{'recorded' if ok else 'NOT RECORDED'} project={project_id} "
            f"via full walk (rewritten history) at {head[:12]}: {why}",
        )
        return _result(ok, "not_ancestor")

    paths = deps.diff_paths(root, recorded, head)
    if paths is None:
        # M2: a FAILED diff (timeout, git error) is not an empty one. Nothing
        # is analyzed and nothing recorded — the next eligible catch-up
        # retries the same diff instead of silently advancing the baseline.
        log(
            f"skip project={project_id} {recorded[:12]}..{head[:12]}: "
            "git diff failed (timeout or git error); baseline unchanged"
        )
        return "diff_failed"
    files, vanished = indexable_files(root, paths)

    # A code file that vanished in the range must be PRUNED, and only the
    # analyzer's ``--incremental --since-commit`` walk does that (its
    # deleted-file pass). Same route as a huge diff: one incremental walk
    # instead of a scoped batch that cannot express a deletion.
    if not files and vanished == 0:
        ok, why = deps.record(project_id, head)
        log(
            f"{'recorded' if ok else 'NOT RECORDED'} project={project_id} "
            f"{recorded[:12]}..{head[:12]}: no indexable file changed: {why}"
        )
        return _result(ok, "empty_diff")

    if vanished > 0 or len(files) > max_files:
        label = "deleted_files" if vanished > 0 else "huge_diff"
        res = _run_walk(
            root, prefix, head, recorded, full=False, lock_root=lock_root,
            analyzer=analyzer, python=python, state_dir=state_dir, log=log,
            timeout=timeout, deps=deps, label=label,
        )
        if res.reason == "busy":
            return "busy"
        if not res.ok:
            log(
                f"{label}: FAILED project={project_id} {recorded[:12]}..{head[:12]}: "
                f"{res.reason}; baseline unchanged"
            )
            return _failed("analyzer_failed", label)
        ok, why = deps.record(project_id, head)
        log(
            f"{'recorded' if ok else 'NOT RECORDED'} project={project_id} "
            f"{recorded[:12]}..{head[:12]} via incremental walk ({label}, "
            f"{vanished} deleted code file(s)): files={res.files_analyzed} "
            f"deleted_files={res.deleted_files} removed_entities={res.deleted_entities} "
            f"ms={res.duration_ms}: {why}",
        )
        return _result(ok, label)

    key = path_key(lock_root)
    lock = _acquire_lock(state_dir, key)
    if lock is None:
        log(f"busy: another analyzer run holds the root lock for {root}; nothing recorded")
        return "busy"
    list_file = state_dir / f"codegraph_head_catchup_files_{key}.txt"
    try:
        try:
            list_file.write_text("\n".join(files) + "\n", encoding="utf-8")
        except OSError as exc:
            log(f"skip project={root}: cannot write the file list ({exc})")
            return "busy"
        argv = build_scoped_argv(
            root, prefix, str(list_file), index_dot_claude_flag(root)
        )
        log(
            f"batch project={project_id} prefix={prefix} {recorded[:12]}..{head[:12]} "
            f"files={len(files)} vanished={vanished}"
        )
        res = deps.run_analyzer(python, analyzer, argv, timeout)
        if not res.ok:
            log(
                f"FAILED project={project_id} {recorded[:12]}..{head[:12]}: "
                f"{res.reason}; baseline unchanged"
            )
            return _failed("analyzer_failed", "batch")
        ok, why = deps.record(project_id, head)
        log(
            f"{'recorded' if ok else 'NOT RECORDED'} project={project_id} "
            f"{recorded[:12]}..{head[:12]}: files={res.files_analyzed} "
            f"entities={res.entities_indexed} ms={res.duration_ms}: {why}"
        )
        return _result(ok, "batch")
    finally:
        try:
            list_file.unlink(missing_ok=True)
        except OSError:
            pass
        _release_lock(lock)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vco_lib.codegraph_head_catchup")
    ap.add_argument("--project-root", required=True)
    ap.add_argument("--analyzer", required=True)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--max-files", type=int, default=None,
                    help="changed files above which the analyzer's incremental "
                         f"walk replaces the scoped batch (default "
                         f"{DEFAULT_MAX_DIFF_FILES}; env "
                         "VCO_CODEGRAPH_HEAD_CATCHUP_MAX_FILES)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = ap.parse_args(argv)

    root = Path(args.project_root)
    state_dir = Path(args.state_dir) if args.state_dir else root / ".claude" / "state"
    # Precedence: --max-files > the env knob > the 2,000 default. The hook
    # passes no flag, so the env knob is the real operator control.
    max_files = args.max_files
    if max_files is None:
        raw = os.environ.get("VCO_CODEGRAPH_HEAD_CATCHUP_MAX_FILES", "").strip()
        max_files = int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_MAX_DIFF_FILES
    elif max_files <= 0:
        max_files = DEFAULT_MAX_DIFF_FILES
    log = RunLog(root / ".claude" / "logs" / LOG_NAME)
    try:
        catchup(
            str(root), args.analyzer, sys.executable, state_dir, log,
            max_files=max_files, timeout=args.timeout,
        )
    except Exception as exc:  # noqa: BLE001 — detached child: log, never raise
        log(f"ERROR unexpected: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())