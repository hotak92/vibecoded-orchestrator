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
   timeout, insert errors) it logs and records NOTHING, so the path stays
   stale and is retried after the throttle window.

Every decision is one line in ``<project>/.claude/logs/codegraph_extras_refresh.log``
(size-capped). Exit status is always 0.

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
) -> list[str]:
    """Analyzer argv (after the script) for syncing ONE extra path.

    must match vct_launcher_core::db::codegraph_extras::extra_path_sync_args
    """
    argv = [path, "--project", prefix, "--json-progress"]
    if incremental:
        argv.append("--incremental")
        sha = (since_commit or "").strip()
        if sha:
            argv += ["--since-commit", sha]
    return argv


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


def run_analyzer(
    python: str, analyzer: str, argv: Sequence[str], timeout: float,
) -> AnalyzerResult:
    """Run the analyzer bounded by ``timeout``; parse its ``{"final": true}``
    report line. Failure = non-zero exit, timeout, spawn error, or a final
    report with ``insert_errors`` (a partial index must not be recorded as
    current — it would never be retried)."""
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
    entities = sum(int(final.get(k, 0) or 0) for k in ("modules", "classes", "functions", "apis"))
    return AnalyzerResult(True, "ok", int(final.get("files_analyzed", 0) or 0), entities, duration_ms)


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
            argv = build_extra_sync_argv(path, prefix, bool(last), last)
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
    ap.add_argument("--analyzer", required=True)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--min-interval", type=int, default=None)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = ap.parse_args(argv)

    root = Path(args.project_root)
    state_dir = Path(args.state_dir) if args.state_dir else root / ".claude" / "state"
    log = RunLog(root / ".claude" / "logs" / LOG_NAME)
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
