# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 WP-C3 — the Edit surface query rework, pinned at the wrapper.

RED on the base tree: the old pre-edit hook built the SEMANTIC query
``"<module-basename> <first 200 chars of new_string>"`` and ran the two
legacy producer CLIs — the stub seams (VCO_ROUTER_KG_SCRIPT / VCO_CG_SCRIPT)
never record, and the old query shape is exactly what these tests forbid.

Pins (plan §WP-F row 6):
  * enclosing-symbol extraction drives the EXACT code-graph leg
    (Python AND Rust tables, through the real hook);
  * the old ``module + new_string[:200]`` query NEVER appears in producer
    argv — a unique marker planted in new_string must not surface in the
    KG query, and the KG query must carry module+symbol+path topic;
  * the edit policy's self-file exclusion (--exclude-file) and identity
    (--source-file) reach the producer argv;
  * no enclosing symbol recoverable → no CG leg (a wrong symbol is worse
    than none), while the KG path-topic leg still runs.
"""
from __future__ import annotations

import pytest

from tests.test_v02101_router_surfaces import Rig, needs_bash  # noqa: E402


@pytest.fixture()
def rig(tmp_path):
    """Local re-instantiation of the shared rig (importing the fixture
    object across modules trips ruff F811 against the test parameters)."""
    return Rig(tmp_path)

_MARKER = "ZZQX_MARKER_9f31_not_in_any_query"


def _py_edit_payload(rig: Rig, session: str = "sess-eq-1") -> tuple[dict, str]:
    f = rig.proj / "retrieval_mod.py"
    f.write_text(
        "class Retriever:\n"
        "    def rerank(self, items):\n"
        "        scored = 1\n"
        "        return scored\n",
        encoding="utf-8",
    )
    payload = {
        "tool_name": "Edit", "session_id": session, "prompt_id": "p-eq",
        "cwd": str(rig.proj),
        "tool_input": {"file_path": str(f), "old_string": "scored = 1",
                       "new_string": f"scored = 2  # {_MARKER}"},
    }
    return payload, str(f)


def _rs_edit_payload(rig: Rig, session: str = "sess-eq-2") -> tuple[dict, str]:
    f = rig.proj / "widget.rs"
    f.write_text(
        "pub struct Widget {\n    pub name: String,\n}\n\n"
        "impl Widget {\n"
        "    fn rename(&mut self, n: String) {\n"
        "        self.name = n;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    payload = {
        "tool_name": "Edit", "session_id": session, "prompt_id": "p-eq",
        "cwd": str(rig.proj),
        "tool_input": {"file_path": str(f), "old_string": "self.name = n;",
                       "new_string": f"self.name = n.trim().to_string(); // {_MARKER}"},
    }
    return payload, str(f)


@needs_bash
class TestEditQueryRework:
    def test_old_semantic_query_never_in_producer_argv(self, rig: Rig) -> None:
        """The v0.2.101 contract: new_string content is NEVER query text."""
        payload, f = _py_edit_payload(rig)
        r = rig.run("pre-edit-context-inject", payload)
        assert r.returncode == 0, r.stderr
        kg = rig.kg_records()
        assert kg, r.stderr[-800:]
        query = kg[0][0]
        assert _MARKER not in query
        assert "scored = 2" not in query
        # the query IS the module+symbol+path topic (§C3)
        assert "retrieval" in query or "mod" in query
        assert "rerank" in query  # the enclosing symbol joins the topic

    def test_python_enclosing_symbol_drives_exact_leg(self, rig: Rig) -> None:
        payload, f = _py_edit_payload(rig)
        r = rig.run("pre-edit-context-inject", payload)
        assert r.returncode == 0, r.stderr
        cg = rig.cg_records()
        assert cg, r.stderr[-800:]
        symbols = [argv[2] for argv in cg]
        assert "rerank" in symbols  # innermost enclosing def
        for argv in cg:
            assert argv[:2] == ["structure", "callers"]
            assert "--hook-format" in argv
            assert argv[argv.index("--source-file") + 1] == f
            assert argv[argv.index("--exclude-file") + 1] == f  # self-file excluded

    def test_rust_enclosing_symbol_table(self, rig: Rig) -> None:
        payload, f = _rs_edit_payload(rig)
        r = rig.run("pre-edit-context-inject", payload)
        assert r.returncode == 0, r.stderr
        cg = rig.cg_records()
        assert cg, r.stderr[-800:]
        symbols = [argv[2] for argv in cg]
        assert "rename" in symbols
        # the impl block name may join (nearest-first, ≤3) — but the MARKER
        # from new_string must not be anywhere in any argv
        for argv in cg:
            assert all(_MARKER not in tok for tok in argv)

    def test_no_enclosing_symbol_no_cg_leg(self, rig: Rig) -> None:
        """old_string absent from the file → no symbol → NO exact leg (a
        wrong symbol is worse than none); the KG path-topic leg still runs."""
        f = rig.proj / "plain.py"
        f.write_text("VALUE = 1\n", encoding="utf-8")
        payload = {
            "tool_name": "Edit", "session_id": "sess-eq-3", "prompt_id": "p-eq",
            "cwd": str(rig.proj),
            "tool_input": {"file_path": str(f),
                           "old_string": "text that is not in the file",
                           "new_string": "VALUE = 2"},
        }
        r = rig.run("pre-edit-context-inject", payload)
        assert r.returncode == 0, r.stderr
        assert rig.cg_records() == []
        kg = rig.kg_records()
        assert kg, "the KG path-topic leg is independent of symbol recovery"
        assert "plain" in kg[0][0]

    def test_edit_profile_floor_argv(self, rig: Rig) -> None:
        payload, _f = _py_edit_payload(rig, session="sess-eq-4")
        r = rig.run("pre-edit-context-inject", payload)
        assert r.returncode == 0, r.stderr
        kargv = rig.kg_records()[0]
        assert kargv[kargv.index("--injection-profile") + 1] == "edit"
        assert "--hook-format" in kargv
