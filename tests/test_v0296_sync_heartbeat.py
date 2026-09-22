# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.96 ship-gate MAJOR-1: the seed child's ``[VCO-EVENT]`` heartbeat
producer in ``templates/scripts/sync_knowledge_graph.py``.

WP-1 built the transport (the relay in ``vco_lib/child_process.py``) and
WP-8 built the consumer (the update stall watchdog in
``update_pipeline.rs``), but no lane built the PRODUCER: the seed child
emitted zero ``[VCO-EVENT]`` lines, so every legitimate GUI seed longer
than the watchdog window (default 600 s) produced a false "update may be
stalled" notice. The WP-1 relay tests pinned the transport against a
fictional emitter; these tests pin the real one.

Pinned here, in-process (the child-side transport is pinned end-to-end in
``tests/test_v0296_child_process.py``):

* the env gate — ``VCO_PROGRESS_STREAM=1`` (the launcher is the only
  setter) emits, an unset gate stays silent on CLI runs;
* the beat shape — one ``start`` per tree walk, one throttled ``ok`` tick
  per ``_HEARTBEAT_MIN_INTERVAL_S`` at most, one closing ``ok``; a fast
  walk emits ONLY the two bracketing beats (no per-node spam);
* the GUI parse contract, mirrored from ``update_pipeline.rs``
  ``read_stdout`` + ``installer_step_to_user_label``: step token, phase
  ``start``/``ok``, single line, 0 < detail < 200 chars;
* the per-chunk hook stays silent unless a walk armed it;
* the finalize regen announces itself before its (captured, up to 600 s)
  silence;
* install.py's seed env carries ``VCO_PROGRESS_STREAM`` through
  ``_subprocess_env_with_embedding``'s ``os.environ.copy()`` — the
  threading assumption the whole gate rests on.

All synthetic: no Weaviate, no embedding backend, no network.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"


def _load(project_root: Path):
    """Load a FRESH sync_knowledge_graph module pinned at *project_root*.

    Fresh instance per call: the heartbeat's module-level throttle state
    (``_heartbeat_last_emit``) must not leak between tests.
    """
    import os

    os.environ["KG_BASE_DIR"] = str(project_root)
    os.environ["KG_COLLECTION"] = "TestProject_KnowledgeGraph"
    os.environ["DEVELOPMENT_COLLECTION"] = "TestProject_Development"
    os.environ["DUAL_EMBEDDING_ENABLED"] = "false"
    os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"

    mod_name = f"_skg_hb_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:
        raise pytest.skip(
            f"sync_knowledge_graph.py runtime deps missing ({exc}); skipping.")
    return mod


def _seed_tree(root: Path, n: int) -> None:
    kn = root / "knowledge" / "concepts"
    kn.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (kn / f"node-{i}.md").write_text(
            f"---\ntitle: Node {i}\n---\n\nbody {i}", encoding="utf-8")


def _stub_sync(mod, outcome: str) -> list[Path]:
    """Replace ``sync_node`` with an instant fake; returns the files seen."""
    seen: list[Path] = []

    def fake(server, md_file):  # noqa: ANN001 — matches the real signature
        seen.append(md_file)
        return mod.SyncOutcome(outcome, mod._relative_file_path(md_file))

    mod.sync_node = fake
    return seen


def _event_lines(capsys) -> list[str]:
    out = capsys.readouterr().out
    return [ln for ln in out.splitlines() if ln.startswith("[VCO-EVENT]")]


class _Server:
    """Stand-in server object — the stubbed sync_node never touches it."""


