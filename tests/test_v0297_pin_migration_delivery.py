# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — the one-time panel Default-pin migration actually reaches a machine.

v0.2.95 wired ``vco_lib.vscode_settings.migrate_default_pins`` into every
install/update through ``vco_lib.machine_migrations``, and every test was
green. In the field it never ran once: install.py imported ``vscode_settings``
IN-PROCESS, and that module imports ``model_router``, which only the install's
venv provides. The launcher starts install.py on the system interpreter, so
every update logged ``No module named 'model_router'`` to ``install.jsonl`` —
where nobody looks — and moved on. pytest runs inside the venv, which is why
no test ever saw it.

So these tests drive the REAL caller (``install._run_machine_migrations``)
with the in-process ``model_router`` import POISONED: the leg must succeed
anyway, because it now runs as a child of the venv interpreter. A regression
back to an in-process import turns ``test_update_removes_a_first_party_pin``
red.

Containment: the child's ledger (``VCT_STATE_DIR``), its VS Code targets
(``VCT_VSCODE_SETTINGS_FILES`` plus a throwaway user home / XDG / APPDATA) and
the auto-resolution trail (``install.PROJECT_ROOT``) all live under
``tmp_path``. The real ``~/.config/Code`` and ``~/.vct`` are never in scope.
"""
from __future__ import annotations

import ast
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

import install
from tests.common.child_env import child_env
from vco_lib import install_companions
from vco_lib import machine_migrations as mm
from vco_lib.deferral_report import DeferralEntry, DeferralReport

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_BLOCK_KEY = "claudeCode.environmentVariables"
MODEL_KEY = "ANTHROPIC_MODEL"
OPUS_1M = "claude-opus-5[1m]"
GLM_1M = "claude-gw/glm-5.3[1m]"
LEDGER_REL = Path("model-gateway") / "vscode-default-pin-migration.json"


class Harness:
    """What one test needs: where things land, and what the caller said."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "orchestrator-root"
        self.root.mkdir()
        self.state = tmp_path / "vct-root"
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.user_dir = tmp_path / "Code" / "User"
        self.user_dir.mkdir(parents=True)
        self.settings = self.user_dir / "settings.json"
        self.events: list[tuple[str, str, str]] = []
        self.report = DeferralReport()

    @property
    def ledger(self) -> Path:
        return self.state / LEDGER_REL

    def write_settings(self, model: str | None) -> bytes:
        block: dict[str, Any] = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:11436"}
        if model is not None:
            block[MODEL_KEY] = model
        payload = {"editor.fontSize": 13, ENV_BLOCK_KEY: block}
        self.settings.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
        return self.settings.read_bytes()

    def block(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8")).get(ENV_BLOCK_KEY, {})

    def run_update_step(self) -> None:
        """The update's own call: ``install._run_machine_migrations(report)``."""
        install._run_machine_migrations(self.report)

    def auto_resolutions(self) -> list[dict]:
        trail = self.root / ".claude" / "logs" / "auto-resolutions.jsonl"
        if not trail.is_file():
            return []
        return [json.loads(line) for line in trail.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def harness(tmp_path: Path, monkeypatch) -> Harness:
    h = Harness(tmp_path)

    # The CHILD's world. It inherits os.environ, so the pins go there; the
    # import pins come from `child_env` (this checkout's vco_lib AND its
    # claude_mcp_servers first, ahead of whatever the venv's editable finder
    # names).
    pinned = child_env(
        VCT_STATE_DIR=str(h.state),
        VCT_VSCODE_SETTINGS_FILES=str(h.settings),
        VCT_USER_HOME_OVERRIDE=str(h.home),
        XDG_CONFIG_HOME=str(h.home / ".config"),
        APPDATA=str(h.home / "AppData" / "Roaming"),
    )
    for key, value in pinned.items():
        if os.environ.get(key) != value:
            monkeypatch.setenv(key, value)

    # The install's venv interpreter = the one running pytest (it has the
    # venv's packages); the checkout the child runs from = a throwaway root,
    # so the auto-resolution trail lands under tmp_path.
    monkeypatch.setattr(
        install_companions, "resolve_install_venv_python",
        lambda _root, **_kw: Path(sys.executable),
    )
    monkeypatch.setattr(install, "PROJECT_ROOT", h.root)
    monkeypatch.setattr(
        install, "_log_install_event", lambda step, phase, detail="", **_kw: h.events.append((step, phase, detail)),
    )
    # The metrics leg is not under test here and reads its own state.
    monkeypatch.setattr(mm, "_metrics_archive", lambda _event: {"ok": True, "status": "skipped"})

    # install.py's OWN interpreter cannot import model_router (the launcher
    # case). An in-process `from vco_lib.vscode_settings import ...` now fails
    # exactly as it did in the field; the child is unaffected.
    monkeypatch.setitem(sys.modules, "model_router", None)
    monkeypatch.setitem(sys.modules, "model_router.fileperms", None)
    monkeypatch.delitem(sys.modules, "vco_lib.vscode_settings", raising=False)
    return h


# ---------------------------------------------------------------------------
# act
# ---------------------------------------------------------------------------


def test_update_removes_a_first_party_pin(harness: Harness, capsys):
    """The update step removes the pre-ruling pin, writes ledger + backup, and
    says so on every surface: stdout, install event, auto-resolution trail."""
    harness.write_settings(OPUS_1M)

    harness.run_update_step()

    assert MODEL_KEY not in harness.block(), "the pin must be gone"
    assert harness.block()["ANTHROPIC_BASE_URL"], "nothing else is touched"
    ledger = json.loads(harness.ledger.read_text(encoding="utf-8"))
    (target,) = ledger["targets"]
    assert target["status"] == "removed" and target["value"] == OPUS_1M
    backup = Path(target["backup_path"])
    assert backup.is_file()
    assert OPUS_1M in backup.read_text(encoding="utf-8"), "the backup holds the old file"

    out = capsys.readouterr().out
    assert OPUS_1M in out and str(harness.settings) in out and str(backup) in out
    assert any(
        step == mm.STEP_PANEL_PIN and phase == "ok" for step, phase, _ in harness.events
    ), harness.events
    (row,) = harness.auto_resolutions()
    assert row["condition_id"] == mm.AUTO_RESOLUTION_PANEL_PIN
    assert OPUS_1M in row["detail"] and str(backup) in row["detail"]
    assert not harness.report.has_condition(mm.CID_PANEL_PIN_FAILED)


def test_a_successful_run_clears_an_earlier_failure(harness: Harness):
    """The failure entry a previous update left behind is cleared by the run
    that proves the migration ran — its paired resolution."""
    harness.report.add_entry(
        DeferralEntry(
            condition_id=mm.CID_PANEL_PIN_FAILED, title="t", detected="d",
            why_deferred="w", command_to_apply="c",
        )
    )
    harness.write_settings(OPUS_1M)

    harness.run_update_step()

    assert not harness.report.has_condition(mm.CID_PANEL_PIN_FAILED)


# ---------------------------------------------------------------------------
# leave alone
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_ledger_present_opens_no_settings_file(harness: Harness):
    """Once per MACHINE: with the ledger present, a pin set afterwards is the
    user's. The settings file is made unreadable — any attempt to open it
    would surface as a refusal — and the child reports no target at all."""
    harness.ledger.parent.mkdir(parents=True)
    harness.ledger.write_text("{}", encoding="utf-8")
    before = harness.write_settings(OPUS_1M)
    harness.settings.chmod(0)
    try:
        harness.run_update_step()
    finally:
        harness.settings.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert harness.settings.read_bytes() == before
    assert harness.auto_resolutions() == []
    assert not harness.report.has_condition(mm.CID_PANEL_PIN_FAILED)
    assert not any(phase == "warn" for _step, phase, _ in harness.events), harness.events


def test_a_non_first_party_pin_is_left_byte_for_byte(harness: Harness, capsys):
    """A gateway/vendor id is provably not VCO's (both write sites refuse one):
    kept, reported, untouched. The migration still counts as having run."""
    before = harness.write_settings(GLM_1M)

    harness.run_update_step()

    assert harness.settings.read_bytes() == before
    ledger = json.loads(harness.ledger.read_text(encoding="utf-8"))
    assert [t["status"] for t in ledger["targets"]] == ["kept"]
    assert "never wrote" in capsys.readouterr().out
    assert harness.auto_resolutions() == []


# ---------------------------------------------------------------------------
# soft-fail, visible
# ---------------------------------------------------------------------------


def _assert_visible_failure(harness: Harness, out: str, reason_fragment: str) -> None:
    entry = harness.report.entry_for(mm.CID_PANEL_PIN_FAILED)
    assert entry is not None, "a leg that could not run must reach UPDATE_DEFERRED.md"
    assert reason_fragment in entry.detected
    assert mm.CID_PANEL_PIN_FAILED in out and reason_fragment in out
    assert any(
        step == mm.STEP_PANEL_PIN and phase == "warn" and reason_fragment in detail
        for step, phase, detail in harness.events
    ), harness.events


def test_no_venv_is_a_visible_soft_fail(harness: Harness, monkeypatch, capsys):
    """No venv interpreter: the update goes on, and the user is told."""
    monkeypatch.setattr(install_companions, "resolve_install_venv_python", lambda _root, **_kw: None)
    before = harness.write_settings(OPUS_1M)

    harness.run_update_step()  # must not raise

    assert harness.settings.read_bytes() == before
    assert not harness.ledger.exists(), "nothing ran, so nothing is recorded as done"
    _assert_visible_failure(harness, capsys.readouterr().out, "no venv interpreter")


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell script stands in for a broken venv")
def test_a_broken_venv_child_is_a_visible_soft_fail(harness: Harness, monkeypatch, capsys, tmp_path):
    """The venv's own import fails (a BROKEN install): loud, with its stderr."""
    broken = tmp_path / "broken-python"
    broken.write_text(
        "#!/bin/sh\necho \"ModuleNotFoundError: No module named 'model_router'\" >&2\nexit 1\n",
        encoding="utf-8",
    )
    broken.chmod(0o755)
    monkeypatch.setattr(install_companions, "resolve_install_venv_python", lambda _root, **_kw: broken)
    harness.write_settings(OPUS_1M)

    harness.run_update_step()

    _assert_visible_failure(harness, capsys.readouterr().out, "No module named 'model_router'")


def test_a_jsonc_settings_file_is_migrated_through_the_update(harness: Harness):
    """VS Code's own file with a comment: read, the pin cut out, the comment kept."""
    harness.settings.write_text(
        "{\n    // my pin\n"
        f'    "{ENV_BLOCK_KEY}": {{"{MODEL_KEY}": "{OPUS_1M}"}},\n}}\n',
        encoding="utf-8",
    )

    harness.run_update_step()

    after = harness.settings.read_text(encoding="utf-8")
    assert "// my pin" in after and MODEL_KEY not in after
    assert not harness.report.has_condition(mm.CID_PANEL_PIN_FAILED)


def test_an_uncheckable_file_is_reported_and_retried_until_it_is_read(harness: Harness, capsys):
    """A file the migration cannot check stays owed: reported on every update
    until a run reads it, and that run clears the entry."""
    harness.settings.write_text(
        json.dumps({ENV_BLOCK_KEY: "not an object"}), encoding="utf-8",
    )
    before = harness.settings.read_bytes()

    harness.run_update_step()

    assert harness.settings.read_bytes() == before
    _assert_visible_failure(harness, capsys.readouterr().out, "every update retries")

    harness.write_settings(OPUS_1M)  # the user fixes it; a pre-ruling pin is in it
    harness.run_update_step()

    assert MODEL_KEY not in harness.block()
    assert not harness.report.has_condition(mm.CID_PANEL_PIN_FAILED)


# ---------------------------------------------------------------------------
# the call site in main(): after the venv exists
# ---------------------------------------------------------------------------


def _top_level_call(body: list, name: str) -> "ast.Call | None":
    """The ``name(...)`` call that is itself a statement of ``body`` — not one
    nested under an ``if`` / ``try`` / ``with``, which could skip it."""
    for stmt in body:
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
            and stmt.value.func.id == name
        ):
            return stmt.value
    return None


def test_main_runs_the_migrations_after_the_venv_step_with_the_run_report():
    """STRUCTURAL, and stated as such (review F14): ``main()`` is a long
    sequential flow no unit test can drive, so this reads its AST — which a
    name in a comment cannot satisfy — rather than executing it. The
    ``--lightweight`` and ``--uninstall`` legs below ARE executed.

    What the AST can prove, it checks: exactly one call anywhere in
    ``main()``; that call and step 5's ``_install_requirements`` are both
    top-level statements of ``main()``'s body (so no branch inside ``main()``
    can skip one and not the other); the migration comes after the venv
    step (which is what makes it run on a FIRST install); and it is handed
    the run's deferral report. What it cannot prove is which EARLY RETURN a
    given argv takes — the two driven tests below cover the two that matter.
    """
    tree = ast.parse((REPO_ROOT / "install.py").read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    everywhere = [
        n for n in ast.walk(main)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_run_machine_migrations"
    ]
    assert len(everywhere) == 1, "exactly one call site"
    call = _top_level_call(main.body, "_run_machine_migrations")
    requirements = _top_level_call(main.body, "_install_requirements")
    assert call is not None, "the call must not sit under a branch of main()"
    assert requirements is not None, "step 5 must be a top-level statement of main()"
    assert call.lineno > requirements.lineno
    assert [a.id for a in call.args if isinstance(a, ast.Name)] == ["_deferral_report"]


# ---------------------------------------------------------------------------
# which install paths migrate: install / update / lightweight — not uninstall
# ---------------------------------------------------------------------------


def test_lightweight_reinstall_runs_the_migrations_with_its_report(monkeypatch):
    """``--lightweight`` is an install run on an existing machine (the
    launcher's re-install / relocate path), and the module's contract is
    "every install and update". It has a venv by then (triage ran), so it
    migrates, into its own deferral report."""
    import argparse

    from tests.test_install_lightweight import _ProjectRootFixture

    calls: list = []
    monkeypatch.setattr(install, "_run_machine_migrations", lambda report: calls.append(report))
    with _ProjectRootFixture() as fx:
        (fx.root / ".venv" / "bin").mkdir(parents=True)
        fake_py = fx.root / ".venv" / "bin" / "python"
        fake_py.write_text(
            f"#!/usr/bin/env bash\necho '{sys.version_info.major}.{sys.version_info.minor}'\n",
            encoding="utf-8",
        )
        fake_py.chmod(0o755)
        (fx.root / "requirements.txt").write_text("foo==1.0\n", encoding="utf-8")
        install._record_state_hashes(fx.root)
        args = argparse.Namespace(
            lightweight=True, lightweight_old_path=None, no_containers=True, dev=False,
        )
        assert install._run_lightweight(args) == 0

    assert len(calls) == 1 and isinstance(calls[0], DeferralReport)


def test_uninstall_does_not_migrate(monkeypatch):
    """``--uninstall`` removes VCO: rewriting the user's VS Code settings on
    the way out is not what they asked for, and the ledger would land in the
    state being removed. main() dispatches it before the venv step, which is
    where the migrations run — driven here, not read."""
    calls: list = []
    monkeypatch.setattr(install, "_run_machine_migrations", lambda report: calls.append(report))
    monkeypatch.setattr(install, "_run_uninstall", lambda _args: 0)
    # --suppress-lean-ctx-warning: that pre-flight reads ~/.claude directly,
    # which the W-CLAUDE guard (rightly) refuses to let a test do.
    monkeypatch.setattr(
        install.sys, "argv",
        ["install.py", "--uninstall", "--yes", "--suppress-lean-ctx-warning"],
    )

    assert install.main() == 0
    assert calls == []
