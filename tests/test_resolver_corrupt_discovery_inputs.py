# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""F-8 — resolver triplet must handle CORRUPT hub-discovery inputs identically.

Three resolver clients discover the launcher hub from the SAME inputs
(``$VCT_HUB_PORT`` / ``$VCT_HUB_TOKEN`` env, ``<state>/hub.port`` /
``hub.token`` files):

  * Python — ``vco_lib/project_config.py::_discover_hub``
  * Bash   — ``templates/scripts/vct_project_config.sh`` (``hub_port`` / ``hub_token``)
  * PowerShell — ``templates/scripts/vct_project_config.ps1`` (``Get-HubPort`` / ``Get-HubToken``)

Before F-8, the three DIVERGED on corrupt inputs (finding
``.claude/context/reviews/v0273-fable-review/findings/F-findings.md`` §F-8):
a garbage ``hub.port`` was used verbatim by bash (→ malformed URL),
validated-and-defaulted by ps1, and RAISED by python; a non-integer
``$VCT_HUB_PORT`` threw an UNCAUGHT terminating error in ps1 that took the
Windows hook host down. This file pins the ONE unified contract:

  CONTRACT
  --------
  * corrupt PORT (non-integer ``$VCT_HUB_PORT``, non-integer ``hub.port``
    file, or unreadable ``hub.port``) → warn to stderr + fall through to
    the default port (7700). NEVER crash. NEVER emit a garbage/partial
    resolution.
  * unreadable / absent ``hub.token`` → warn (unreadable case) + treat as
    "no token" → the hub is genuinely unreachable (the token has no sane
    default). NEVER crash with a raw traceback.

The SAME synthetic corrupt-input fixtures are driven through all three
implementations so a future divergence in any one of them fails here.
The ps1 subset gates on a PowerShell runtime being on PATH (mirrors
``tests/test_resolver_schema_version_warning.py``). All fixtures are
synthetic — no project-identifying strings, no real secrets.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
BASH_CLIENT = REPO_ROOT / "templates" / "scripts" / "vct_project_config.sh"
PS1_CLIENT = REPO_ROOT / "templates" / "scripts" / "vct_project_config.ps1"

# Prefer pwsh (cross-platform PowerShell Core); fall back to
# powershell.exe (Windows in-box PS 5.1) when only that is available.
_PWSH = shutil.which("pwsh") or shutil.which("powershell")

# Synthetic corrupt-input values shared by all three implementations.
GARBAGE_PORT = "77O0"          # letter O, not zero — partial/typo write.
NON_NUMERIC_PORT = "not-a-port"
VALID_PORT = "7700"

# The port a corrupt resolution MUST fall through to.
DEFAULT_PORT = "7700"

# stderr warning-kind tokens the three implementations agree on. Each
# implementation prints its own line, but every line contains the kind so
# a test can assert the SAME kind fired for the SAME corrupt input.
KIND_PORT_INVALID = "hub_port_invalid"
KIND_PORT_UNREADABLE = "hub_port_unreadable"
KIND_TOKEN_UNREADABLE = "hub_token_unreadable"


def _fresh_state_dir() -> str:
    return tempfile.mkdtemp(prefix="vct-corrupt-test-")


def _bash_library() -> str:
    """Return a path to the bash client with its final ``main "$@"``
    invocation stripped, so we can source the discovery helpers without
    triggering the CLI entry-point (mirrors the established pattern in
    ``templates/scripts/test_vct_project_config_rate_limit.sh``)."""
    src = BASH_CLIENT.read_text(encoding="utf-8")
    lines = [ln for ln in src.splitlines() if ln.strip() != 'main "$@"']
    fd, path = tempfile.mkstemp(prefix="vct-cfg-lib-", suffix=".sh")
    os.close(fd)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _ps1_library() -> str:
    """Return a path to the ps1 client with (a) the top
    ``[CmdletBinding()] param(...)`` block removed — so dot-sourcing does
    not demand the Mandatory ``-Project`` argument — and (b) the bottom
    ``# ── Main`` block removed — so no hub-dependent resolve flow runs.
    The discovery functions (``Get-HubPort`` / ``Get-HubToken``) and their
    ``Emit-Warning`` dependency are all defined between those boundaries."""
    src = PS1_CLIENT.read_text(encoding="utf-8")
    # Strip the CmdletBinding/param block: from `[CmdletBinding` up to and
    # including the closing `)` on its own line (the block ends at line 44
    # `)` at base). Match conservatively by finding the param-close.
    cb_idx = src.find("[CmdletBinding")
    if cb_idx != -1:
        # The param() block closes with a line that is exactly ")".
        after = src.find("\n)\n", cb_idx)
        if after != -1:
            src = src[:cb_idx] + src[after + len("\n)\n"):]
    marker = "# ── Main"
    idx = src.find(marker)
    body = src[:idx] if idx != -1 else src
    fd, path = tempfile.mkstemp(prefix="vct-cfg-lib-", suffix=".ps1")
    os.close(fd)
    # Preserve the UTF-8 BOM the original ships with (BOM discipline).
    Path(path).write_text("﻿" + body, encoding="utf-8")
    return path


