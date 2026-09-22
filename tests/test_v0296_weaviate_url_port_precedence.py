# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.96 — ``WEAVIATE_PORT`` is read, and read in exactly ONE home.

Two things are pinned here, and they fail for different reasons on purpose.

**1. The precedence itself** (``TestPrecedence``). ``WEAVIATE_PORT`` is a
declared, documented knob — ``docs/TROUBLESHOOTING.md`` tells the user to
change it in ``.env`` when 8081 is occupied, and ``BOOTSTRAP.md`` names it as
the way to give an install its own containers. Until v0.2.96
``weaviate_helpers.weaviate_url_default`` ignored it and jumped straight to
:data:`DEFAULT_WEAVIATE_PORT`, so the knob changed nothing for every caller
except ``install.py`` (which carried five hand-written copies that DID read
it). A user on 8082 therefore got 8081 from the shared helper — a connection
refused at best, and at worst a DIFFERENT install's Weaviate answering on the
canonical port.

**2. That the copies are gone** (``TestOneHome``). Fixing the helper is
worthless if the hand-written copies survive, so every migrated site is
pinned BEHAVIOURALLY: the test sets ``WEAVIATE_PORT`` and asserts the URL the
site actually addresses. A site that still spells its own fallback produces
8081 and goes red. This is deliberately not a source scan — a scan is
satisfied by the string appearing in a comment (see
``knowledge/concepts/``, "never guard wiring with a source scan").

