# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.49 Bug K — kg-sync venv picker rejects venvs missing weaviate_mcp.

Background
----------
Pre-fix (v0.2.37 Gap 6b through v0.2.48), the bash kg-sync wrapper's
``venv_has_kg_deps()`` validator only checked that ``import weaviate``
(the upstream client lib) succeeded. That gate was insufficient: a
candidate venv could have ``weaviate`` pip-installed (e.g. an unrelated
project's ``.venv`` where the user ran ``pip install weaviate-client``
on their own) WITHOUT having our editable ``weaviate_mcp`` package
installed.

When kg-sync then activated such a venv and invoked
``sync_knowledge_graph.py``, the script crashed at

    from weaviate_mcp.chunking import TokenCounter, Chunker

(``templates/scripts/sync_knowledge_graph.py`` line 52, pip-installed
editable by ``install.py`` A1 in v0.2.38). The user-visible error:

    ModuleNotFoundError: No module named 'weaviate_mcp'

Fix
---
``venv_has_kg_deps()`` now does a single subprocess that imports BOTH
modules. A candidate venv is accepted only when both succeed:

    "$py" -c "import weaviate, weaviate_mcp"

Also (same commit, Bug K cross-OS hardening):
  - Detect Windows-shape python at ``$v/Scripts/python.exe`` /
    ``$v/Scripts/python3.exe`` (in addition to POSIX ``$v/bin/python``
    / ``$v/bin/python3``) — kg-sync runs under Git Bash / WSL2 on
    Windows where forward-slash paths work but the venv layout uses
    ``Scripts/``.
  - Invoke the venv's python binary directly instead of
    ``source bin/activate`` (POSIX-only, doesn't exist in Windows
    venvs). Export ``VIRTUAL_ENV`` so child processes still see the
    venv as active.

Test scenarios
--------------
We exercise the kg-sync wrapper end-to-end via subprocess with a set
of fake venv layouts:

