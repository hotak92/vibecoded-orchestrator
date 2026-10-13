# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Lane reservations — isolated subagent worktrees in ANOTHER repo (v0.2.102 W6-AA).

WHY
---
VCO's WorktreeCreate hook (``templates/hooks/worktree-guard.{sh,ps1}``)
creates every ``isolation: worktree`` agent's worktree, and by default it does
so in the coordinating project's own repo, detached at HEAD. A coordinator in
project P that dispatches an implementation lane whose work belongs in another
repo R (at a chosen base commit, on a chosen branch) could not get an ISOLATED
agent there, so it pre-created R's worktree by hand and dispatched a
NON-isolated agent, losing the harness's edit guard.

The harness accepts a WorktreeCreate path in another repository, new or
pre-existing, and then holds the agent's Edit/Write to it (verified live on
Claude Code 2.1.285 with scratch repos, 2026-10-13). What it
cannot do is tell the hook WHICH repo, base and branch a given launch wants:
the payload is only ``{session_id, transcript_path, cwd, prompt_id,
hook_event_name, name: "agent-<id>"}``. A reservation is how the coordinator
says it, one step ahead of the dispatch::

    python -m vco_lib.lane_reservation reserve --project P --repo R \\
        --base <commit> --branch <name> [--id <id>] [--existing-path <path>]
    # → prints the reservation id; put it in the lane's brief, then dispatch
    #   ONE isolated agent in that message.

On the next WorktreeCreate for P whose identifier starts with ``agent-``,
worktree-guard runs ``consume``: the OLDEST pending, unexpired reservation is
claimed (FIFO — isolated lanes are dispatched one per message, owner rule),
the allow-list is re-checked, and the worktree is created in R (or the
pre-existing one adopted). The guard binds the agent to the reservation in
its launch record (``_lib/worktree-records.*``); SubagentStart tells the
agent its reservation id, worktree, repo, branch and base; SubagentStop keeps
a worktree with work for review and reaps an unchanged one, against R.

ALLOW-LIST (owner, 2026-10-13)
------------------------------
R must be P itself or one of P's ENABLED extra code-graph paths (the launcher's
"Add code folder"), read from the hub: ``project_config.resolve(P)
.code_graph_extra_paths``. Normalized real paths are compared. When the hub
cannot answer, reservations are REFUSED, at reserve time and again at consume
time; plain isolation (no reservation) never asks the hub and is unaffected.

STATE LAYOUT — ``<P>/.claude/state/lane-reservations/``
------------------------------------------------------
* ``pending/res-<created_ns:020d>-<id>.json`` — waiting; the zero-padded
  creation time makes lexical order FIFO order. Written atomically
  (tempfile + ``os.replace``; the tempfile never matches ``res-*.json``).
* ``claimed/`` — taken by a launch. Claiming is ``os.rename`` from
  ``pending/``: atomic on one filesystem, so of two concurrent launches
  exactly one wins and the other moves on to the next file. The record then
  gains ``status`` (``consumed`` | ``failed``), ``agent_name``, ``path`` and
  ``reason``.
* ``expired/`` — a reservation past its TTL (default 15 min), moved aside by
  the next ``list``/``consume``; it is never consumed.
* ``cancelled/`` — ``cancel --id``.
* ``.lock`` — serializes ``reserve`` (id and branch uniqueness).

Files in ``claimed/``, ``expired/`` and ``cancelled/`` are pruned after 30
days. Launch records live with worktree-guard's, not here.

PATH CONVENTION (new worktrees)
-------------------------------
``<parent of R's toplevel>/<R basename>.vco-lanes/<branch-slug>-<id>`` —
absolute, symlink-resolved, and OUTSIDE R's working tree, so nothing R tracks
or ignores is involved and R's ``git status`` never shows it. Example: R =
``/src/app`` → ``/src/app.vco-lanes/feature-x-lr-1a2b3c4d``. Use
``--existing-path`` to adopt a worktree you created elsewhere.

