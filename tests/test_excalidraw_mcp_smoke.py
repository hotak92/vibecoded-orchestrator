# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Live smoke test for the esbuild-bundled Excalidraw MCP entry (P0-3).

Why a LIVE subprocess test and not an argv-shape test: argv-shape
assertions (``"--flag" in repr(cmd)``) cannot catch the actual P0-3
failure mode — the vendored tree shipping without resolvable
transitive dependencies, so ``node dist/mcp/index.js`` dies on its
first ``import pino`` before any JSON-RPC traffic. The only test that
proves the bundle is alive is spawning it and completing a real
JSON-RPC ``initialize`` round-trip over stdio.

The bundled entry is fully self-contained (every dependency inlined by
esbuild — see ``vco_lib/excalidraw_mcp_fork/VENDORED.md`` "Bundling
recipe"), so this test needs only a ``node`` binary, NOT node_modules.

Startup behaviour exercised implicitly: the server's ``detectMode``
health-checks the (absent) canvas server, fails fast on connection
refused, and falls back to standalone mode — so the test passes on a
machine with nothing listening on the canvas port.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_ENTRY = (
    REPO_ROOT / "vco_lib" / "excalidraw_mcp_fork" / "dist" / "mcp" / "index.bundled.js"
)

INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "vco-smoke-test", "version": "0.0.1"},
    },
}


@unittest.skipIf(shutil.which("node") is None, "node not on PATH")
class ExcalidrawBundledMcpSmokeTests(unittest.TestCase):
    """Real-subprocess JSON-RPC round-trip against the bundled entry."""

    def test_bundled_entry_exists(self):
        self.assertTrue(
            BUNDLED_ENTRY.is_file(),
            f"bundled MCP entry missing at {BUNDLED_ENTRY} — regenerate "
            "with `npm run bundle` in vco_lib/excalidraw_mcp_fork/ "
            "(see VENDORED.md)",
        )

    def test_initialize_round_trip_over_stdio(self):
        """Spawn the bundle, send `initialize`, expect a valid response.

        20 s timeout bounds the worst case (cold node start + the
        canvas-server health-check fetch failing on connection refused);
        the observed happy path completes in well under 2 s.
        """
        node = shutil.which("node")
        proc = subprocess.run(
            [node, str(BUNDLED_ENTRY)],
            input=(json.dumps(INITIALIZE_REQUEST) + "\n").encode("utf-8"),
            capture_output=True,
            timeout=20,
            cwd=str(BUNDLED_ENTRY.parent),
        )

        # stdout must contain exactly the JSON-RPC channel — one
        # response line for our request. (Logging goes to stderr;
        # anything non-JSON on stdout would corrupt the MCP transport.)
        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        self.assertTrue(
            stdout,
            "no stdout from bundled MCP — likely a startup crash; "
            f"stderr: {proc.stderr.decode('utf-8', errors='replace')[:2000]}",
        )

        first_line = stdout.splitlines()[0]
        response = json.loads(first_line)

        self.assertEqual(response.get("jsonrpc"), "2.0")
        self.assertEqual(response.get("id"), 1)
        self.assertNotIn(
            "error", response,
            f"initialize returned a JSON-RPC error: {response}",
        )
        result = response.get("result", {})
        server_info = result.get("serverInfo", {})
        self.assertEqual(server_info.get("name"), "excalidraw-mcp-server")
        self.assertIn("protocolVersion", result)


if __name__ == "__main__":
    unittest.main()
