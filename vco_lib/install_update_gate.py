# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""install.py-side update-gate lockfile choreography (P2c-a, v0.2.75).

Extracted from ``install.py main()`` — the V52-AI initial lockfile write +
atexit delete, and the A-6 per-phase deadline refreshes — so the flow logic
has ONE owner and main() keeps thin call sites
(``gate.begin()`` / ``gate.refresh(phase)``).

Primitives (write/read/delete/stale-cleanup) stay in
:mod:`vco_lib.update_gate`; this module owns only the *when/whether* policy
for an install.py run:

* ``begin()`` — ``--update`` runs only: write the lockfile
  (``phase="install_py"``) and register the atexit delete. The lockfile's
  15-minute ``expected_completion_by`` is the second line of defense if
  atexit never fires (SIGKILL) — the launcher's boot-time stale cleanup
  removes it. Fresh installs never write one (no binary swap ahead → no
  fork-bomb window).

* ``refresh(phase)`` — A-6 (v0.2.73): ``update_gate.write_lockfile``
  recomputes ``expected_completion_by`` from *now*, so re-calling it at
  each long phase boundary keeps the fixed deadline from silently expiring
  mid-update on weak hardware / cold cache (venv rebuild, multi-GB Ollama
  pulls, Weaviate seed, cargo build can EACH approach the window). A
  stale-because-slow lockfile would let an MCP respawn against a mid-swap
  binary — the exact V52-AI fork-bomb the gate exists to prevent. Mirrors
  the Rust ``update_gate.rs::advance_phase`` contract.

  P3a rider (v0.2.75): during ``--update``, a lockfile ABSENT at refresh
  time is **re-created** rather than no-op'd. We are provably mid-update
  (the phase string names where), so an absent lockfile means something
  deleted it out from under the run — a launcher boot's stale cleanup
  racing a slower-than-deadline phase is the canonical case — and
  continuing unprotected re-exposes the fork-bomb for the remainder of the
  run. Fresh installs keep the historical no-op: they never wrote a
  lockfile, and a foreign one (a concurrent launcher-driven update) is not
  ours to touch.

Every path is best-effort + soft-fail: a lockfile write failure must never
abort the install (the atexit delete + boot-time stale cleanup remain the
backstops).
"""

from __future__ import annotations

import atexit
from typing import Callable

from vco_lib import update_gate

#: Per-phase deadline window. Matches update_gate.DEFAULT_UPDATE_DURATION_MIN;
#: each begin()/refresh() re-arms a full window from *now*.
DEFAULT_PHASE_DURATION_MIN = 15


class InstallUpdateGate:
    """Owns the update-in-progress lockfile for one ``install.py`` run.

    Constructed with the run ``mode`` (``"update"`` / ``"install"``) so the
    call sites in main() stay unconditional — the policy (update-only)
    lives here, not at every call site.
    """

    def __init__(
        self,
        mode: str,
        *,
        expected_duration_min: int = DEFAULT_PHASE_DURATION_MIN,
        log: Callable[[str], None] = print,
    ) -> None:
        self.is_update = mode == "update"
        self._expected_duration_min = expected_duration_min
        self._log = log
        self._atexit_registered = False

    # ------------------------------------------------------------------

    def _register_atexit_delete(self) -> None:
        """Register the end-of-process lockfile delete exactly once.

        atexit covers every exit path (clean return, sys.exit, raised
        exception, signal-turned-exception). Guarded so a P3a re-create
        after ``begin()`` doesn't stack duplicate handlers (a double
        delete would be harmless — delete_lockfile is idempotent — but
        one registration is the honest shape).
        """
        if self._atexit_registered:
            return
        atexit.register(update_gate.delete_lockfile)
        self._atexit_registered = True

    def begin(self) -> None:
        """V52-AI: write the initial lockfile + arm the atexit delete.

        ``--update`` only; a fresh install is a silent no-op. The launcher
        path may also have written a lockfile via Rust's
        ``UpdateInProgressGuard``; a second write just extends the deadline
        — same atomic semantics either way.
        """
        if not self.is_update:
            return
        try:
            update_gate.write_lockfile(
                phase="install_py",
                expected_duration_min=self._expected_duration_min,
            )
            self._register_atexit_delete()
        except Exception as e:  # noqa: BLE001 — soft-fail
            self._log(f"[update_gate] failed to write lockfile (soft-fail): {e}")

    def refresh(self, phase: update_gate.Phase) -> None:
        """A-6: re-extend the deadline at a major phase transition.

        Fresh installs: no-op (nothing of ours to extend; a foreign
        lockfile from a concurrent launcher-driven update is left alone).

        ``--update`` runs: recompute the deadline from *now*. P3a rider —
        if the lockfile has gone ABSENT mid-update, RE-CREATE it at this
        phase instead of no-op'ing (see module docstring for why the
        historical no-op was wrong here).
        """
        if not self.is_update:
            return
        try:
            recreate = update_gate.read_lockfile() is None
            update_gate.write_lockfile(
                phase=phase,
                expected_duration_min=self._expected_duration_min,
            )
            if recreate:
                self._register_atexit_delete()
                self._log(
                    f"[update_gate] lockfile absent mid-update — re-created "
                    f"(phase={phase}); a stale-cleanup likely raced a slow phase"
                )
        except Exception as e:  # noqa: BLE001 — soft-fail
            self._log(f"[update_gate] deadline refresh soft-failed ({phase}): {e}")
