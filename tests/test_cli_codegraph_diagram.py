# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for ``vco codegraph-diagram`` (Phase 3 CLI).

Coverage:

* Happy path: tmp_path output, stubbed fetch + render, file written +
  indexer called, exit 0 + JSON payload shape.
* Seed not found: exit 2, error reaches stderr (human) or JSON payload.
* ``--print`` mode: no file write, stdout carries the Mermaid source.
* Scope filter is passed through to the renderer (the CLI doesn't filter
  edges itself; it relays the scope to ``fetch_subgraph`` and that
  module's tests cover the per-scope behaviour — here we just confirm
  the relay).
* Output path NOT under ``.claude/diagrams/``: indexer is skipped, the
  human / JSON report records the skip; exit 0.
* Validation: ``--hops`` over the cap is rejected with exit 2.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.cli import codegraph_diagram as cli  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _args(
    seed_symbol: str = "pkg.foo",
    *,
    hops: int = 2,
    scope: str = "calls",
    output: Optional[Path] = None,
    max_nodes: int = 50,
    no_modules: bool = False,
    title: Optional[str] = None,
    print_: bool = False,
    json_: bool = False,
    project: Optional[str] = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        seed_symbol=seed_symbol,
        hops=hops,
        scope=scope,
        output=output,
        max_nodes=max_nodes,
        no_modules=no_modules,
        title=title,
        print=print_,
        json=json_,
        project=project,
    )


