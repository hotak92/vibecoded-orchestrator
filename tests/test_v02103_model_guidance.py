# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Provider-aware model guidance (v0.2.103, plan item 12).

The defect this pins: the ``{{MODEL_SELECTION_GRID}}`` probe read a TRANSIENT
secret-store failure (keychain busy, vct-hub restarting mid-update) as "no
key", and the rendered guidance silently dropped the provider's rows. The
probe is now TRI-STATE — present / absent / unavailable — and an unavailable
answer falls back to the last-known provider record rather than dropping
rows. A refresh engine (``vco_lib.model_guidance``) re-renders the
provider-dependent guidance through the SAME materialize/splice code the
update uses, writing only when the output changed.

Pinned here:

* the tri-state mapping and the retry-then-record fallback (a transient
  failure keeps the recorded rows; a definitive absence removes them);
* the record lifecycle (written only on a definitive probe round, and only
  when it changed);
* the provider-conditional CLAUDE.md sections (``provider_zai`` & co. render
  iff the gateway section renders AND the vendor is reachable);
* the refresh engine end to end on a synthetic install: key added → rows
  appear; key removed → rows gone; unchanged output → no write; the user
  section outside the managed markers is byte-identical afterwards.
"""

from __future__ import annotations

import io
import json
import sys
import contextlib
from pathlib import Path

import pytest

from vco_lib import claude_md_sections as cms
from vco_lib import model_guidance
from vco_lib import model_selection
from vco_lib import project_init as _pi

REPO = Path(__file__).resolve().parent.parent
PROJECT_TEMPLATE = REPO / "templates" / "CLAUDE.md.template"

VENDORS = ("zai", "qwen", "kimi")


def _state_probe(states: dict[str, "model_selection.KeyState"],
                 calls: list[str] | None = None):
    """A tri-state probe driven by a dict; optionally records the call order."""
    def probe(vendor_id: str) -> "model_selection.KeyState":
        if calls is not None:
            calls.append(vendor_id)
        return states[vendor_id]
    return probe


def _no_sleep(_seconds: float) -> None:
    return None


def _fake_vendors_module(monkeypatch: pytest.MonkeyPatch, vendors: dict) -> None:
    """Stand a fake ``model_router.vendors`` in sys.modules (pyright-clean)."""
    import types

    module = types.ModuleType("model_router.vendors")
    setattr(module, "VENDORS", vendors)
    monkeypatch.setitem(sys.modules, "model_router.vendors", module)


class TestTriStateProbe:
    def test_keyresult_maps_to_three_states(self, monkeypatch):
        """present / absent / unavailable from the gateway's own KeyResult —
        the ``transient`` flag is what separates a busy store from a miss."""
        import model_router.secrets as secrets

        class _Resolver:
            def __init__(self, result):
                self._result = result

            def resolve(self, _vendor):
                return self._result

        vendors = {"zai": object(), "qwen": object(), "kimi": object()}
        _fake_vendors_module(monkeypatch, vendors)

        cases = [
            (secrets.KeyResult(key="k", state="resolved"),
             model_selection.KeyState.PRESENT),
            (secrets.KeyResult(problem="no key found"),
             model_selection.KeyState.ABSENT),
            (secrets.KeyResult(problem="busy", transient=True),
             model_selection.KeyState.UNAVAILABLE),
        ]
        for result, expected in cases:
            monkeypatch.setattr(secrets, "VendorKeyResolver",
                                lambda _r=result: _Resolver(_r))
            probe = model_selection.vendor_key_state_probe()
            assert probe("zai") is expected

    def test_bool_probe_still_folds_transient_into_a_miss(self, monkeypatch):
        """The delivery gate's bool probe keeps its contract: a transient
        store answers False (a miss never deletes; the held copy carries
        forward)."""
        import model_router.secrets as secrets

        class _Resolver:
            def resolve(self, _vendor):
                return secrets.KeyResult(problem="busy", transient=True)

        monkeypatch.setattr(secrets, "VendorKeyResolver", lambda: _Resolver())
        _fake_vendors_module(monkeypatch, {"zai": object()})
        probe = model_selection.vendor_key_probe()
        assert probe("zai") is False


class TestTransientKeepsRows:
    def test_unavailable_falls_back_to_the_record(self, tmp_path):
        """THE field defect: the store is momentarily unreadable mid-update,
        the record says zai's key was there — the zai rows MUST survive."""
        record = tmp_path / "model-guidance.json"
        record.write_text(json.dumps({
            "version": 1,
            "providers": {"zai": True, "qwen": True, "kimi": False},
        }), encoding="utf-8")
        states = {v: model_selection.KeyState.UNAVAILABLE for v in VENDORS}
        providers = model_selection.reachable_providers(
            state_probe=_state_probe(states), state_path=record,
            sleeper=_no_sleep)
        assert providers == frozenset({"anthropic", "zai", "qwen"})
        md = model_selection.render_grid(providers=providers)
        assert "GLM-5.3 †" in md and "Qwen3.8-Max †" in md
        assert "Kimi K3" not in md

    def test_unavailable_is_retried_before_the_record_is_consulted(
            self, tmp_path):
        record = tmp_path / "model-guidance.json"
        calls: list[str] = []
        states = {v: model_selection.KeyState.UNAVAILABLE for v in VENDORS}
        model_selection.reachable_providers(
            state_probe=_state_probe(states, calls), state_path=record,
            sleeper=_no_sleep)
        # Every vendor probed twice (the initial pass + one retry pass)
        # before the (absent) record decides.
        assert calls == sorted(VENDORS) + sorted(VENDORS)

    def test_a_retry_that_recovers_never_touches_the_record(self, tmp_path):
        record = tmp_path / "model-guidance.json"
        record.write_text(json.dumps({
            "version": 1, "providers": {"zai": False},
        }), encoding="utf-8")
        attempts = {"n": 0}

        def probe(vendor_id: str) -> "model_selection.KeyState":
            if vendor_id != "zai":
                return model_selection.KeyState.ABSENT
            attempts["n"] += 1
            return (model_selection.KeyState.UNAVAILABLE if attempts["n"] == 1
                    else model_selection.KeyState.PRESENT)

        providers = model_selection.reachable_providers(
            state_probe=probe, state_path=record, sleeper=_no_sleep)
        assert "zai" in providers
        # The recovered answer is the truth now: the record is rewritten.
        saved = json.loads(record.read_text(encoding="utf-8"))
        assert saved["providers"]["zai"] is True

    def test_unavailable_without_a_record_renders_claude_only(
            self, tmp_path, capsys):
        states = {v: model_selection.KeyState.UNAVAILABLE for v in VENDORS}
        providers = model_selection.reachable_providers(
            state_probe=_state_probe(states),
            state_path=tmp_path / "no-record.json", sleeper=_no_sleep)
        assert providers == model_selection.ALWAYS_PROVIDERS
        err = capsys.readouterr().err
        assert "unavailable" in err and "last-known" in err

    def test_a_definitive_absence_drops_the_recorded_provider(self, tmp_path):
        """Key REMOVED: the store answers a clean miss — the rows go, and the
        record follows so the next transient outage keeps them gone."""
        record = tmp_path / "model-guidance.json"
        record.write_text(json.dumps({
            "version": 1,
            "providers": {"zai": True, "qwen": True, "kimi": True},
        }), encoding="utf-8")
        states = {"zai": model_selection.KeyState.PRESENT,
                  "qwen": model_selection.KeyState.ABSENT,
                  "kimi": model_selection.KeyState.ABSENT}
        providers = model_selection.reachable_providers(
            state_probe=_state_probe(states), state_path=record,
            sleeper=_no_sleep)
        assert providers == frozenset({"anthropic", "zai"})
        saved = json.loads(record.read_text(encoding="utf-8"))
        assert saved["providers"] == {"zai": True, "qwen": False,
                                      "kimi": False}

    def test_record_not_written_when_a_vendor_stayed_unavailable(
            self, tmp_path):
        """A round with any UNAVAILABLE vendor is not a truth about the
        machine — it must not overwrite the last-known record."""
        record = tmp_path / "model-guidance.json"
        original = {"version": 1,
                    "providers": {"zai": True, "qwen": False, "kimi": True}}
        record.write_text(json.dumps(original), encoding="utf-8")
        states = {"zai": model_selection.KeyState.UNAVAILABLE,
                  "qwen": model_selection.KeyState.PRESENT,
                  "kimi": model_selection.KeyState.ABSENT}
        providers = model_selection.reachable_providers(
            state_probe=_state_probe(states), state_path=record,
            sleeper=_no_sleep)
        # zai kept from the record, qwen seen live, kimi definitively gone.
        assert providers == frozenset({"anthropic", "zai", "qwen"})
        assert json.loads(record.read_text(encoding="utf-8")) == original

    def test_record_not_rewritten_when_unchanged(self, tmp_path):
        record = tmp_path / "model-guidance.json"
        record.write_text(json.dumps({
            "version": 1,
            "providers": {"zai": True, "qwen": False, "kimi": False},
        }), encoding="utf-8")
        before = record.stat().st_mtime_ns
        states = {"zai": model_selection.KeyState.PRESENT,
                  "qwen": model_selection.KeyState.ABSENT,
                  "kimi": model_selection.KeyState.ABSENT}
        model_selection.reachable_providers(
            state_probe=_state_probe(states), state_path=record,
            sleeper=_no_sleep)
        assert record.stat().st_mtime_ns == before

    def test_a_corrupt_record_is_no_record(self, tmp_path, capsys):
        record = tmp_path / "model-guidance.json"
        record.write_text("{ not json", encoding="utf-8")
        states = {v: model_selection.KeyState.UNAVAILABLE for v in VENDORS}
        providers = model_selection.reachable_providers(
            state_probe=_state_probe(states), state_path=record,
            sleeper=_no_sleep)
        assert providers == model_selection.ALWAYS_PROVIDERS

    def test_provider_override_short_circuits_the_probe(self, tmp_path):
        """The refresh run resolves the set ONCE for the whole process:
        every later render in it must see exactly that set."""
        model_selection.set_provider_override(
            frozenset({"anthropic", "kimi"}))
        try:
            providers = model_selection.reachable_providers(
                state_path=tmp_path / "ignored.json")
            assert providers == frozenset({"anthropic", "kimi"})
            md = model_selection.render_grid()
            # Row fragments are the discriminators — the Sonnet note NAMES
            # GLM-5.3 in prose (same trap test_model_selection_grid documents).
            assert "Kimi K3 †" in md and "| GLM-5.3 † (" not in md
        finally:
            model_selection.set_provider_override(None)


