# SPDX-License-Identifier: AGPL-3.0-or-later
"""v0.2.49: tests for install.py's main()-entry single-instance lock.

Closes the 13-minute-deadlock pattern reported 2026-06-05 from a contributor's
machine where two concurrent `install.py --update` invocations
interleaved on shared state.

Test coverage:
  1. Lock acquires on clean state + writes pid/timestamp diagnostic.
  2. Lock times out + sys.exit(1) when held by another process.
  3. Soft-fail with WARNING when lock dir is unwritable.
  4. _install_advisory_lock yields immediately when main-entry held
     (cross-OS reentrancy short-circuit via _MAIN_ENTRY_LOCK_HELD flag).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


# Repo root is the parent of tests/. Inject so `import install` resolves
# to the install.py at the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def install_module(monkeypatch):
    """Import install.py with a fresh _MAIN_ENTRY_LOCK_HELD state.

    Tests share the same install module across the session (Python
    caches it in sys.modules); the flag must be reset between tests so
    one test's mutation doesn't leak into the next.
    """
    import install  # noqa: WPS433 — late-binding required
    # Reset the cross-test mutation surface BEFORE each test runs.
    install._MAIN_ENTRY_LOCK_HELD = False
    install._MAIN_ENTRY_LOCK_HANDLE = None
    yield install
    # Best-effort post-test cleanup so a leftover lock handle from a
    # failing test doesn't keep the OS lock alive for the next one.
    handle = getattr(install, "_MAIN_ENTRY_LOCK_HANDLE", None)
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass
    install._MAIN_ENTRY_LOCK_HELD = False
    install._MAIN_ENTRY_LOCK_HANDLE = None


def test_main_entry_lock_acquires_on_clean_state(
    tmp_path: Path, monkeypatch, install_module
):
    """Acquire succeeds on a clean tmpdir; writes pid + timestamp;
    sets _MAIN_ENTRY_LOCK_HELD = True.
    """
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))

    handle = install_module._install_singleton_lock_or_die(timeout_seconds=1.0)
    try:
        # Returned handle is a file object (None means soft-fail).
        assert handle is not None, "expected non-None handle on clean state"
        # The flag must be set so V44-I yields cleanly when it runs later.
        assert install_module._MAIN_ENTRY_LOCK_HELD is True, (
            "expected _MAIN_ENTRY_LOCK_HELD=True after acquire"
        )
        # Lock file exists at the canonical location.
        lock_path = tmp_path / "install.py.lock"
        assert lock_path.exists(), (
            f"expected lock file at {lock_path}; "
            f"got {list(tmp_path.iterdir())}"
        )
        # File body holds PID + timestamp (newline-separated).
        body = lock_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(body) >= 2, (
            f"expected at least 2 lines (pid + ts); got {body!r}"
        )
        assert body[0].strip() == str(os.getpid()), (
            f"first line should be holder PID; got {body[0]!r}"
        )
        # Timestamp is an integer-castable unix seconds value.
        ts = int(float(body[1].strip()))
        assert ts > 0 and ts <= int(time.time()) + 5, (
            f"expected recent unix timestamp; got {body[1]!r}"
        )
    finally:
        # Release the lock so the next test isn't blocked.
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def test_main_entry_lock_times_out_when_held_by_subprocess(
    tmp_path: Path, monkeypatch, install_module, capsys
):
    """When another process holds the lock, the in-process attempt
    times out and sys.exit(1).

    POSIX-only: msvcrt.locking on Windows has different cross-process
    semantics that would require a separate subprocess-coordination
    test harness. The flock-based test below covers the deadlock-
    closure path that actually matters for the reported bug
    (deadlocks observed on Linux + macOS).
    """
    if sys.platform == "win32":
        pytest.skip("subprocess-coordination test is POSIX-only (see docstring)")

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))

    # Spawn a helper that takes the OS lock + sleeps for several
    # seconds so the parent's short-timeout attempt is guaranteed to
    # observe contention.
    helper_src = textwrap.dedent(
        f"""
        import fcntl, os, sys, time
        lock_path = {str(tmp_path / "install.py.lock")!r}
        fp = open(lock_path, "a+b")
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Write the diagnostic payload so the parent reports our PID.
        fp.seek(0); fp.truncate(0)
        fp.write(f"{{os.getpid()}}\\n{{int(time.time())}}\\n".encode())
        fp.flush()
        # Signal ready by printing on stdout so parent can sync.
        print("ready", flush=True)
        # Hold the lock long enough for the parent to time out.
        time.sleep(5.0)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", helper_src],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for the helper to signal it has the lock.
        ready_line = proc.stdout.readline()
        assert ready_line.strip() == "ready", (
            f"helper failed to acquire lock; stdout={ready_line!r} "
            f"stderr={proc.stderr.read()!r}"
        )

        # Now attempt in this process with a short timeout — must
        # sys.exit(1) (the function is "or_die" by design).
        with pytest.raises(SystemExit) as excinfo:
            install_module._install_singleton_lock_or_die(
                timeout_seconds=0.5
            )
        assert excinfo.value.code == 1, (
            f"expected exit code 1; got {excinfo.value.code}"
        )
        # The error message MUST name the holder PID + the lock path.
        err = capsys.readouterr().err
        assert "already running" in err, (
            f"expected 'already running' in stderr; got: {err!r}"
        )
        assert str(proc.pid) in err, (
            f"expected holder PID {proc.pid} in stderr; got: {err!r}"
        )
        # The flag must NOT be set (we never acquired the lock).
        assert install_module._MAIN_ENTRY_LOCK_HELD is False, (
            "_MAIN_ENTRY_LOCK_HELD must remain False on failed acquire"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_main_entry_lock_soft_fails_when_dir_unwritable(
    tmp_path: Path, monkeypatch, install_module, capsys
):
    """When the lock parent dir can't be created (path-component is a
    regular file, not a dir), the function emits WARNING to stderr,
    returns None, and does NOT sys.exit.
    """
    # Place a file at the would-be parent path so mkdir(parents=True)
    # fails with NotADirectoryError when it tries to mkdir under the
    # file. Mirrors the V44-I soft-fail test pattern.
    blocker = tmp_path / "blocker"
    blocker.write_text("not-a-dir", encoding="utf-8")
    monkeypatch.setenv("VCT_STATE_DIR", str(blocker / "subdir"))

    # Must NOT raise SystemExit (soft-fail discipline).
    handle = install_module._install_singleton_lock_or_die(timeout_seconds=0.5)
    assert handle is None, (
        f"expected None on soft-fail; got {handle!r}"
    )
    # Flag must NOT be set — caller decides whether to proceed without
    # the lock; we explicitly do NOT claim "main-entry holds the lock"
    # because we don't.
    assert install_module._MAIN_ENTRY_LOCK_HELD is False, (
        "_MAIN_ENTRY_LOCK_HELD must remain False on soft-fail"
    )
    # WARNING must have been emitted to stderr.
    captured = capsys.readouterr()
    assert "WARNING" in captured.err, (
        f"expected WARNING in stderr; got: stderr={captured.err!r} "
        f"stdout={captured.out!r}"
    )


def test_advisory_lock_yields_immediately_when_main_entry_held(
    tmp_path: Path, monkeypatch, install_module
):
    """When _MAIN_ENTRY_LOCK_HELD is True, _install_advisory_lock
    yields without touching the OS lock.

    Verification: with the flag set, the context manager must NOT
    create a lock file (it short-circuits before the open() call) AND
    must NOT block waiting on the OS lock — it returns immediately
    with `None`. This is the cross-OS reentrancy fix: POSIX
    fcntl.flock IS reentrant for the same fd in the same process, but
    Windows msvcrt.locking is NOT — without this short-circuit, V44-I
    would deadlock on Windows when called from a main()-locked process.
    """
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))

    # Simulate "main() has already acquired the entry lock".
    install_module._MAIN_ENTRY_LOCK_HELD = True

    start = time.monotonic()
    # Use a very short timeout — if the short-circuit weren't there
    # and the OS lock were available, this would still succeed within
    # 0.1s. The stronger evidence is "no lock file created".
    with install_module._install_advisory_lock(timeout_seconds=0.1) as fp:
        # Yielded value is None per the short-circuit branch
        # (`yield None; return`).
        assert fp is None, (
            f"expected None when short-circuiting; got {fp!r}"
        )
    elapsed = time.monotonic() - start

    # Lock file MUST NOT exist — the short-circuit skips the open().
    lock_path = tmp_path / "install.py.lock"
    assert not lock_path.exists(), (
        f"short-circuit must not create lock file; found {lock_path}"
    )
    # Must yield quickly (no retry-with-backoff). Generous bound to
    # tolerate CI scheduler jitter; the real signal is "no lock file".
    assert elapsed < 1.0, (
        f"short-circuit should be near-instant; took {elapsed:.3f}s"
    )