def _make_subgraph(seed: str = "pkg.foo", *, kind: str = "function") -> dict:
    """Minimal subgraph payload with 2 nodes + 1 edge — exercises the happy path."""
    return {
        "nodes": [
            {"id": "n_seed", "label": seed.split(".")[-1], "kind": kind,
             "module": "pkg", "full_name": seed},
            {"id": "n_callee", "label": "bar", "kind": "function",
             "module": "pkg", "full_name": "pkg.bar"},
        ],
        "edges": [
            {"from": "n_seed", "to": "n_callee", "kind": "calls", "label": "calls"},
        ],
        "seed_found": True,
        "seed_kind": kind,
        "seed_full_name": seed,
        "truncated": False,
        "truncation_reason": None,
    }


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Patch fetch + render + indexer + project resolver wrappers.

    Returns a holder dict so individual tests can pre-load the
    ``subgraph`` to return / make ``index_diagram`` raise / etc.
    """
    state: dict[str, Any] = {
        "subgraph": _make_subgraph(),
        "rendered": (
            "---\ntitle: pkg.foo\n---\nflowchart TD\n"
            '    n_seed["foo()"]\n    n_callee["bar()"]\n    n_seed --> n_callee\n'
        ),
        "indexed_calls": [],
        "index_error": None,
        "project_id": "demo-project-id",
        "project_name": None,  # no per-project prefix in tests by default
    }

    def fake_fetch(spec, project):
        state["fetch_args"] = (spec, project)
        return state["subgraph"]

    def fake_render(subgraph, *, title, include_modules):
        state["render_args"] = (subgraph, title, include_modules)
        return state["rendered"]

    def fake_generate(spec, project, title):
        # Tests that go through `generate` (not direct fetch/render)
        # still hit this single wrapper.
        return state["rendered"]

    def fake_index(file_path, project_id, chat_id):
        if state["index_error"] is not None:
            raise state["index_error"]
        state["indexed_calls"].append((str(file_path), project_id, chat_id))
        # Mimic a DiagramRow-ish return.
        class _Row:
            pass
        r = _Row()
        r.wrote_sidecar = True  # type: ignore[attr-defined]
        r.wrote_weaviate = False  # type: ignore[attr-defined]
        return r

    def fake_resolve_project_name():
        return state["project_name"]

    def fake_resolve_project_id():
        return state["project_id"]

    monkeypatch.setattr(cli, "_fetch_subgraph", fake_fetch)
    monkeypatch.setattr(cli, "_render", fake_render)
    monkeypatch.setattr(cli, "_generate", fake_generate)
    monkeypatch.setattr(cli, "_index_diagram", fake_index)
    monkeypatch.setattr(cli, "_resolve_project_name", fake_resolve_project_name)
    monkeypatch.setattr(cli, "_resolve_project_id_for_indexing", fake_resolve_project_id)

    return state


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_write_to_scoped_path_calls_indexer(self, stub_pipeline, tmp_path, monkeypatch, capsys):
        out = tmp_path / ".claude" / "diagrams" / "codegraph" / "foo.mmd"
        # CWD anchor so any path-derivation logic resolves relative to tmp_path.
        monkeypatch.chdir(tmp_path)
        exit_code = cli.cmd_codegraph_diagram(_args(output=out))
        assert exit_code == cli.EXIT_OK
        # File created.
        assert out.exists()
        assert out.read_text(encoding="utf-8").startswith("---")
        # Indexer received the resolved path.
        assert len(stub_pipeline["indexed_calls"]) == 1
        path_arg, pid, _chat = stub_pipeline["indexed_calls"][0]
        assert Path(path_arg) == out.resolve()
        assert pid == "demo-project-id"
        # Human summary mentions the path.
        captured = capsys.readouterr()
        assert str(out) in captured.out

    def test_json_output(self, stub_pipeline, tmp_path, monkeypatch, capsys):
        out = tmp_path / ".claude" / "diagrams" / "codegraph" / "foo.mmd"
        monkeypatch.chdir(tmp_path)
        exit_code = cli.cmd_codegraph_diagram(_args(output=out, json_=True))
        assert exit_code == cli.EXIT_OK
        captured = capsys.readouterr()
        payload = json.loads(captured.out.strip())
        assert payload["command"] == "codegraph-diagram"
        assert payload["exit_code"] == cli.EXIT_OK
        assert payload["overall"] == "ok"
        assert payload["mode"] == "write"
        assert payload["nodes"] == 2
        assert payload["edges"] == 1
        assert payload["indexed"] is True
        assert payload["seed_full_name"] == "pkg.foo"

    def test_default_output_path_is_under_scoped_dir(
        self, stub_pipeline, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        exit_code = cli.cmd_codegraph_diagram(_args())
        assert exit_code == cli.EXIT_OK
        # Default sanitised path: .claude/diagrams/codegraph/foo.mmd
        expected = tmp_path / ".claude" / "diagrams" / "codegraph" / "foo.mmd"
        assert expected.exists()


# ---------------------------------------------------------------------------
# --print mode
# ---------------------------------------------------------------------------


class TestPrintMode:
    def test_print_does_not_write_file(self, stub_pipeline, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        exit_code = cli.cmd_codegraph_diagram(_args(print_=True))
        assert exit_code == cli.EXIT_OK
        # Nothing under .claude/diagrams.
        diagrams_root = tmp_path / ".claude" / "diagrams"
        assert not diagrams_root.exists()
        # Mermaid source on stdout.
        captured = capsys.readouterr()
        assert "flowchart TD" in captured.out
        # Indexer not invoked.
        assert stub_pipeline["indexed_calls"] == []

    def test_print_json_wraps_mermaid_in_payload(
        self, stub_pipeline, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.chdir(tmp_path)
        exit_code = cli.cmd_codegraph_diagram(_args(print_=True, json_=True))
        assert exit_code == cli.EXIT_OK
        captured = capsys.readouterr()
        payload = json.loads(captured.out.strip())
        assert payload["mode"] == "print"
        assert "flowchart TD" in payload["mermaid"]


# ---------------------------------------------------------------------------
# Seed not found
# ---------------------------------------------------------------------------


class TestSeedNotFound:
    def test_human(self, stub_pipeline, tmp_path, monkeypatch, capsys):
        stub_pipeline["subgraph"] = {
            "nodes": [], "edges": [], "seed_found": False,
            "seed_kind": None, "seed_full_name": None,
            "truncated": False, "truncation_reason": None,
        }
        monkeypatch.chdir(tmp_path)
        exit_code = cli.cmd_codegraph_diagram(_args(seed_symbol="no.such.thing"))
        assert exit_code == cli.EXIT_ENV_PROBLEM
        captured = capsys.readouterr()
        assert "no.such.thing" in captured.err
        assert "could not be resolved" in captured.err

    def test_json(self, stub_pipeline, tmp_path, monkeypatch, capsys):
        stub_pipeline["subgraph"] = {
            "nodes": [], "edges": [], "seed_found": False,
            "seed_kind": None, "seed_full_name": None,
            "truncated": False, "truncation_reason": None,
        }
        monkeypatch.chdir(tmp_path)
        exit_code = cli.cmd_codegraph_diagram(_args(seed_symbol="no.such.thing", json_=True))
        assert exit_code == cli.EXIT_ENV_PROBLEM
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["exit_code"] == cli.EXIT_ENV_PROBLEM
        assert payload["overall"] == "seed_not_found"


# ---------------------------------------------------------------------------
# Scope is relayed through to fetch
# ---------------------------------------------------------------------------


class TestScopeRelay:
    def test_extends_scope_passed_to_fetch(self, stub_pipeline, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cli.cmd_codegraph_diagram(_args(scope="extends", print_=True))
        spec, _project = stub_pipeline["fetch_args"]
        assert spec.scope == "extends"

    def test_all_scope_accepted(self, stub_pipeline, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        exit_code = cli.cmd_codegraph_diagram(_args(scope="all", print_=True))
        assert exit_code == cli.EXIT_OK
        spec, _ = stub_pipeline["fetch_args"]
        assert spec.scope == "all"


# ---------------------------------------------------------------------------
# Output NOT under .claude/diagrams → indexer skipped
# ---------------------------------------------------------------------------


class TestOutsideScopedPath:
    def test_indexer_skipped(self, stub_pipeline, tmp_path, monkeypatch, capsys):
        # Output deliberately outside .claude/diagrams.
        out = tmp_path / "elsewhere" / "diagram.mmd"
        monkeypatch.chdir(tmp_path)
        exit_code = cli.cmd_codegraph_diagram(_args(output=out, json_=True))
        assert exit_code == cli.EXIT_OK  # Soft-fail — diagram still written.
        assert out.exists()
        # Indexer NOT called.
        assert stub_pipeline["indexed_calls"] == []
        # JSON payload records the skip.
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["indexed"] is False
        assert "not under" in (payload["index_error"] or "").lower()


# ---------------------------------------------------------------------------
# --hops validation
# ---------------------------------------------------------------------------


class TestHopsValidation:
    def test_zero_hops_rejected(self, stub_pipeline, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        exit_code = cli.cmd_codegraph_diagram(_args(hops=0))
        assert exit_code == cli.EXIT_ENV_PROBLEM
        assert "hops" in capsys.readouterr().err

    def test_over_cap_rejected(self, stub_pipeline, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        exit_code = cli.cmd_codegraph_diagram(_args(hops=99))
        assert exit_code == cli.EXIT_ENV_PROBLEM
        assert "MAX_HOPS" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Indexer error degrades gracefully
# ---------------------------------------------------------------------------


class TestIndexerError:
    def test_indexer_raise_is_soft_fail(self, stub_pipeline, tmp_path, monkeypatch, capsys):
        stub_pipeline["index_error"] = RuntimeError("boom")
        out = tmp_path / ".claude" / "diagrams" / "codegraph" / "foo.mmd"
        monkeypatch.chdir(tmp_path)
        exit_code = cli.cmd_codegraph_diagram(_args(output=out, json_=True))
        assert exit_code == cli.EXIT_OK
        assert out.exists()
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["indexed"] is False
        assert "boom" in (payload["index_error"] or "")


# ---------------------------------------------------------------------------
# Filename sanitiser
# ---------------------------------------------------------------------------


class TestSanitiseFilename:
    @pytest.mark.parametrize("seed,expected", [
        ("vco_lib.diagram_indexer.index_diagram", "index-diagram"),
        ("api.UserManager", "user-manager"),
        ("api/routes.py", "routes-py"),
        ("", "untitled"),
        ("plain", "plain"),
        ("SomeCamelCaseName", "some-camel-case-name"),
        ("__private", "private"),
    ])
    def test_sanitise(self, seed: str, expected: str) -> None:
        assert cli._sanitise_filename(seed) == expected


# ---------------------------------------------------------------------------
# _is_under_diagrams_dir
# ---------------------------------------------------------------------------


class TestIsUnderDiagramsDir:
    def test_under(self, tmp_path):
        p = tmp_path / ".claude" / "diagrams" / "codegraph" / "foo.mmd"
        p.parent.mkdir(parents=True)
        p.touch()
        assert cli._is_under_diagrams_dir(p) is True

    def test_outside(self, tmp_path):
        p = tmp_path / "docs" / "foo.mmd"
        p.parent.mkdir(parents=True)
        p.touch()
        assert cli._is_under_diagrams_dir(p) is False