class TestProviderSections:
    def _pin_gateway(self, monkeypatch, renders: bool) -> None:
        monkeypatch.setattr(cms, "_gateway_renders",
                            lambda *a, **k: renders)

    def _pin_providers(self, monkeypatch, providers) -> None:
        monkeypatch.setattr(model_selection, "reachable_providers",
                            lambda **k: frozenset(providers))

    def test_provider_ids_are_registered_features(self):
        for fid in ("provider_zai", "provider_qwen", "provider_kimi",
                    "provider_glm"):
            assert fid in cms.ALL_FEATURES

    def test_zai_section_renders_only_with_gateway_and_key(
            self, tmp_path, monkeypatch):
        folder = tmp_path / "proj"
        folder.mkdir()
        self._pin_providers(monkeypatch, {"anthropic", "zai"})
        self._pin_gateway(monkeypatch, True)
        assert "provider_zai" in cms.active_sections(
            folder, needed={"provider_zai"})
        self._pin_gateway(monkeypatch, False)
        assert "provider_zai" not in cms.active_sections(
            folder, needed={"provider_zai"})

    def test_glm_section_needs_either_glm_vendor(self, tmp_path, monkeypatch):
        folder = tmp_path / "proj"
        folder.mkdir()
        self._pin_gateway(monkeypatch, True)
        self._pin_providers(monkeypatch, {"anthropic", "qwen"})
        sections = cms.active_sections(
            folder, needed={"provider_glm", "provider_zai"})
        assert "provider_glm" in sections
        assert "provider_zai" not in sections

    def test_a_probe_failure_renders_the_section(self, tmp_path, monkeypatch,
                                                 capsys):
        """The conservative contract holds for provider sections too: a
        could-not-ask never hides text."""
        folder = tmp_path / "proj"
        folder.mkdir()
        self._pin_gateway(monkeypatch, True)

        def boom(**_k):
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(model_selection, "reachable_providers", boom)
        assert "provider_kimi" in cms.active_sections(
            folder, needed={"provider_kimi"})
        assert "provider probe failed" in capsys.readouterr().err


