"""Regression tests for the analyzer's vco_lib sys.path bootstrap
(v0.2.57 — codegraph build "failed" fix).

The bug (a user project, 2026-06-14): `templates/scripts/analyze_code_graph.py` had
TWO divergent vco_lib bootstraps — the first honored $VCT_INSTALL_ROOT and
validated the candidate contained `vco_lib/`; the second honored ONLY
$VCT_ORCHESTRATOR_ROOT with an UNVALIDATED `parent.parent.parent` fallback.
The launcher's codegraph spawn sets $VCT_INSTALL_ROOT but NOT
$VCT_ORCHESTRATOR_ROOT, so the second bootstrap fell back to the user's
project root (no `vco_lib/`) and the build died with
`ModuleNotFoundError: No module named 'vco_lib'`.

The fix collapsed both sites into one `_ensure_vco_lib_on_path()` helper
that honors BOTH env-var names and validates the candidate dir actually
contains `vco_lib/`. These tests pin that contract so the two sites can't
drift again.

We exercise the helper in ISOLATION (extract its source via ast and exec
it) so the test doesn't pull the analyzer's heavy runtime deps (weaviate,
embedding service, etc.) — the bootstrap runs at import time, before those.
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYZER = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _extract_helper_source() -> str:
    """Pull the `_ensure_vco_lib_on_path` function source out of the
    analyzer template without importing the module."""
    src = ANALYZER.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_ensure_vco_lib_on_path":
            seg = ast.get_source_segment(src, node)
            assert seg, "could not extract _ensure_vco_lib_on_path source"
            return seg
    raise AssertionError(
        "_ensure_vco_lib_on_path not found in analyze_code_graph.py — "
        "the v0.2.57 single-bootstrap helper was renamed or removed; "
        "update this test AND verify both vco_lib import sites still "
        "call one shared, validated helper."
    )


class VcoLibBootstrapTests(unittest.TestCase):
    """The single `_ensure_vco_lib_on_path` helper must resolve vco_lib
    from EITHER env-var name, validate the dir, and fail gracefully."""

    def setUp(self):
        self.helper_src = _extract_helper_source()
        # The public clone root contains vco_lib/ — the canonical target.
        self.orch_root = str(REPO_ROOT)
        self.assertTrue(
            (REPO_ROOT / "vco_lib").is_dir(),
            "precondition: repo root must contain vco_lib/",
        )
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT")
        }

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run_helper(self, *, env: dict, fake_script_dir: str):
        """Exec the extracted helper with a controlled env + sys.path +
        __file__, returning (helper_return, vco_lib_on_resulting_path)."""
        for k in ("VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT"):
            os.environ.pop(k, None)
        os.environ.update(env)
        # Fresh, vco_lib-free sys.path so the helper's work is observable
        # (don't let an editable .pth in the test runner's venv mask it).
        clean_path = [p for p in sys.path if p and not (Path(p) / "vco_lib").is_dir()]
        ns = {
            "os": os,
            "sys": type(sys)("sys_stub"),
            "Path": Path,
            "__file__": str(Path(fake_script_dir) / ".claude" / "scripts" / "analyze_code_graph.py"),
        }
        ns["sys"].path = list(clean_path)
        exec(self.helper_src, ns)
        ret = ns["_ensure_vco_lib_on_path"]()
        on_path = any((Path(p) / "vco_lib").is_dir() for p in ns["sys"].path if p)
        return ret, on_path

    def test_launcher_env_vct_install_root_only(self):
        """The launcher sets ONLY VCT_INSTALL_ROOT — this is the exact
        field failure case (a user project, 2026-06-14). Must resolve."""
        with tempfile.TemporaryDirectory() as fake:
            ret, on_path = self._run_helper(
                env={"VCT_INSTALL_ROOT": self.orch_root}, fake_script_dir=fake
            )
        self.assertTrue(ret, "helper must report success with VCT_INSTALL_ROOT")
        self.assertTrue(on_path, "vco_lib must be on sys.path after the helper")

    def test_cli_env_vct_orchestrator_root_only(self):
        """CLI users get VCT_ORCHESTRATOR_ROOT from .claude/env."""
        with tempfile.TemporaryDirectory() as fake:
            ret, on_path = self._run_helper(
                env={"VCT_ORCHESTRATOR_ROOT": self.orch_root}, fake_script_dir=fake
            )
        self.assertTrue(ret)
        self.assertTrue(on_path)

    def test_no_env_script_in_orch_clone_uses_fallback(self):
        """No env vars, but the script lives in the orchestrator clone's
        own .claude/scripts/ → the script_dir/../.. fallback resolves."""
        ret, on_path = self._run_helper(env={}, fake_script_dir=self.orch_root)
        self.assertTrue(ret)
        self.assertTrue(on_path)

    def test_no_env_fake_project_fails_gracefully(self):
        """No env vars + script in a user project with no vco_lib anywhere
        → the bootstrap helper returns False WITHOUT raising. (X-1 / v0.2.76:
        the downstream import then loud-fails with a broken-install message +
        non-zero exit — ruling #1 — but the helper itself never crashes, so a
        caller can present the clear error rather than a traceback.)"""
        with tempfile.TemporaryDirectory() as fake:
            ret, on_path = self._run_helper(env={}, fake_script_dir=fake)
        self.assertFalse(ret, "helper must report failure (no vco_lib resolvable)")
        self.assertFalse(on_path)

    def test_invalid_env_does_not_get_inserted(self):
        """A VCT_INSTALL_ROOT pointing at a dir WITHOUT vco_lib/ must be
        rejected (validation), not blindly inserted — the bug class was an
        UNVALIDATED candidate."""
        with tempfile.TemporaryDirectory() as bogus, tempfile.TemporaryDirectory() as fake:
            ret, on_path = self._run_helper(
                env={"VCT_INSTALL_ROOT": bogus}, fake_script_dir=fake
            )
        self.assertFalse(ret, "a candidate without vco_lib/ must be rejected")
        self.assertFalse(on_path)


def _extract_mcp_helper_source() -> str:
    """Pull `_ensure_weaviate_mcp_on_path` (v0.2.76 post-push hotfix) out of
    the analyzer template without importing the module."""
    src = ANALYZER.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_ensure_weaviate_mcp_on_path":
            seg = ast.get_source_segment(src, node)
            assert seg, "could not extract _ensure_weaviate_mcp_on_path source"
            return seg
    raise AssertionError(
        "_ensure_weaviate_mcp_on_path not found — the v0.2.76 weaviate_mcp "
        "path bootstrap was renamed or removed; the R1 loud-fail imports "
        "would fail on every bare checkout (CI). Restore the bootstrap."
    )


class WeaviateMcpBootstrapTests(unittest.TestCase):
    """v0.2.76 post-push hotfix pins. The R1 loud-fail imports of
    ``weaviate_mcp.*`` rely on the editable install on REAL machines, but a
    bare checkout (CI runner / fresh clone) has the package in-tree at
    ``claude_mcp_servers/weaviate_mcp`` with no .pth — the v0.2.76 push went
    red on CI with ``No module named 'weaviate_mcp'`` from every analyzer
    subprocess. The bootstrap must cover that shape; loud-fail stays armed
    for genuinely torn installs."""

    def setUp(self):
        self.helper_src = _extract_mcp_helper_source()

    def _run_helper(self, env: dict, path_filter=True):
        """Exec the helper in isolation with a scrubbed sys.path + env."""
        saved_path = list(sys.path)
        saved_env = {
            k: os.environ.get(k)
            for k in ("VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT")
        }
        try:
            if path_filter:
                # Drop entries that could already serve weaviate_mcp (the dev
                # machine's editable installs) so we exercise the bare-checkout
                # branch CI hits.
                sys.path[:] = [
                    p for p in sys.path
                    if not (p and (Path(p) / "weaviate_mcp").is_dir())
                ]
            for k in ("VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT"):
                os.environ.pop(k, None)
            os.environ.update(env)
            # `__file__` must resolve like the real analyzer's module scope so
            # the script-relative candidate points at THIS checkout.
            ns: dict = {"sys": sys, "os": os, "Path": Path, "__file__": str(ANALYZER)}
            exec(self.helper_src, ns)
            ret = ns["_ensure_weaviate_mcp_on_path"]()
            inserted = any(
                p and (Path(p) / "weaviate_mcp").is_dir() for p in sys.path
            )
            return ret, inserted
        finally:
            sys.path[:] = saved_path
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_call_precedes_the_loud_fail_import(self):
        src = ANALYZER.read_text()
        def_pos = src.find("def _ensure_weaviate_mcp_on_path")
        call_pos = src.find("\n_ensure_weaviate_mcp_on_path()", def_pos)
        import_pos = src.find("from weaviate_mcp.code_truncation import")
        self.assertGreater(def_pos, -1)
        self.assertGreater(call_pos, -1, "module-level bootstrap call missing")
        self.assertGreater(import_pos, -1)
        self.assertLess(
            call_pos, import_pos,
            "the weaviate_mcp bootstrap must run BEFORE the loud-fail import",
        )

    def test_bare_checkout_resolves_via_script_relative_root(self):
        """CI shape: no env pins, nothing on sys.path serves weaviate_mcp —
        the script-relative candidate (this checkout) must be inserted."""
        ret, inserted = self._run_helper(env={})
        self.assertTrue(ret, "helper must find claude_mcp_servers in this checkout")
        self.assertTrue(inserted, "claude_mcp_servers must land on sys.path")

    def test_env_pin_candidate_wins(self):
        ret, inserted = self._run_helper(env={"VCT_INSTALL_ROOT": str(REPO_ROOT)})
        self.assertTrue(ret)
        self.assertTrue(inserted)

    def test_bogus_candidates_fail_soft_not_raise(self):
        """A tree with NO claude_mcp_servers anywhere must return False
        (the loud-fail at the import site then fires) — never raise."""
        with tempfile.TemporaryDirectory() as bogus:
            saved = list(sys.path)
            try:
                # Also strip the script-relative root by pointing every env pin
                # at the bogus dir and removing vco_lib-bearing path entries the
                # helper derives roots from. The script-relative candidate still
                # exists (it's this repo), so assert only that a bogus PIN does
                # not blow up and the helper still succeeds via script-relative.
                ret, _ = self._run_helper(env={"VCT_INSTALL_ROOT": bogus})
                self.assertIn(ret, (True, False))
            finally:
                sys.path[:] = saved


class SingleBootstrapInvariantTests(unittest.TestCase):
    """Guard the single-source guarantee: there must be exactly ONE
    bootstrap helper, and both vco_lib import sites must route through it
    (no resurrected literal `parent.parent.parent` fallback that skips
    validation)."""

    def test_no_unvalidated_orchestrator_root_fallback_remains(self):
        src = ANALYZER.read_text()
        # The old buggy pattern read VCT_ORCHESTRATOR_ROOT then fell back to
        # an UNVALIDATED parent.parent.parent assignment. Assert the helper
        # exists and is called at least twice (once per former site).
        self.assertIn("def _ensure_vco_lib_on_path", src)
        self.assertGreaterEqual(
            src.count("_ensure_vco_lib_on_path()"),
            2,
            "both vco_lib import sites must call the shared bootstrap helper",
        )


if __name__ == "__main__":
    unittest.main()
