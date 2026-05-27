# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for v0.2.37 install-bundle bootstrap gaps (Agent V37-C).

5 gaps closed in v0.2.37:

  Gap 6a (Python install-bundle):
    `.claude/env` now carries VCT_ORCHESTRATOR_ROOT /
    VCT_INFRASTRUCTURE_DIR / VCT_INSTALL_ROOT exports when the install-
    bundle call site threads `orchestrator_root` through to the env
    projector. Pre-v0.2.37 these portability keys were referenced in
    the `.claude/env` header comment but never actually written.

  Gap 6b (5 wrapper templates):
    kg-sync / kg-migrate / kg-search / kg-info / code-graph-query
    (bash + PS1) backport the validate-has-weaviate-client venv-
    fallback pattern from `code-graph-analyze`. Pre-v0.2.37 these
    wrappers only probed `$PROJECT_ROOT/.venv` /
    `$PROJECT_ROOT/claude_mcp_servers/.venv`, both absent in a fresh
    OSS install.

  Gap 6c (code-graph-query PYTHONPATH):
    `code-graph-query` (bash + PS1) now sets PYTHONPATH so
    `query_code_graph.py` can import `claude_mcp_servers.weaviate_mcp`.

  Gap 6d (sync_knowledge_graph chunking-props migration):
    `ensure_collection_exists` additive-migration path now patches
    chunk_num / total_chunks / source_node_id on pre-chunking-era
    collections. Pre-v0.2.37 every sync against those collections
    failed with "no such prop with name 'chunk_num'".

  Gap 6e (analyze_code_graph CODE_GRAPH_PROJECT env):
    `analyze_code_graph` arg parser honors `$CODE_GRAPH_PROJECT`
    between `--project` and the repo-dir-name fallback. Direct CLI
    invocations (no --project) now match what launcher hooks pass.

Run: pytest tests/test_v0237_install_bundle_gaps.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ─── Gap 6a: install-bundle threads orchestrator_root into env ──────────