class TestTemplateConditionals:
    """The provider-specific prose of the project template renders exactly
    when its provider section is active (the completeness gate renders the
    two feature extremes; these pin the per-provider middle)."""

    def _render(self, active: set[str]) -> str:
        return _pi.render_conditional_blocks(
            PROJECT_TEMPLATE.read_text(encoding="utf-8"),
            active_modules=active)

    def test_zai_only_renders_the_zai_lane_and_glm_routing(self):
        out = self._render({"model_gateway", "provider_zai", "provider_glm"})
        assert "- **The z.ai lane**" in out
        assert "The qwen-vendor lane" not in out
        assert "The kimi-vendor lane" not in out
        assert "When you route to GLM" in out
        # The provider-neutral teaching survives every provider mix.
        assert "{{MODEL_SELECTION_GRID}}" in out
        assert "conditional on YOU having supplied" in out
        assert "Window size is known" in out

    def test_qwen_renders_glm_routing_too(self):
        out = self._render({"model_gateway", "provider_qwen", "provider_glm"})
        assert "The qwen-vendor lane" in out
        assert "- **The z.ai lane**" not in out
        assert "When you route to GLM" in out

    def test_no_vendor_keys_renders_no_lane_and_no_glm_routing(self):
        out = self._render({"model_gateway"})
        assert "## Model routing" in out
        assert "- **The z.ai lane**" not in out
        assert "The qwen-vendor lane" not in out
        assert "The kimi-vendor lane" not in out
        assert "When you route to GLM" not in out
        assert "{{MODEL_SELECTION_GRID}}" in out
        assert "{{#" not in out and "{{/if_" not in out

    def test_gateway_off_renders_nothing_vendor_shaped(self):
        out = self._render(set()).lower()
        for token in ("glm", "claude-gw", "model routing", "kimi"):
            assert token not in out

    def test_kimi_note_has_no_machine_local_budget(self):
        """Machine-local budget/quota facts live in the owner's memory, not
        in the shipped grid (owner ruling 2026-10-14)."""
        grid = model_selection.load_grid()
        kimi_notes = [n.text for n in grid.notes if "kimi-k3" in n.models]
        assert kimi_notes
        for text in kimi_notes:
            assert "5h window" not in text
            assert "subscription budget" not in text
            # The task-fit guidance survives.
            assert "bounded lanes or audits" in text

    def test_intro_points_at_the_automatic_refresh(self):
        grid = model_selection.load_grid()
        joined = "\n".join(grid.intro)
        assert "re-run the update to add its rows" not in joined
        assert "refreshes automatically" in joined


