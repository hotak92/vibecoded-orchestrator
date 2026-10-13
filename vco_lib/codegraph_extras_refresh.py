# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Automatic re-index of a project's EXTRA code-graph paths (v0.2.100 W5R-04).

An extra path (launcher → project → Codegraph → "Extra codegraph paths") feeds
another checkout into the project's OWN code-graph collections. Before this
module the only thing that ever re-indexed one was the panel's manual Sync
button, so a path whose repo moved on kept serving months-old functions and
callers to every session of the project.

The trigger lives in the Stop hook (``templates/hooks/stop-codegraph-drain.
{sh,ps1}``), which spawns this module DETACHED — a Stop hook must never block
the turn. For each ENABLED extra path of the calling project it:

1. reads the path's repo ``HEAD`` and compares it with ``last_indexed_commit``
   (served by the hub in ``/config``) — the ONE staleness rule,
   :func:`extra_path_is_stale`, shared with the panel's "stale" badge;
2. when behind, and not attempted within ``--min-interval`` seconds (per-path
   throttle, stamped BEFORE the run so a failing path is not retried every
   turn), runs the analyzer with the SAME argv the Sync button uses
   (:func:`build_extra_sync_argv`), into the project's own collection prefix,
   bounded by ``--timeout``;
3. on success records the new commit through the hub route
   ``POST /api/v1/projects/{id}/codegraph/extras/indexed`` — launcher.db keeps
   its single writer; this module never opens it. On failure (non-zero exit,
   timeout, insert errors, a failed deleted-file prune) it logs and records
   NOTHING, so the path stays stale and is retried after the throttle window.
   The incremental run also removes the entities of files deleted or renamed
   away since ``last_indexed_commit`` (``vco_lib.codegraph_deleted_files``);
   the counts are in the ``done`` log line.

Every decision is one line in ``<project>/.claude/logs/codegraph_extras_refresh.log``
(size-capped). Exit status is always 0.

v0.2.102 S16-7 adds two more entries (same module, same CLI):
``--queued`` drains the per-extra edit queues
(``codegraph_drain_extra_<md5>_<sid>.txt``) the routing hooks fill when a
code file under an ENABLED extra path is written — a scoped
``--only-files-from`` analyzer run with the extra's own collection args, so
an extra-path edit is re-indexed as promptly as a primary-root one;
``--list-enabled-extra-paths`` prints the membership list the hooks cache
under ``.claude/state/codegraph_extra_paths_cache.txt`` (refreshed on a TTL
or when launcher.db changes — never a per-edit DB read).

Rust counterparts (MUST MATCH, both assert
``tests/fixtures/codegraph_extra_sync_argv.json``):
``vct_launcher_core::db::codegraph_extras::{extra_path_sync_args,
extra_path_is_stale}``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

#: Default per-path throttle between re-index ATTEMPTS (seconds).
DEFAULT_MIN_INTERVAL_SECONDS = 3600
#: Hard ceiling on one analyzer run — mirrors the launcher's
#: ``ANALYZE_TIMEOUT_SECS`` for the Sync button (30 min).
DEFAULT_TIMEOUT_SECONDS = 1800
#: Log file cap; when exceeded the older half is dropped.
LOG_MAX_BYTES = 256 * 1024
LOG_NAME = "codegraph_extras_refresh.log"


# ── The shared rules (MUST MATCH the Rust fns named in the module doc) ─────


def build_extra_sync_argv(
    path: str,
    prefix: str,
    incremental: bool,
    since_commit: Optional[str],
    primary_root: Optional[str] = None,
) -> list[str]:
    """Analyzer argv (after the script) for syncing ONE extra path.

    ``primary_root`` (the project's own folder) adds ``--primary-root``, so the
    analyzer refuses an extra path that overlaps the project (v0.2.102 W6-CGD,
    the rule in :mod:`vco_lib.codegraph_extra_overlap`).

    must match vct_launcher_core::db::codegraph_extras::extra_path_sync_args
    """
    # --as-extra-path: the analyzer's repo_path IS an extra path here, so its
    # deletes must never judge a row it cannot prove is this path's (v0.2.100).
    argv = [path, "--project", prefix, "--json-progress", "--as-extra-path"]
    if primary_root:
        argv += ["--primary-root", primary_root]
    if incremental:
        argv.append("--incremental")
        sha = (since_commit or "").strip()
        if sha:
            argv += ["--since-commit", sha]
    return argv


