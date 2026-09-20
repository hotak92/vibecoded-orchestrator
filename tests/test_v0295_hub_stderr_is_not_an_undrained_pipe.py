# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The Step 22 harness must never hand the hub an undrained pipe.

`vct-hub` logs every diagnostic to stderr for the whole life of the
process (vct-hub/src/main.rs, "Diagnostics" section) and its real callers
redirect that to a file or to /dev/null. A test harness that passes
``subprocess.PIPE`` and then never reads it substitutes a FIXED-SIZE
kernel buffer for that sink — 4 KiB on Windows, 16 KiB on macOS, 64 KiB
on Linux. Once the buffer fills the hub blocks inside the write, holding
Rust's process-wide stderr lock, and every request handler that logs
blocks behind it. The observable symptom is a request that is accepted
and then never answered: the client times out in ``getresponse()``,
never at connect, and Windows sees it first because its buffer is the
smallest of the three.

That is not a hypothetical shape — it is the shape of the
``windows-latest`` Step 22 reds on PR #375, where the same test passed in
33 ms on main and four non-Windows cells passed in the same run.

These tests pin the ROUTING decision behaviourally: they drive the real
``start_hub`` with ``subprocess.Popen`` intercepted, so no hub is spawned
and no machine state is touched, and assert on what the harness asked the
OS for. A regression to ``subprocess.PIPE`` fails them.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from tests.common.sandbox import SandboxLayout
from tests.integration.step22_multi_project import fixture as step22_fixture


def _layout(tmp_path: Path) -> SandboxLayout:
    state_dir = tmp_path / ".vct-step22-unittest"
    state_dir.mkdir(parents=True, exist_ok=True)
    return SandboxLayout(
        run_id="unittest",
        state_dir=state_dir,
        collection_prefix="STEP22_unittest_",
        keychain_module_prefix="step22-unittest-",
    )


class _CapturedPopen:
    """Stands in for ``subprocess.Popen`` and records the io kwargs."""

    def __init__(self) -> None:
        self.kwargs: dict = {}

    def __call__(self, argv, **kwargs):  # noqa: ANN001 - mirrors Popen
        self.kwargs = kwargs
        raise _StopStartHub()


class _StopStartHub(Exception):
    """Ends ``start_hub`` right after the spawn decision is made."""


def _drive_start_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[_CapturedPopen, SandboxLayout]:
    layout = _layout(tmp_path)
    fake_binary = tmp_path / "vct-hub-stub"
    fake_binary.write_text("not a real binary", encoding="utf-8")

    captured = _CapturedPopen()
    monkeypatch.setattr(step22_fixture.subprocess, "Popen", captured)

    with pytest.raises(_StopStartHub):
        step22_fixture.start_hub(layout, fake_binary)
    return captured, layout


def test_hub_stdio_is_never_an_undrained_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE ACT being pinned: neither stream may be ``subprocess.PIPE``.

    A pipe is only safe when something drains it. ``HubProc.stop()`` does
    not, and no test in the module reads either stream, so the harness
    must not ask for one.
    """
    captured, _layout_ = _drive_start_hub(tmp_path, monkeypatch)

    assert captured.kwargs, "start_hub never reached the spawn"
    for stream in ("stdout", "stderr"):
        assert captured.kwargs[stream] is not subprocess.PIPE, (
            f"start_hub handed the hub an undrained {stream} PIPE; the hub "
            "logs for its whole life and will block once the kernel buffer "
            "fills (4 KiB on Windows), wedging every handler that logs"
        )


def test_hub_stdio_goes_to_real_files_under_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replacement must be a FILE, not merely 'not a pipe'.

    DEVNULL would also dodge the deadlock but would throw away the hub's
    own account of a failure — which is exactly what a red CI cell needs.
    So assert on real, writable files inside the sandbox state dir.
    """
    captured, layout = _drive_start_hub(tmp_path, monkeypatch)

    for stream, expected in (
        ("stdout", layout.hub_stdout_log()),
        ("stderr", layout.hub_stderr_log()),
    ):
        handle = captured.kwargs[stream]
        assert isinstance(handle, io.IOBase), (
            f"{stream} must be a file object, got {handle!r}"
        )
        opened_path = getattr(handle, "name", None)
        assert opened_path is not None and Path(opened_path) == expected, (
            f"{stream} must land at {expected}, got {opened_path!r}"
        )
        assert expected.exists(), f"{stream} log file was not created"

    # The sandbox owns them, so teardown removes them with everything else.
    assert layout.hub_stderr_log().parent == layout.state_dir


def test_the_parent_keeps_no_open_handle_on_the_log_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LEAVE-ALONE half: the child owns the descriptors, the parent does
    not. A parent handle left open is a fd leak per hub start and, on
    Windows, a handle the sandbox teardown has to fight over."""
    captured, _layout_ = _drive_start_hub(tmp_path, monkeypatch)

    for stream in ("stdout", "stderr"):
        handle = captured.kwargs[stream]
        assert isinstance(handle, io.IOBase), (
            f"{stream} must be a file object to have a handle to close, "
            f"got {handle!r}"
        )
        assert handle.closed, (
            f"the parent's {stream} handle must be closed after the spawn"
        )