# ─── Bash driver ────────────────────────────────────────────────────────
#
# We source the library portion and call `hub_port` / `hub_token` directly
# rather than the full resolve flow — that isolates the discovery contract
# from any hub round-trip and keeps the test hermetic (no listener needed).


def _run_bash_fn(
    fn: str,
    *,
    env_extra: dict[str, str],
    state_dir: str,
) -> subprocess.CompletedProcess[str]:
    """Source the bash client library and invoke ``fn`` (``hub_port`` or
    ``hub_token``). Returns the CompletedProcess (stdout = fn's stdout,
    stderr = any warning). Never raises for a non-zero rc — the caller
    asserts on it."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/tmp",
        "VCT_STATE_DIR": state_dir,
        # Route the rate-limit warn-state file into the tempdir so we don't
        # pollute $HOME/.vct/cache, and force every warning through (no
        # 5-min suppression) so the test observes it deterministically.
        "VCO_HOOK_DEBUG": "1",
    }
    env.update(env_extra)
    lib = _bash_library()
    try:
        snippet = f'source "{lib}"; {fn}'
        return subprocess.run(
            ["bash", "-c", snippet],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        try:
            os.unlink(lib)
        except OSError:
            pass


class BashCorruptDiscoveryTest(unittest.TestCase):
    def test_bash_garbage_port_file_warns_and_defaults(self) -> None:
        state = _fresh_state_dir()
        (Path(state) / "hub.port").write_text(GARBAGE_PORT, encoding="utf-8")
        r = _run_bash_fn("hub_port", env_extra={}, state_dir=state)
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertEqual(r.stdout.strip(), DEFAULT_PORT,
                         f"garbage port must default; stdout={r.stdout!r}")
        self.assertIn(KIND_PORT_INVALID, r.stderr, r.stderr)

    def test_bash_non_integer_env_port_warns_and_defaults(self) -> None:
        state = _fresh_state_dir()
        r = _run_bash_fn(
            "hub_port",
            env_extra={"VCT_HUB_PORT": NON_NUMERIC_PORT},
            state_dir=state,
        )
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertEqual(r.stdout.strip(), DEFAULT_PORT, r.stdout)
        self.assertIn(KIND_PORT_INVALID, r.stderr, r.stderr)

    def test_bash_valid_env_port_no_warning(self) -> None:
        state = _fresh_state_dir()
        r = _run_bash_fn(
            "hub_port",
            env_extra={"VCT_HUB_PORT": VALID_PORT},
            state_dir=state,
        )
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertEqual(r.stdout.strip(), VALID_PORT, r.stdout)
        self.assertNotIn(KIND_PORT_INVALID, r.stderr, r.stderr)

    def test_bash_unreadable_port_warns_and_defaults(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("running as root: perm bits don't gate reads")
        state = _fresh_state_dir()
        pf = Path(state) / "hub.port"
        pf.write_text(VALID_PORT, encoding="utf-8")
        pf.chmod(0o000)
        try:
            r = _run_bash_fn("hub_port", env_extra={}, state_dir=state)
        finally:
            pf.chmod(0o600)
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertEqual(r.stdout.strip(), DEFAULT_PORT, r.stdout)
        self.assertIn(KIND_PORT_UNREADABLE, r.stderr, r.stderr)

    def test_bash_unreadable_token_warns_and_empty(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("running as root: perm bits don't gate reads")
        state = _fresh_state_dir()
        tf = Path(state) / "hub.token"
        tf.write_text("some-token", encoding="utf-8")
        tf.chmod(0o000)
        try:
            r = _run_bash_fn("hub_token", env_extra={}, state_dir=state)
        finally:
            tf.chmod(0o600)
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertEqual(r.stdout.strip(), "",
                         f"unreadable token must be empty; stdout={r.stdout!r}")
        self.assertIn(KIND_TOKEN_UNREADABLE, r.stderr, r.stderr)


# ─── PowerShell driver ──────────────────────────────────────────────────


@unittest.skipIf(
    _PWSH is None,
    "no PowerShell runtime on PATH (pwsh / powershell.exe). PS1 "
    "corrupt-discovery tests skipped — install PowerShell Core 7+ to "
    "exercise this matrix on non-Windows hosts.",
)
class PowerShellCorruptDiscoveryTest(unittest.TestCase):
    def _run_ps1_fn(
        self,
        fn: str,
        *,
        env_extra: dict[str, str],
        state_dir: str,
    ) -> subprocess.CompletedProcess[str]:
        """Dot-source the ps1 client and invoke ``fn`` (``Get-HubPort`` /
        ``Get-HubToken``). Returns the CompletedProcess."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/tmp",
            "VCT_STATE_DIR": state_dir,
            "VCO_HOOK_DEBUG": "1",
        }
        env.update(env_extra)
        # This class is @skipIf(_PWSH is None), so _PWSH is a str here.
        assert _PWSH is not None
        # Dot-source the library portion (Main block stripped) then call
        # the discovery function directly — no hub round-trip needed.
        lib = _ps1_library()
        try:
            cmd = f'. "{lib}"; {fn}'
            return subprocess.run(
                [_PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            try:
                os.unlink(lib)
            except OSError:
                pass

    def test_ps1_garbage_port_file_warns_and_defaults(self) -> None:
        state = _fresh_state_dir()
        (Path(state) / "hub.port").write_text(GARBAGE_PORT, encoding="utf-8")
        r = self._run_ps1_fn("Get-HubPort", env_extra={}, state_dir=state)
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertIn(DEFAULT_PORT, r.stdout, f"stdout={r.stdout!r}")
        self.assertIn(KIND_PORT_INVALID, r.stderr, r.stderr)

    def test_ps1_non_integer_env_port_warns_no_throw(self) -> None:
        # The pre-F-8 bug: `[int]$Env:VCT_HUB_PORT` on a non-numeric value
        # threw a TERMINATING error. Post-fix it must warn + default and
        # exit 0 (no unhandled exception).
        state = _fresh_state_dir()
        r = self._run_ps1_fn(
            "Get-HubPort",
            env_extra={"VCT_HUB_PORT": NON_NUMERIC_PORT},
            state_dir=state,
        )
        self.assertEqual(r.returncode, 0,
                         f"ps1 must not throw on non-int env port; "
                         f"stderr={r.stderr!r}")
        self.assertIn(DEFAULT_PORT, r.stdout, r.stdout)
        self.assertIn(KIND_PORT_INVALID, r.stderr, r.stderr)

    def test_ps1_valid_env_port_no_warning(self) -> None:
        state = _fresh_state_dir()
        r = self._run_ps1_fn(
            "Get-HubPort",
            env_extra={"VCT_HUB_PORT": VALID_PORT},
            state_dir=state,
        )
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        self.assertIn(VALID_PORT, r.stdout, r.stdout)
        self.assertNotIn(KIND_PORT_INVALID, r.stderr, r.stderr)

    def test_ps1_unreadable_token_warns_no_throw(self) -> None:
        if os.name != "nt" and os.geteuid() == 0:
            self.skipTest("running as root: perm bits don't gate reads")
        state = _fresh_state_dir()
        tf = Path(state) / "hub.token"
        tf.write_text("some-token", encoding="utf-8")
        # chmod 000 only bites on POSIX; on Windows this is best-effort.
        try:
            tf.chmod(0o000)
        except OSError:
            self.skipTest("cannot chmod on this platform")
        try:
            r = self._run_ps1_fn("Get-HubToken", env_extra={}, state_dir=state)
        finally:
            tf.chmod(0o600)
        self.assertEqual(r.returncode, 0,
                         f"ps1 must not throw on unreadable token; "
                         f"stderr={r.stderr!r}")
        self.assertIn(KIND_TOKEN_UNREADABLE, r.stderr, r.stderr)


# ─── Python driver ──────────────────────────────────────────────────────


class PythonCorruptDiscoveryTest(unittest.TestCase):
    """Drives ``vco_lib/project_config.py::_discover_hub`` directly against
    the SAME corrupt fixtures, asserting warn+default on port and
    warn+HubUnreachable on token (never a raw OSError/ValueError)."""

    def setUp(self) -> None:
        from vco_lib import project_config

        self.pc = project_config
        self.state = _fresh_state_dir()
        self.pc._test_clear_cache()
        # Point vct_root_dir at our tempdir so file reads hit the fixtures.
        self._patch = mock.patch.object(
            self.pc, "vct_root_dir", return_value=Path(self.state)
        )
        self._patch.start()
        # Clear any ambient hub env so the file branch is exercised.
        self._env_patch = mock.patch.dict(
            os.environ, {}, clear=False
        )
        self._env_patch.start()
        os.environ.pop("VCT_HUB_PORT", None)
        os.environ.pop("VCT_HUB_TOKEN", None)

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._patch.stop()
        self.pc._test_clear_cache()

    def _capture_discover(self) -> tuple[int | None, Exception | None, str]:
        """Run _discover_hub, capturing stderr.

        Returns ``(port, exc, stderr)``: on success ``port`` is the resolved
        port and ``exc`` is None; on failure ``port`` is None and ``exc`` is
        the raised exception. Exactly one of ``port`` / ``exc`` is set."""
        captured: list[str] = []
        with mock.patch.object(
            self.pc.sys.stderr, "write",
            side_effect=lambda s: captured.append(s),
        ):
            try:
                port, _token = self.pc._discover_hub()
                return port, None, "".join(captured)
            except Exception as exc:  # noqa: BLE001 — test asserts the type
                return None, exc, "".join(captured)

    def test_py_garbage_port_file_warns_and_defaults(self) -> None:
        (Path(self.state) / "hub.port").write_text(GARBAGE_PORT, encoding="utf-8")
        (Path(self.state) / "hub.token").write_text("t", encoding="utf-8")
        port, exc, stderr = self._capture_discover()
        self.assertIsNone(exc, f"garbage port must NOT raise; got {exc!r}")
        self.assertEqual(port, int(DEFAULT_PORT), port)
        self.assertIn(KIND_PORT_INVALID, stderr, stderr)

    def test_py_non_integer_env_port_warns_and_defaults(self) -> None:
        os.environ["VCT_HUB_PORT"] = NON_NUMERIC_PORT
        (Path(self.state) / "hub.token").write_text("t", encoding="utf-8")
        port, exc, stderr = self._capture_discover()
        self.assertIsNone(exc, f"non-int env port must NOT raise; got {exc!r}")
        self.assertEqual(port, int(DEFAULT_PORT), port)
        self.assertIn(KIND_PORT_INVALID, stderr, stderr)

    def test_py_valid_env_port_no_warning(self) -> None:
        os.environ["VCT_HUB_PORT"] = VALID_PORT
        (Path(self.state) / "hub.token").write_text("t", encoding="utf-8")
        port, exc, stderr = self._capture_discover()
        self.assertIsNone(exc, exc)
        self.assertEqual(port, int(VALID_PORT), port)
        self.assertNotIn(KIND_PORT_INVALID, stderr, stderr)

    def test_py_unreadable_port_warns_and_defaults(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("running as root: perm bits don't gate reads")
        pf = Path(self.state) / "hub.port"
        pf.write_text(VALID_PORT, encoding="utf-8")
        (Path(self.state) / "hub.token").write_text("t", encoding="utf-8")
        pf.chmod(0o000)
        try:
            port, exc, stderr = self._capture_discover()
        finally:
            pf.chmod(0o600)
        self.assertIsNone(exc, f"unreadable port must NOT raise; got {exc!r}")
        self.assertEqual(port, int(DEFAULT_PORT), port)
        self.assertIn(KIND_PORT_UNREADABLE, stderr, stderr)

    def test_py_unreadable_token_warns_and_hubunreachable(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("running as root: perm bits don't gate reads")
        (Path(self.state) / "hub.port").write_text(VALID_PORT, encoding="utf-8")
        tf = Path(self.state) / "hub.token"
        tf.write_text("some-token", encoding="utf-8")
        tf.chmod(0o000)
        try:
            port, exc, stderr = self._capture_discover()
        finally:
            tf.chmod(0o600)
        # Token has no default → HubUnreachable (the caller's env-fallback
        # path), NOT a raw OSError, and the warning kind matches sh/ps1.
        self.assertIsNone(port, f"expected raise, got port={port!r}")
        self.assertIsInstance(exc, self.pc.HubUnreachable, repr(exc))
        self.assertIn(KIND_TOKEN_UNREADABLE, stderr, stderr)


if __name__ == "__main__":
    unittest.main()