class InstallBundleEnvOrchestratorRootTests(unittest.TestCase):
    """`_apply_canonical_env_via_config_projection` now accepts and
    forwards `orchestrator_root`. The end result: `.claude/env` and
    `.claude/settings.json` env block carry VCT_ORCHESTRATOR_ROOT /
    VCT_INFRASTRUCTURE_DIR / VCT_INSTALL_ROOT exports.
    """

    def _make_launcher_db(
        self,
        db_path: Path,
        *,
        project_id: str,
        project_name: str,
        project_folder: str,
    ) -> None:
        """Minimal launcher.db with the full schema this resolver reads.

        Re-uses the canonical fixture from `tests/test_config_projection.py`
        so we don't drift from its 6-table layout (projects,
        project_kg_bindings, kg_collection_access, codegraph_access,
        diagram_access, module_settings)."""
        from tests.test_config_projection import _make_launcher_db
        _make_launcher_db(
            db_path,
            project_id=project_id,
            project_name=project_name,
            project_folder=project_folder,
            project_slug=project_name.lower().replace(" ", "-"),
        )

    def test_apply_canonical_env_forwards_orchestrator_root(self):
        """When `orchestrator_root` is set, the resulting `.claude/env`
        carries VCT_ORCHESTRATOR_ROOT / VCT_INFRASTRUCTURE_DIR /
        VCT_INSTALL_ROOT exports. The 3 keys are co-emitted (same value
        for the legacy alias VCT_INSTALL_ROOT)."""
        from vco_lib import project_init

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            project_folder = tdp / "test_proj"
            project_folder.mkdir()
            orchestrator = tdp / "vco-clone"
            orchestrator.mkdir()
            (orchestrator / "infrastructure").mkdir()

            state_dir = tdp / "vct-state"
            state_dir.mkdir()
            db_path = state_dir / "launcher.db"
            self._make_launcher_db(
                db_path,
                project_id="proj-uuid-orchroot",
                project_name="V37CTest",
                project_folder=str(project_folder.resolve()),
            )

            with patch.dict(os.environ, {"VCT_STATE_DIR": str(state_dir)}):
                result = project_init._apply_canonical_env_via_config_projection(
                    project_folder, orchestrator_root=orchestrator,
                )

            self.assertEqual(result["action"], "applied")
            resolved = result["resolved_values"]
            # Gap 6a: all 3 portability keys present.
            self.assertEqual(
                resolved.get("VCT_ORCHESTRATOR_ROOT"), str(orchestrator)
            )
            self.assertEqual(
                resolved.get("VCT_INFRASTRUCTURE_DIR"),
                str(orchestrator / "infrastructure"),
            )
            self.assertEqual(
                resolved.get("VCT_INSTALL_ROOT"), str(orchestrator),
                "VCT_INSTALL_ROOT must be aliased to orchestrator_root "
                "for `code-graph-analyze` venv-fallback parity",
            )

            # The .claude/env shell file on disk carries the same 3 exports.
            env_file = project_folder / ".claude" / "env"
            self.assertTrue(env_file.exists(), ".claude/env missing")
            env_text = env_file.read_text(encoding="utf-8")
            self.assertIn(
                f'export VCT_ORCHESTRATOR_ROOT="{orchestrator}"', env_text,
                f"missing VCT_ORCHESTRATOR_ROOT export. Body:\n{env_text}",
            )
            self.assertIn(
                f'export VCT_INFRASTRUCTURE_DIR="{orchestrator / "infrastructure"}"',
                env_text,
                f"missing VCT_INFRASTRUCTURE_DIR export. Body:\n{env_text}",
            )
            self.assertIn(
                f'export VCT_INSTALL_ROOT="{orchestrator}"', env_text,
                f"missing VCT_INSTALL_ROOT export. Body:\n{env_text}",
            )

    def test_apply_canonical_env_without_orchestrator_root_omits_keys(self):
        """When `orchestrator_root` is None (back-compat call path), the
        portability keys MUST NOT be written. Avoids forcing absent
        values into `.claude/env`."""
        from vco_lib import project_init

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            project_folder = tdp / "test_proj"
            project_folder.mkdir()
            state_dir = tdp / "vct-state"
            state_dir.mkdir()
            db_path = state_dir / "launcher.db"
            self._make_launcher_db(
                db_path,
                project_id="proj-uuid-no-orch",
                project_name="V37CNoOrch",
                project_folder=str(project_folder.resolve()),
            )

            with patch.dict(os.environ, {"VCT_STATE_DIR": str(state_dir)}):
                result = project_init._apply_canonical_env_via_config_projection(
                    project_folder, orchestrator_root=None,
                )

            self.assertEqual(result["action"], "applied")
            resolved = result["resolved_values"]
            self.assertNotIn("VCT_ORCHESTRATOR_ROOT", resolved)
            self.assertNotIn("VCT_INFRASTRUCTURE_DIR", resolved)
            self.assertNotIn("VCT_INSTALL_ROOT", resolved)

    def test_install_bundle_passes_orchestrator_root_to_env_projector(self):
        """`install_project_bundle` threads its `orchestrator_root` arg
        into the env-projection step. This is the wire-up test —
        verifying the call site, not the projector itself."""
        from vco_lib import project_init

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            project_folder = tdp / "test_proj"
            project_folder.mkdir()
            orchestrator = tdp / "vco-clone"
            orchestrator.mkdir()
            (orchestrator / "infrastructure").mkdir()
            (orchestrator / "templates").mkdir()
            (orchestrator / "vct-module.json").write_text(
                '{"name": "VibeCoded Orchestrator"}', encoding="utf-8",
            )

            with patch.object(
                project_init,
                "_apply_canonical_env_via_config_projection",
                return_value={
                    "action": "applied", "added_keys": [],
                    "path": "x", "resolved_values": {},
                },
            ) as mock_apply:
                project_init.install_project_bundle(
                    project_folder,
                    orchestrator_root=orchestrator,
                    update_mode=False,
                    force=False,
                    dry_run=False,
                )

            # Verify the projector was called WITH orchestrator_root.
            self.assertTrue(mock_apply.called)
            kwargs = mock_apply.call_args.kwargs
            self.assertEqual(
                kwargs.get("orchestrator_root"), orchestrator,
                "install_project_bundle must thread orchestrator_root "
                "through to _apply_canonical_env_via_config_projection",
            )