def nested_extra_refusal(path: str, project_root: str) -> str:
    """The refusal sentence when ``path`` overlaps the project's own folder, else "".

    A row registered before registration refused overlaps, or nested later by
    a project move, is never walked as an extra (its files are the project's,
    or excluded from it on purpose); the caller logs the sentence and skips.
    """
    from vco_lib.codegraph_extra_overlap import classify

    v = classify(path, project_root)
    return v.message if v.refused else ""


def extra_path_is_stale(head: Optional[str], last_indexed_commit: Optional[str]) -> bool:
    """True iff HEAD is known and differs from the last indexed commit.

    must match vct_launcher_core::db::codegraph_extras::extra_path_is_stale
    """
    h = (head or "").strip()
    if not h:
        return False
    last = last_indexed_commit.strip() if last_indexed_commit is not None else None
    return last != h


# ── Small helpers ────────────────────────────────────────────────────────


def path_key(path: str) -> str:
    """md5 of the path string — the same key the drain hook's per-root lock
    (``codegraph_drain_root_<md5>.lock``) uses, so a re-index of an extra
    path serialises with a drain batch for the same checkout."""
    return hashlib.md5(path.encode("utf-8")).hexdigest()  # noqa: S324 — a key, not security


def git_head(path: str) -> Optional[str]:
    """``git -C <path> rev-parse HEAD`` or None (non-git / git missing)."""
    try:
        out = subprocess.run(
            ["git", "-C", path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    sha = out.stdout.strip()
    return sha or None


class RunLog:
    """Append-only, size-capped, one-line-per-decision log."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __call__(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > LOG_MAX_BYTES:
                data = self.path.read_bytes()
                self.path.write_bytes(data[len(data) // 2:])
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass  # logging is best-effort; never fail the refresh over it


@dataclass
class AnalyzerResult:
    ok: bool
    reason: str
    files_analyzed: int = 0
    entities_indexed: int = 0
    duration_ms: int = 0
    #: v0.2.100: files deleted/renamed away since the last indexed commit, and
    #: the entities removed for them (the analyzer's deleted-file prune).
    deleted_files: int = 0
    deleted_entities: int = 0


def run_analyzer(
    python: str, analyzer: str, argv: Sequence[str], timeout: float,
) -> AnalyzerResult:
    """Run the analyzer bounded by ``timeout``; parse its ``{"final": true}``
    report line. Failure = non-zero exit, timeout, spawn error, or a final
    report with ``insert_errors`` or ``deleted_file_prune_failures`` (a partial
    index must not be recorded as current — it would never be retried; the
    analyzer also exits 5 for the latter under ``--since-commit``)."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [python, analyzer, *argv],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return AnalyzerResult(False, f"timed out after {int(timeout)}s (killed)")
    except OSError as exc:
        return AnalyzerResult(False, f"spawn failed: {exc}")
    duration_ms = int((time.monotonic() - started) * 1000)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().replace("\n", " | ")[-400:]
        return AnalyzerResult(False, f"exit {proc.returncode}: {tail or 'no stderr'}",
                              duration_ms=duration_ms)
    final: dict = {}
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("final") is True:
            final = obj
    if int(final.get("insert_errors", 0) or 0) > 0:
        return AnalyzerResult(False, f"{final['insert_errors']} insert error(s); partial index not recorded",
                              duration_ms=duration_ms)
    if int(final.get("deleted_file_prune_failures", 0) or 0) > 0:
        return AnalyzerResult(
            False,
            f"{final['deleted_file_prune_failures']} deleted-file prune failure(s); "
            "entities of deleted files remain, commit not recorded",
            duration_ms=duration_ms,
        )
    entities = sum(int(final.get(k, 0) or 0) for k in ("modules", "classes", "functions", "apis"))
    return AnalyzerResult(
        True, "ok", int(final.get("files_analyzed", 0) or 0), entities, duration_ms,
        deleted_files=int(final.get("deleted_files", 0) or 0),
        deleted_entities=int(final.get("deleted_file_entities", 0) or 0),
    )


def record_indexed(project_id: str, path: str, commit: str, res: AnalyzerResult) -> tuple[bool, str]:
    """POST the new commit to the hub (single launcher.db writer)."""
    from vco_lib import project_config

    try:
        resp = project_config.hub_post_json(
            f"projects/{project_id}/codegraph/extras/indexed",
            {
                "path": path,
                "commit": commit,
                "files_analyzed": res.files_analyzed,
                "entities_indexed": res.entities_indexed,
                "duration_ms": res.duration_ms,
            },
        )
    except project_config.ResolverError as exc:
        return False, f"hub unreachable: {exc}"
    if resp.status_code == 200:
        return True, "recorded"
    return False, f"hub answered {resp.status_code}: {resp.text[:200]}"


# ── v0.2.102 S16-7: the membership list + the per-edit queued drain ────────


def list_enabled_extra_paths(project_root: str, *, resolve: Optional[Callable] = None) -> list[str]:
    """Absolute paths of the project's ENABLED extra code-graph paths.

    This is the membership list the edit-routing hooks
    (``templates/hooks/_lib/route-touched-path.{sh,ps1}``) cache under
    ``.claude/state/codegraph_extra_paths_cache.txt`` — they refresh the cache
    through the ``--list-enabled-extra-paths`` CLI rather than resolving per
    edit. Raises whatever the resolver raises (hub down / project
    unregistered): the caller keeps its stale cache on a failure exit.
    """
    if resolve is None:
        from vco_lib import project_config

        resolve = project_config.resolve
    cfg = resolve(project_root)
    return [e.path for e in getattr(cfg, "code_graph_extra_paths", ()) if getattr(e, "enabled", False)]


def refresh_queued(
    project_root: str,
    analyzer: str,
    python: str,
    state_dir: Path,
    log: Callable[[str], None],
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    deps: Optional[Deps] = None,
) -> dict[str, str]:
    """Drain the per-extra edit queues (``codegraph_drain_extra_<md5>_<sid>.txt``).

    v0.2.102 S16-7: a Write/Edit/Bash-written code file under an ENABLED extra
    path is queued by the routing hooks for a prompt re-index; the Stop hook
    spawns this entry (``--queued``) DETACHED. Each queued path is analyzed
    with the extra's OWN collection args (:func:`build_extra_sync_argv`, the
    one argv builder) in scoped ``--only-files-from`` mode — never under the
    primary root's collection.

    Throttle intent kept: the commit-stamp path's per-path ``--min-interval``
    protects the analyzer from repeated FULL walks; a queued scoped drain is
    cheap, so NEITHER the 120 s primary-drain window NOR the per-path extras
    throttle applies here. The per-path ``codegraph_drain_root_<md5>.lock``
    DOES apply (it serialises every analyzer run over the same checkout);
    when held, the lines are put back on the queue for a later drain.

    The commit is deliberately NOT recorded: an ``--only-files-from`` walk
    does not run the deleted-file prune an incremental ``--since-commit``
    pass would, so ``last_indexed_commit`` stays owned by the commit-stamp
    path (:func:`refresh_extras`) and the panel's staleness badge. Returns
    ``{path: outcome}``. Never raises.
    """
    if deps is None:
        from vco_lib import project_config

        deps = Deps(resolve=project_config.resolve)
    outcomes: dict[str, str] = {}
    try:
        cfg = deps.resolve(project_root)
    except Exception as exc:  # noqa: BLE001 — hub down/unregistered: queues persist
        log(f"queued: skip project={project_root}: config unavailable ({exc})")
        return outcomes
    prefix = getattr(cfg, "code_graph_collection_prefix", "")
    extras = [e for e in getattr(cfg, "code_graph_extra_paths", ()) if getattr(e, "enabled", False)]
    if not prefix:
        log(f"queued: skip project={project_root}: no code-graph prefix")
        return outcomes

    for extra in extras:
        path = extra.path
        key = path_key(path)
        refusal = nested_extra_refusal(path, project_root)
        if refusal:  # queues stay put; the row is never walked as an extra
            outcomes[path] = "overlaps_project"
            log(f"queued: skip path={path}: {refusal}")
            continue
        try:
            queues = sorted(state_dir.glob(f"codegraph_drain_extra_{key}_*.txt"))
        except OSError:
            queues = []
        if not queues:
            continue
        # Consume every session's queue for this extra by atomic rename, so a
        # concurrent append starts a fresh queue (same contract as the
        # primary drain's consume step).
        asides: list[Path] = []
        lines: list[str] = []
        for q in queues:
            aside = q.with_name(f"{q.name}.draining.{os.getpid()}")
            try:
                q.rename(aside)
            except OSError:
                continue
            asides.append(aside)
            try:
                lines.extend(aside.read_text(encoding="utf-8").splitlines())
            except OSError:
                pass
        # Dedup (first-seen order) + keep only files provably under THIS extra
        # path; drop transient scratch the same way the primary drain does.
        prefix_slash = path.rstrip("/") + "/"
        seen: set[str] = set()
        files: list[str] = []
        for ln in lines:
            ln = ln.strip()
            if not ln or ln in seen:
                continue
            seen.add(ln)
            if not ln.startswith(prefix_slash):
                continue
            if "/.claude/state/" in ln:
                continue
            files.append(ln)
        if not files:
            outcomes[path] = "empty"
            for a in asides:
                try:
                    a.unlink()
                except OSError:
                    pass
            continue

        lock = state_dir / f"codegraph_drain_root_{key}.lock"
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            lock.mkdir()
        except FileExistsError:
            outcomes[path] = "busy"
            _restore_extra_queue(state_dir, key, files)
            log(f"queued: busy path={path}: another analyzer run holds the lock; queue restored")
            continue
        except OSError as exc:
            outcomes[path] = "lock_error"
            _restore_extra_queue(state_dir, key, files)
            log(f"queued: skip path={path}: cannot create lock ({exc}); queue restored")
            continue
        list_file = state_dir / f"codegraph_drain_extra-files-{key}.txt"
        try:
            try:
                (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")
                list_file.write_text("\n".join(files) + "\n", encoding="utf-8")
            except OSError:
                pass
            argv = build_extra_sync_argv(path, prefix, False, None, project_root) + [
                "--only-files-from", str(list_file),
            ]
            log(f"queued: drain path={path} prefix={prefix} files={len(files)}")
            res = deps.run_analyzer(python, analyzer, argv, timeout)
            if not res.ok:
                outcomes[path] = "analyzer_failed"
                _restore_extra_queue(state_dir, key, files)
                log(f"queued: FAILED path={path}: {res.reason}; queue restored, commit not recorded")
                continue
            outcomes[path] = "drained"
            log(
                f"queued: done path={path} files={res.files_analyzed} "
                f"entities={res.entities_indexed} ms={res.duration_ms} "
                "(commit not recorded: the commit-stamp refresh owns last_indexed_commit)"
            )
        finally:
            for a in asides:
                try:
                    a.unlink()
                except OSError:
                    pass
            try:
                list_file.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                (lock / "pid").unlink(missing_ok=True)
                lock.rmdir()
            except OSError:
                pass

    # ── GC (v0.2.102 fix-round L-A4) ───────────────────────────────────────
    # A queue written just before its extra was disabled/removed in the
    # launcher is never consumed by the loop above (which iterates CURRENT
    # enabled extras) and would sit under .claude/state/ forever, retriggering
    # the Stop-hook spawn on every turn. Remove those orphans and say so in
    # the log. Safety rails:
    #   * reached ONLY when the hub config resolved (an unreachable hub
    #     returns above, before any deletion);
    #   * a queue whose key belongs to a CURRENTLY-ENABLED extra is never
    #     touched (a `_restored` retry file included);
    #   * names that are not per-extra queues (the transient
    #     `codegraph_drain_extra-files-<key>.txt` analyzer input, anything
    #     unparsable) are left alone — except the pre-fix-round-2 legacy
    #     transient `codegraph_drain_extra_list_<key>.txt`, which is GC'd so
    #     a killed mid-drain leftover stops triggering no-op Stop spawns.
    enabled_keys = {path_key(e.path) for e in extras}
    try:
        strays = sorted(state_dir.glob("codegraph_drain_extra_*.txt"))
    except OSError:
        strays = []
    prefix_len = len("codegraph_drain_extra_")
    for stray in strays:
        name = stray.name
        if name.startswith("codegraph_drain_extra_list_"):
            try:
                stray.unlink()
                log(f"queued: gc stale analyzer input {name} (legacy transient name)")
            except OSError:
                pass
            continue
        key = name[prefix_len:prefix_len + 32]
        is_queue = (
            len(key) == 32
            and all(c in "0123456789abcdef" for c in key)
            and len(name) > prefix_len + 32
            and name[prefix_len + 32] == "_"
        )
        if not is_queue or key in enabled_keys:
            continue
        try:
            stray.unlink()
            log(f"queued: gc orphan queue={name} (extra removed or disabled)")
        except OSError:
            pass
    return outcomes


def _restore_extra_queue(state_dir: Path, key: str, files: list[str]) -> None:
    """Put undrained lines back on a per-extra queue (busy / analyzer failure).

    Uses the ``_restored`` session slot — the drain globs
    ``codegraph_drain_extra_<key>_*.txt``, so the next Stop picks them up.
    """
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / f"codegraph_drain_extra_{key}_restored.txt").open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write("\n".join(files) + "\n")
    except OSError:
        pass


# ── The driver ───────────────────────────────────────────────────────────


@dataclass
class Deps:
    """Injection seam for tests (defaults are the real implementations)."""

    resolve: Callable[[str], object]
    git_head: Callable[[str], Optional[str]] = git_head
    run_analyzer: Callable[..., AnalyzerResult] = run_analyzer
    record: Callable[[str, str, str, AnalyzerResult], tuple[bool, str]] = record_indexed
    now: Callable[[], float] = time.time


def refresh_extras(
    project_root: str,
    analyzer: str,
    python: str,
    state_dir: Path,
    log: Callable[[str], None],
    *,
    min_interval: int = DEFAULT_MIN_INTERVAL_SECONDS,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    deps: Optional[Deps] = None,
) -> dict[str, str]:
    """Check every enabled extra path of ``project_root``; re-index the stale
    ones. Returns ``{path: outcome}`` (for tests and the log). Never raises."""
    if deps is None:
        from vco_lib import project_config

        deps = Deps(resolve=project_config.resolve)
    outcomes: dict[str, str] = {}
    try:
        cfg = deps.resolve(project_root)
    except Exception as exc:  # noqa: BLE001 — hub down/unregistered: nothing to do
        log(f"skip project={project_root}: config unavailable ({exc})")
        return outcomes
    project_id = getattr(cfg, "project_id", "")
    prefix = getattr(cfg, "code_graph_collection_prefix", "")
    extras = [e for e in getattr(cfg, "code_graph_extra_paths", ()) if getattr(e, "enabled", False)]
    if not project_id or not prefix:
        log(f"skip project={project_root}: no project id / code-graph prefix")
        return outcomes

    for extra in extras:
        path = extra.path
        last = extra.last_indexed_commit
        refusal = nested_extra_refusal(path, project_root)
        if refusal:
            outcomes[path] = "overlaps_project"
            log(f"skip path={path}: {refusal}")
            continue
        head = deps.git_head(path)
        if not extra_path_is_stale(head, last):
            outcomes[path] = "up_to_date" if head else "no_git_head"
            continue
        assert head is not None  # stale ⇒ head known
        key = path_key(path)
        stamp = state_dir / f"codegraph_extra_refresh_{key}.ts"
        try:
            last_try = float(stamp.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            last_try = 0.0
        now = deps.now()
        if last_try and now - last_try < min_interval:
            outcomes[path] = "throttled"
            continue
        lock = state_dir / f"codegraph_drain_root_{key}.lock"
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            lock.mkdir()
        except FileExistsError:
            outcomes[path] = "busy"
            log(f"busy path={path}: another analyzer run holds the lock")
            continue
        except OSError as exc:
            outcomes[path] = "lock_error"
            log(f"skip path={path}: cannot create lock ({exc})")
            continue
        try:
            try:
                (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")
                stamp.write_text(str(int(now)), encoding="utf-8")
            except OSError:
                pass
            argv = build_extra_sync_argv(path, prefix, bool(last), last, project_root)
            log(f"reindex path={path} prefix={prefix} {last or 'never'}..{head[:12]}")
            res = deps.run_analyzer(python, analyzer, argv, timeout)
            if not res.ok:
                outcomes[path] = "analyzer_failed"
                log(f"FAILED path={path}: {res.reason}; last_indexed_commit unchanged")
                continue
            ok, why = deps.record(project_id, path, head, res)
            outcomes[path] = "recorded" if ok else "record_failed"
            log(
                f"{'done' if ok else 'NOT RECORDED'} path={path} commit={head[:12]} "
                f"files={res.files_analyzed} entities={res.entities_indexed} "
                f"deleted_files={res.deleted_files} removed_entities={res.deleted_entities} "
                f"ms={res.duration_ms}: {why}"
            )
        finally:
            try:
                (lock / "pid").unlink(missing_ok=True)
                lock.rmdir()
            except OSError:
                pass
    return outcomes


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vco_lib.codegraph_extras_refresh")
    ap.add_argument("--project-root", required=True)
    # Optional only because --list-enabled-extra-paths never runs the analyzer;
    # the two run modes validate its presence below.
    ap.add_argument("--analyzer", default=None)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--min-interval", type=int, default=None)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    ap.add_argument(
        "--queued", action="store_true",
        help="v0.2.102 S16-7: drain the per-extra edit queues "
             "(codegraph_drain_extra_<md5>_<sid>.txt) instead of the commit-stamp check",
    )
    ap.add_argument(
        "--list-enabled-extra-paths", action="store_true",
        help="print the project's ENABLED extra paths (one per line) for the "
             "routing hooks' membership cache, then exit; exits 3 when the "
             "config cannot be resolved so the caller keeps its stale cache",
    )
    args = ap.parse_args(argv)

    root = Path(args.project_root)

    if args.list_enabled_extra_paths:
        try:
            for p in list_enabled_extra_paths(str(root)):
                print(p)
        except Exception as exc:  # noqa: BLE001 — caller keeps its stale cache
            print(f"list-enabled-extra-paths: config unavailable: {exc}", file=sys.stderr)
            return 3
        return 0

    if not args.analyzer:
        ap.error("--analyzer is required unless --list-enabled-extra-paths is given")

    state_dir = Path(args.state_dir) if args.state_dir else root / ".claude" / "state"
    log = RunLog(root / ".claude" / "logs" / LOG_NAME)
    if args.queued:
        try:
            refresh_queued(
                str(root), args.analyzer, sys.executable, state_dir, log,
                timeout=args.timeout,
            )
        except Exception as exc:  # noqa: BLE001 — detached child: log, never raise
            log(f"queued: ERROR unexpected: {exc!r}")
        return 0
    min_interval = args.min_interval
    if min_interval is None:
        raw = os.environ.get("VCO_CODEGRAPH_EXTRAS_MIN_INTERVAL_SECONDS", "")
        min_interval = int(raw) if raw.isdigit() else DEFAULT_MIN_INTERVAL_SECONDS
    try:
        refresh_extras(
            str(root), args.analyzer, sys.executable, state_dir, log,
            min_interval=min_interval, timeout=args.timeout,
        )
    except Exception as exc:  # noqa: BLE001 — detached child: log, never raise
        log(f"ERROR unexpected: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
