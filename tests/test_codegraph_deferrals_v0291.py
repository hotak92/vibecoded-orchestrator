# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 wave-3 — the code-graph analyzer's deferral tenancy.

The family moved out of ``templates/scripts/analyze_code_graph.py`` (the
ratchet's required direction) when MAJOR-1 needed a THIRD operation: the
NARROW paired clear its two backend skip paths never had.

Why the clear matters, pinned here: both skip paths ``return 0`` AFTER
re-emitting their entry, so no retry dispatcher can infer success from the
exit code. The clear is the evidence, and it is the only thing that resolves
these conditions.

Fully hermetic — a temp folder, the real locked emitter, no services.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import codegraph_deferrals as cd  # noqa: E402

NO_BACKEND = "code_graph_no_embedding_backend"
CODE_DOWN = "code_graph_code_backend_unreachable"


def _cids(folder: Path) -> list[str]:
    from vco_lib.deferral_report import DeferralReport

    report = DeferralReport.read(folder)
    if not report:
        return []
    return [e.condition_id for e in report.entries]


class TenancyTests(unittest.TestCase):
    def test_the_two_ids_are_named_once(self) -> None:
        self.assertEqual(
            set(cd.CODE_GRAPH_BACKEND_CIDS), {NO_BACKEND, CODE_DOWN},
        )

    def test_no_backend_emits_a_registered_condition(self) -> None:
        with TemporaryDirectory() as td:
            folder = Path(td)
            cd.emit_no_backend(folder, RuntimeError("nothing answered"))
            self.assertEqual(_cids(folder), [NO_BACKEND])

    def test_code_backend_down_names_the_service_to_restart(self) -> None:
        cases = {
            "codesage_embed": ("CodeEmbed", "podman start vco_code_embed"),
            "openai_embed": ("OpenAI", "OPENAI_API_KEY"),
            "qwen3_embed": ("Ollama", "podman start vco_ollama"),
        }
        for slot, (hint, cmd) in cases.items():
            with self.subTest(slot=slot):
                with TemporaryDirectory() as td:
                    folder = Path(td)
                    cd.emit_code_backend_down(folder, slot, "some-model")
                    from vco_lib.deferral_report import DeferralReport

                    entry = DeferralReport.read(folder).entries[0]
                self.assertEqual(entry.condition_id, CODE_DOWN)
                self.assertIn(hint, entry.title)
                self.assertIn(cmd, entry.command_to_apply)
                self.assertIn(slot, entry.detected)

    def test_the_clear_resolves_BOTH_ids(self) -> None:
        """A completed walk falsifies both premises, so both go — whichever
        one was emitted."""
        with TemporaryDirectory() as td:
            folder = Path(td)
            cd.emit_no_backend(folder, RuntimeError("x"))
            cd.emit_code_backend_down(folder, "codesage_embed", "m")
            self.assertEqual(sorted(_cids(folder)), sorted([NO_BACKEND, CODE_DOWN]))

            removed = cd.clear_backend_deferrals(folder)

            self.assertEqual(removed, 2)
            self.assertEqual(_cids(folder), [])

    def test_clearing_an_absent_condition_is_a_no_op(self) -> None:
        with TemporaryDirectory() as td:
            self.assertEqual(cd.clear_backend_deferrals(Path(td)), 0)

    def test_the_clear_leaves_other_entries_alone(self) -> None:
        from vco_lib.deferral_emit import emit
        from vco_lib.deferral_report import DeferralEntry

        with TemporaryDirectory() as td:
            folder = Path(td)
            emit(folder, DeferralEntry(
                condition_id="mcp_registration_failed", title="t",
                detected="d", why_deferred="w", command_to_apply="c",
                severity="warning",
            ))
            cd.emit_no_backend(folder, RuntimeError("x"))

            cd.clear_backend_deferrals(folder)

            self.assertEqual(_cids(folder), ["mcp_registration_failed"])


def _analyzer_module():
    """Load ``analyze_code_graph.py`` as a module (same loader the deferral
    argparse sweep uses). Skips when its heavy imports are unavailable."""
    import importlib.util

    path = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
    spec = importlib.util.spec_from_file_location("_seam_analyze_code_graph", str(path))
    if spec is None or spec.loader is None:  # pragma: no cover — defensive
        raise unittest.SkipTest(f"cannot load analyzer from {path}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:  # pragma: no cover — env without weaviate-client
        raise unittest.SkipTest("analyzer dependencies unavailable")
    return mod


class AnalyzerWiringTests(unittest.TestCase):
    """The analyzer keeps ONE thin wrapper and CALLS the clear on success."""

    def setUp(self) -> None:
        self.src = (
            REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
        ).read_text(encoding="utf-8")

    def test_the_success_path_clears(self) -> None:
        body = self.src[self.src.index("_print_codegraph_provenance(embedding_service"):]
        body = body[: body.index("return 0") + len("return 0")]
        self.assertIn('_deferral_op("clear_backend_deferrals", deferral_root)', body)

    def test_both_skip_paths_still_exit_zero_and_re_emit(self) -> None:
        """The premise of the whole design: an exit code is not evidence."""
        for emitter in ('_deferral_op("emit_no_backend", deferral_root',
                        '_deferral_op("emit_code_backend_down", deferral_root'):
            with self.subTest(emitter=emitter):
                after = self.src[self.src.index(emitter):]
                self.assertIn("return 0", after[: after.index("\n\n")+900])

    def test_the_entry_bodies_are_not_inline_any_more(self) -> None:
        self.assertNotIn("DeferralEntry(", self.src[self.src.index(
            "def _deferral_op"):])
        self.assertIn("from vco_lib import codegraph_deferrals", self.src)

    def test_every_dispatched_op_name_exists_on_the_module(self) -> None:
        """``_deferral_op`` dispatches by NAME through a soft-fail guard, so a
        typo would silently disable a ledger write. Pin every name."""
        import re

        names = set(re.findall(r'_deferral_op\(\s*"([a-z_]+)"', self.src))
        self.assertEqual(
            names,
            {"emit_no_backend", "emit_code_backend_down", "clear_backend_deferrals"},
        )
        for name in names:
            with self.subTest(op=name):
                self.assertTrue(callable(getattr(cd, name, None)), name)


class LedgerRootSeamTests(unittest.TestCase):
    """MAJOR-A (wave-3 re-review): ``--deferral-root`` pins the ledger root.

    The analyzer's ``install_root`` serves the EmbeddingService config lookup
    AND — historically — the deferral ledger. Those are different axes: a
    retry dispatched over a user project inherits the session's
    ``$VCT_ORCHESTRATOR_ROOT``, so the ledger half has to be pinnable by the
    caller that will re-read it. The flag governs BOTH the emit and the clear
    (one variable), and its ABSENCE must reproduce today's resolution exactly
    so launcher / bundle direct invocation is unchanged.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _analyzer_module()

    def test_the_parser_accepts_the_seam(self) -> None:
        args = self.mod._build_arg_parser().parse_args(
            ["/repo", "--deferral-root", "/some/project"],
        )
        self.assertEqual(args.deferral_root, Path("/some/project"))

    def test_the_flag_is_absent_by_default(self) -> None:
        args = self.mod._build_arg_parser().parse_args(["/repo"])
        self.assertIsNone(args.deferral_root)

    def test_a_pinned_root_wins(self) -> None:
        with TemporaryDirectory() as td:
            pinned = Path(td)
            self.assertEqual(
                self.mod._resolve_deferral_root(pinned, Path("/orchestrator")),
                pinned.resolve(),
            )

    def test_flag_absent_is_todays_resolution_verbatim(self) -> None:
        """The seam adds NO second env read: absent ⇒ the caller's default
        (``install_root``) is returned unchanged, env set or not."""
        import os
        from unittest import mock

        default = Path("/orchestrator")
        for env in ({"VCT_ORCHESTRATOR_ROOT": "/elsewhere"}, {}):
            with self.subTest(env=env or "unset"):
                with mock.patch.dict(os.environ, env, clear=not env):
                    self.assertIs(
                        self.mod._resolve_deferral_root(None, default), default,
                    )

    def test_emit_and_clear_share_the_one_root_variable(self) -> None:
        """Emit-root == clear-root == the root the dispatcher re-reads."""
        src = (
            REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "deferral_root = _resolve_deferral_root(args.deferral_root, install_root)",
            src,
        )
        for site in ('_deferral_op("emit_no_backend", deferral_root',
                     '_deferral_op("emit_code_backend_down", deferral_root',
                     '_deferral_op("clear_backend_deferrals", deferral_root)'):
            with self.subTest(site=site):
                self.assertIn(site, src)
        self.assertNotIn("_deferral_op(\"emit_no_backend\", install_root", src)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
