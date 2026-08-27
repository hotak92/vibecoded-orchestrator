# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-E item 1 — the wrapper MCP entries must resolve from ANY cwd.

## The defect this pins

The mermaid / excalidraw entries written into the GLOBAL ``~/.claude.json``
are spawned as::

    <venv-python> -m claude_mcp_servers.wrappers.<proxy>

with ``env.PYTHONPATH`` set by the entry builders. Through v0.2.90 that value
was ``<install_root>/claude_mcp_servers`` — a path INSIDE the package. The
dotted name ``claude_mcp_servers.wrappers.<proxy>`` can only resolve when the
package's PARENT is on ``sys.path``, so the ONLY thing making these entries
work was ``python -m``'s implicit cwd-prepend. Claude Code spawns stdio MCPs
with cwd = the session's project directory, and ``~/.claude.json`` is global,
so the entries resolved for the orchestrator root and died instantly
everywhere else::

    Error while finding module specification for
    'claude_mcp_servers.wrappers.mermaid_proxy'
    (ModuleNotFoundError: No module named 'claude_mcp_servers')

rc=1, before ANY package code runs — which is why the wrappers' own
script-mode import fallbacks (``_base.py``/``mermaid_proxy.py``) cannot help.

## Why a shape test is not enough

``tests/test_install_mcp_registration.py`` asserts the built ``env`` value.
That is necessary but not sufficient: it pins the string, not the behaviour of
the interpreter that consumes it. This module SPAWNS the built entry from a
foreign cwd — the live-CLI-parser lesson (v0.2.x: argv-shape tests passed while
the real binary rejected the argv).

Two legs:

* :class:`WrapperEntryResolvesFromForeignCwdTests` — HERMETIC gate. Runs the
  entry's own interpreter with the entry's own env from a temp cwd and asks
  ``importlib.util.find_spec`` to resolve the exact dotted name the entry
  passes to ``-m``. No network, no npx/node, no third-party imports. This is
  the leg that red-proofs (it fails on the pre-fix PYTHONPATH).
* :class:`WrapperEntryLiveHandshakeTests` — the full ``initialize`` round-trip
  against the real upstream, SKIPPED when the machine lacks the upstream
  toolchain. Env-probing gates de-hermeticize a suite (v0.2.89 lesson), so the
  environment-dependent leg is the skippable one and never the gate.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
# The builder's HOME (install.py only re-exports it). Importing the module
# directly keeps this regression test off install.py's heavy import graph.
from vco_lib import install_mcp  # noqa: E402

# (entry name, dotted module the entry passes to `-m`, upstream binary needed)
_WRAPPERS = (
    ("mermaid", "claude_mcp_servers.wrappers.mermaid_proxy", "npx"),
    ("excalidraw", "claude_mcp_servers.wrappers.excalidraw_proxy", "node"),
)


def _built_entries() -> dict[str, dict]:
    """The REAL entries this repo would register, built against this repo.

    ``venv_python`` is the running interpreter: the spawned child is then the
    same interpreter pytest runs under, so the test needs no venv discovery
    and cannot silently probe a different Python than it asserts about.
    """
    entries = install_mcp._build_python_mcp_entries(
        REPO_ROOT, Path(sys.executable), 8081, 11435, 50052, 11440,
    )
    return {name: entry for name, entry, _ in entries}


def _child_env(entry: dict) -> dict[str, str]:
    """Process env for the spawned entry: inherited env + the entry's own env.

    Mirrors how Claude Code spawns a stdio MCP (the entry's ``env`` block is
    overlaid on the inherited environment).
    """
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (entry.get("env") or {}).items()})
    return env


class WrapperEntryResolvesFromForeignCwdTests(unittest.TestCase):
    """HERMETIC gate: the built entry's `-m` target resolves from any cwd."""

    def test_wrapper_module_resolves_from_unrelated_cwd(self) -> None:
        entries = _built_entries()
        probe = (
            "import importlib.util as u, sys; "
            "sys.exit(0 if u.find_spec(sys.argv[1]) is not None else 3)"
        )
        for name, dotted, _upstream in _WRAPPERS:
            with self.subTest(wrapper=name):
                entry = entries[name]
                # The entry really does invoke `python -m <dotted>`.
                self.assertEqual(entry["args"][:2], ["-m", dotted])
                with tempfile.TemporaryDirectory() as td:
                    proc = subprocess.run(
                        [entry["command"], "-c", probe, dotted],
                        cwd=td,
                        env=_child_env(entry),
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"`python -m {dotted}` cannot resolve from cwd={td!r} with the "
                    f"registered PYTHONPATH "
                    f"({entry['env'].get('PYTHONPATH')!r}).\n"
                    f"stderr: {proc.stderr.strip()}",
                )
                self.assertNotIn("No module named 'claude_mcp_servers'", proc.stderr)


class WrapperEntryLiveHandshakeTests(unittest.TestCase):
    """LIVE leg: spawn the built entry from a foreign cwd, assert an
    ``initialize`` reply. Skipped when the upstream toolchain is absent."""

    @staticmethod
    def _skip_reason(upstream: str) -> str | None:
        try:
            import aiohttp  # noqa: F401
        except Exception:  # pragma: no cover — environment-dependent
            return "aiohttp not importable by this interpreter"
        if shutil.which(upstream) is None:  # pragma: no cover
            return f"upstream launcher `{upstream}` not on PATH"
        return None

    def test_initialize_reply_from_unrelated_cwd(self) -> None:
        entries = _built_entries()
        for name, dotted, upstream in _WRAPPERS:
            with self.subTest(wrapper=name):
                reason = self._skip_reason(upstream)
                if reason:
                    self.skipTest(f"{name}: {reason}")
                entry = entries[name]
                with tempfile.TemporaryDirectory() as td:
                    proc = subprocess.Popen(
                        [entry["command"], *entry["args"]],
                        cwd=td,
                        env=_child_env(entry),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                    )
                    try:
                        request = {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {},
                                "clientInfo": {"name": "vco-regression", "version": "0"},
                            },
                        }
                        assert proc.stdin is not None and proc.stdout is not None
                        proc.stdin.write(json.dumps(request) + "\n")
                        proc.stdin.flush()
                        # stdin stays OPEN: EOF is a client disconnect and the
                        # proxy correctly tears the upstream down before its
                        # reply is relayed (the 2026-08-23 probe artifact).
                        box: dict[str, str] = {}

                        def _read() -> None:
                            box["line"] = proc.stdout.readline()  # type: ignore[union-attr]

                        reader = threading.Thread(target=_read, daemon=True)
                        reader.start()
                        reader.join(90)
                        line = box.get("line", "")
                        self.assertTrue(
                            line.strip(),
                            f"{name}: no initialize reply within 90s from cwd={td!r}; "
                            f"child alive={proc.poll() is None}",
                        )
                        reply = json.loads(line)
                        self.assertEqual(reply.get("id"), 1)
                        self.assertIn("result", reply, f"{name}: {reply}")
                        self.assertIn("protocolVersion", reply["result"])
                    finally:
                        proc.kill()
                        try:
                            proc.communicate(timeout=15)
                        except Exception:  # pragma: no cover — best-effort drain
                            pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