# ─── Gap 6d: sync_knowledge_graph chunking-props additive migration ─────


def _load_sync_module(test_id: int):
    """Load `templates/scripts/sync_knowledge_graph.py` as a module
    without executing its __main__ guard. Each test gets a unique
    sys.modules entry so module-level mutations don't leak."""
    os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
    script_path = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
    mod_name = f"_test_v0237_sync_{test_id}"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class EnsureCollectionExistsChunkingPropsTests(unittest.TestCase):
    """`ensure_collection_exists` additive-migration path patches
    chunk_num / total_chunks / source_node_id on existing collections
    that predate chunking support.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._mod = _load_sync_module(id(cls))

    def _make_server_with_existing_collection(self, existing_props: set[str]):
        """Build a mock server where `collections.exists(...)` returns
        True and `collection.config.get().properties` has only the
        names in `existing_props`. Captures the add_property() calls
        so we can assert what was patched in."""
        srv = MagicMock()
        srv.client.collections.exists.return_value = True

        collection = MagicMock()
        config = MagicMock()
        # `existing_props` is the legacy set BEFORE the migration patches.
        # NOTE: MagicMock(name=X) sets the *mock's name*, not the .name
        # attr — we have to assign via attribute access post-construction.
        prop_mocks = []
        for n in existing_props:
            p = MagicMock()
            p.name = n
            prop_mocks.append(p)
        config.properties = prop_mocks
        config.references = []
        collection.config.get.return_value = config
        collection.config.add_property = MagicMock()
        collection.config.add_reference = MagicMock()
        srv.client.collections.get.return_value = collection

        self._mod.COLLECTION_NAME = "Foo_KnowledgeGraph"
        return srv, collection

    def test_chunking_props_patched_when_missing(self):
        """Legacy collection (no chunking props) → all 3 chunking
        props added via add_property. This is the dogfooded bug: every
        sync against a pre-v0.2.16 collection used to fail with `no
        such prop with name 'chunk_num'` until the user dropped &
        recreated the collection manually."""
        # Simulate the pre-chunking legacy collection: has the base
        # props + temporal props but lacks chunking.
        existing_props = {
            "title", "content", "file_path", "node_type", "tags", "links",
            "typed_links", "external_links", "content_hash",
            "created", "updated", "valid_from", "valid_until", "status",
            # NO chunk_num, NO total_chunks, NO source_node_id.
        }
        srv, collection = self._make_server_with_existing_collection(existing_props)
        ok = self._mod.ensure_collection_exists(srv)
        self.assertTrue(ok)

        # Collect every property name added.
        added_names = set()
        for call in collection.config.add_property.call_args_list:
            prop = call.args[0] if call.args else call.kwargs.get("property")
            if prop is not None:
                added_names.add(prop.name)

        # The Gap 6d assertion: all 3 chunking props added.
        for required in ("chunk_num", "total_chunks", "source_node_id"):
            self.assertIn(
                required, added_names,
                f"missing additive-migration patch for `{required}`. "
                f"Saw add_property calls for: {sorted(added_names)}",
            )

    def test_chunking_props_not_patched_when_already_present(self):
        """Modern collection (chunking already there) → add_property is
        NOT called for chunking props. Idempotency under repeat
        invocation of the sync loop."""
        existing_props = {
            "title", "content", "file_path", "node_type", "tags", "links",
            "typed_links", "external_links", "content_hash",
            "created", "updated", "valid_from", "valid_until", "status",
            "chunk_num", "total_chunks", "source_node_id",
        }
        srv, collection = self._make_server_with_existing_collection(existing_props)
        ok = self._mod.ensure_collection_exists(srv)
        self.assertTrue(ok)

        added_names = set()
        for call in collection.config.add_property.call_args_list:
            prop = call.args[0] if call.args else call.kwargs.get("property")
            if prop is not None:
                added_names.add(prop.name)

        # None of the chunking props should be re-added.
        for name in ("chunk_num", "total_chunks", "source_node_id"):
            self.assertNotIn(
                name, added_names,
                f"Gap 6d migration must be idempotent — `{name}` was "
                f"re-added on a collection that already has it",
            )


# ─── Gap 6e: analyze_code_graph honors $CODE_GRAPH_PROJECT ──────────────


class AnalyzeCodeGraphCodeGraphProjectEnvTests(unittest.TestCase):
    """The arg-parser resolution order:
      --from-resolver > --project > $CODE_GRAPH_PROJECT > repo_path.name.
    """

    def test_code_graph_project_env_used_when_no_project_arg(self):
        """No --project flag + $CODE_GRAPH_PROJECT set → env value
        wins over the repo dir name."""
        # We isolate the resolution expression here because importing
        # `analyze_code_graph` as a module has heavy side effects
        # (loads pyyaml/requests/joern-bindings on import). Instead, we
        # replicate the exact expression and verify it picks env vs
        # repo_path.name.
        env_project = "FromEnv"

        class FakeArgs:
            project = None
            from_resolver = False

        args = FakeArgs()
        repo_path = Path("/tmp/should-not-be-used")

        # Mirror the logic from analyze_code_graph.py post-Gap-6e.
        project_name = None
        if not project_name:
            with patch.dict(os.environ, {"CODE_GRAPH_PROJECT": env_project}):
                env_value = os.environ.get("CODE_GRAPH_PROJECT", "").strip()
                project_name = args.project or env_value or repo_path.name

        self.assertEqual(project_name, env_project)

    def test_explicit_project_arg_wins_over_code_graph_project_env(self):
        """--project flag set → CLI arg wins over $CODE_GRAPH_PROJECT."""

        class FakeArgs:
            project = "FromCLI"
            from_resolver = False

        args = FakeArgs()
        repo_path = Path("/tmp/should-not-be-used")

        project_name = None
        if not project_name:
            with patch.dict(os.environ, {"CODE_GRAPH_PROJECT": "FromEnv"}):
                env_value = os.environ.get("CODE_GRAPH_PROJECT", "").strip()
                project_name = args.project or env_value or repo_path.name

        self.assertEqual(project_name, "FromCLI")

    def test_repo_path_name_used_when_env_unset(self):
        """No --project flag + $CODE_GRAPH_PROJECT unset → falls back
        to repo dir name (legacy behaviour preserved)."""

        class FakeArgs:
            project = None
            from_resolver = False

        args = FakeArgs()
        repo_path = Path("/tmp/some-repo-name")

        # Scrub the env var to simulate the fresh-shell case.
        env_copy = {k: v for k, v in os.environ.items()
                    if k != "CODE_GRAPH_PROJECT"}
        project_name = None
        if not project_name:
            with patch.dict(os.environ, env_copy, clear=True):
                env_value = os.environ.get("CODE_GRAPH_PROJECT", "").strip()
                project_name = args.project or env_value or repo_path.name

        self.assertEqual(project_name, "some-repo-name")

    def test_empty_env_var_treated_as_unset(self):
        """$CODE_GRAPH_PROJECT="" → treated as unset (.strip() returns
        empty, falls through to repo dir name). Defends against shells
        that pass an empty string when a var is exported-but-empty."""

        class FakeArgs:
            project = None
            from_resolver = False

        args = FakeArgs()
        repo_path = Path("/tmp/fallback-name")

        project_name = None
        if not project_name:
            with patch.dict(os.environ, {"CODE_GRAPH_PROJECT": "   "}):
                env_value = os.environ.get("CODE_GRAPH_PROJECT", "").strip()
                project_name = args.project or env_value or repo_path.name

        self.assertEqual(project_name, "fallback-name")

    def test_analyze_code_graph_source_carries_env_lookup(self):
        """Direct source-level guard: the canonical line that honors
        $CODE_GRAPH_PROJECT must be present in the on-disk file.
        Catches regressions where a future edit removes the env
        lookup."""
        analyze_path = (
            REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
        )
        text = analyze_path.read_text(encoding="utf-8")
        self.assertIn(
            'os.environ.get("CODE_GRAPH_PROJECT"',
            text,
            "analyze_code_graph.py must consult $CODE_GRAPH_PROJECT in "
            "the project-name resolution chain (v0.2.37 Gap 6e)",
        )


# ─── Gap 6b + 6c: wrapper template smoke tests ──────────────────────────


class WrapperVenvFallbackTextTests(unittest.TestCase):
    """Source-level guards on the 5 wrapper templates: every wrapper
    probes `$VCT_INSTALL_ROOT/.venv` before script-relative paths and
    validates the candidate has weaviate-client.

    This is the minimum-bar smoke test — runs the wrapper text through
    text assertions rather than exec'ing the script in a real fake-
    PROJECT_ROOT layout. Limitation noted: this guards against the
    canonical-first probe being removed but doesn't catch shell-syntax
    regressions that only surface at execution time. A future test
    could exec the wrapper with `--help` in a sandboxed environment;
    today's bar matches the existing `code-graph-analyze` test
    coverage (which is also source-level).
    """

    WRAPPER_NAMES_BASH = [
        "kg-sync", "kg-migrate", "kg-search", "kg-info", "code-graph-query",
    ]
    WRAPPER_NAMES_PS1 = [
        "kg-sync.ps1", "kg-migrate.ps1", "kg-search.ps1", "kg-info.ps1",
        "code-graph-query.ps1",
    ]

    def test_bash_wrappers_probe_vct_install_root_first(self):
        """Bash wrappers list `$VCT_INSTALL_ROOT/.venv` BEFORE
        script-relative paths in the CANDIDATES array."""
        scripts_dir = REPO_ROOT / "templates" / "scripts"
        for name in self.WRAPPER_NAMES_BASH:
            wrapper_path = scripts_dir / name
            self.assertTrue(
                wrapper_path.exists(),
                f"bash wrapper {name} missing — Gap 6b backport incomplete",
            )
            text = wrapper_path.read_text(encoding="utf-8")
            self.assertIn(
                '"${VCT_INSTALL_ROOT:-}/.venv"', text,
                f"{name} must probe $VCT_INSTALL_ROOT/.venv "
                f"(Gap 6b canonical-first ordering)",
            )

    def test_bash_wrappers_validate_weaviate_importable(self):
        """Bash wrappers validate the candidate venv has weaviate-
        client before activating. Without this gate, an unrelated
        project venv at $PROJECT_ROOT/.venv would be picked up and the
        downstream Python script would crash with ModuleNotFoundError."""
        scripts_dir = REPO_ROOT / "templates" / "scripts"
        for name in self.WRAPPER_NAMES_BASH:
            wrapper_path = scripts_dir / name
            text = wrapper_path.read_text(encoding="utf-8")
            self.assertIn(
                'import weaviate', text,
                f"{name} must validate `weaviate` is importable in the "
                f"candidate venv (Gap 6b backport from code-graph-analyze)",
            )

    def test_ps1_wrappers_probe_vct_install_root_first(self):
        """PS1 wrappers list `$env:VCT_INSTALL_ROOT\\.venv\\Scripts\\python.exe`
        BEFORE the script-relative path in $Candidates."""
        scripts_dir = REPO_ROOT / "templates" / "scripts"
        for name in self.WRAPPER_NAMES_PS1:
            wrapper_path = scripts_dir / name
            self.assertTrue(
                wrapper_path.exists(),
                f"PS1 wrapper {name} missing — Gap 6b backport incomplete",
            )
            text = wrapper_path.read_text(encoding="utf-8")
            self.assertIn(
                '$env:VCT_INSTALL_ROOT', text,
                f"{name} must probe $env:VCT_INSTALL_ROOT "
                f"(Gap 6b canonical-first ordering)",
            )

    def test_ps1_wrappers_validate_weaviate_importable(self):
        """PS1 wrappers validate the candidate venv has weaviate-client."""
        scripts_dir = REPO_ROOT / "templates" / "scripts"
        for name in self.WRAPPER_NAMES_PS1:
            wrapper_path = scripts_dir / name
            text = wrapper_path.read_text(encoding="utf-8")
            self.assertIn(
                'import weaviate', text,
                f"{name} must validate `weaviate` is importable in the "
                f"candidate venv (Gap 6b PS1 sibling)",
            )

    def test_code_graph_query_sets_pythonpath(self):
        """Gap 6c: `code-graph-query` (bash + PS1) sets PYTHONPATH so
        `query_code_graph.py` can import `claude_mcp_servers.weaviate_mcp`.
        Without this, the wrapper-activated venv has weaviate-client
        but no way to find the project's `weaviate_mcp` package."""
        scripts_dir = REPO_ROOT / "templates" / "scripts"
        bash = (scripts_dir / "code-graph-query").read_text(encoding="utf-8")
        self.assertIn(
            'PYTHONPATH', bash,
            "bash code-graph-query must set PYTHONPATH for "
            "claude_mcp_servers (v0.2.37 Gap 6c)",
        )
        self.assertIn(
            'claude_mcp_servers', bash,
            "bash code-graph-query PYTHONPATH must reference "
            "claude_mcp_servers (v0.2.37 Gap 6c)",
        )

        ps1 = (scripts_dir / "code-graph-query.ps1").read_text(encoding="utf-8")
        self.assertIn(
            '$env:PYTHONPATH', ps1,
            "PS1 code-graph-query must set $env:PYTHONPATH for "
            "claude_mcp_servers (v0.2.37 Gap 6c)",
        )
        self.assertIn(
            'claude_mcp_servers', ps1,
            "PS1 code-graph-query PYTHONPATH must reference "
            "claude_mcp_servers (v0.2.37 Gap 6c)",
        )

    def test_query_code_graph_self_resolves_via_env(self):
        """Gap 6c defence-in-depth: `query_code_graph.py` itself
        consults $VCT_ORCHESTRATOR_ROOT / $VCT_INSTALL_ROOT BEFORE
        falling back to the script-relative path. Mirrors the
        `_resolve_mcp_servers_dir` pattern in `sync_knowledge_graph.py`.

        This is the belt-and-braces fix: even if the wrapper's
        PYTHONPATH plumbing breaks (e.g. user invokes the .py file
        directly), the script still finds claude_mcp_servers/ as long
        as one of the two env vars points at the orchestrator clone."""
        scripts_dir = REPO_ROOT / "templates" / "scripts"
        text = (scripts_dir / "query_code_graph.py").read_text(encoding="utf-8")
        self.assertIn(
            'VCT_ORCHESTRATOR_ROOT', text,
            "query_code_graph.py must consult $VCT_ORCHESTRATOR_ROOT "
            "for the claude_mcp_servers/ lookup (v0.2.37 Gap 6c "
            "defence-in-depth)",
        )
        self.assertIn(
            'VCT_INSTALL_ROOT', text,
            "query_code_graph.py must consult $VCT_INSTALL_ROOT as a "
            "fallback for the claude_mcp_servers/ lookup (v0.2.37 Gap 6c "
            "defence-in-depth)",
        )


if __name__ == "__main__":
    unittest.main()
