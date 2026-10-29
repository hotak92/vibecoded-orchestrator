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
   ``--only-files-from`` run — the drain's own batched shape;
4. behind by MORE than ``--max-files`` → the same efficient path the analyzer
   already has: one ``--incremental --since-commit <recorded>`` walk (which
   also prunes the entities of deleted/renamed files);
5. the recorded commit is MISSING or is NOT an ancestor (a rewritten history)
   → fall back to the analyzer's plain ``--incremental`` walk and record
   nothing — a baseline that cannot be proven is never advanced;
6. the analysis succeeded → record HEAD through the hub route
   ``POST /api/v1/projects/{id}/codegraph/analyzed`` (launcher.db keeps its
   single writer: this module never opens it). A failed run records NOTHING,
   so the next eligible catch-up retries the same diff.

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
import os
import re
import sys
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
LOG_NAME = "codegraph_head_catchup.log"

#: The drain's extension gate, byte for byte
#: (``templates/hooks/stop-codegraph-drain.sh :: CODE_RE``). MUST MATCH.
CODE_RE = re.compile(
    r"\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$"
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
    drain batch over the same checkout."""
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


def diff_paths(root: str, old: str, new: str) -> list[str]:
    """``git diff --name-only -z <old>..<new>`` → repo-relative paths.

    NUL-separated (``-z``) so a path holding a space or a quote survives
    verbatim. An empty list on any git failure — the caller then records
    nothing rather than claiming coverage it cannot see.
    """
    rc, out, _err = run_git(Path(root), ["diff", "--name-only", "-z", f"{old}..{new}"])
    if rc != 0:
        return []
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


# ── Recording the new baseline (hub = the single launcher.db writer) ─────


def record_analyzed_commit(project_id: str, commit: str) -> tuple[bool, str]:
    """POST the new baseline to the hub
    (``/projects/{id}/codegraph/analyzed`` → advance-only stamp)."""
    from vco_lib import project_config

    try:
        resp = project_config.hub_post_json(
            f"projects/{project_id}/codegraph/analyzed", {"commit": commit}
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
    diff_paths: Callable[[str, str, str], list[str]] = diff_paths
    run_analyzer: Callable[..., AnalyzerResult] = run_analyzer
    record: Callable[[str, str], tuple[bool, str]] = record_analyzed_commit
    canonical_root: Callable[[str], Optional[str]] = canonical_repo_root


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


def _run_incremental(
    project_root: str,
    prefix: str,
    head: str,
    since: Optional[str],
    *,
    analyzer: str,
    python: str,
    state_dir: Path,
    log: Callable[[str], None],
    timeout: int,
    deps: Deps,
    label: str,
) -> AnalyzerResult:
    """One ``--incremental`` walk under the per-root lock AND the project's
    single-flight walk claim (so it never twins a launcher/driver walk of the
    same project — the 2026-10-14 field defect)."""
    from vco_lib.codegraph_walk import WalkAlreadyRunning, walk_claim

    key = path_key(project_root)
    lock = _acquire_lock(state_dir, key)
    if lock is None:
        log(f"{label}: busy — another analyzer run holds the root lock; nothing recorded")
        return AnalyzerResult(False, "busy")
    try:
        try:
            with walk_claim(prefix):
                argv = build_incremental_argv(
                    project_root, prefix, since, index_dot_claude_flag(project_root)
                )
                log(
                    f"{label}: walk {project_root} prefix={prefix} "
                    f"{since or 'HEAD~1'}..{head[:12]}"
                )
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
    (one of ``up_to_date``, ``no_git_head``, ``no_config``,
    ``worktree_session``, ``no_recorded``, ``not_ancestor``, ``busy``,
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

    head = deps.git_head(root)
    if not head:
        log(f"skip project={root}: no git HEAD (not a repo, no commits, or git missing)")
        return "no_git_head"
    if recorded and recorded == head:
        return "up_to_date"

    canonical = deps.canonical_root(root)
    if canonical and Path(canonical) != Path(root).resolve():
        log(
            f"skip project={root}: linked worktree of {canonical} — the main "
            "checkout's own session owns this catch-up (FIX-A')"
        )
        return "worktree_session"

    if not recorded:
        log(
            f"no recorded commit for project={project_id} prefix={prefix} — "
            "falling back to the analyzer's incremental walk; nothing recorded "
            "(an unprovable baseline is never advanced)"
        )
        res = _run_incremental(
            root, prefix, head, None,
            analyzer=analyzer, python=python, state_dir=state_dir, log=log,
            timeout=timeout, deps=deps, label="no_recorded",
        )
        return "busy" if res.reason == "busy" else (
            "no_recorded" if res.ok else "analyzer_failed"
        )

    if not deps.is_ancestor(root, recorded, head):
        log(
            f"recorded commit {recorded[:12]} is NOT an ancestor of HEAD "
            f"{head[:12]} (rewritten history or unknown object) — falling back "
            "to the analyzer's incremental walk; nothing recorded"
        )
        res = _run_incremental(
            root, prefix, head, None,
            analyzer=analyzer, python=python, state_dir=state_dir, log=log,
            timeout=timeout, deps=deps, label="not_ancestor",
        )
        return "busy" if res.reason == "busy" else (
            "not_ancestor" if res.ok else "analyzer_failed"
        )

    files, vanished = indexable_files(root, deps.diff_paths(root, recorded, head))

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
        return "recorded" if ok else "record_failed"

    if vanished > 0 or len(files) > max_files:
        label = "deleted_files" if vanished > 0 else "huge_diff"
        res = _run_incremental(
            root, prefix, head, recorded,
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
            return "analyzer_failed"
        ok, why = deps.record(project_id, head)
        log(
            f"{'recorded' if ok else 'NOT RECORDED'} project={project_id} "
            f"{recorded[:12]}..{head[:12]} via incremental walk ({label}, "
            f"{vanished} deleted code file(s)): files={res.files_analyzed} "
            f"deleted_files={res.deleted_files} removed_entities={res.deleted_entities} "
            f"ms={res.duration_ms}: {why}",
        )
        return "recorded" if ok else "record_failed"

    key = path_key(root)
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
            return "analyzer_failed"
        ok, why = deps.record(project_id, head)
        log(
            f"{'recorded' if ok else 'NOT RECORDED'} project={project_id} "
            f"{recorded[:12]}..{head[:12]}: files={res.files_analyzed} "
            f"entities={res.entities_indexed} ms={res.duration_ms}: {why}"
        )
        return "recorded" if ok else "record_failed"
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