class TestTheEmitterGate:
    def test_gated_walk_emits_start_and_done_beats(self, tmp_path, capsys,
                                                   monkeypatch):
        root = tmp_path / "proj"
        _seed_tree(root, 3)
        mod = _load(root)
        monkeypatch.setenv("VCO_PROGRESS_STREAM", "1")
        _stub_sync(mod, mod.OUTCOME_SYNCED)

        tally = mod.sync_all_nodes(_Server())

        assert tally.succeeded == 3
        events = _event_lines(capsys)
        # A FAST walk emits exactly the two bracketing beats — the throttle
        # suppresses every mid-walk tick (the "must not spam small syncs"
        # requirement).
        assert events == [
            "[VCO-EVENT] kg-sync start syncing knowledge: 3 nodes",
            "[VCO-EVENT] kg-sync ok synced knowledge: 3 nodes "
            "(3 ok, 0 failed, 0 skipped)",
        ]

    def test_gate_off_cli_run_is_silent(self, tmp_path, capsys, monkeypatch):
        root = tmp_path / "proj"
        _seed_tree(root, 3)
        mod = _load(root)
        monkeypatch.delenv("VCO_PROGRESS_STREAM", raising=False)
        _stub_sync(mod, mod.OUTCOME_SYNCED)

        mod.sync_all_nodes(_Server())

        assert _event_lines(capsys) == [], (
            "a CLI run (no VCO_PROGRESS_STREAM) must not emit event lines"
        )

    def test_docs_walk_emits_beats_too(self, tmp_path, capsys, monkeypatch):
        root = tmp_path / "proj"
        _seed_tree(root, 1)
        (root / "docs").mkdir()
        (root / "docs" / "guide.md").write_text("doc body", encoding="utf-8")
        mod = _load(root)
        monkeypatch.setenv("VCO_PROGRESS_STREAM", "1")
        seen: list[Path] = []
        mod.sync_doc = lambda server, md: (
            seen.append(md),
            mod.SyncOutcome(mod.OUTCOME_SYNCED, str(md)),
        )[1]

        mod.sync_all_docs(_Server())

        assert len(seen) == 1
        events = _event_lines(capsys)
        assert events[0] == "[VCO-EVENT] kg-sync start syncing docs: 1 nodes"
        assert events[-1].startswith(
            "[VCO-EVENT] kg-sync ok synced docs: 1 nodes (1 ok")


class TestTheThrottle:
    def test_ticks_are_time_based_not_per_node(self, tmp_path, capsys,
                                               monkeypatch):
        root = tmp_path / "proj"
        _seed_tree(root, 3)
        mod = _load(root)
        monkeypatch.setenv("VCO_PROGRESS_STREAM", "1")
        # Force the throttle always-due: every non-final node now ticks.
        mod._HEARTBEAT_MIN_INTERVAL_S = -1.0
        _stub_sync(mod, mod.OUTCOME_SYNCED)

        mod.sync_all_nodes(_Server())

        events = _event_lines(capsys)
        assert len(events) == 4, events  # start + ticks for idx 1,2 + done
        assert any("knowledge: 1/3 nodes (1 ok" in ln for ln in events)
        assert any("knowledge: 2/3 nodes (2 ok" in ln for ln in events)

    def test_chunk_hook_requires_an_armed_walk(self, tmp_path, capsys,
                                               monkeypatch):
        root = tmp_path / "proj"
        _seed_tree(root, 1)
        mod = _load(root)
        monkeypatch.setenv("VCO_PROGRESS_STREAM", "1")

        mod._heartbeat_note_chunk()
        assert _event_lines(capsys) == [], (
            "a single-file CLI run never armed a walk — the chunk hook "
            "must stay silent"
        )

        mod._emit_sync_event("start", "syncing knowledge: 1 nodes")
        mod._HEARTBEAT_MIN_INTERVAL_S = -1.0
        mod._heartbeat_note_chunk()
        assert any("chunk-level progress" in ln for ln in _event_lines(capsys))


