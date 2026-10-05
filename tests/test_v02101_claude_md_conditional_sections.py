# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 wave 2 (T1 + T2) — conditional rendering on the ROOT path, the
`claude_md_sections` resolver (plan D2) and the bundle-path UUID fix (D4).

What is pinned here:

* the root render path (``rendered_root_files.render_entry``) now runs the
  SAME conditional primitive the project pipeline uses: inactive feature
  sections drop, active ones render, and no ``{{#``/``{{/`` tag ever survives
  into the rendered file;
* the plan-risk-4 decision: a MALFORMED conditional on the root path fails
  the entry loudly (the existing ``failed`` outcome, nothing written) — it
  never ships raw tags silently the way the bundle path's historical
  swallow does;
* every resolver probe soft-fails to RENDER (conservative default) with one
  stderr line, never a raise — the value feeds a render;
* ``model_gateway`` follows the DELIVERY gate (SKIP hides, DELIVER and
  UNKNOWN render), ``lean_ctx`` follows the folder's own settings.json hook
  registration, ``rl_retrieval`` follows the license validator call the RL
  pipeline itself gates on;
* D4: the bundle path resolves the folder to its launcher.db UUID, so an
  explicit ``project_modules`` row finally reaches the project render (the
  old call passed ``str(folder)`` where the table keys on the UUID — a row
  could never match).
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import (  # noqa: E402
    insert_rows,
    make_launcher_db,
)
from vco_lib import claude_md_sections as cms  # noqa: E402
from vco_lib import gateway_ensure  # noqa: E402
from vco_lib import module_gated_delivery as mgd  # noqa: E402
from vco_lib import project_templates as pt  # noqa: E402
from vco_lib import rendered_root_files as rrf  # noqa: E402

ROOT_TEMPLATE = REPO_ROOT / "templates" / "ORCHESTRATOR-CLAUDE.md.template"
USER_TEMPLATE = REPO_ROOT / "templates" / "CLAUDE.md.template"

UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

# Strings unique to each conditional section of the SHIPPED root template.
# (lean-ctx sentinels updated with the v0.2.101 allow-list + tee/pointer
# rewrite of that section: the "Three-tier bypass hierarchy" table heading
# is gone; the pointer line is the section's load-bearing promise.)
LEAN_CTX_SECTION = "**Lean-ctx Bash compression — allow-listed, lossless**"
LEAN_CTX_POINTER = "[lean-ctx-tee]"
RL_SECTION = "**RL retrieval reranking is active for this project**"
# v0.2.101: the root template's diagrams bullet ("**mermaid** /
# **excalidraw**" as registered MCP servers) was REMOVED — the wrapper MCPs
# are no longer registered on install, so the bullet's claim was false on
# every fresh install. The root template carries no diagrams conditional
# any more (the PROJECT template keeps its true one — the Diagrams-tab
# workflow section — and the `diagrams` module row stays default-on), so
# the pinned expectation is now the bullet's ABSENCE.
RETIRED_DIAGRAMS_MCP_BULLET = "- **mermaid** / **excalidraw**"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _root_entry() -> rrf.RenderedRootFile:
    return next(e for e in rrf.entries() if e.path == "CLAUDE.md")


def _synthetic_root(tmp_path: Path, *, template: bytes | None = None) -> Path:
    root = tmp_path / "root"
    (root / "templates").mkdir(parents=True)
    (root / "templates" / "ORCHESTRATOR-CLAUDE.md.template").write_bytes(
        template if template is not None else ROOT_TEMPLATE.read_bytes())
    return root


def _pin_sections(monkeypatch: pytest.MonkeyPatch, sections) -> None:
    """Pin the resolver the render path consults (the tests below drive the
    render, not the machine state)."""
    monkeypatch.setattr(cms, "active_sections", lambda *a, **k: frozenset(sections))


def _render_root(root: Path):
    with contextlib.redirect_stderr(io.StringIO()):
        outcome = rrf.render_entry(root, _root_entry())
    target = root / "CLAUDE.md"
    text = target.read_text(encoding="utf-8") if target.is_file() else None
    return outcome, text


def _db(tmp_path: Path, name: str, folder: Path,
        rows: list[tuple[str, int]]) -> Path:
    db = make_launcher_db(
        tmp_path / name,
        projects=[{"project_id": UUID, "name": "P", "folder_path": str(folder)}])
    if rows:
        insert_rows(db, "project_modules", [
            {"project_id": UUID, "module_name": mod, "enabled": enabled}
            for mod, enabled in rows
        ])
    return db


def _pin_machine_signal(monkeypatch: pytest.MonkeyPatch, configured) -> None:
    monkeypatch.setattr(
        gateway_ensure, "machine_gateway_signal",
        lambda *a, **k: gateway_ensure.MachineGatewaySignal(
            configured=configured, registration="test", panel="test",
            reason="test signal"))


# ---------------------------------------------------------------------------
# T1 — the conditional pass on the root render path
# ---------------------------------------------------------------------------

class TestRootRenderConditionalSections:
    def test_root_render_drops_inactive_sections(self, tmp_path, monkeypatch):
        root = _synthetic_root(tmp_path)
        _pin_sections(monkeypatch, frozenset())
        outcome, text = _render_root(root)
        assert outcome.status in ("created", "auto_block_updated", "full_rewrite")
        assert text is not None
        assert "{{#" not in text and "{{/if_" not in text
        assert LEAN_CTX_SECTION not in text
        assert LEAN_CTX_POINTER not in text
        assert RL_SECTION not in text
        assert RETIRED_DIAGRAMS_MCP_BULLET not in text
        # The document around the dropped sections still renders.
        assert "## Context Efficiency" in text
        assert "### Hook System" in text

    def test_root_render_keeps_sections_when_features_on(self, tmp_path, monkeypatch):
        root = _synthetic_root(tmp_path)
        _pin_sections(monkeypatch, cms.ALL_FEATURES)
        outcome, text = _render_root(root)
        assert outcome.status in ("created", "auto_block_updated", "full_rewrite")
        assert text is not None
        assert "{{#" not in text and "{{/if_" not in text
        assert LEAN_CTX_SECTION in text and LEAN_CTX_POINTER in text
        assert RL_SECTION in text
        assert RETIRED_DIAGRAMS_MCP_BULLET not in text

    def test_malformed_conditional_fails_the_entry_never_ships_raw_tags(
            self, tmp_path):
        """Plan risk 4, decided: on the ROOT path a malformed conditional is
        the existing ``failed`` outcome — nothing is written, so raw tags can
        never leak silently into the file every agent on the install reads."""
        root = _synthetic_root(tmp_path, template=(
            "<!-- BEGIN: AUTO -->\n"
            "{{#if_module_active diagrams}}\n"
            "unclosed body\n"
            "<!-- END: AUTO -->\n").encode("utf-8"))
        # A pre-existing target must be left byte-identical (no partial write).
        existing = "mine above\n<!-- BEGIN: AUTO -->\nold\n<!-- END: AUTO -->\nmine below\n"
        (root / "CLAUDE.md").write_text(existing, encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            outcome = rrf.render_entry(root, _root_entry())
        assert outcome.status == "failed"
        assert outcome.is_failure
        assert "malformed conditional" in outcome.detail
        assert (root / "CLAUDE.md").read_text(encoding="utf-8") == existing

    def test_malformed_conditional_on_a_fresh_root_writes_nothing(self, tmp_path):
        root = _synthetic_root(tmp_path, template=(
            "{{#if_module_active diagrams}}\nunclosed\n").encode("utf-8"))
        with contextlib.redirect_stderr(io.StringIO()):
            outcome = rrf.render_entry(root, _root_entry())
        assert outcome.status == "failed"
        assert not (root / "CLAUDE.md").exists()


# ---------------------------------------------------------------------------
# T1 — the resolver's conservative defaults (plan D2)
# ---------------------------------------------------------------------------

class TestConservativeProbeFailures:
    def test_every_probe_failure_renders_its_section_with_one_stderr_line(
            self, tmp_path, monkeypatch, capsys):
        folder = tmp_path / "proj"
        folder.mkdir()
        # Make each probe blow up in a different way:
        def _raiser(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(mgd, "resolve_project_id_for_folder", _raiser)
        monkeypatch.setattr(mgd, "gateway_agents_gate", _raiser)
        monkeypatch.setitem(sys.modules, "VCThelpers.license", None)
        # lean-ctx: settings.json is a DIRECTORY → read raises OSError.
        settings = folder / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.mkdir()

        sections = cms.active_sections(folder)
        err = capsys.readouterr().err
        assert {"diagrams", cms.MODEL_GATEWAY, cms.LEAN_CTX, cms.RL_RETRIEVAL} <= set(
            sections)
        assert err.count("[vco] claude-md-sections:") == 4
        assert "project_modules probe failed" in err
        assert "model-gateway probe failed" in err
        assert "could not be read" in err
        assert "license validator unavailable" in err

    def test_render_never_crashes_when_the_resolver_is_broken(
            self, tmp_path, monkeypatch):
        """The owner rule end to end: even a resolver that raises outright
        must not take the render down. The call site falls back to the
        conservative direction its probes take — EVERY section renders — with
        one stderr line, and no tag leaks."""
        root = _synthetic_root(tmp_path)

        def _raiser(*_a, **_k):
            raise RuntimeError("resolver down")

        monkeypatch.setattr(cms, "active_sections", _raiser)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            outcome = rrf.render_entry(root, _root_entry())
        assert outcome.status in ("created", "auto_block_updated", "full_rewrite")
        text = (root / "CLAUDE.md").read_text(encoding="utf-8")
        assert LEAN_CTX_SECTION in text and RL_SECTION in text
        assert RETIRED_DIAGRAMS_MCP_BULLET not in text
        assert "{{#" not in text and "{{/if_" not in text
        assert "section resolver failed" in err.getvalue()

    def test_rl_probe_uses_the_pipelines_own_call_shape(self, tmp_path, monkeypatch):
        folder = tmp_path / "proj"
        folder.mkdir()
        calls: list = []
        fake = types.ModuleType("VCThelpers.license")

        def feature_enabled(feature, module_id=None):
            calls.append((feature, module_id))
            return False

        fake.feature_enabled = feature_enabled
        monkeypatch.setitem(sys.modules, "VCThelpers.license", fake)
        sections = cms.active_sections(folder, needed={cms.RL_RETRIEVAL})
        assert cms.RL_RETRIEVAL not in sections
        # The SAME call the retrieval pipeline gates on (search_pipeline.py),
        # so the section and the behaviour cannot drift apart.
        assert calls == [("rl_retrieval", "vct-rl-reranker")]

    def test_rl_probe_exception_renders(self, tmp_path, monkeypatch, capsys):
        folder = tmp_path / "proj"
        folder.mkdir()
        fake = types.ModuleType("VCThelpers.license")

        def feature_enabled(*_a, **_k):
            raise RuntimeError("validator exploded")

        fake.feature_enabled = feature_enabled
        monkeypatch.setitem(sys.modules, "VCThelpers.license", fake)
        assert cms.RL_RETRIEVAL in cms.active_sections(
            folder, needed={cms.RL_RETRIEVAL})
        assert "RL license probe failed" in capsys.readouterr().err


class TestLeanCtxDetection:
    def test_reads_the_folders_own_settings(self, tmp_path):
        folder = tmp_path / "proj"
        settings = folder / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            # Missing settings file is an ANSWER: nothing is registered here.
            assert cms.LEAN_CTX not in cms.active_sections(
                folder, needed={cms.LEAN_CTX})
            # The shipped registration shape (any event).
            settings.write_text(json.dumps({"hooks": {"PreToolUse": [
                {"matcher": "*", "hooks": [
                    {"type": "command",
                     "command": "bash \"${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/"
                                "lean-ctx-rewrite.sh\""}]}]}}),
                encoding="utf-8")
            assert cms.LEAN_CTX in cms.active_sections(
                folder, needed={cms.LEAN_CTX})
            # The launcher's toggle REMOVES the entry → section absent.
            settings.write_text(json.dumps({"hooks": {"PreToolUse": []}}),
                                encoding="utf-8")
            assert cms.LEAN_CTX not in cms.active_sections(
                folder, needed={cms.LEAN_CTX})
            # A parseable settings object with no hooks key → absent.
            settings.write_text(json.dumps({"env": {}}), encoding="utf-8")
            assert cms.LEAN_CTX not in cms.active_sections(
                folder, needed={cms.LEAN_CTX})
            # Malformed JSON is a DETECTION FAILURE → renders (conservative).
            settings.write_text("{not json at all", encoding="utf-8")
            assert cms.LEAN_CTX in cms.active_sections(
                folder, needed={cms.LEAN_CTX})
        assert "does not parse" in err.getvalue()


class TestModelGatewayFollowsDeliveryGate:
    """D2: SKIP hides the section, DELIVER and UNKNOWN render it — the same
    verdict that decides delivery of the gateway agent definitions."""

    def test_explicit_enabled_row_renders(self, tmp_path):
        folder = tmp_path / "proj"
        folder.mkdir()
        db = _db(tmp_path, "db-on", folder, [("model_gateway", 1)])
        assert cms.MODEL_GATEWAY in cms.active_sections(
            folder, db_path=db, needed={cms.MODEL_GATEWAY})

    def test_explicit_disabled_row_hides_even_with_a_gateway_machine(
            self, tmp_path, monkeypatch):
        folder = tmp_path / "proj"
        folder.mkdir()
        _pin_machine_signal(monkeypatch, True)
        db = _db(tmp_path, "db-off", folder, [("model_gateway", 0)])
        assert cms.MODEL_GATEWAY not in cms.active_sections(
            folder, db_path=db, needed={cms.MODEL_GATEWAY})

    def test_no_row_plus_machine_signal_renders(self, tmp_path, monkeypatch):
        folder = tmp_path / "proj"
        folder.mkdir()
        _pin_machine_signal(monkeypatch, True)
        db = _db(tmp_path, "db-norow", folder, [])
        assert cms.MODEL_GATEWAY in cms.active_sections(
            folder, db_path=db, needed={cms.MODEL_GATEWAY})

    def test_no_row_no_gateway_hides(self, tmp_path, monkeypatch):
        folder = tmp_path / "proj"
        folder.mkdir()
        _pin_machine_signal(monkeypatch, False)
        db = _db(tmp_path, "db-nosig", folder, [])
        assert cms.MODEL_GATEWAY not in cms.active_sections(
            folder, db_path=db, needed={cms.MODEL_GATEWAY})

    def test_unknown_signal_renders(self, tmp_path, monkeypatch):
        folder = tmp_path / "proj"
        folder.mkdir()
        _pin_machine_signal(monkeypatch, None)  # could not ask → UNKNOWN
        db = _db(tmp_path, "db-unknown", folder, [])
        assert cms.MODEL_GATEWAY in cms.active_sections(
            folder, db_path=db, needed={cms.MODEL_GATEWAY})

    def test_unreadable_db_renders(self, tmp_path):
        folder = tmp_path / "proj"
        folder.mkdir()
        bad = tmp_path / "bad" / "launcher.db"
        bad.parent.mkdir()
        bad.write_bytes(b"this is not sqlite")
        assert cms.MODEL_GATEWAY in cms.active_sections(
            folder, db_path=bad, needed={cms.MODEL_GATEWAY})


class TestGatewaySectionRendersPayload:
    """G1 (v0.2.101): ``status --json`` carries what the render DOES.

    The Services-page toggle must show what the CLAUDE.md render actually
    does, so the payload the launcher's python-bridge command passes through
    names it (``claude_md_section.renders``) instead of letting Rust/TS
    re-derive the mapping. The mapping itself is
    ``cms.gateway_section_renders`` — ONE home, shared with the render path.
    """

    def test_mapping_only_a_positive_skip_hides(self):
        verdicts = [
            (mgd.GateState.DELIVER, mgd.SIGNAL_PROJECT_ROW, True),
            (mgd.GateState.DELIVER, mgd.SIGNAL_MACHINE, True),
            (mgd.GateState.SKIP, mgd.SIGNAL_PROJECT_ROW, False),
            (mgd.GateState.SKIP, mgd.SIGNAL_MACHINE, False),
            # UNKNOWN never hides text — the render's own contract.
            (mgd.GateState.UNKNOWN, mgd.SIGNAL_MACHINE, True),
            (mgd.GateState.UNKNOWN, mgd.SIGNAL_LAUNCHER_DB, True),
        ]
        for state, signal, expected in verdicts:
            verdict = mgd.GateVerdict(
                state, signal, "test", machine_configured=None)
            assert cms.gateway_section_renders(verdict) is expected, (
                f"{state}/{signal} must map to renders={expected}")

    def test_status_payload_carries_the_render_answer(self, tmp_path,
                                                      monkeypatch):
        folder = tmp_path / "proj"
        folder.mkdir()
        for configured, expected in ((True, True), (False, False)):
            _pin_machine_signal(monkeypatch, configured)
            payload = mgd.status_payload([folder])
            assert payload["gate"]["state"] == (
                "deliver" if configured else "skip")
            assert payload["claude_md_section"] == {"renders": expected}, (
                "the payload must say what the render does, from the "
                "render's own mapping")

    def test_status_payload_without_a_folder_has_no_section_answer(self,
                                                                   monkeypatch):
        _pin_machine_signal(monkeypatch, False)
        payload = mgd.status_payload(None)
        assert "claude_md_section" not in payload, (
            "no folder → no gate verdict → no render answer to give")
        assert "folders" not in payload

    def test_status_payload_batches_every_folder_in_one_call(self, tmp_path,
                                                             monkeypatch):
        """S2 (v0.2.101 review): the Services page asks for ALL projects in
        ONE interpreter start — one ``status`` invocation carrying every
        ``--folder``, one machine signal, a verdict per folder."""
        folders = []
        for name in ("proj-a", "proj-b", "proj-c"):
            f = tmp_path / name
            f.mkdir()
            folders.append(f)
        calls = {"n": 0}

        def counting_signal(*a, **k):
            calls["n"] += 1
            return gateway_ensure.MachineGatewaySignal(
                configured=True, registration="test", panel="test",
                reason="test signal")

        monkeypatch.setattr(gateway_ensure, "machine_gateway_signal",
                            counting_signal)
        payload = mgd.status_payload(folders)
        assert calls["n"] == 1, "the machine signal is probed ONCE per call"
        by_folder = payload["folders"]
        assert set(by_folder) == {str(f) for f in folders}
        for entry in by_folder.values():
            assert entry["gate"]["state"] == "deliver"
            assert entry["claude_md_section"] == {"renders": True}
        # Single-folder back-compat: the top-level verdict stays for the
        # one-folder shape the launcher's agents-gate command reads.
        single = mgd.status_payload(folders[:1])
        assert single["gate"]["state"] == "deliver"
        assert single["claude_md_section"] == {"renders": True}
        assert set(single["folders"]) == {str(folders[0])}

    def test_batched_scan_reads_each_directory_once(self, tmp_path,
                                                    monkeypatch):
        """N-2 (v0.2.101 review): the per-folder dirs include the USER
        agents dir every time, so an N-folder call would report each
        ``~/.claude/agents`` problem N times. The scan list is deduped —
        each directory is read exactly once."""
        seen = []

        def recording_scan(dirs):
            seen.extend(str(d) for d in dirs)
            return []

        monkeypatch.setattr(mgd, "check_agent_model_ids", recording_scan)
        folders = [tmp_path / "a", tmp_path / "b", tmp_path / "c"]
        for f in folders:
            f.mkdir()
        mgd.status_payload(folders)
        duplicates = {d for d in seen if seen.count(d) > 1}
        assert duplicates == set(), (
            f"every directory must be scanned exactly once; duplicates: "
            f"{duplicates}")

    def test_batched_keys_echo_the_callers_exact_folder_strings(
            self, tmp_path, monkeypatch):
        """N-3 (v0.2.101 review): the answer is keyed by the EXACT folder
        string the caller sent. ``str(Path(...))`` normalises (drops a
        trailing slash, resolves ``.``), so a launcher.db ``folder_path``
        in non-canonical form would miss its own verdict and sit at
        "could not ask" forever."""
        _pin_machine_signal(monkeypatch, True)
        raw = [str(tmp_path) + "/", "./" + tmp_path.name]
        payload = mgd.status_payload(raw)
        assert set(payload["folders"]) == set(raw), (
            "the keys must echo the caller's strings verbatim, not the "
            f"normalised {set(payload['folders'])}")


class TestModulesVerdictSource:
    def test_diagrams_disabled_row_drops_it(self, tmp_path):
        folder = tmp_path / "proj"
        folder.mkdir()
        db = _db(tmp_path, "db-diag", folder, [("diagrams", 0)])
        assert cms.active_sections(
            folder, db_path=db, needed={"diagrams"}) == frozenset()

    def test_unregistered_folder_gets_the_default_on_set(self, tmp_path):
        folder = tmp_path / "proj"
        folder.mkdir()
        db = _db(tmp_path, "db-other", tmp_path, [])  # another folder registered
        assert "diagrams" in cms.active_sections(
            folder, db_path=db, needed={"diagrams"})

    def test_project_id_override_skips_folder_resolution(self, tmp_path):
        folder = tmp_path / "proj"
        folder.mkdir()
        db = _db(tmp_path, "db-pid", folder, [("diagrams", 0)])
        # The override names the registered UUID directly — the row applies
        # even though nothing resolved the folder.
        assert cms.active_sections(
            folder, db_path=db, project_id=UUID,
            needed={"diagrams"}) == frozenset()


class TestNeededFilter:
    def test_untagged_template_probes_nothing(self, tmp_path, monkeypatch):
        """``needed`` is the hermeticity + cost contract: a document that
        carries no tag for a feature never triggers that feature's probe (the
        license probe in particular may contact the licensing backend)."""
        folder = tmp_path / "proj"
        folder.mkdir()
        calls: list[str] = []

        def _spy(name, result):
            def _fn(*_a, **_k):
                calls.append(name)
                return result
            return _fn

        monkeypatch.setattr(cms, "_module_sections", _spy("modules", set()))
        monkeypatch.setattr(cms, "_gateway_renders", _spy("gateway", True))
        monkeypatch.setattr(cms, "_lean_ctx_renders", _spy("lean", True))
        monkeypatch.setattr(cms, "_rl_renders", _spy("rl", True))
        assert cms.active_sections(
            folder, needed=cms.tagged_features("no tags here at all")
        ) == frozenset()
        assert calls == []
        # And with tags, exactly the tagged features are probed.
        assert cms.active_sections(
            folder, needed=cms.tagged_features(
                "{{#if_module_active lean_ctx}}\nx\n{{/if_module_active}}\n"
                "{{#if_module_inactive rl_retrieval}}\ny\n{{/if_module_inactive}}")
        ) == {cms.LEAN_CTX, cms.RL_RETRIEVAL}
        assert calls == ["lean", "rl"]

    def test_tagged_features_reads_both_polarities(self):
        assert cms.tagged_features(
            "{{#if_module_active diagrams}}\n{{/if_module_active}}\n"
            "{{#if_module_inactive model_gateway}}\n{{/if_module_inactive}}\n"
            "plain {{UPPER_SNAKE}} stays out") == {"diagrams", "model_gateway"}


# ---------------------------------------------------------------------------
# T2 — D4: the bundle path resolves the project UUID
# ---------------------------------------------------------------------------

def _project_orch(tmp_path: Path) -> Path:
    orch = tmp_path / "orch"
    (orch / "templates").mkdir(parents=True)
    (orch / "vct-module.json").write_text("{}\n", encoding="utf-8")
    (orch / "templates" / "CLAUDE.md.template").write_bytes(
        USER_TEMPLATE.read_bytes())
    return orch


class TestBundlePathResolvesProjectUuid:
    """RED before the D4 fix: `project_templates` passed `str(folder)` to a
    resolver keyed on the launcher.db UUID, so an explicit row could never
    match and the section silently rendered (or dropped) on defaults alone."""

    def test_gateway_enabled_row_renders_the_section(self, tmp_path, monkeypatch):
        orch = _project_orch(tmp_path)
        project = tmp_path / "proj"
        project.mkdir()
        db = _db(tmp_path, "db-gw", project, [("model_gateway", 1)])
        monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
        pt.install_project_level_templates(
            project, orchestrator_root=orch, project_name="Proj", dry_run=False)
        text = (project / "CLAUDE.md").read_text(encoding="utf-8")
        assert "## Model routing" in text
        assert "{{#" not in text and "{{/if_" not in text

    def test_diagrams_disabled_row_drops_the_section(self, tmp_path, monkeypatch):
        orch = _project_orch(tmp_path)
        project = tmp_path / "proj"
        project.mkdir()
        db = _db(tmp_path, "db-diag", project, [("diagrams", 0)])
        monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
        _pin_machine_signal(monkeypatch, False)  # keep the gateway leg moot
        pt.install_project_level_templates(
            project, orchestrator_root=orch, project_name="Proj", dry_run=False)
        text = (project / "CLAUDE.md").read_text(encoding="utf-8")
        assert "## Diagrams (Mermaid + Excalidraw)" not in text
        assert "## Model routing" not in text
        # The rest of the managed body rendered.
        assert "## VCO Paths" in text

    def test_launcher_path_keeps_its_project_id_override(self, tmp_path, monkeypatch):
        """`render_claude_md` (the launcher's toggle path) passes the id it
        already holds; the row must decide without any folder resolution."""
        orch = _project_orch(tmp_path)
        project = tmp_path / "proj"
        project.mkdir()
        db = _db(tmp_path, "db-launcher", tmp_path, [("diagrams", 0)])
        with contextlib.redirect_stderr(io.StringIO()):
            result = pt.render_claude_md(
                project, orchestrator_root=orch, project_name="Proj",
                project_id=UUID, db_path=db)
        assert "diagrams" not in result["active_modules"]
        text = (project / "CLAUDE.md").read_text(encoding="utf-8")
        assert "## Diagrams (Mermaid + Excalidraw)" not in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