1. **No venv anywhere**: empty tmpdir, no ``VCT_INSTALL_ROOT``,
   ``SCRIPT_DIR/../../.venv`` doesn't exist. Wrapper should fall
   through to system ``python3`` (which will fail with its own
   ``ModuleNotFoundError`` — we don't care, we just want to verify
   the picker didn't pick a wrong venv).

2. **weaviate-only fake venv (regression pin)**: ``$VCT_INSTALL_ROOT
   /.venv/bin/python`` exists and reports ``import weaviate`` works
   but ``import weaviate, weaviate_mcp`` fails. Post-fix the picker
   MUST reject this venv. **Pre-fix this test failed** because the
   wrapper only checked ``import weaviate``.

3. **Both-importable fake venv**: ``$VCT_INSTALL_ROOT/.venv/bin
   /python`` reports both imports OK. Picker MUST accept it and
   invoke that python — we assert via a sentinel-file side effect
   that this exact python was the one invoked.

We implement the fake "venv python" as a small shell script (bash
on POSIX) that returns appropriate exit codes for the
``-c "import ..."`` probes and writes a sentinel file when invoked
on the real ``sync_knowledge_graph.py`` argument. The script then
exits 0 (we don't try to run the real sync — that needs Weaviate).

Limitations
-----------
- Windows-shape detection is NOT exercised by the live subprocess
  path (we'd need a Windows host with PowerShell). Instead we assert
  the script SOURCE contains the Windows branch, mirroring the
  source-level assertion style used in
  ``tests/test_v0237_install_bundle_gaps.py``.
- The "no venv anywhere" case writes a fake ``python3`` shim into a
  controlled PATH so we can detect when the picker falls through.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KG_SYNC = REPO_ROOT / "templates" / "scripts" / "kg-sync"


def _make_fake_venv(
    venv_root: Path,
    *,
    has_weaviate: bool,
    has_weaviate_mcp: bool,
    sentinel_path: Path,
) -> Path:
    """Create a fake venv at ``venv_root`` whose ``bin/python`` is a
    bash script that:
      - exits 0 for ``-c "import weaviate"`` (single module) iff
        ``has_weaviate``.
      - exits 0 for ``-c "import weaviate, weaviate_mcp"`` (the
        post-fix validator's single subprocess) iff
        ``has_weaviate AND has_weaviate_mcp``.
      - exits 0 for any other ``-c`` probe (defensive: if the
        wrapper does another import-probe variant in the future, the
        test won't silently pass the wrong assertion).
      - When invoked NOT in ``-c`` mode (i.e. on a real script
        path), touches ``sentinel_path`` so the test can verify
        which python ran.

    Returns the path to the fake python binary (``$venv_root/bin/python``).
    """
    bin_dir = venv_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    py = bin_dir / "python"

    # The shell script. We bash-quote variables defensively. We rely
    # on a single `-c <code>` form (which is what venv_has_kg_deps()
    # uses); the wrapper's invocation on sync_knowledge_graph.py
    # passes positional args (no `-c`).
    has_weaviate_str = "1" if has_weaviate else "0"
    has_weaviate_mcp_str = "1" if has_weaviate_mcp else "0"
    sentinel = str(sentinel_path)

    script = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # Fake python for kg-sync regression test.
        HAS_WEAVIATE={has_weaviate_str}
        HAS_WEAVIATE_MCP={has_weaviate_mcp_str}
        SENTINEL={shutil_quote(sentinel)}

        # Probe form: python -c "<code>"
        if [ "$1" = "-c" ]; then
            CODE="$2"
            # Distinguish the post-fix two-module probe from the pre-fix
            # one-module probe. We don't try to literal-match — instead
            # we look for `weaviate_mcp` as a substring.
            if echo "$CODE" | grep -q 'weaviate_mcp'; then
                # Post-fix probe: needs BOTH.
                if [ "$HAS_WEAVIATE" = "1" ] && [ "$HAS_WEAVIATE_MCP" = "1" ]; then
                    exit 0
                else
                    exit 1
                fi
            elif echo "$CODE" | grep -q 'weaviate'; then
                # Pre-fix probe: needs only weaviate.
                if [ "$HAS_WEAVIATE" = "1" ]; then
                    exit 0
                else
                    exit 1
                fi
            else
                # Any other -c probe: treat as success (defensive).
                exit 0
            fi
        fi

        # Non-probe invocation: this is the wrapper running the
        # actual sync script with our fake python. Drop a sentinel so
        # the test knows we got picked, then exit 0.
        echo "FAKE_PYTHON_RAN" > "$SENTINEL"
        exit 0
    """)

    py.write_text(script, encoding="utf-8")
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Also create a minimal `bin/activate` that mimics the real venv-
    # activate script's PATH-mutation behavior. Pre-fix the wrapper
    # used `source "$VENV_PATH/bin/activate"` — when the activate
    # script ran, it prepended $VENV/bin to PATH, so the subsequent
    # `python` invocation resolved to our fake python (NOT system
    # python). Without this fixture the pre-fix code path can't be
    # demonstrated end-to-end (no activate => python resolves via
    # the unmodified PATH => sentinel never fires regardless of pre/
    # post-fix). The activate script we emit is the minimum that
    # makes that path observable.
    activate = bin_dir / "activate"
    activate.write_text(
        textwrap.dedent(f"""\
            # Fake venv activate script for kg-sync regression test.
            # Mimics the PATH-prepend behavior of a real venv's
            # activate so the post-`source activate` `python` lookup
            # resolves to this venv's fake python.
            export VIRTUAL_ENV={shutil_quote(str(venv_root))}
            export PATH={shutil_quote(str(bin_dir))}:$PATH
        """),
        encoding="utf-8",
    )
    # No chmod needed — `source` reads the file, doesn't exec it.

    return py


def shutil_quote(s: str) -> str:
    """Shell-quote a string for embedding inside a bash script literal."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _make_path_with_noop_python3(stub_dir: Path) -> str:
    """Build a PATH value that resolves ``python3`` to a no-op shim
    (exit-0 immediately, no I/O). This isolates the wrapper's
    fall-through path from the host's real ``python3`` — which on
    maintainer machines have the orchestrator's editable ``weaviate_mcp``
    installed AND a working Weaviate, so a fall-through would
    actually run a real sync (and timeout the test).

    We keep ``/usr/bin`` and ``/bin`` on the PATH so the wrapper can
    still find ``bash`` builtins (it shouldn't need any other binary
    in the wrapper body itself — all logic is bash builtins +
    invocation of $py / $VENV_PYTHON).

    Returns the PATH string suitable for ``env["PATH"]``.
    """
    stub_dir.mkdir(parents=True, exist_ok=True)
    py = stub_dir / "python3"
    py.write_text(
        "#!/usr/bin/env bash\n"
        "# Test stub: no-op python3 so kg-sync fall-through doesn't\n"
        "# accidentally invoke the real sync against live Weaviate.\n"
        "exit 0\n",
        encoding="utf-8",
    )
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # Also expose `python` (some bash builtins prefer it).
    py_link = stub_dir / "python"
    py_link.write_text(
        "#!/usr/bin/env bash\nexit 0\n",
        encoding="utf-8",
    )
    py_link.chmod(py_link.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # Keep system bin dirs so the wrapper can use bash builtins +
    # subprocess utilities (the wrapper itself doesn't shell out to
    # any external binary other than the venv python and the
    # fall-through python3, but be safe).
    return os.pathsep.join([str(stub_dir), "/usr/bin", "/bin"])


def _run_kg_sync(
    *,
    vct_install_root: Path | None,
    fallback_python_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the kg-sync wrapper with a controlled env.

    ``fallback_python_dir`` is a tmpdir into which a no-op
    ``python3`` shim is written + prepended to PATH, so when the
    wrapper falls through (no venv accepted), it runs our stub
    instead of the host's real python3 (which may have ``weaviate_mcp``
    installed AND a working Weaviate connection, causing a real sync
    that exceeds the test timeout).
    """
    env = os.environ.copy()
    # Strip anything the host shell may have exported. We also drop
    # PYTHONPATH because the host's site-packages may shadow our
    # stub via #!/usr/bin/env python3 lookups.
    env.pop("VCT_INSTALL_ROOT", None)
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONPATH", None)
    if vct_install_root is not None:
        env["VCT_INSTALL_ROOT"] = str(vct_install_root)
    if extra_env:
        env.update(extra_env)
    env["PATH"] = _make_path_with_noop_python3(fallback_python_dir)
    return subprocess.run(
        ["bash", str(KG_SYNC), "--all"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


class KgSyncVenvPickerBugK(unittest.TestCase):
    """v0.2.49 Bug K regression tests for the kg-sync venv picker."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)
        self.sentinel = self.tmp / "sentinel.txt"
        # Make sure the wrapper can't find ANY of the script-relative
        # fallback venvs (orchestrator clone's own .venv etc.). We
        # achieve this by pointing VCT_INSTALL_ROOT at a tmpdir BUT
        # never creating the .venv inside it (for the empty case).
        # For the SCRIPT_DIR/../../.venv fallback, the test asserts
        # only on the EXIT CODE / sentinel — if a real orchestrator
        # venv exists, the wrapper may pick it. To make the test
        # deterministic in that case, we ALSO override the sentinel
        # check to verify our fake-python sentinel — if the real venv
        # was picked, our sentinel won't appear, but the run will
        # likely succeed for the real script. Acceptable: the tests
        # explicitly check fakes the wrapper picks, not "no other
        # venv was picked" in absolute terms.

    def test_kg_sync_exists_and_executable(self) -> None:
        """Sanity: the kg-sync wrapper is present + executable."""
        self.assertTrue(KG_SYNC.exists(), f"kg-sync missing at {KG_SYNC}")
        self.assertTrue(
            os.access(KG_SYNC, os.X_OK),
            f"kg-sync not executable: {KG_SYNC}",
        )

    def test_validator_checks_both_weaviate_and_weaviate_mcp(self) -> None:
        """v0.2.49 Bug K source-level gate: the validator must probe
        BOTH ``weaviate`` and ``weaviate_mcp`` in the same -c call.

        This is the regression pin for the source-level fix — it
        protects against a future edit reverting the validator to
        the pre-fix single-import probe.
        """
        text = KG_SYNC.read_text(encoding="utf-8")
        self.assertIn(
            "import weaviate, weaviate_mcp",
            text,
            "kg-sync: venv_has_kg_deps() must validate BOTH `weaviate` "
            "AND `weaviate_mcp` are importable. Pre-fix it only "
            "checked `weaviate`, letting unrelated venvs through and "
            "crashing sync_knowledge_graph.py at `from weaviate_mcp"
            ".chunking import Chunker`.",
        )

    def test_validator_detects_windows_python(self) -> None:
        """v0.2.49 Bug K cross-OS hardening: the validator's python-
        binary detection must include Windows-shape paths
        (``$v/Scripts/python.exe``) so kg-sync works under Git Bash /
        WSL2 on Windows. Pre-fix only POSIX-shape was checked."""
        text = KG_SYNC.read_text(encoding="utf-8")
        self.assertIn(
            "Scripts/python.exe",
            text,
            "kg-sync: must detect Windows-shape python at "
            "$v/Scripts/python.exe (Git Bash / WSL2). Pre-fix only "
            "POSIX-shape bin/python was probed.",
        )

    def test_wrapper_invokes_venv_python_directly_not_source_activate(self) -> None:
        """v0.2.49 Bug K cross-OS hardening: the wrapper must invoke
        the venv's python binary directly, NOT ``source bin/activate``.

        Rationale: ``source bin/activate`` is POSIX-only — Windows
        venvs (incl. under Git Bash / WSL2) don't have a
        ``bin/activate`` script. Direct invocation works on every OS;
        exporting ``VIRTUAL_ENV`` keeps child processes informed.
        """
        text = KG_SYNC.read_text(encoding="utf-8")
        # We must not source bin/activate any more — that pattern is
        # the pre-fix POSIX-only path.
        self.assertNotIn(
            'source "$VENV_PATH/bin/activate"',
            text,
            "kg-sync: must not `source bin/activate` (POSIX-only — "
            "Windows venvs have no bin/activate). Use direct "
            "venv-python invocation instead.",
        )
        # The post-fix wrapper exports VIRTUAL_ENV so child processes
        # see the venv as active.
        self.assertIn(
            "VIRTUAL_ENV",
            text,
            "kg-sync: must export VIRTUAL_ENV so child processes see "
            "the venv as active (replaces what `source activate` did "
            "for env vars).",
        )

    def test_picker_rejects_weaviate_only_venv(self) -> None:
        """**Regression pin for v0.2.49 Bug K**.

        Scenario: ``$VCT_INSTALL_ROOT/.venv/bin/python`` exists. It
        imports ``weaviate`` successfully but not ``weaviate_mcp``.

        Pre-fix: the wrapper accepted this venv (only ``weaviate``
        was probed) and then invoked ``sync_knowledge_graph.py`` with
        a venv that crashed at line 52 with ``ModuleNotFoundError:
        No module named 'weaviate_mcp'``.

        Post-fix: the wrapper REJECTS this venv (both imports must
        succeed) and falls through to the next candidate or to system
        python.

        We assert: the fake python in ``$VCT_INSTALL_ROOT/.venv`` did
        NOT get invoked on a non-probe arg (sentinel file absent).
        """
        install_root = self.tmp / "install_root"
        install_root.mkdir()
        _make_fake_venv(
            install_root / ".venv",
            has_weaviate=True,
            has_weaviate_mcp=False,
            sentinel_path=self.sentinel,
        )
        # Run the wrapper; it should reject our fake venv.
        result = _run_kg_sync(
            vct_install_root=install_root,
            fallback_python_dir=self.tmp / "stub_path",
        )
        # The sentinel must NOT exist — our fake python should not
        # have been invoked on the real (non-probe) script call,
        # because the picker rejected the venv.
        self.assertFalse(
            self.sentinel.exists(),
            f"v0.2.49 Bug K regression: the wrapper picked a venv "
            f"that has `weaviate` but lacks `weaviate_mcp` — pre-fix "
            f"behavior. stdout={result.stdout!r} stderr={result.stderr!r}",
        )

    def test_picker_accepts_both_importable_venv(self) -> None:
        """Scenario: ``$VCT_INSTALL_ROOT/.venv/bin/python`` reports
        BOTH ``weaviate`` and ``weaviate_mcp`` importable. The
        picker MUST accept it and invoke that python on the real
        script.

        We assert: the sentinel file exists (our fake python ran).
        """
        install_root = self.tmp / "install_root"
        install_root.mkdir()
        fake_py = _make_fake_venv(
            install_root / ".venv",
            has_weaviate=True,
            has_weaviate_mcp=True,
            sentinel_path=self.sentinel,
        )
        result = _run_kg_sync(
            vct_install_root=install_root,
            fallback_python_dir=self.tmp / "stub_path",
        )
        self.assertTrue(
            self.sentinel.exists(),
            f"v0.2.49 Bug K: the wrapper REJECTED a venv that has "
            f"both `weaviate` AND `weaviate_mcp` (both-importable). "
            f"fake_py={fake_py} stdout={result.stdout!r} "
            f"stderr={result.stderr!r}",
        )

    def test_picker_falls_through_when_no_venv_has_deps(self) -> None:
        """Scenario: ``$VCT_INSTALL_ROOT/.venv/bin/python`` exists
        but reports NEITHER ``weaviate`` nor ``weaviate_mcp`` (and
        no other candidate venv has the deps either).

        Expected: the wrapper falls through to bare ``python3`` (no
        venv activated). Our fake python's sentinel must NOT appear.
        """
        install_root = self.tmp / "install_root"
        install_root.mkdir()
        _make_fake_venv(
            install_root / ".venv",
            has_weaviate=False,
            has_weaviate_mcp=False,
            sentinel_path=self.sentinel,
        )
        _run_kg_sync(
            vct_install_root=install_root,
            fallback_python_dir=self.tmp / "stub_path",
        )
        self.assertFalse(
            self.sentinel.exists(),
            "wrapper picked a venv that imports NEITHER weaviate nor "
            "weaviate_mcp — should have fallen through to system python3",
        )


@unittest.skipUnless(sys.platform == "win32", "Windows-only path detection")
class KgSyncWindowsPythonDetection(unittest.TestCase):
    """v0.2.49 Bug K — Windows-shape detection. Only runs on Windows
    hosts; on POSIX hosts we have no easy way to create an
    executable ``.exe`` fixture that the bash script would pick up
    via its ``-x`` test. The source-level assertion
    (``test_validator_detects_windows_python`` above) protects the
    grep-able marker on all platforms.
    """

    def test_placeholder(self) -> None:
        # If a future PR adds a Windows CI runner, expand this class to
        # build a fake $venv\Scripts\python.exe wrapper and assert the
        # bash kg-sync (running under Git Bash) picks it.
        self.skipTest(
            "Live Windows-venv-shape test deferred until Windows CI exists "
            "for the bash kg-sync path. Source-level grep is covered above."
        )


if __name__ == "__main__":
    unittest.main()
