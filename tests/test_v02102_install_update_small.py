# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 lane W3-C — install/update small fixes.

Covers the two code fixes of the lane (NB-09, NB-10). The other items are
verified where they live:

* P299-C1 (preserved-by-design vs user-modified) is CLOSED by v0.2.100
  F-W2-08(a) — pinned by
  ``tests/test_v02100_bundle_leftovers.py::KnowledgeKeptAndRunLogTests``.
* NB-12's premise is false at source (agents install flat, depth 3, from
  ``templates/agents/free`` / ``module-gateway`` and packs alike — see the
  lane report); no change, no test.
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from vco_lib import install_resume as ir  # noqa: E402
from vco_lib import update_lock  # noqa: E402

# ── NB-09 — `_win_pid_alive` 259 ambiguity (+ batch-6 review C-1) ──────────

_SYNCHRONIZE = 0x00100000
_QUERY_LIMITED = 0x1000
_WAIT_FAILED = 0xFFFFFFFF


class _FakeKernel32:
    """The three-call kernel32 surface ``_win_pid_alive`` touches, scripted
    WITH ACCESS-RIGHT SEMANTICS (batch-6 review C-1): the fake grants exactly
    the access mask ``OpenProcess`` asked for, and ``WaitForSingleObject``
    returns ``WAIT_FAILED`` when that mask lacks ``SYNCHRONIZE`` — the
    documented behaviour of a query-limited-only handle on real Windows.

    ``exit_code`` is what ``GetExitCodeProcess`` writes into the out-param;
    ``wait_result`` is what ``WaitForSingleObject`` returns once the handle
    carries ``SYNCHRONIZE``.
    """

    def __init__(self, exit_code: int, wait_result: int = 0x0102) -> None:
        self.exit_code = exit_code
        self.wait_result = wait_result
        self.wait_calls: list[tuple[int, int]] = []
        self.granted_access: int | None = None
        self._handle = ctypes.c_void_p(0x1234)

    def OpenProcess(self, access, inherit, pid):  # noqa: ANN001
        self.granted_access = access
        return self._handle

    def CloseHandle(self, handle):  # noqa: ANN001
        return True

    def GetExitCodeProcess(self, handle, out):  # noqa: ANN001
        # ``out`` is ``ctypes.byref(DWORD())`` — a CArgObject whose underlying
        # object (``_obj``) is the out-param a real kernel32 call writes to.
        out._obj.value = self.exit_code
        return True

    def WaitForSingleObject(self, handle, millis):  # noqa: ANN001
        self.wait_calls.append((int(handle.value or 0), millis))
        if self.granted_access is None or not (self.granted_access & _SYNCHRONIZE):
            return _WAIT_FAILED  # no SYNCHRONIZE on the handle — cannot wait
        return self.wait_result


def _probe(monkeypatch, exit_code: int, wait_result: int):
    fake = _FakeKernel32(exit_code, wait_result)
    monkeypatch.setattr(update_lock, "_kernel32", lambda: fake)
    monkeypatch.setattr(update_lock, "_IS_WINDOWS", True)
    return update_lock._win_pid_alive(4242), fake


class TestWinPidAlive259Ambiguity:
    """A process that EXITED with code 259 must read as dead (was: alive)."""

    def test_exited_with_259_is_not_alive(self, monkeypatch):
        # 259 + WAIT_OBJECT_0: the handle is signalled → the process exited
        # (with exit code 259). Requires the SYNCHRONIZE access right — with
        # the pre-C-1 query-limited-only handle this fake answers WAIT_FAILED
        # and the disambiguation cannot happen (red against the v1 fix).
        alive, fake = _probe(monkeypatch, exit_code=259, wait_result=0x0000)
        assert alive is False
        assert fake.wait_calls and fake.wait_calls[0][1] == 0  # zero-timeout

    def test_live_reporting_259_is_alive(self, monkeypatch):
        # 259 + WAIT_TIMEOUT: still running — the documented live answer.
        alive, _ = _probe(monkeypatch, exit_code=259, wait_result=0x0102)
        assert alive is True

    def test_real_exit_code_short_circuits_the_wait(self, monkeypatch):
        # Any code ≠ 259 is a real exit code; no wait is needed.
        alive, fake = _probe(monkeypatch, exit_code=0, wait_result=0x0102)
        assert alive is False
        assert fake.wait_calls == []

    def test_opens_with_synchronize_access(self, monkeypatch):
        # C-1 pin: the wait needs SYNCHRONIZE on the handle; the open must
        # request SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION.
        _, fake = _probe(monkeypatch, exit_code=259, wait_result=0x0102)
        assert fake.granted_access is not None
        assert fake.granted_access & _SYNCHRONIZE
        assert fake.granted_access & _QUERY_LIMITED

    def test_wait_failed_falls_back_to_conservative_alive(self, monkeypatch):
        # WAIT_FAILED (or any non-OBJECT_0 result) cannot distinguish live
        # from exited-at-259 — the conservative answer is ALIVE (the base
        # behaviour), NOT None: decide() maps None to PROCEED-with-warning,
        # which would let a second update through while the holder runs.
        alive, _ = _probe(monkeypatch, exit_code=259, wait_result=_WAIT_FAILED)
        assert alive is True

    def test_unknowable_259_holder_refuses_the_update(self, monkeypatch):
        # The regression C-1 names, end to end: a live holder whose wait
        # cannot tell must keep the lock REFUSING, not PROCEED-with-warning.
        fake = _FakeKernel32(259, _WAIT_FAILED)
        monkeypatch.setattr(update_lock, "_kernel32", lambda: fake)
        monkeypatch.setattr(update_lock, "_IS_WINDOWS", True)
        decision = update_lock.decide(
            update_lock.Claim(pid=4242, claimed_at=None),
            alive=update_lock._win_pid_alive,
            started=lambda _pid: None,
            lineage=lambda: [111],  # holder is NOT our ancestor → no early PROCEED
        )
        assert decision.verdict == update_lock.REFUSE


# ── NB-10 — `_RESUME_STATE` import-time snapshot ───────────────────────────


class TestResumeStateLiveSession:
    """``_should_skip_step`` must read the LIVE session, not an import-time
    copy of its phases (a test or pre-main helper that replaces
    ``_RESUME_SESSION`` saw the empty import-time dict)."""

    def test_session_replaced_after_import_is_seen(self, monkeypatch):
        monkeypatch.setattr(
            install, "_RESUME_SESSION",
            ir.Session(enabled=True, phases={"1/10": "ok", "4/10": "skip"}))
        assert install._should_skip_step("1/10") is True
        assert install._should_skip_step("4/10") is True

    def test_uncompleted_step_still_runs(self, monkeypatch):
        monkeypatch.setattr(
            install, "_RESUME_SESSION",
            ir.Session(enabled=True, phases={"1/10": "ok"}))
        assert install._should_skip_step("5/10") is False

    def test_disabled_session_skips_nothing(self, monkeypatch):
        monkeypatch.setattr(
            install, "_RESUME_SESSION",
            ir.Session(enabled=False, phases={"1/10": "ok"}))
        assert install._should_skip_step("1/10") is False

    def test_the_import_time_alias_is_gone(self):
        # The stale-snapshot hazard WAS the module-level alias; NB-10 removes
        # it so no future reader can bind a copy of the phases.
        assert not hasattr(install, "_RESUME_STATE")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
