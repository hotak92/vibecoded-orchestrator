# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""vco_lib.git_bundle_backup — the ONE "save git history to a verified bundle" home.

v0.2.100 (F-W3-13, A>B>C rule A). Two destructive git operations save what
they could destroy into ``<vct_root>/backups/`` first and REFUSE to proceed
when the save fails:

* the launcher's ``ResetHard`` update kind (``update_run.rs``
  ``create_reset_backup``: the ``vco-backup/<stamp>`` refs, then this bundle);
* :func:`vco_lib.hard_cut.hard_cut` step 1 (``--all``).

Both used to carry their own ``git bundle create`` + ``git bundle verify``
sequence. The sequence now lives here, and the Rust side calls it through
``python -m vco_lib.git_bundle_backup create … --json``:

1. create the backups directory;
2. ``git bundle create <dir>/<name> <refs…>``;
3. ``git bundle verify <dir>/<name>`` — a bundle that does not verify is NOT
   a backup;
4. on any failure after the file may exist, remove the partial bundle (so a
   refusal leaves nothing half-made behind and a retry is not blocked) and
   report what happened.

Read-only on the repository (a bundle reads refs; it never moves one).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

__all__ = ["BundleResult", "create_verified_bundle", "main"]

#: Bound on each of the two git steps (a bundle of a large history takes
#: seconds; a git that does not finish in this time is stuck).
GIT_TIMEOUT_S = 600

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass
class BundleResult:
    """What :func:`create_verified_bundle` did."""

    ok: bool
    path: Optional[Path] = None
    error: str = ""
    #: One line per step, for the caller's log / result record.
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "path": str(self.path) if self.path else None,
                "error": self.error, "message": self.error, "steps": list(self.steps)}


def _tail(res: "subprocess.CompletedProcess[str]") -> str:
    lines = [ln.strip() for ln in (res.stderr or "").splitlines() if ln.strip()]
    return lines[-1] if lines else "no stderr"


def _discard(path: Path, result: BundleResult) -> None:
    try:
        path.unlink(missing_ok=True)
        result.steps.append(f"removed the partial bundle {path}")
    except OSError as exc:
        result.steps.append(f"could not remove the partial bundle {path} ({exc})")
        result.error += f" The partial bundle {path} could not be removed ({exc})."


def create_verified_bundle(
    repo: Path,
    backups_dir: Path,
    name: str,
    refs: Sequence[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    run: Optional[RunFn] = None,
    timeout_s: float = GIT_TIMEOUT_S,
) -> BundleResult:
    """Write ``<backups_dir>/<name>`` holding ``refs`` (any ``git bundle
    create`` rev-list arguments: ``--all``, branch names, ``^<excluded>``) and
    verify it. ``ok`` only when the bundle exists AND verifies; otherwise the
    partial file is removed and ``error`` says which step failed and why."""
    _run = run or subprocess.run
    result = BundleResult(ok=False)
    if "/" in name or "\\" in name or not name:
        result.error = f"invalid bundle name {name!r}"
        return result
    backups_dir = Path(backups_dir)
    try:
        backups_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.error = f"could not create the backup directory {backups_dir} ({exc})"
        return result
    path = backups_dir / name
    result.path = path
    sub_env = dict(env) if env is not None else None
    for verb, argv in (
        ("create", ["git", "bundle", "create", str(path), *refs]),
        ("verify", ["git", "bundle", "verify", str(path)]),
    ):
        try:
            res = _run(argv, cwd=str(repo), env=sub_env, timeout=timeout_s,
                       capture_output=True, text=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            result.error = f"`git bundle {verb}` could not run ({exc})."
            _discard(path, result)
            return result
        if res.returncode != 0:
            result.error = (f"`git bundle {verb}` exited {res.returncode}: {_tail(res)}"
                            + (" — the backup is not trustworthy." if verb == "verify" else "."))
            _discard(path, result)
            return result
        result.steps.append(f"git bundle {verb}: {path}")
    result.ok = True
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m vco_lib.git_bundle_backup",
        description="Save git history to a VERIFIED bundle (the backup before a reset).",
    )
    sub = ap.add_subparsers(dest="verb", required=True)
    c = sub.add_parser("create", help="git bundle create + verify; remove it on failure")
    c.add_argument("--repo", type=Path, required=True)
    c.add_argument("--dir", type=Path, required=True, help="the backups directory")
    c.add_argument("--name", required=True, help="the bundle file name")
    c.add_argument("--ref", action="append", default=[], required=True,
                   help="a `git bundle create` rev-list argument (repeatable)")
    c.add_argument("--json", action="store_true",
                   help="print one JSON object on stdout (ok, path, error, steps)")
    args = ap.parse_args(argv)
    result = create_verified_bundle(args.repo, args.dir, args.name, args.ref)
    if args.json:
        print(json.dumps(result.to_dict()))
    else:
        print(str(result.path) if result.ok else result.error,
              file=sys.stdout if result.ok else sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