CONSUME EXIT CODES (the guard's contract)
-----------------------------------------
* 0 — consumed; stdout is five lines: path, HEAD sha, R toplevel, record mode
  (``reservation`` | ``reservation_existing``), a one-line JSON object of
  extra record fields.
* 3 — nothing to consume (no pending unexpired reservation, or the identifier
  is not an ``agent-*`` subagent): the guard continues with its ordinary
  behaviour.
* 2 — a reservation was claimed but failed validation or creation: the guard
  aborts the create LOUDLY with the stderr reason; it never falls back to a
  worktree of P.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from vco_lib.atomic import LockTimeout, atomic_write_json, exclusive_file_lock
from vco_lib.git_meta import run_git

#: Default reservation lifetime. A coordinator reserves immediately before
#: dispatching, so a reservation older than this is a leftover of an aborted
#: dispatch and must not capture an unrelated later launch.
DEFAULT_TTL_SECONDS = 15 * 60

#: Claimed/expired/cancelled reservation files are kept this long for audit.
RETENTION_DAYS = 30

STATE_SUBDIR = Path(".claude") / "state" / "lane-reservations"
LANES_SUFFIX = ".vco-lanes"

EXIT_OK = 0
EXIT_FAILED = 2
EXIT_NONE = 3

#: Only subagent launches consume reservations. ``claude --worktree <name>``
#: also fires WorktreeCreate, with the user's own name; it must never take a
#: lane's reservation.
SUBAGENT_NAME_PREFIX = "agent-"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: Read-only git probes (rev-parse, worktree list, status) only.
#: ``git worktree add`` deliberately has NO timeout: on a very large repo the
#: checkout can take minutes, and killing a slow-but-working create would
#: leave a half-written worktree and an unlaunchable agent. The outer bound is
#: the harness's own hook timeout: the WorktreeCreate registration in
#: templates/settings.json.*.template carries no ``timeout``, so Claude Code's
#: command-hook default (600 s) applies. A VCO-side ceiling would add nothing
#: that bound does not already give, and could only ever cut real work short.
_GIT_READ_TIMEOUT_S = 15


class ReservationError(Exception):
    """A refused reserve/consume, with the user-facing reason."""


@dataclass(frozen=True)
class Created:
    """What ``consume`` hands back to the guard."""

    path: str
    head: str
    repo: str
    mode: str
    extra: dict


# ── paths ────────────────────────────────────────────────────────────────


def norm(p: str | Path) -> str:
    """Absolute, symlink-resolved, case-normalized (Windows) path string."""
    return os.path.normcase(os.path.realpath(os.path.abspath(str(p))))


def state_dir(project: Path) -> Path:
    return project / STATE_SUBDIR


def _sub(project: Path, name: str) -> Path:
    return state_dir(project) / name


def lane_worktree_path(repo_top: str, branch: str, res_id: str) -> str:
    """The path convention for a NEW reservation worktree (see module doc)."""
    top = Path(os.path.realpath(repo_top))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-.") or "lane"
    return str(top.parent / f"{top.name}{LANES_SUFFIX}" / f"{slug}-{res_id}")


# ── git ──────────────────────────────────────────────────────────────────


def _git(repo: str | Path, *args: str) -> tuple[Optional[int], str, str]:
    return run_git(Path(repo), args, timeout=_GIT_READ_TIMEOUT_S)


def repo_toplevel(repo: str | Path) -> str:
    if not Path(repo).is_dir():
        raise ReservationError(f"repo {repo} is not a directory")
    rc, out, err = _git(repo, "rev-parse", "--show-toplevel")
    if rc != 0 or not out:
        raise ReservationError(f"repo {repo} is not a git repository ({err or 'git rev-parse failed'})")
    return norm(out)


def resolve_commit(repo_top: str, ref: str) -> str:
    rc, out, err = _git(repo_top, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    if rc != 0 or not out:
        raise ReservationError(f"base {ref!r} does not resolve to a commit in {repo_top}")
    return out


def check_branch_name(repo_top: str, branch: str) -> None:
    rc, _out, _err = _git(repo_top, "check-ref-format", "--branch", branch)
    if rc != 0 or branch.startswith("-"):
        raise ReservationError(f"branch {branch!r} is not a valid branch name")


def branch_exists(repo_top: str, branch: str) -> bool:
    rc, _out, _err = _git(repo_top, "show-ref", "--verify", "-q", f"refs/heads/{branch}")
    return rc == 0


def list_worktrees(repo_top: str) -> list[dict]:
    """``git worktree list --porcelain`` as dicts {path, branch, head}."""
    rc, out, err = _git(repo_top, "worktree", "list", "--porcelain")
    if rc != 0:
        raise ReservationError(f"cannot list worktrees of {repo_top}: {err}")
    entries: list[dict] = []
    cur: dict = {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if cur:
                entries.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur["path"] = norm(val)
        elif key == "branch":
            cur["branch"] = val[len("refs/heads/"):] if val.startswith("refs/heads/") else val
        elif key == "HEAD":
            cur["head"] = val
    return entries


def branch_checked_out_at(repo_top: str, branch: str) -> Optional[str]:
    for wt in list_worktrees(repo_top):
        if wt.get("branch") == branch:
            return wt["path"]
    return None


def worktree_is_clean(path: str) -> bool:
    rc, out, _err = _git(path, "status", "--porcelain", "--untracked-files=all")
    return rc == 0 and not out


# ── allow-list ───────────────────────────────────────────────────────────


def allowed_roots(project: Path) -> list[str]:
    """P itself + P's ENABLED extra code-graph paths, normalized. Raises
    :class:`ReservationError` when the hub cannot answer (owner rule: hub down
    → reservations refused; plain isolation is unaffected)."""
    from vco_lib import project_config  # noqa: PLC0415 — import only when used

    try:
        cfg = project_config.resolve(project)
    except project_config.ResolverError as exc:
        raise ReservationError(
            "lane reservations are REFUSED: the launcher hub could not supply this "
            f"project's code folders ({type(exc).__name__}: {exc}). A reservation may "
            "only target the project itself or one of its enabled code folders "
            "(launcher: Identity tab -> Add code folder), and that list lives in the "
            "hub. Start the launcher (or `vct-hub --start-if-not-running`) and retry. "
            "Plain worktree isolation (no reservation) is unaffected."
        ) from exc
    roots = [norm(project)]
    roots += [norm(e.path) for e in cfg.code_graph_extra_paths if e.enabled]
    return roots


def check_allowed(project: Path, repo: str | Path) -> None:
    roots = allowed_roots(project)
    if norm(repo) not in roots:
        raise ReservationError(
            f"repo {norm(repo)} is not allowed for project {norm(project)}: a lane "
            "reservation may target only the project itself or one of its ENABLED "
            "code folders (launcher: Identity tab -> Add code folder). Allowed: "
            + ", ".join(roots)
        )


# ── reservation files ────────────────────────────────────────────────────


def _load(p: Path) -> Optional[dict]:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _pending_files(project: Path) -> list[Path]:
    d = _sub(project, "pending")
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("res-*.json") if p.is_file())


def _is_expired(rec: dict, now: float) -> bool:
    try:
        return float(rec.get("expires_at", 0)) <= now
    except (TypeError, ValueError):
        return True


def _move(src: Path, dest_dir: Path) -> Optional[Path]:
    """Atomic rename into ``dest_dir``; ``None`` when someone else moved it first."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    try:
        os.rename(src, dest)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return dest


def expire_stale(project: Path, now: Optional[float] = None) -> list[str]:
    """Move expired pending reservations to ``expired/``; returns their ids."""
    now = time.time() if now is None else now
    out = []
    for p in _pending_files(project):
        rec = _load(p)
        if rec is None or _is_expired(rec, now):
            if _move(p, _sub(project, "expired")) is not None:
                out.append(str((rec or {}).get("id", p.name)))
    return out


def prune_old(project: Path, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    cut = now - RETENTION_DAYS * 86400
    for sub in ("claimed", "expired", "cancelled"):
        d = _sub(project, sub)
        if not d.is_dir():
            continue
        for p in d.glob("res-*.json"):
            try:
                if p.is_file() and not p.is_symlink() and p.stat().st_mtime < cut:
                    p.unlink()
            except OSError:
                pass


def list_pending(project: Path) -> list[dict]:
    expire_stale(project)
    return [r for r in (_load(p) for p in _pending_files(project)) if r]


# ── reserve ──────────────────────────────────────────────────────────────


def _validate_existing(repo_top: str, path: str, branch: str, base_sha: str) -> dict:
    """An --existing-path must be a registered, non-main worktree of R, on
    ``branch``, clean, and contain ``base_sha``. Returns its worktree entry."""
    entries = list_worktrees(repo_top)
    want = norm(path)
    if entries and entries[0].get("path") == want:
        raise ReservationError(f"{want} is the main checkout of {repo_top}, not a separate worktree")
    hit = next((e for e in entries if e.get("path") == want), None)
    if hit is None:
        raise ReservationError(f"{want} is not a registered worktree of {repo_top}")
    if hit.get("branch") != branch:
        raise ReservationError(
            f"worktree {want} is on {hit.get('branch') or 'a detached HEAD'}, not on branch {branch!r}"
        )
    if not worktree_is_clean(want):
        raise ReservationError(f"worktree {want} has uncommitted or untracked changes")
    rc, _o, _e = _git(want, "merge-base", "--is-ancestor", base_sha, "HEAD")
    if rc != 0:
        raise ReservationError(f"worktree {want} does not contain base {base_sha}")
    return hit


def reserve(
    project: Path,
    repo: str | Path,
    base: str,
    branch: str,
    *,
    res_id: Optional[str] = None,
    existing_path: Optional[str] = None,
    ttl_s: int = DEFAULT_TTL_SECONDS,
    now: Optional[float] = None,
) -> dict:
    """Validate and write one pending reservation; returns the record."""
    project = Path(norm(project))
    if not project.is_dir():
        raise ReservationError(f"project {project} is not a directory")
    if ttl_s <= 0:
        raise ReservationError("--ttl must be a positive number of seconds")
    res_id = res_id or f"lr-{secrets.token_hex(4)}"
    if not _ID_RE.match(res_id):
        raise ReservationError(f"reservation id {res_id!r} must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
    if not Path(str(repo)).is_dir():
        raise ReservationError(f"repo {repo} is not a directory")
    check_allowed(project, repo)
    repo_top = repo_toplevel(repo)
    base_sha = resolve_commit(repo_top, base)
    check_branch_name(repo_top, branch)
    record: dict[str, Any] = {
        "id": res_id,
        "project": str(project),
        "repo": norm(repo),
        "repo_toplevel": repo_top,
        "base": base,
        "base_sha": base_sha,
        "branch": branch,
    }
    if existing_path:
        _validate_existing(repo_top, existing_path, branch, base_sha)
        record["existing_path"] = norm(existing_path)
    else:
        where = branch_checked_out_at(repo_top, branch)
        if where:
            raise ReservationError(f"branch {branch!r} is already checked out at {where}")
        if branch_exists(repo_top, branch):
            raise ReservationError(
                f"branch {branch!r} already exists in {repo_top}; pick a new branch name, "
                "or create the worktree yourself and pass --existing-path"
            )
        target = lane_worktree_path(repo_top, branch, res_id)
        if os.path.lexists(target):
            raise ReservationError(f"worktree path {target} already exists")
        record["path"] = target
    now = time.time() if now is None else now
    pend = _sub(project, "pending")
    pend.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(state_dir(project) / ".lock", timeout_s=10):
        expire_stale(project, now)
        for other in list_pending(project):
            if other.get("id") == res_id:
                raise ReservationError(f"a pending reservation with id {res_id!r} already exists")
            if other.get("repo_toplevel") == repo_top and other.get("branch") == branch:
                raise ReservationError(
                    f"pending reservation {other.get('id')!r} already targets branch {branch!r} in {repo_top}"
                )
        created_ns = time.time_ns()
        record.update(created_at=now, expires_at=now + ttl_s, ttl_s=ttl_s, status="pending")
        atomic_write_json(pend / f"res-{created_ns:020d}-{res_id}.json", record)
    prune_old(project, now)
    return record


def cancel(project: Path, res_id: str) -> bool:
    project = Path(norm(project))
    for p in _pending_files(project):
        rec = _load(p)
        if rec and rec.get("id") == res_id:
            return _move(p, _sub(project, "cancelled")) is not None
    return False


# ── consume ──────────────────────────────────────────────────────────────


def _claim_oldest(project: Path, now: float) -> Optional[tuple[Path, dict]]:
    for p in _pending_files(project):
        rec = _load(p)
        if rec is None or _is_expired(rec, now):
            _move(p, _sub(project, "expired"))
            continue
        got = _move(p, _sub(project, "claimed"))
        if got is not None:
            return got, rec
    return None


def _refire(project: Path, name: str) -> Optional[Created]:
    """The same agent firing WorktreeCreate again (retry): hand back the
    worktree its reservation already produced, when it is still registered."""
    d = _sub(project, "claimed")
    if not d.is_dir():
        return None
    for p in sorted(d.glob("res-*.json"), reverse=True):
        rec = _load(p)
        if not rec or rec.get("agent_name") != name or rec.get("status") != "consumed":
            continue
        try:
            listed = {e.get("path") for e in list_worktrees(rec["repo_toplevel"])}
        except (ReservationError, KeyError):
            return None
        if rec.get("path") in listed:
            return _created_from(rec, rec["path"], str(rec.get("head", "")))
        return None
    return None


def _created_from(rec: dict, path: str, head: str) -> Created:
    existing = bool(rec.get("existing_path"))
    return Created(
        path=path,
        head=head,
        repo=rec["repo_toplevel"],
        mode="reservation_existing" if existing else "reservation",
        extra={
            "reservation_id": rec["id"],
            "repo": rec["repo"],
            "branch": rec["branch"],
            "base_ref": rec["base"],
            "base_sha": rec["base_sha"],
            "branch_created": "no" if existing else "yes",
        },
    )


def _materialize(rec: dict) -> tuple[str, str]:
    """Create (or adopt) the reservation's worktree; returns (path, HEAD)."""
    repo_top = repo_toplevel(rec["repo"])
    if repo_top != rec["repo_toplevel"]:
        raise ReservationError(f"repo {rec['repo']} now resolves to {repo_top}, not {rec['repo_toplevel']}")
    base_sha = resolve_commit(repo_top, rec["base_sha"])
    if rec.get("existing_path"):
        path = rec["existing_path"]
        _validate_existing(repo_top, path, rec["branch"], base_sha)
    else:
        path = rec["path"]
        branch = rec["branch"]
        check_branch_name(repo_top, branch)
        where = branch_checked_out_at(repo_top, branch)
        if where:
            raise ReservationError(f"branch {branch!r} is already checked out at {where}")
        if branch_exists(repo_top, branch):
            raise ReservationError(f"branch {branch!r} already exists in {repo_top}")
        if os.path.lexists(path):
            raise ReservationError(f"worktree path {path} already exists")
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ReservationError(
                f"could not create the lane worktree path {path}: {exc}") from exc
        try:
            # No timeout, on purpose — see _GIT_READ_TIMEOUT_S above.
            proc = subprocess.run(  # noqa: S603 — argv is ours, never shell
                ["git", "-C", repo_top, "worktree", "add", "-b", branch, path, base_sha],
                capture_output=True, encoding="utf-8", errors="replace", check=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise ReservationError(f"git worktree add could not run: {exc}") from exc
        if proc.returncode != 0:
            raise ReservationError(f"git worktree add failed: {(proc.stderr or proc.stdout).strip()}")
    rc, head, err = _git(path, "rev-parse", "HEAD")
    if rc != 0 or not head:
        raise ReservationError(f"cannot read HEAD of {path}: {err}")
    return norm(path), head


def consume(project: Path, name: str, *, now: Optional[float] = None) -> Optional[Created]:
    """Claim the oldest pending reservation for ``name`` and produce its
    worktree. ``None`` = nothing to consume. Raises :class:`ReservationError`
    (after recording the failure on the claimed file) when a claimed
    reservation cannot be honoured."""
    project = Path(norm(project))
    if not name.startswith(SUBAGENT_NAME_PREFIX):
        return None
    again = _refire(project, name)
    if again is not None:
        return again
    now = time.time() if now is None else now
    got = _claim_oldest(project, now)
    if got is None:
        return None
    claimed, rec = got
    rec["agent_name"] = name
    rec["claimed_at"] = now
    try:
        check_allowed(project, rec["repo"])
        path, head = _materialize(rec)
    except (ReservationError, KeyError, OSError) as exc:
        if isinstance(exc, ReservationError):
            reason = str(exc)
        elif isinstance(exc, KeyError):
            reason = f"malformed reservation: missing {exc}"
        else:
            # Any other filesystem failure on the way to the worktree
            # (unwritable parent, permissions): same one-line refusal.
            reason = f"could not create the lane worktree path: {exc}"
        rec.update(status="failed", reason=reason)
        try:
            atomic_write_json(claimed, rec)
        except OSError as wexc:
            reason = f"{reason} (and recording the failure on {claimed} failed: {wexc})"
        raise ReservationError(f"lane reservation {rec.get('id')!r}: {reason}") from exc
    rec.update(status="consumed", path=path, head=head)
    atomic_write_json(claimed, rec)
    prune_old(project, now)
    return _created_from(rec, path, head)


# ── CLI ──────────────────────────────────────────────────────────────────


def _cmd_reserve(a: argparse.Namespace) -> int:
    rec = reserve(Path(a.project), a.repo, a.base, a.branch, res_id=a.id,
                  existing_path=a.existing_path, ttl_s=a.ttl)
    where = rec.get("existing_path") or rec.get("path")
    print(rec["id"])
    print(
        f"lane-reservation: {rec['id']} -> {where} (repo {rec['repo_toplevel']}, branch "
        f"{rec['branch']}, base {rec['base_sha'][:12]}), expires in {rec['ttl_s']} s. Put the id "
        "in the lane brief and dispatch ONE isolated agent in that message.",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_list(a: argparse.Namespace) -> int:
    recs = list_pending(Path(norm(a.project)))
    if a.json:
        print(json.dumps(recs, indent=2))
        return EXIT_OK
    now = time.time()
    for r in recs:
        left = int(float(r.get("expires_at", 0)) - now)
        where = r.get("existing_path") or r.get("path")
        print(f"{r.get('id')}\t{r.get('repo_toplevel')}\t{r.get('branch')}\t"
              f"{str(r.get('base_sha', ''))[:12]}\t{where}\t{left}s")
    return EXIT_OK


def _cmd_cancel(a: argparse.Namespace) -> int:
    if cancel(Path(a.project), a.id):
        print(f"lane-reservation: cancelled {a.id}", file=sys.stderr)
        return EXIT_OK
    print(f"lane-reservation: no pending reservation {a.id!r}", file=sys.stderr)
    return 1


def _cmd_consume(a: argparse.Namespace) -> int:
    got = consume(Path(a.project), a.name)
    if got is None:
        return EXIT_NONE
    sys.stdout.write("\n".join([got.path, got.head, got.repo, got.mode,
                                json.dumps(got.extra, separators=(",", ":"))]) + "\n")
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m vco_lib.lane_reservation",
                                 description="Reserve an isolated subagent worktree in a chosen repo/base/branch.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reserve", help="reserve the next isolated agent's worktree")
    r.add_argument("--project", required=True, help="the coordinating project (P)")
    r.add_argument("--repo", required=True, help="target repo (R): P itself or one of P's enabled code folders")
    r.add_argument("--base", required=True, help="base commit (any rev that resolves to a commit)")
    r.add_argument("--branch", required=True, help="branch to create (or that --existing-path is on)")
    r.add_argument("--id", help="reservation id (default: lr-<random>)")
    r.add_argument("--existing-path", help="adopt this existing worktree of R instead of creating one")
    r.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS, help="lifetime in seconds (default 900)")
    r.set_defaults(fn=_cmd_reserve)
    li = sub.add_parser("list", help="list pending reservations")
    li.add_argument("--project", required=True)
    li.add_argument("--json", action="store_true")
    li.set_defaults(fn=_cmd_list)
    c = sub.add_parser("cancel", help="cancel a pending reservation")
    c.add_argument("--project", required=True)
    c.add_argument("--id", required=True)
    c.set_defaults(fn=_cmd_cancel)
    co = sub.add_parser("consume", help="(worktree-guard) consume the oldest reservation")
    co.add_argument("--project", required=True)
    co.add_argument("--name", required=True, help="the WorktreeCreate identifier (agent-<id>)")
    co.set_defaults(fn=_cmd_consume)
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = _parser().parse_args(argv)
    try:
        return int(a.fn(a))
    except (ReservationError, LockTimeout, OSError) as exc:
        # OSError: an unwritable state dir and the like — a one-line refusal
        # like every other failure, never a traceback.
        print(f"lane-reservation: REFUSED - {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