class TestTheGuiParseContract:
    """Python mirror of update_pipeline.rs's read_stdout filter +
    installer_step_to_user_label: only these shapes reach the modal."""

    def test_every_emitted_line_parses(self, tmp_path, capsys, monkeypatch):
        root = tmp_path / "proj"
        _seed_tree(root, 4)
        mod = _load(root)
        monkeypatch.setenv("VCO_PROGRESS_STREAM", "1")
        mod._HEARTBEAT_MIN_INTERVAL_S = -1.0
        _stub_sync(mod, mod.OUTCOME_SYNCED)

        mod.sync_all_nodes(_Server())
        mod._emit_sync_event("ok", "x" * 500)  # pathological long detail

        events = _event_lines(capsys)
        assert len(events) >= 5
        for ln in events:
            assert ln.startswith("[VCO-EVENT] "), ln
            rest = ln[len("[VCO-EVENT] "):]
            step, phase, detail = (rest.split(" ", 2) + [""])[:3]
            assert step == "kg-sync", (
                "the step token is deliberately unknown to "
                "installer_step_to_user_label so the detail surfaces"
            )
            assert phase in {"start", "ok"}, ln
            assert 0 < len(detail) < 200, (
                "the unknown-step fallback drops empty or >=200-char "
                f"details: {ln!r}"
            )

    def test_multiline_detail_collapses_to_one_line(self, tmp_path, capsys,
                                                    monkeypatch):
        root = tmp_path / "proj"
        mod = _load(root)
        monkeypatch.setenv("VCO_PROGRESS_STREAM", "1")

        mod._emit_sync_event("ok", "line1\nline2\r\nline3")

        events = _event_lines(capsys)
        assert events == ["[VCO-EVENT] kg-sync ok line1 line2 line3"]

    def test_emission_never_raises(self, tmp_path, monkeypatch):
        mod = _load(tmp_path / "proj")
        monkeypatch.setenv("VCO_PROGRESS_STREAM", "1")
        real_write = sys.stdout.write

        def broken_write(_text):
            raise OSError("parent stdout gone")

        monkeypatch.setattr(sys.stdout, "write", broken_write)
        try:
            mod._emit_sync_event("start", "must not raise")
            mod._heartbeat_note_chunk()
        finally:
            monkeypatch.setattr(sys.stdout, "write", real_write)


class TestTheFinalizeBeat:
    def test_regen_announces_itself_before_its_captured_silence(
            self, tmp_path, capsys, monkeypatch):
        """The post-summary regen runs capture_output=True for up to 600 s —
        without a beat right before it, that silence lands on the update
        watchdog's clock with the modal showing nothing new."""
        root = tmp_path / "proj"
        _seed_tree(root, 1)
        mod = _load(root)
        monkeypatch.setenv("VCO_PROGRESS_STREAM", "1")
        gen = (tmp_path / "claude_mcp_servers" / "scripts"
               / "generate_node_formats.py")
        gen.parent.mkdir(parents=True)
        gen.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

        import vco_lib.python_exe as pex
        monkeypatch.setattr(pex, "resolve_install_root", lambda: tmp_path)
        ran: list[tuple] = []
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: ran.append(a)
            or types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        )

        mod._regen_node_formats_after_full_sync()

        assert len(ran) == 1, "the regen must actually have been spawned"
        assert any(
            "refreshing KG summaries" in ln for ln in _event_lines(capsys)), (
            "the regen must announce itself BEFORE its captured silence"
        )


class TestTheEnvThreading:
    def test_seed_env_carries_the_gate_from_install_py(self, monkeypatch):
        """install.py's `_subprocess_env_with_embedding()` (the base of both
        seed_env builds) starts from os.environ.copy() — the launcher's
        VCO_PROGRESS_STREAM=1 therefore reaches this child with NO install.py
        change. Pin that assumption so a future curated env allow-list
        cannot silently drop it."""
        import install

        monkeypatch.setenv("VCO_PROGRESS_STREAM", "1")
        monkeypatch.setenv("ACTIVE_EMBEDDING", "qwen3")  # env-explicit: no DB read
        env = install._subprocess_env_with_embedding()
        assert env.get("VCO_PROGRESS_STREAM") == "1"