Layering note, because the precedence LOOKS like it could outrank the DB and
must not: the Weaviate *instance* is machine-global, not per-project. The hub
serves ``LocalConfig::load().weaviate_url``; the per-row
``project_kg_bindings.weaviate_url`` column is preserved but read by no
resolver (stated in ``launcher/src-tauri/src/commands/binding_reconcile.rs``).
The launcher does not compete with these env vars — ``config_projection.py``
WRITES both of them from the DB-resolved port, so they are the transport of
the DB value, not a rival to it. See the docstring on
``weaviate_url_default`` for the full chain.
"""
from __future__ import annotations

import os
import sys
import textwrap
import pathlib
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import weaviate_helpers as _wh  # noqa: E402

SENTINEL_PORT = "19731"
SENTINEL_URL = f"http://localhost:{SENTINEL_PORT}"


class _PortEnv:
    """Context manager: ``WEAVIATE_PORT`` set, ``WEAVIATE_URL`` absent.

    The shape that used to be broken everywhere. Restores both keys.
    """

    def __init__(self, port: str = SENTINEL_PORT) -> None:
        self._port = port
        self._saved: "dict[str, str | None]" = {}

    def __enter__(self) -> "_PortEnv":
        for key in ("WEAVIATE_URL", "WEAVIATE_PORT"):
            self._saved[key] = os.environ.get(key)
        os.environ.pop("WEAVIATE_URL", None)
        os.environ["WEAVIATE_PORT"] = self._port
        return self

    def __exit__(self, *_exc) -> None:
        for key, val in self._saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


class TestPrecedence(unittest.TestCase):
    """WEAVIATE_URL > WEAVIATE_PORT > DEFAULT_WEAVIATE_PORT."""

    def setUp(self) -> None:
        self._saved = {
            k: os.environ.get(k) for k in ("WEAVIATE_URL", "WEAVIATE_PORT")
        }
        for k in ("WEAVIATE_URL", "WEAVIATE_PORT"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_level_3_neither_set_uses_the_constant(self):
        self.assertEqual(
            _wh.weaviate_url_default(),
            f"http://localhost:{_wh.DEFAULT_WEAVIATE_PORT}",
        )

    def test_level_2_port_alone_is_honoured(self):
        """The regression this release exists to fix."""
        os.environ["WEAVIATE_PORT"] = "8082"
        self.assertEqual(_wh.weaviate_url_default(), "http://localhost:8082")

    def test_level_1_url_alone_is_used_verbatim(self):
        os.environ["WEAVIATE_URL"] = "https://weaviate.internal:9443/"
        self.assertEqual(
            _wh.weaviate_url_default(), "https://weaviate.internal:9443/"
        )

    def test_both_set_url_wins_even_when_the_ports_disagree(self):
        """THE case a future editor gets wrong.

        A full URL is the stronger statement: it names scheme, host AND
        port. Rewriting its port from ``WEAVIATE_PORT`` would make
        ``WEAVIATE_URL`` unable to mean what it says, so the disagreement is
        not reconciled — it is ranked.
        """
        os.environ["WEAVIATE_URL"] = "http://other-host:7777"
        os.environ["WEAVIATE_PORT"] = "8082"
        self.assertEqual(_wh.weaviate_url_default(), "http://other-host:7777")

    def test_empty_url_is_unset_not_a_literal_and_falls_to_the_port(self):
        """``WEAVIATE_URL=`` in a .env is a mis-populated var, not a request
        for the empty URL — the same coercion ``KG_COLLECTION`` gets."""
        os.environ["WEAVIATE_URL"] = ""
        os.environ["WEAVIATE_PORT"] = "8082"
        self.assertEqual(_wh.weaviate_url_default(), "http://localhost:8082")

    def test_both_empty_falls_all_the_way_to_the_constant(self):
        os.environ["WEAVIATE_URL"] = ""
        os.environ["WEAVIATE_PORT"] = ""
        self.assertEqual(
            _wh.weaviate_url_default(),
            f"http://localhost:{_wh.DEFAULT_WEAVIATE_PORT}",
        )

    def test_surrounding_whitespace_is_stripped_at_both_levels(self):
        """A .env written on Windows and sourced on Linux carries ``\\r``;
        pasting that into the URL breaks every request built from it."""
        os.environ["WEAVIATE_PORT"] = " 8082\r"
        self.assertEqual(_wh.weaviate_url_default(), "http://localhost:8082")
        os.environ["WEAVIATE_URL"] = "  http://h:1\r\n"
        self.assertEqual(_wh.weaviate_url_default(), "http://h:1")

    def test_a_nonnumeric_port_is_interpolated_not_discarded(self):
        """Deliberate: a typo must fail LOUDLY at connect time.

        Silently resolving a bad port back to 8081 would send the caller to
        whatever else is on the canonical port — the exact harm level 2
        exists to prevent.
        """
        os.environ["WEAVIATE_PORT"] = "eighty-eighty-two"
        self.assertEqual(
            _wh.weaviate_url_default(), "http://localhost:eighty-eighty-two"
        )

    def test_the_environment_is_re_read_on_every_call(self):
        """Never cached at import — hooks and tests mutate os.environ
        between calls and must see the live value."""
        os.environ["WEAVIATE_PORT"] = "8082"
        first = _wh.weaviate_url_default()
        os.environ["WEAVIATE_PORT"] = "8083"
        second = _wh.weaviate_url_default()
        self.assertEqual(first, "http://localhost:8082")
        self.assertEqual(second, "http://localhost:8083")


class TestOneHome(unittest.TestCase):
    """Every migrated site resolves through the shared helper.

    Each assertion is behavioural: with ``WEAVIATE_PORT`` set and
    ``WEAVIATE_URL`` unset, a migrated site addresses the sentinel port. An
    un-migrated copy addresses 8081 and fails.
    """

    # ── the four module-local aliases ────────────────────────────────────
    # Kept as patchable module attributes rather than hard-moved: dozens of
    # tests patch these names, and a sibling helper calling the MODULE-LOCAL
    # name would stop being intercepted if the name vanished. See
    # knowledge/concepts/weaviate-helper-convergence-mock-surface-pattern.md.

    def test_project_init_alias_delegates(self):
        from vco_lib import project_init
        with _PortEnv():
            self.assertEqual(project_init._weaviate_url_default(), SENTINEL_URL)

    def test_weaviate_schema_alias_delegates(self):
        from vco_lib import weaviate_schema
        with _PortEnv():
            self.assertEqual(
                weaviate_schema._weaviate_url_default(), SENTINEL_URL
            )

    def test_install_weaviate_alias_delegates(self):
        from vco_lib import install_weaviate
        with _PortEnv():
            self.assertEqual(
                install_weaviate._weaviate_url_default(), SENTINEL_URL
            )

    def test_embedding_enrichment_alias_delegates(self):
        """Only the trailing-slash trim is local; the resolution is not."""
        from vco_lib import embedding_enrichment
        with _PortEnv():
            self.assertEqual(embedding_enrichment._weaviate_url(), SENTINEL_URL)

    def test_the_aliases_are_not_a_second_copy_of_the_body(self):
        """Patching the ONE home changes every alias — which is what makes
        it one home rather than four agreeing implementations."""
        from vco_lib import (
            embedding_enrichment,
            install_weaviate,
            project_init,
            weaviate_schema,
        )
        with mock.patch.object(
            _wh, "weaviate_url_default", return_value="http://patched:1"
        ):
            self.assertEqual(
                project_init._weaviate_url_default(), "http://patched:1"
            )
            self.assertEqual(
                weaviate_schema._weaviate_url_default(), "http://patched:1"
            )
            self.assertEqual(
                install_weaviate._weaviate_url_default(), "http://patched:1"
            )
            self.assertEqual(
                embedding_enrichment._weaviate_url(), "http://patched:1"
            )

    # ── the inline call-sites, probed through the wire ───────────────────

    @staticmethod
    def _capture_url(module, attr_path: str = "urllib.request.urlopen"):
        """Patch *module*'s urlopen to record the URL and then soft-fail.

        Every probed site wraps its request in ``except Exception: return``,
        so raising is the cheapest way to stop after the URL is built.
        """
        seen: "list[str]" = []

        def _fake(url, *a, **kw):
            seen.append(url if isinstance(url, str) else getattr(url, "full_url", ""))
            raise OSError("probe stop")

        return seen, mock.patch(f"{module}.{attr_path}", _fake)

    def test_install_emit_lowercase_codegraph_cleanup_deferrals(self):
        import install
        seen, patcher = self._capture_url("install")
        with _PortEnv(), patcher:
            install._emit_lowercase_codegraph_cleanup_deferrals(mock.MagicMock())
        self.assertTrue(seen, "the site never issued a request")
        self.assertIn(SENTINEL_PORT, seen[0], seen)

    def test_install_emit_orchestrator_root_schema_deferrals(self):
        import install
        seen, patcher = self._capture_url("install")
        with _PortEnv(), patcher:
            install._emit_orchestrator_root_schema_deferrals(mock.MagicMock())
        self.assertTrue(seen, "the site never issued a request")
        self.assertIn(SENTINEL_PORT, seen[0], seen)

    def test_install_weaviate_detect_legacy_shared_kg_class(self):
        from vco_lib import install_weaviate
        seen, patcher = self._capture_url("urllib.request", "urlopen")
        with _PortEnv(), patcher:
            install_weaviate.detect_legacy_shared_kg_class(mock.MagicMock())
        self.assertTrue(seen, "the site never issued a request")
        self.assertIn(SENTINEL_PORT, seen[0], seen)

    def test_project_init_bundle_update_pointer_heal(self):
        """The site reaches the wire, so the assertion can actually fail.

        This test used to guard its only assertion behind ``if seen:`` and
        note that the function "returns early when the launcher DB is
        absent". Under the suite's redirected state dir the DB is ALWAYS
        absent, so ``seen`` was always empty and the test could not fail for
        any implementation — it was counted as covering this call-site while
        asserting nothing. The precondition is one empty file, so the honest
        fix is to satisfy it rather than to tolerate skipping it.
        """
        import tempfile
        from unittest import mock as _mock

        from vco_lib import project_init

        seen, patcher = self._capture_url("vco_lib.project_init")
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "launcher.db").touch()  # existence is all the gate checks
            with _mock.patch("vco_lib.paths.vct_root_dir", return_value=root):
                with _PortEnv(), patcher:
                    project_init._bundle_update_pointer_heal()
        self.assertTrue(
            seen,
            "the site never issued a request — the early-return gates moved, "
            "so this test is measuring nothing again",
        )
        self.assertIn(SENTINEL_PORT, seen[0], seen)


class TestMcpMirrorParity(unittest.TestCase):
    """The ONE sanctioned mirror agrees with the shared home, on every case.

    ``claude_mcp_servers/weaviate_mcp/server.py`` deliberately does not
    import ``vco_lib`` — it boots on half-installed environments where the
    import would fail, so a boot-critical dependency there would trade a
    real failure mode for a cosmetic convergence. That makes its copy a
    category-C mirror, and the repo's rule for category C is that a parity
    test locks it to the original.

    The mirror is re-evaluated here rather than imported, because the MCP
    binds ``WEAVIATE_URL`` once at module import and importing the server
    module drags in ``weaviate`` + the whole MCP surface. What is pinned is
    the EXPRESSION, extracted from the shipped source, so an edit to either
    side that changes behaviour fails this test.
    """

    MIRROR_SRC = (
        REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
    )

    CASES = (
        {},
        {"WEAVIATE_PORT": "8082"},
        {"WEAVIATE_URL": "http://h:1"},
        {"WEAVIATE_URL": "http://h:1", "WEAVIATE_PORT": "8082"},
        {"WEAVIATE_URL": "", "WEAVIATE_PORT": "8082"},
        {"WEAVIATE_URL": "", "WEAVIATE_PORT": ""},
        {"WEAVIATE_URL": "  ", "WEAVIATE_PORT": " 8082 "},
    )

    def _mirror_expression(self) -> str:
        """The three mirror lines, lifted verbatim from the shipped file."""
        text = self.MIRROR_SRC.read_text(encoding="utf-8")
        lines = [
            ln for ln in text.splitlines()
            if ln.startswith(
                ("_WEAVIATE_URL_ENV =", "_WEAVIATE_PORT_ENV =", "WEAVIATE_URL =")
            )
        ]
        self.assertEqual(
            len(lines), 3,
            "the MCP mirror's shape changed; re-derive this parity test "
            f"against {self.MIRROR_SRC}",
        )
        return "\n".join(lines)

    def test_mirror_matches_the_shared_home_on_every_case(self):
        expr = self._mirror_expression()
        saved = {
            k: os.environ.get(k) for k in ("WEAVIATE_URL", "WEAVIATE_PORT")
        }
        try:
            for case in self.CASES:
                for k in ("WEAVIATE_URL", "WEAVIATE_PORT"):
                    os.environ.pop(k, None)
                os.environ.update(case)
                scope: "dict[str, object]" = {"os": os}
                exec(expr, scope)  # noqa: S102 — our own shipped source
                self.assertEqual(
                    scope["WEAVIATE_URL"],
                    _wh.weaviate_url_default(),
                    f"MCP mirror diverged from the shared home for {case!r}. "
                    "Both must change together — see the comment block above "
                    "the mirror in weaviate_mcp/server.py.",
                )
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_the_mirror_names_its_shared_home(self):
        """A mirror without a pointer home is how the next editor creates a
        third copy."""
        text = self.MIRROR_SRC.read_text(encoding="utf-8")
        self.assertIn("vco_lib/weaviate_helpers.py", text)
        self.assertIn("weaviate_url_default", text)


class TestShippedMirrorParity(unittest.TestCase):
    """Every SHIPPED category-C mirror agrees with the shared home.

    These six sites cannot call ``weaviate_url_default``, each for a reason
    recorded above the mirror in the file itself:

    * ``detect_duplicates.py`` — module-level constant, and the file
      deliberately keeps NO module-level ``vco_lib`` dependency so that
      ``--help`` and an argparse error do not require it.
    * ``search_knowledge.py`` / ``get_node_info.py`` — module-level constants
      evaluated BEFORE anything puts ``vco_lib`` on ``sys.path``; every
      ``vco_lib`` use in those files is function-local and guarded.
    * ``generate-kg-summary.py`` — treats ``vco_lib`` as optional throughout
      (its only use is a ``try/except Exception`` import).
    * ``session-start-retrieval-health.{sh,ps1}`` — stdlib-only Python
      embedded in a hook, run under whatever interpreter ``find-python``
      resolves, which is not guaranteed to be the VCO venv.

    The sibling scripts that DO hard-import ``vco_lib`` at module scope
    (``process_documents``, ``maintain_knowledge_graph``,
    ``sync_knowledge_graph``, ``query_code_graph``, ``generate-code-summary``)
    are deliberately absent from this list: they CALL the shared home, and
    ``TestShippedCallers`` pins that instead.

    As in :class:`TestMcpMirrorParity`, the EXPRESSION is lifted from the
    shipped source and executed — importing these modules would drag in
    ``weaviate`` and, for the hooks, is not possible at all. An edit to
    either side that changes behaviour fails here.
    """

    #: ``(relative path, trailing transform applied by the mirror)``. The two
    #: hooks ``.rstrip("/")`` their result, so the expectation must too —
    #: that is a real difference in the shipped line, not a drift.
    MIRRORS = (
        ("templates/scripts/detect_duplicates.py", False),
        ("templates/scripts/search_knowledge.py", False),
        ("templates/scripts/get_node_info.py", False),
        ("templates/scripts/generate-kg-summary.py", False),
        ("templates/hooks/session-start-retrieval-health.sh", True),
        ("templates/hooks/session-start-retrieval-health.ps1", True),
    )

    _PREFIXES = ("_weaviate_url_env =", "_weaviate_port_env =", "weaviate_url =")

    CASES = TestMcpMirrorParity.CASES

    def _mirror_expression(self, rel: str) -> str:
        """The three mirror lines, lifted verbatim and dedented."""
        src = REPO_ROOT / rel
        lines = [
            ln for ln in src.read_text(encoding="utf-8").splitlines()
            # Name-prefix AND a resolution marker on the RHS: a downstream
            # consumer such as ``weaviate_url = urlparse(WEAVIATE_URL)``
            # shares the prefix but is not part of the mirror.
            if ln.lstrip().lower().startswith(self._PREFIXES)
            and ("os.getenv(" in ln or "os.environ.get(" in ln
                 or "http://localhost:" in ln)
        ]
        self.assertEqual(
            len(lines), 3,
            f"the mirror in {rel} changed shape (found {len(lines)} lines, "
            "expected 3); re-derive this parity test against the file",
        )
        return textwrap.dedent("\n".join(lines))

    def test_every_shipped_mirror_matches_the_shared_home(self):
        saved = {
            k: os.environ.get(k) for k in ("WEAVIATE_URL", "WEAVIATE_PORT")
        }
        try:
            for rel, rstrips in self.MIRRORS:
                expr = self._mirror_expression(rel)
                for case in self.CASES:
                    for k in ("WEAVIATE_URL", "WEAVIATE_PORT"):
                        os.environ.pop(k, None)
                    os.environ.update(case)
                    scope: "dict[str, object]" = {"os": os}
                    exec(expr, scope)  # noqa: S102 — our own shipped source
                    expected = _wh.weaviate_url_default()
                    if rstrips:
                        expected = expected.rstrip("/")
                    self.assertEqual(
                        scope["WEAVIATE_URL"], expected,
                        f"{rel} diverged from the shared home for {case!r}. "
                        "Both must change together — see the MIRROR comment "
                        "block above the expression in that file.",
                    )
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_every_mirror_names_its_shared_home(self):
        """A mirror without a pointer home is how the next editor creates a
        third copy."""
        for rel, _ in self.MIRRORS:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("vco_lib/weaviate_helpers.py", text, rel)
            self.assertIn("weaviate_url_default", text, rel)

    def test_no_source_file_still_spells_the_old_url_only_fallback(self):
        """The defect shape itself, banned across the shipped + library trees.

        Reading ``WEAVIATE_URL`` with the canonical port as its literal
        fallback is the exact expression that ignored ``WEAVIATE_PORT``.

        **This is a completeness check, NOT the behavioural proof** — the
        proof is the parity loop above plus ``TestShippedCallers`` and
        ``TestParameterDefaultsConsultTheEnvironment``, which observe the URL
        each site actually addresses. A scan can only stop the old shape
        being reintroduced by copy-paste; it can never show the new one
        works (see "never guard wiring with a source scan"). It earns its
        place because that copy-paste is precisely how this defect spread to
        ~20 sites across three trees in the first place.
        """
        offenders = []
        for path in sorted(
            list((REPO_ROOT / "templates" / "scripts").rglob("*.py"))
            + list((REPO_ROOT / "templates" / "hooks").glob("*"))
            # vco_lib + the MCP maintenance scripts are swept too: the same
            # copy-paste produced instances in BOTH trees, and two of them
            # (`vco_lib/cli/verify_diagrams.py`, `claude_mcp_servers/scripts/
            # migrate_to_new_embeddings.py`) were found by this scan rather
            # than by the inventory the lane was handed.
            + list((REPO_ROOT / "vco_lib").rglob("*.py"))
            + list((REPO_ROOT / "claude_mcp_servers").rglob("*.py"))
        ):
            # The shared home's own docstring quotes the precedence it
            # implements; the MCP mirror names the shape it replaced.
            if path.name in {"weaviate_helpers.py"}:
                continue
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for pattern in (
                'os.getenv("WEAVIATE_URL", "http://localhost:8081")',
                'os.environ.get("WEAVIATE_URL", "http://localhost:8081")',
            ):
                if pattern in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {pattern}")
        self.assertEqual(
            offenders, [],
            "a source file still resolves the Weaviate URL from "
            "WEAVIATE_URL alone, ignoring WEAVIATE_PORT. Either call "
            "vco_lib.weaviate_helpers.weaviate_url_default (preferred, when "
            "vco_lib is already a hard dependency of that file) or add a "
            "category-C mirror and register it in "
            "TestShippedMirrorParity.MIRRORS:\n  " + "\n  ".join(offenders),
        )


class TestShippedCallers(unittest.TestCase):
    """The five shipped scripts that CALL the shared home actually do.

    The brief that produced these sites assumed every ``templates/scripts``
    file treats ``vco_lib`` as optional. Four of them do not: they import
    ``vco_lib`` UNGUARDED at module scope, above the constant, so a mirror
    there would have been a third copy defending a boundary that does not
    exist — the failure mode recorded in
    ``knowledge/concepts/analyzer-runs-under-install-venv-vco-lib-importable-2026-07-09.md``.
    The fifth (``generate-code-summary.py``) is gated by ``_collection_prefix``,
    which returns None and makes ``run()`` a no-op when vco_lib is absent, so
    by the time ``_connect_weaviate`` runs the import is proven.

    Pinned BEHAVIOURALLY (import the module under a sentinel port and read
    what it resolved), never by a source scan.
    """

    #: ``(tree, module)`` whose module-level ``WEAVIATE_URL`` constant must
    #: track the environment. ``generate-code-summary.py`` and
    #: ``query_code_graph.py`` are covered separately below — the former
    #: resolves inside a function, the latter behind a config-file branch.
    MODULE_LEVEL = (
        ("templates/scripts", "process_documents"),
        ("templates/scripts", "maintain_knowledge_graph"),
        ("templates/scripts", "sync_knowledge_graph"),
        # Found by the completeness scan, not by the handed inventory. Both
        # already import `vco_lib.log_setup` BARE at module scope — with a
        # comment saying a failed vco_lib import here is a broken install —
        # so a mirror would have defended a boundary that does not exist.
        ("claude_mcp_servers/scripts", "repair_kg_typed_links"),
        ("claude_mcp_servers/scripts", "migrate_to_new_embeddings"),
    )

    def test_module_level_constants_follow_weaviate_port(self):
        for extra in (
            REPO_ROOT / "templates" / "scripts",
            REPO_ROOT / "claude_mcp_servers",
            REPO_ROOT / "claude_mcp_servers" / "scripts",
        ):
            if str(extra) not in sys.path:
                sys.path.insert(0, str(extra))
        import importlib

        for where, name in self.MODULE_LEVEL:
            with self.subTest(script=f"{where}/{name}.py"), _PortEnv():
                sys.modules.pop(name, None)
                # NOT wrapped in a skip-on-ImportError: every one of these is
                # importable in this suite (verified), and a silent skip here
                # would turn a real regression into a green run.
                mod = importlib.import_module(name)
                self.assertEqual(
                    mod.WEAVIATE_URL, SENTINEL_URL,
                    f"{where}/{name}.py ignored WEAVIATE_PORT — it "
                    "must call vco_lib.weaviate_helpers.weaviate_url_default",
                )
                sys.modules.pop(name, None)

    def test_generate_code_summary_connect_uses_the_shared_home(self):
        """``_connect_weaviate`` must address the env-resolved URL.

        Red-proof shape: ``weaviate.connect_to_local`` is replaced with a
        recorder, so what is asserted is the host/port the function actually
        TARGETS, not the source text.
        """
        import importlib.util

        src = REPO_ROOT / "templates" / "scripts" / "generate-code-summary.py"
        spec = importlib.util.spec_from_file_location("_gcs_under_test", src)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        seen: "dict[str, object]" = {}

        class _FakeWeaviate:
            @staticmethod
            def connect_to_local(**kw):
                seen.update(kw)
                return "client"

        with _PortEnv():
            with mock.patch.dict(sys.modules, {"weaviate": _FakeWeaviate}):
                self.assertEqual(mod._connect_weaviate(), "client")
        self.assertEqual(seen.get("port"), int(SENTINEL_PORT))
        self.assertEqual(seen.get("host"), "localhost")

    def test_query_code_graph_no_config_branch_reads_the_env(self):
        """The stock-install branch (no ``mcp-config.json``) must read env.

        This site is a DIFFERENT defect from the rest of the family: it read
        no environment variable at all, and because ``mcp-config.json`` does
        not exist on a stock install, the hardcoded branch was the DEFAULT
        one. The module binds ``WEAVIATE_URL`` at import, so the import
        happens inside the sentinel env with the config path forced absent.
        """
        import importlib

        mcp_dir = REPO_ROOT / "claude_mcp_servers"
        scripts_dir = REPO_ROOT / "templates" / "scripts"
        for extra in (str(scripts_dir), str(mcp_dir)):
            if extra not in sys.path:
                sys.path.insert(0, extra)

        with _PortEnv():
            # Point $VCT_CLAUDE_DIR at an empty dir so CONFIG_PATH is absent
            # (the stock-install shape) rather than the developer's real
            # ~/.claude, which may carry an mcp-config.json.
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.dict(os.environ, {"VCT_CLAUDE_DIR": tmp}):
                    sys.modules.pop("query_code_graph", None)
                    # NOT wrapped in skip-on-ImportError (F-8): the module is
                    # importable in this suite, and a silent skip here would
                    # turn a real import regression into a green run — the
                    # same reason the sibling arm in MODULE_LEVEL was removed.
                    mod = importlib.import_module("query_code_graph")
                    try:
                        self.assertEqual(
                            mod.WEAVIATE_URL, SENTINEL_URL,
                            "query_code_graph's no-config branch still "
                            "hardcodes the URL; it must call "
                            "vco_lib.weaviate_helpers.weaviate_url_default",
                        )
                    finally:
                        sys.modules.pop("query_code_graph", None)

    def test_query_code_graph_env_outranks_the_config_file(self):
        """v0.2.96 (6b): the environment outranks ``mcp-config.json``.

        The file branch used to WIN over ``WEAVIATE_URL``/``WEAVIATE_PORT``,
        inverted relative to every peer resolver (``config.rs`` resolves
        default -> toml -> ``VCT_WEAVIATE_URL`` -> ``WEAVIATE_URL``). With a
        canary config present AND ``WEAVIATE_PORT`` set (``WEAVIATE_URL``
        unset), the module must resolve the sentinel — proving the port knob
        reaches the site even on the config branch.
        """
        import importlib
        import json
        import tempfile

        mcp_dir = REPO_ROOT / "claude_mcp_servers"
        scripts_dir = REPO_ROOT / "templates" / "scripts"
        for extra in (str(scripts_dir), str(mcp_dir)):
            if extra not in sys.path:
                sys.path.insert(0, extra)

        with _PortEnv():
            with tempfile.TemporaryDirectory() as tmp:
                cfg = (
                    pathlib.Path(tmp) / "workflow" / "config" / "mcp-config.json"
                )
                cfg.parent.mkdir(parents=True)
                cfg.write_text(
                    json.dumps({
                        "weaviate": {
                            "url": "http://localhost:42424",
                            "grpc_port": 50052,
                        },
                    }),
                    encoding="utf-8",
                )
                with mock.patch.dict(os.environ, {"VCT_CLAUDE_DIR": tmp}):
                    sys.modules.pop("query_code_graph", None)
                    # NOT wrapped in skip-on-ImportError (see F-8 above).
                    mod = importlib.import_module("query_code_graph")
                    try:
                        self.assertEqual(
                            mod.WEAVIATE_URL, SENTINEL_URL,
                            "query_code_graph let mcp-config.json outrank "
                            "the environment; env must win, matching every "
                            "peer resolver",
                        )
                    finally:
                        sys.modules.pop("query_code_graph", None)


class TestDiagramIndexerResolvesTheInstance(unittest.TestCase):
    """6e — ``diagram_indexer``'s two Weaviate seams resolve through the home.

    Both used to read ``os.environ.get("WEAVIATE_URL")`` alone: ``None``
    when unset (a silent "skipped" that read as success) and blind to
    ``WEAVIATE_PORT``. Pinned behaviourally at the seam both sites share —
    ``_validate_weaviate_url`` is replaced with a recorder that stops the
    function before any client is built, so what is asserted is the URL
    the function actually resolved, never a source text.
    """

    def _recorded_url(self, call):
        from vco_lib import diagram_indexer as di

        seen: "dict[str, str]" = {}

        class _Stop(Exception):
            pass

        def _rec(url):
            seen["url"] = url
            raise _Stop

        with mock.patch.object(di, "_validate_weaviate_url", _rec):
            with self.assertRaises(_Stop):
                call(di)
        return seen.get("url")

    def test_delete_targets_the_env_resolved_instance(self):
        with _PortEnv():
            url = self._recorded_url(
                lambda di: di._weaviate_delete_by_file_path(
                    "some/diagram.mmd",
                    weaviate_url=None,
                    collection_name="Acme_Diagrams",
                )
            )
        self.assertEqual(
            url, SENTINEL_URL,
            "diagram_indexer delete ignored WEAVIATE_PORT — it must resolve "
            "through vco_lib.weaviate_helpers.weaviate_url_default",
        )

    def test_upsert_targets_the_env_resolved_instance(self):
        from vco_lib import diagram_indexer as di

        row = di.DiagramRow(
            project_id="acme",
            diagram_name="d",
            diagram_type="mermaid",
            file_path="some/diagram.mmd",
            category_path="x",
            enabled=1,
            inferred_title=None,
            diagram_kind=None,
            content_text=None,
            node_count=None,
            edge_count=None,
            chat_id=None,
            linked_session_summary=None,
            config_json=None,
            created_at=0,
            updated_at=0,
        )
        with _PortEnv():
            url = self._recorded_url(
                lambda di: di._weaviate_upsert(
                    row, weaviate_url=None, collection_name="Acme_Diagrams",
                )
            )
        self.assertEqual(
            url, SENTINEL_URL,
            "diagram_indexer upsert ignored WEAVIATE_PORT — it must resolve "
            "through vco_lib.weaviate_helpers.weaviate_url_default",
        )


class TestParameterDefaultsConsultTheEnvironment(unittest.TestCase):
    """``vco_lib`` entry points whose default bypassed the environment.

    The shape was ``weaviate_url: str = "http://localhost:8081"`` (or
    ``weaviate_url or "http://localhost:8081"`` in the body). A bound
    default-arg value is frozen at import; a string LITERAL is worse than
    frozen — it consults the environment not at all. So a caller that passed
    nothing addressed the canonical port even when ``WEAVIATE_URL`` /
    ``WEAVIATE_PORT`` named a different instance, and in three of these the
    value was then threaded into SUBPROCESSES as ``WEAVIATE_URL``.

    Each default is now resolved at CALL time through the shared home. Pinned
    behaviourally: call with the argument OMITTED and observe the URL the
    function actually uses.
    """

    def test_kg_binding_heal_count_uses_the_env_when_url_is_falsy(self):
        from vco_lib import kg_binding_heal as kbh

        seen: "list[str]" = []

        def _fake_urlopen(req, timeout=None):  # noqa: ANN001
            seen.append(req.full_url if hasattr(req, "full_url") else str(req))
            raise OSError("unroutable by design — no network in this suite")

        with _PortEnv():
            with mock.patch.object(kbh.urllib.request, "urlopen", _fake_urlopen):
                # Falsy url → must resolve from env, not from a literal.
                kbh._count_weaviate_class_objects("", "SomeClass")
        self.assertTrue(seen, "the counter never issued a request")
        self.assertTrue(
            seen[0].startswith(SENTINEL_URL),
            f"kg_binding_heal ignored WEAVIATE_PORT: addressed {seen[0]!r}",
        )

    def test_schema_migration_runner_count_uses_the_env_when_url_is_falsy(self):
        from vco_lib import schema_migration_runner as smr

        seen: "list[str]" = []

        def _fake_urlopen(req, timeout=None):  # noqa: ANN001
            seen.append(req.full_url if hasattr(req, "full_url") else str(req))
            raise OSError("unroutable by design — no network in this suite")

        import urllib.request as _ur

        with _PortEnv():
            with mock.patch.object(_ur, "urlopen", _fake_urlopen):
                smr._weaviate_class_object_count("", "SomeClass")
        self.assertTrue(seen, "the counter never issued a request")
        self.assertTrue(
            seen[0].startswith(SENTINEL_URL),
            f"schema_migration_runner ignored WEAVIATE_PORT: {seen[0]!r}",
        )

    def test_hard_cut_threads_the_env_resolved_url_into_subprocesses(self):
        """The highest-consequence of the three signature defaults.

        ``hard_cut`` seeds ``sub_env["WEAVIATE_URL"]`` with ``setdefault``
        and then runs ``install.py --update`` plus the migration runner
        against it. With the old literal, a hard cut on a relocated install
        pointed BOTH at ``localhost:8081``.
        """
        from vco_lib import hard_cut as hc

        captured: "dict[str, object]" = {}

        def _fake_runner(*args, **kwargs):
            captured.setdefault("env", kwargs.get("env"))
            raise RuntimeError("stop after the first subprocess — env captured")

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            # A `.git` must exist or hard_cut aborts BEFORE step 1 and never
            # spawns anything — the env would then never be observable.
            clone_root = Path(tmp) / "clone"
            (clone_root / ".git").mkdir(parents=True)
            vct_root = Path(tmp) / "vct"
            vct_root.mkdir()
            with _PortEnv():
                try:
                    hc.hard_cut(
                        "0.1.0", "0.3.0",
                        clone_root=clone_root,
                        vct_root=vct_root,
                        project_id=None,
                        stamp="stamp",
                        runner=_fake_runner,
                        deferral_writer=lambda *a, **k: True,
                        migration_runner=lambda **k: None,
                        # weaviate_url deliberately OMITTED — that is the bug.
                    )
                except Exception:  # noqa: BLE001 — the abort path is not the point
                    pass

        env = captured.get("env") or {}
        self.assertEqual(
            env.get("WEAVIATE_URL"), SENTINEL_URL,
            "hard_cut threaded the wrong WEAVIATE_URL into its subprocess "
            f"env: {env.get('WEAVIATE_URL')!r}. It must resolve through "
            "vco_lib.weaviate_helpers.weaviate_url_default.",
        )

    def test_reconcile_codegraph_registry_probes_the_env_resolved_instance(self):
        """The ``reconcile_codegraph_registry`` signature default, behaviourally.

        Driven to its first Weaviate touch (``_codegraph_collection_has_rows``)
        with ``weaviate_url`` OMITTED, so what is observed is the instance the
        existence probe actually targets — the value that then decides whether
        edge scripts run.
        """
        from vco_lib import codegraph_registry_reconcile as cgrr
        from vco_lib import schema_migration_runner as smr

        seen: "list[str]" = []

        def _fake_has_rows(url, class_names):  # noqa: ANN001
            seen.append(url)
            raise SystemExit(0)  # stop before any edge subprocess

        with _PortEnv():
            with mock.patch.object(
                cgrr, "_read_codegraph_bindings_ro",
                lambda _db: [("project-1", "Demo")],
            ), mock.patch.object(
                smr, "_codegraph_collection_has_rows", _fake_has_rows
            ):
                try:
                    cgrr.reconcile_codegraph_registry(
                        None,
                        db_path=Path("/nonexistent.db"),
                        migrations_dir=REPO_ROOT / "migrations",
                        project_root=REPO_ROOT,
                        env={},
                        # weaviate_url deliberately OMITTED — that is the bug.
                    )
                except SystemExit:
                    pass

        self.assertEqual(
            seen, [SENTINEL_URL],
            "reconcile_codegraph_registry probed the wrong instance: "
            f"{seen!r}. Its weaviate_url default must resolve through "
            "weaviate_url_default() at call time.",
        )

    def test_no_signature_default_still_hardcodes_the_url(self):
        """Completeness over the family, by SIGNATURE not by grep.

        ``inspect.signature`` is used rather than a text scan because the
        thing that must not be a literal IS the bound default value.
        """
        import inspect

        from vco_lib import codegraph_registry_reconcile as cgrr
        from vco_lib import hard_cut as hc
        from vco_lib import schema_migration_runner as smr

        targets = (
            ("run_schema_migrations", smr.run_schema_migrations),
            ("reconcile_codegraph_registry", cgrr.reconcile_codegraph_registry),
            ("hard_cut", hc.hard_cut),
        )
        for name, fn in targets:
            with self.subTest(fn=name):
                default = inspect.signature(fn).parameters["weaviate_url"].default
                self.assertEqual(
                    default, "",
                    f"{name}'s weaviate_url default is the literal "
                    f"{default!r}. A bound default cannot see the "
                    "environment; use the empty sentinel and resolve through "
                    "weaviate_url_default() in the body.",
                )

    def test_install_weaviate_prune_addresses_the_env_resolved_instance(self):
        """Both ``_prune_stale_kg_rows`` sites, in one end-to-end drive.

        This one is worth the setup cost: the second site is a **batch
        DELETE**. With the old literal, a prune whose caller passed a falsy
        URL would have read its stale-row list from one Weaviate and issued
        ``/v1/batch/objects`` against ``localhost:8081`` — i.e. deleted rows
        from whichever instance answered there. The test therefore asserts
        the URL of the delete, not just of the read.
        """
        import json as _json
        import tempfile
        import urllib.request as _ur

        from vco_lib import install_weaviate as iw

        urls: "list[str]" = []

        class _Resp:
            def __init__(self, payload: bytes) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def _fake_urlopen(req, timeout=None):  # noqa: ANN001
            urls.append(req.full_url)
            if req.full_url.endswith("/v1/graphql"):
                return _Resp(_json.dumps({
                    "data": {"Get": {"KG": [
                        {"_additional": {"id": "uuid-1"},
                         "file_path": "knowledge/gone.md"},
                    ]}},
                }).encode())
            return _Resp(b"[]")

        with tempfile.TemporaryDirectory() as tmp:
            with _PortEnv():
                with mock.patch.object(_ur, "urlopen", _fake_urlopen):
                    iw._prune_stale_kg_rows(
                        "KG",
                        "",  # falsy → must resolve from the env
                        dry_run=False,
                        project_root=Path(tmp),
                        is_orchestrator_root_install=lambda: False,
                    )

        self.assertEqual(
            urls,
            [f"{SENTINEL_URL}/v1/graphql", f"{SENTINEL_URL}/v1/batch/objects"],
            "install_weaviate._prune_stale_kg_rows addressed the wrong "
            f"instance: {urls!r}. Both the stale-row READ and the batch "
            "DELETE must resolve through _weaviate_url_default().",
        )

    def test_verify_diagrams_probe_targets_the_env_resolved_instance(self):
        """``vco doctor``-adjacent check found by the completeness scan.

        Not in the inventory this lane was handed. It read ``WEAVIATE_URL``
        alone, so on an install relocated via ``WEAVIATE_PORT`` it probed
        whatever answered on 8081 and reported the project's Diagrams class
        MISSING — a FAIL verdict against a healthy install.
        """
        from vco_lib.cli import verify_diagrams as vd

        seen: "dict[str, object]" = {}

        class _FakeWeaviate:
            @staticmethod
            def connect_to_custom(**kw):
                seen.update(kw)
                raise RuntimeError("no network in this suite — args captured")

            @staticmethod
            def connect_to_local(**kw):
                seen.update(kw)
                raise RuntimeError("no network in this suite — args captured")

        with _PortEnv():
            with mock.patch.dict(sys.modules, {"weaviate": _FakeWeaviate}):
                res = vd._check_weaviate_class("Demo", fix=False, quick=False)
        self.assertTrue(
            seen,
            "verify_diagrams never attempted a connection "
            f"(result was {res.status!r}: {res.detail!r})",
        )
        self.assertEqual(
            seen.get("http_port"), int(SENTINEL_PORT),
            f"verify_diagrams ignored WEAVIATE_PORT: connected with {seen!r}",
        )
        self.assertEqual(seen.get("http_host"), "localhost")

    def test_kg_sync_drift_cli_default_follows_the_env(self):
        """The argparse default is built inside ``main()``, so it CAN call the
        shared home — and must, or ``python -m vco_lib.kg_sync_drift`` reports
        drift against the wrong instance."""
        from vco_lib import kg_sync_drift as ksd

        captured: "dict[str, object]" = {}

        class _BoundBinding:
            status = "bound"
            kg_collection = "SomeCollection"
            detail = ""

        def _fake_scan(*args, **kwargs):
            captured.update(kwargs)
            raise SystemExit(0)  # stop before any I/O; the arg is captured

        with _PortEnv():
            with mock.patch.object(
                ksd, "check_kg_binding", lambda *a, **k: _BoundBinding()
            ), mock.patch.object(
                ksd, "surface_binding_gap", lambda *a, **k: None
            ), mock.patch.object(ksd, "scan_drift", _fake_scan):
                with self.assertRaises(SystemExit):
                    ksd.main(["--project-root", str(REPO_ROOT)])
        self.assertEqual(
            captured.get("weaviate_url"), SENTINEL_URL,
            "kg_sync_drift's --weaviate-url default ignored WEAVIATE_PORT",
        )


if __name__ == "__main__":
    unittest.main()