# ---------------------------------------------------------------------------
# The refresh engine, end to end on a synthetic install
# ---------------------------------------------------------------------------


def _synthetic_install(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A fake orchestrator root + launcher.db + one registered project whose
    CLAUDE.md carries the managed markers. The root gets a stub venv
    interpreter so the render has no missing-path finding (a finding would
    emit a deferral row, whose ledger injects the reminder block at the top
    of CLAUDE.md and muddies the splice assertions)."""
    root = tmp_path / "root"
    (root / "templates").mkdir(parents=True)
    (root / "templates" / "CLAUDE.md.template").write_bytes(
        PROJECT_TEMPLATE.read_bytes())
    (root / "templates" / "ORCHESTRATOR-CLAUDE.md.template").write_bytes(
        (REPO / "templates" / "ORCHESTRATOR-CLAUDE.md.template").read_bytes())
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    from tests.common.launcher_db_fixture import make_launcher_db

    db = make_launcher_db(
        tmp_path / "db",
        projects=[{"project_id": "p1", "name": "Proj",
                   "folder_path": str(project)}])
    return root, project, db


def _seed_project_claude_md(project: Path, *, user_text: str) -> None:
    (project / "CLAUDE.md").write_text(
        f"{user_text}\n\n{_pi.MANAGED_REGION_OPEN}\nold body\n"
        f"{_pi.MANAGED_REGION_CLOSE}\n",
        encoding="utf-8")


def _refresh(root: Path, db: Path, state_path: Path,
             states: dict[str, "model_selection.KeyState"],
             monkeypatch: pytest.MonkeyPatch):
    """One refresh run with the probe and the section resolver pinned."""
    monkeypatch.setattr(
        cms, "_gateway_renders", lambda *a, **k: True)
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        result = model_guidance.refresh_guidance(
            root, db_path=db, state_path=state_path,
            state_probe=_state_probe(states), sleeper=_no_sleep)
    return result, stderr.getvalue()


class TestRefreshEngine:
    def test_key_added_makes_rows_appear_on_refresh(
            self, tmp_path, monkeypatch):
        root, project, db = _synthetic_install(tmp_path)
        _seed_project_claude_md(project, user_text="# Mine\nuser text stays")
        state_path = tmp_path / "state" / "model-guidance.json"
        claude = project / "CLAUDE.md"

        states = {v: model_selection.KeyState.ABSENT for v in VENDORS}
        result, _ = _refresh(root, db, state_path, states, monkeypatch)
        first = claude.read_text(encoding="utf-8")
        assert result["projects"][0]["changed"] is True
        # Row fragments are the discriminators: the rendered grid's Sonnet
        # note NAMES GLM-5.3 in prose (test_model_selection_grid's own trap).
        assert "| GLM-5.3 † (" not in first and "Kimi K3 †" not in first

        # The user adds a z.ai key; the next refresh renders the GLM rows.
        states["zai"] = model_selection.KeyState.PRESENT
        result, notice = _refresh(root, db, state_path, states, monkeypatch)
        second = claude.read_text(encoding="utf-8")
        assert result["projects"][0]["changed"] is True
        assert "| GLM-5.3 † (" in second
        assert "- **The z.ai lane**" in second
        assert "The kimi-vendor lane" not in second
        assert "model guidance refreshed" in notice
        assert "z.ai (GLM) rows added" in notice

    def test_key_removed_makes_rows_disappear(self, tmp_path, monkeypatch):
        root, project, db = _synthetic_install(tmp_path)
        _seed_project_claude_md(project, user_text="# Mine")
        state_path = tmp_path / "state" / "model-guidance.json"
        claude = project / "CLAUDE.md"

        states = {v: model_selection.KeyState.ABSENT for v in VENDORS}
        states["zai"] = model_selection.KeyState.PRESENT
        _refresh(root, db, state_path, states, monkeypatch)
        assert "| GLM-5.3 † (" in claude.read_text(encoding="utf-8")

        states["zai"] = model_selection.KeyState.ABSENT
        result, _ = _refresh(root, db, state_path, states, monkeypatch)
        assert result["projects"][0]["changed"] is True
        text = claude.read_text(encoding="utf-8")
        assert "| GLM-5.3 † (" not in text
        assert "- **The z.ai lane**" not in text

    def test_unchanged_output_writes_nothing(self, tmp_path, monkeypatch):
        root, project, db = _synthetic_install(tmp_path)
        _seed_project_claude_md(project, user_text="# Mine")
        state_path = tmp_path / "state" / "model-guidance.json"
        claude = project / "CLAUDE.md"
        states = {v: model_selection.KeyState.ABSENT for v in VENDORS}
        _refresh(root, db, state_path, states, monkeypatch)
        mtime = claude.stat().st_mtime_ns
        result, _ = _refresh(root, db, state_path, states, monkeypatch)
        assert result["projects"][0]["changed"] is False
        assert claude.stat().st_mtime_ns == mtime

    def test_user_section_is_byte_identical_after_refresh(
            self, tmp_path, monkeypatch):
        root, project, db = _synthetic_install(tmp_path)
        user_text = "# My project\n\nMy own instructions, do not touch."
        _seed_project_claude_md(project, user_text=user_text)
        state_path = tmp_path / "state" / "model-guidance.json"
        claude = project / "CLAUDE.md"
        states = {v: model_selection.KeyState.ABSENT for v in VENDORS}
        states["kimi"] = model_selection.KeyState.PRESENT
        _refresh(root, db, state_path, states, monkeypatch)
        text = claude.read_text(encoding="utf-8")
        assert text.startswith(user_text)
        assert "Kimi K3 †" in text  # the refresh DID change the managed body

    def test_a_project_without_managed_markers_is_left_alone(
            self, tmp_path, monkeypatch):
        root, project, db = _synthetic_install(tmp_path)
        claude = project / "CLAUDE.md"
        claude.write_text("# entirely the user's file\n", encoding="utf-8")
        state_path = tmp_path / "state" / "model-guidance.json"
        states = {v: model_selection.KeyState.ABSENT for v in VENDORS}
        result, _ = _refresh(root, db, state_path, states, monkeypatch)
        assert claude.read_text(encoding="utf-8") == (
            "# entirely the user's file\n")
        assert result["projects"] == []

    def test_a_failing_project_does_not_stop_the_others(
            self, tmp_path, monkeypatch):
        root, project, db = _synthetic_install(tmp_path)
        _seed_project_claude_md(project, user_text="# Mine")
        state_path = tmp_path / "state" / "model-guidance.json"
        states = {v: model_selection.KeyState.ABSENT for v in VENDORS}

        def boom(*_a, **_k):
            raise OSError("disk full")

        import vco_lib.project_templates as project_templates

        monkeypatch.setattr(project_templates, "render_claude_md", boom)
        result, notice = _refresh(root, db, state_path, states, monkeypatch)
        assert result["projects"][0]["status"] == "failed"
        assert "refresh failed" in notice

    def test_root_auto_block_is_refreshed_through_the_same_renderer(
            self, tmp_path, monkeypatch):
        """The orchestrator root goes through ``rendered_root_files`` — the
        SAME marker-splice the update uses — never a second renderer."""
        root, project, db = _synthetic_install(tmp_path)
        from vco_lib import rendered_root_files as rrf

        entry = rrf.entries()[0]
        target = root / Path(entry.path)
        target.write_text(
            f"user head\n{entry.begin_marker}\nold\n{entry.end_marker}\n"
            "user tail\n", encoding="utf-8")
        state_path = tmp_path / "state" / "model-guidance.json"
        states = {v: model_selection.KeyState.ABSENT for v in VENDORS}
        states["zai"] = model_selection.KeyState.PRESENT
        result, _ = _refresh(root, db, state_path, states, monkeypatch)
        text = target.read_text(encoding="utf-8")
        # The deferral ledger may inject its reminder block ABOVE everything
        # (by design — the synthetic root's render emits a first-run row);
        # what the splice must guarantee is that the user's text outside the
        # AUTO markers survives, not its absolute position.
        assert "user head" in text
        assert text.endswith("user tail\n")
        assert "| GLM-5.3 † (" in text
        assert entry.path in result["root"]["changed"]

    def test_last_refresh_is_recorded_for_the_gui(self, tmp_path, monkeypatch):
        root, project, db = _synthetic_install(tmp_path)
        _seed_project_claude_md(project, user_text="# Mine")
        state_path = tmp_path / "state" / "model-guidance.json"
        states = {v: model_selection.KeyState.ABSENT for v in VENDORS}
        _refresh(root, db, state_path, states, monkeypatch)
        states["kimi"] = model_selection.KeyState.PRESENT
        _refresh(root, db, state_path, states, monkeypatch)
        record = json.loads(state_path.read_text(encoding="utf-8"))
        refresh = record["last_refresh"]
        assert refresh["added"] == ["kimi"]
        assert refresh["projects_changed"] >= 1
        assert refresh["at"]

    def test_status_payload_reports_live_states_and_the_record(
            self, tmp_path):
        state_path = tmp_path / "state" / "model-guidance.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({
            "version": 1,
            "providers": {"zai": True, "qwen": False, "kimi": True},
            "last_refresh": {"at": "2026-10-14T10:00:00+00:00",
                             "added": ["kimi"], "removed": [],
                             "projects_changed": 2},
        }), encoding="utf-8")
        states = {"zai": model_selection.KeyState.PRESENT,
                  "qwen": model_selection.KeyState.UNAVAILABLE,
                  "kimi": model_selection.KeyState.ABSENT}
        payload = model_guidance.status_payload(
            state_path=state_path, state_probe=_state_probe(states),
            db_path=tmp_path / "no.db")
        assert payload["vendors"]["zai"]["state"] == "present"
        assert payload["vendors"]["qwen"]["state"] == "unavailable"
        assert payload["vendors"]["kimi"]["state"] == "absent"
        assert payload["vendors"]["zai"]["last_known"] is True
        assert payload["last_refresh"]["added"] == ["kimi"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
