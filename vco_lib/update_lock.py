# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""install.py honours the launcher's machine-wide update claim (v0.2.100, W2R-06).

While the launcher updates this orchestrator it holds ``<vct_root>/update.lock``
(``launcher/src-tauri/src/commands/single_flight.rs::acquire_update_lock``).
Before this module only the launcher read that file, so ``python install.py
--update`` from a shell could run straight into the middle of a GUI update —
two processes interleaving ``git pull`` and ``install.py`` on one tree.

This is the ONE Python reader of that file. Its format MUST match the Rust
writer exactly (pinned by ``tests/test_v02100_update_lock_honoured.py`` against
the literal ``format!("{}\\n{}\\n", own_pid, timestamp)`` in single_flight.rs):

    <holder pid>\\n<unix seconds when claimed>\\n

Decision (:func:`decide`), in order:

* no file / empty / unparseable → PROCEED (install.py cannot tell, and a guard
  that cannot confirm a concurrent update must not block one — the launcher,
  the lock's owner, refuses malformed claims on its side);
* holder dead, or alive but started AFTER the claim was written (a reused pid —
  the success path leaves a claim naming a dead pid, W2R-06) → PROCEED (stale;
  the file is the launcher's to reap, never deleted here);
* holder is THIS process or one of its ancestors → PROCEED: this install.py is
  the launcher's own child (``update_run.rs`` step 8 spawns it). Recognition by
  ancestry needs no cooperation from the launcher — no env token to forward
  through the venv relaunch, nothing a stale environment could replay;
* holder alive, not an ancestor → REFUSE with a clear message (exit 1);
* liveness or ancestry cannot be determined → PROCEED with a warning (the
  pre-0.2.100 behaviour; never wedge an update on an unknowable).
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

LOCK_BASENAME = "update.lock"  # must match single_flight.rs UPDATE_LOCK_BASENAME

PROCEED = "proceed"
REFUSE = "refuse"

#: A process whose start time is within this many seconds AFTER the claim is
#: still treated as the claimant (clock granularity between the two readers).
_START_SLACK_S = 2.0


@dataclass(frozen=True)
class Claim:
    pid: int
    claimed_at: Optional[float]


@dataclass(frozen=True)
class Decision:
    verdict: str
    reason: str


def lock_path(vct_root: Optional[Path] = None) -> Path:
    if vct_root is None:
        from vco_lib.paths import vct_root_dir

        vct_root = vct_root_dir()
    return Path(vct_root) / LOCK_BASENAME


def parse_claim(text: str) -> Optional[Claim]:
    """Parse the Rust writer's two-line body; ``None`` when it names no pid."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    if not lines or not lines[0].isdigit():
        return None
    ts: Optional[float] = None
    if len(lines) > 1:
        try:
            ts = float(lines[1])
        except ValueError:
            ts = None
    return Claim(pid=int(lines[0]), claimed_at=ts)


# ── process facts (psutil when importable — a hard dependency of the venv —
#    with POSIX fallbacks; every probe is tri-state: None = could not tell) ──


def _psutil():
    try:
        import psutil  # type: ignore[import-not-found]

        return psutil
    except Exception:  # noqa: BLE001 — absent/broken → the fallbacks
        return None


def pid_alive(pid: int) -> Optional[bool]:
    ps = _psutil()
    if ps is not None:
        try:
            return bool(ps.pid_exists(pid))
        except Exception:  # noqa: BLE001
            return None
    if os.name == "nt":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def process_start_time(pid: int) -> Optional[float]:
    ps = _psutil()
    if ps is None:
        return None
    try:
        return float(ps.Process(pid).create_time())
    except Exception:  # noqa: BLE001
        return None


def _parent_of(pid: int) -> Optional[int]:
    ps = _psutil()
    if ps is not None:
        try:
            return int(ps.Process(pid).ppid())
        except Exception:  # noqa: BLE001
            return None
    stat = Path(f"/proc/{pid}/stat")
    if stat.is_file():
        try:
            # comm may hold spaces/parens: the fields after the LAST ')' are fixed.
            return int(stat.read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None
    if os.name == "nt":
        return None
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip()) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def ancestors(pid: Optional[int] = None, *, parent_of: Callable[[int], Optional[int]] = _parent_of
              ) -> Optional[list]:
    """``[pid, parent, grandparent, …]`` up to the root; ``None`` when the
    chain could not be read at all (bounded at 64 hops)."""
    cur = os.getpid() if pid is None else pid
    chain = [cur]
    for _ in range(64):
        parent = parent_of(cur)
        if parent is None:
            return chain if len(chain) > 1 else None
        if parent <= 0 or parent in chain:
            return chain
        chain.append(parent)
        cur = parent
    return chain


def decide(claim: Optional[Claim], *, alive: Callable[[int], Optional[bool]] = pid_alive,
           started: Callable[[int], Optional[float]] = process_start_time,
           lineage: Callable[[], Optional[list]] = ancestors) -> Decision:
    """Pure over its probes — see the module docstring for the rule."""
    if claim is None:
        return Decision(PROCEED, "no update claim")
    is_alive = alive(claim.pid)
    if is_alive is False:
        return Decision(PROCEED, f"stale claim (pid {claim.pid} is not running)")
    if is_alive is True and claim.claimed_at is not None:
        began = started(claim.pid)
        if began is not None and began > claim.claimed_at + _START_SLACK_S:
            return Decision(PROCEED, f"stale claim (pid {claim.pid} was reused by a newer process)")
    chain = lineage()
    if chain is not None and claim.pid in chain:
        return Decision(PROCEED, f"this install.py is the child of the update holder (pid {claim.pid})")
    if is_alive is None or chain is None:
        return Decision(PROCEED, f"could not tell whether pid {claim.pid} holds a live update — "
                                 "proceeding (warning)")
    return Decision(REFUSE, f"pid {claim.pid} is updating this orchestrator")


def check(vct_root: Optional[Path] = None, **probes) -> Decision:
    path = lock_path(vct_root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return Decision(PROCEED, "no update claim")
    return decide(parse_claim(text), **probes)


def refuse_if_foreign_update_running(vct_root: Optional[Path] = None, **probes) -> None:
    """install.py ``--update`` entry: exit 1 with a clear message while ANOTHER
    process (not this run's own parent launcher) holds the update claim."""
    decision = check(vct_root, **probes)
    if decision.verdict == REFUSE:
        path = lock_path(vct_root)
        print(
            f"ERROR: another update of this orchestrator is running ({decision.reason}; claim "
            f"at {path}). Refusing to start a second one — wait for it to finish (the "
            "launcher's update window shows its progress). If no update is running, that "
            "process is not an update: delete the claim file and re-run.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if "warning" in decision.reason:
        print(f"  [!] update.lock: {decision.reason}", file=sys.stderr)
