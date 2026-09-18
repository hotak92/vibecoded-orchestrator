# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 Q1 — VCO does not pin the panel Default, and an old pin is removed.

USER RULING 2026-09-17, verbatim: "stop writing the pin, also because we have
the multimodel/remotecontrol switch that may conflict with it, model selection
is the one made by the user in Claude Code's GUI remembered across sessions".

``ANTHROPIC_MODEL`` is an ENV PIN and the documented precedence puts it ABOVE
the ``model`` key ``/model`` saves in ``~/.claude/settings.json`` — so every
launch returns to the pin whatever the user picked. That is the field report
("GUI shows GLM, requests go to Opus"). Two halves are pinned here:

1. **Nothing writes it by itself.** ``point`` writes ``ANTHROPIC_MODEL`` only
   for an explicit ``model=`` (``--model``, or the launcher's opt-in
   checkbox), and neither leg of the mode switch can introduce one.
2. **An existing pin is removed ONCE** — the migration that reaches a machine
   that already carries one, because a fix only new installs receive is a fix
   delivered nowhere. Once per machine: a pin the user sets AFTER the update
   is a deliberate post-ruling act and must survive every later update.

The 2026-09-08 ruling is UNTOUCHED and is re-pinned here from the other side:
no writer, migration included, may put a vendor id into the Default or a tier
slot. A vendor pin already in the file is also what proves the "ours vs
theirs" rule — VCO could never have written one, so the migration leaves it
alone rather than guessing.

Every test drives its own ledger and its own settings file under ``tmp_path``;
the real user's VS Code settings and ``~/.vct`` are never in scope (the
2026-09-10 lesson: a live panel session depends on that file).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from vco_lib import vscode_settings as vs

TOKEN = "q1-synthetic-host-token-not-a-real-credential"
BASE_URL = "http://127.0.0.1:11436"
OPUS = "claude-opus-5"
OPUS_1M = "claude-opus-5[1m]"
GLM_1M = "claude-gw/glm-5.3[1m]"


@pytest.fixture(autouse=True)
def _offline_gateway_probe(monkeypatch):
    """No test here may reach a real port."""
    monkeypatch.setattr(vs, "probe_gateway", lambda **_kw: vs.GATEWAY_STOPPED)


@pytest.fixture(autouse=True)
def _fresh_table_cache():
    """The loader is cached at module level; a stale one ignores a patch."""
    vs._CONTEXT_TABLE_LOADER = None
    yield
    vs._CONTEXT_TABLE_LOADER = None


@pytest.fixture()
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "state" / "model-gateway" / "vscode-default-pin-migration.json"


def _settings(tmp_path: Path, block: dict, *, name: str = "settings.json") -> Path:
    user = tmp_path / "Code" / "User"
    user.mkdir(parents=True, exist_ok=True)
    path = user / name
    payload = {
        "editor.fontSize": 13,
        vs.ENV_BLOCK_KEY: block,
        vs.LOGIN_PROMPT_KEY: True,
    }
    path.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
    return path


def _pointed(model: str | None = None, extra: dict | None = None) -> dict:
    block = {
        "ANTHROPIC_BASE_URL": BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": TOKEN,
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    }
    if model:
        block[vs.MODEL_KEY] = model
    block.update(extra or {})
    return block


def _block(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")).get(vs.ENV_BLOCK_KEY, {})


# ---------------------------------------------------------------------------
# 1. Nothing writes the pin by itself
# ---------------------------------------------------------------------------


def test_point_without_an_explicit_model_writes_no_default(tmp_path: Path):
    """The ruled state: pointing the panel leaves the Default unset."""
    path = _settings(tmp_path, {})
    out = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN)
    assert out["ok"], out["message"]
    assert vs.MODEL_KEY not in _block(path)
    assert vs.MODEL_KEY not in out["keys_written"]


def test_an_explicit_model_is_still_the_opt_in_pin(tmp_path: Path):
    """``--model`` remains, because the owner kept it: an explicit pin."""
    path = _settings(tmp_path, {})
    out = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN, model=OPUS)
    assert out["ok"]
    assert _block(path)[vs.MODEL_KEY].startswith(OPUS)
    assert vs.MODEL_KEY in out["keys_written"]


def test_neither_mode_leg_introduces_a_pin(tmp_path: Path, ledger: Path):
    """Round trip through both legs of the switch adds no Default.

    The switch stashes only what it DROPS, and the only ``ANTHROPIC_MODEL``
    it drops is a gateway-only id — which the restore refuses on the way
    back (2026-09-08). So neither leg can put a pin into a file that had
    none, whatever the stash holds.
    """
    stash = tmp_path / "state" / "model-gateway" / "vscode-mode-stash.json"
    path = _settings(tmp_path, _pointed())
    assert vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)["ok"]
    assert vs.MODEL_KEY not in _block(path)
    back = vs.set_mode(
        path, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash,
    )
    assert back["ok"]
    assert vs.MODEL_KEY not in _block(path)


def test_a_stashed_vendor_default_is_never_restored(tmp_path: Path):
    """The 2026-09-08 ruling, from the stash side: refused, and SAID."""
    stash = tmp_path / "state" / "model-gateway" / "vscode-mode-stash.json"
    path = _settings(tmp_path, _pointed(model=GLM_1M))
    out = vs.set_mode(path, vs.MODE_REMOTE_CONTROL, stash=stash)
    assert out["ok"]
    assert vs.MODEL_KEY in out["values_stashed"], "a vendor Default IS stashed"
    back = vs.set_mode(
        path, vs.MODE_MULTIMODEL, base_url=BASE_URL, token=TOKEN, stash=stash,
    )
    assert back["ok"]
    assert vs.MODEL_KEY not in _block(path)
    assert "claude-gw/glm-5.3" in (back["refusal_reason"] or "")


def test_no_writer_can_put_a_vendor_id_in_the_default_or_a_slot(tmp_path: Path):
    """CLOSURE over every writer, the migration included.

    A source scan would be satisfied by the rule's NAME in a comment; this
    drives each writer against a file seeded with a vendor id in the Default
    and in every slot, and asserts what is in the file afterwards.
    """
    slots = {key: GLM_1M for key in vs.SLOT_OVERRIDE_KEYS}
    writers = (
        ("point", lambda p: vs.point_at_gateway(p, base_url=BASE_URL, token=TOKEN)),
        (
            "point --model <vendor>",
            lambda p: vs.point_at_gateway(
                p, base_url=BASE_URL, token=TOKEN, model=GLM_1M,
            ),
        ),
        (
            "point restoring a vendor stash",
            lambda p: vs.point_at_gateway(
                p, base_url=BASE_URL, token=TOKEN,
                restore_env={vs.MODEL_KEY: GLM_1M},
            ),
        ),
        ("migrate", vs.migrate_default_pin),
    )
    for name, writer in writers:
        path = _settings(tmp_path, _pointed(extra=slots), name=f"{hash(name)}.json")
        writer(path)
        default = _block(path).get(vs.MODEL_KEY)
        assert not vs.is_gateway_only_model(default, None), (
            f"{name} left a gateway-only Default {default!r}"
        )
        # The slots are the user's own keys: preserved verbatim, never
        # rewritten into something else.
        assert {k: v for k, v in _block(path).items() if k in slots} == slots, name


# ---------------------------------------------------------------------------
# 2. The migration — what it removes, what it refuses to touch, and once
# ---------------------------------------------------------------------------


def test_a_first_party_pin_is_removed_and_named(tmp_path: Path, ledger: Path):
    path = _settings(tmp_path, _pointed(model=OPUS_1M))
    out = vs.migrate_default_pins(ledger=ledger, targets=[path])
    assert out["ok"] and out["status"] == "migrated"
    assert vs.MODEL_KEY not in _block(path)
    assert out["removed"] == [{"path": str(path), "value": OPUS_1M}]
    assert OPUS_1M in out["message"]
    # Reversible: the previous bytes are in the backup beside the file.
    backups = list(path.parent.glob(path.name + ".bak-*"))
    assert backups, "the removal must leave a backup"
    assert OPUS_1M in backups[0].read_text(encoding="utf-8")


def test_everything_else_in_the_file_survives_the_removal(tmp_path: Path, ledger: Path):
    """One key. The routing keys, the user's keys and the slots stay put."""
    extra = {"MY_OWN_KEY": "mine", "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-4-5"}
    path = _settings(tmp_path, _pointed(model=OPUS, extra=extra))
    before = json.loads(path.read_text(encoding="utf-8"))
    vs.migrate_default_pins(ledger=ledger, targets=[path])
    after = json.loads(path.read_text(encoding="utf-8"))
    del before[vs.ENV_BLOCK_KEY][vs.MODEL_KEY]
    assert after == before


def test_a_vendor_pin_is_not_ours_and_is_kept(tmp_path: Path, ledger: Path):
    """The ours/theirs rule: VCO never wrote a vendor id, so it never takes one.

    It is surfaced instead — the GUI already offers "Clear default" beside
    ``panel_mode``'s ``default_model_is_vendor``.
    """
    path = _settings(tmp_path, _pointed(model=GLM_1M))
    out = vs.migrate_default_pins(ledger=ledger, targets=[path])
    assert _block(path)[vs.MODEL_KEY] == GLM_1M
    assert out["kept"] == [{"path": str(path), "value": GLM_1M}]
    assert out["removed"] == []
    assert vs.panel_mode(path, ports=(11436,))["default_model_is_vendor"] is True


def test_it_runs_once_per_machine_and_never_eats_a_later_pin(
    tmp_path: Path, ledger: Path,
):
    """The property that makes this a MIGRATION and not a standing policy."""
    path = _settings(tmp_path, _pointed(model=OPUS_1M))
    first = vs.migrate_default_pins(ledger=ledger, targets=[path])
    assert first["status"] == "migrated" and first["ledger_written"]
    assert ledger.is_file()

    # The user deliberately pins one again, after the ruling.
    again = vs.point_at_gateway(path, base_url=BASE_URL, token=TOKEN, model=OPUS)
    assert again["ok"] and _block(path)[vs.MODEL_KEY].startswith(OPUS)

    second = vs.migrate_default_pins(ledger=ledger, targets=[path])
    assert second["status"] == "already-migrated"
    assert second["targets"] == [], "an existing ledger opens no file at all"
    assert _block(path)[vs.MODEL_KEY].startswith(OPUS), "their pin, kept"


def test_the_ledger_records_what_was_done(tmp_path: Path, ledger: Path):
    path = _settings(tmp_path, _pointed(model=OPUS_1M))
    vs.migrate_default_pins(ledger=ledger, targets=[path])
    doc = json.loads(ledger.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1
    row = doc["targets"][0]
    assert row["status"] == "removed" and row["value"] == OPUS_1M
    assert row["backup_path"]
    if os.name != "nt":
        assert oct(ledger.stat().st_mode)[-3:] == "600"


@pytest.mark.parametrize(
    "case",
    ["no-file", "no-key", "no-env-block", "empty-value"],
)
def test_the_removal_is_safe_when_there_is_nothing_to_remove(
    tmp_path: Path, ledger: Path, case: str,
):
    """"Safe when the key is absent" — in every shape absence takes."""
    if case == "no-file":
        path = tmp_path / "Code" / "User" / "settings.json"
    elif case == "no-key":
        path = _settings(tmp_path, _pointed())
    elif case == "no-env-block":
        path = tmp_path / "Code" / "User" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"editor.fontSize": 13}\n', encoding="utf-8")
    else:
        path = _settings(tmp_path, _pointed(model="   "))
    before = path.read_text(encoding="utf-8") if path.is_file() else None
    out = vs.migrate_default_pins(ledger=ledger, targets=[path])
    assert out["ok"] and out["removed"] == []
    assert out["targets"][0]["status"] == "absent"
    assert (path.read_text(encoding="utf-8") if path.is_file() else None) == before
    assert not list(path.parent.glob("*.bak-*")), "nothing to remove, nothing written"


def test_an_unparseable_file_is_refused_byte_for_byte(tmp_path: Path, ledger: Path):
    """JSONC is the user's; a file we cannot parse is never rewritten."""
    path = tmp_path / "Code" / "User" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = '{\n  // a comment VS Code allows\n  "editor.fontSize": 13,\n}\n'
    path.write_text(text, encoding="utf-8")
    out = vs.migrate_default_pins(ledger=ledger, targets=[path])
    assert out["targets"][0]["status"] == "refused"
    assert path.read_text(encoding="utf-8") == text


def test_a_ledger_that_cannot_be_written_does_not_lose_the_removal(
    tmp_path: Path, ledger: Path, monkeypatch,
):
    """Soft-fail on the RECORD, never on the work already done."""
    path = _settings(tmp_path, _pointed(model=OPUS_1M))

    def _boom(*_a, **_kw):
        raise OSError("read-only state dir")

    monkeypatch.setattr(vs, "_write_owner_only_json", _boom)
    out = vs.migrate_default_pins(ledger=ledger, targets=[path])
    assert out["ok"] and out["status"] == "migrated"
    assert out["ledger_written"] is False
    assert vs.MODEL_KEY not in _block(path)
    assert "next update" in out["message"]


# ---------------------------------------------------------------------------
# 3. The delivery path: install.py -> vco_lib.machine_migrations -> here
# ---------------------------------------------------------------------------


def test_machine_migrations_runs_the_pin_leg_and_discloses_it(
    tmp_path: Path, ledger: Path, monkeypatch,
):
    """The module install.py calls, driven — not a source scan.

    A removal from a file the USER owns is printed, not merely logged: that
    disclosure is what makes the one-time removal something they can undo.
    """
    from vco_lib import machine_migrations as mm

    path = _settings(tmp_path, _pointed(model=OPUS_1M))
    # Capture the real function BEFORE patching: a lambda that called the
    # patched name would recurse into itself, and the soft-fail would swallow
    # the RecursionError as a passing-looking 'error'.
    real = vs.migrate_default_pins
    monkeypatch.setattr(
        vs, "migrate_default_pins",
        lambda **_kw: real(ledger=ledger, targets=[path]),
    )
    monkeypatch.setattr(
        mm, "_metrics_archive", lambda _event: {"ok": True, "status": "skipped"},
    )
    lines: list[str] = []
    events: list[tuple] = []
    out = mm.run_every_run(
        on_event=lambda *a: events.append(a), emit=lines.append,
    )
    assert out["panel_default_pin"]["status"] == "migrated"
    assert vs.MODEL_KEY not in _block(path)
    assert any(OPUS_1M in line and str(path) in line for line in lines), lines
    assert any(step == mm.STEP_PANEL_PIN for step, _phase, _detail in events)


def test_install_py_calls_the_machine_migrations_helper(monkeypatch):
    """install.py's side of the wiring, driven through its own helper.

    The helper is what ``main()`` calls; this proves it reaches the module
    (and that a failing migration cannot break an install). The call SITE in
    ``main()`` is checked structurally below, because ``main()`` cannot be
    driven in a unit test.
    """
    import importlib

    install = importlib.import_module("install")
    from vco_lib import machine_migrations as mm

    calls: list[dict] = []
    monkeypatch.setattr(
        mm, "run_every_run", lambda **kw: calls.append(kw) or {},
    )
    install._run_machine_migrations()
    assert len(calls) == 1
    assert calls[0]["on_event"] is install._log_install_event

    def _boom(**_kw):
        raise RuntimeError("migration exploded")

    monkeypatch.setattr(mm, "run_every_run", _boom)
    install._run_machine_migrations()  # soft-fail: must not raise


def test_main_still_carries_the_call_site():
    """An AST check, and deliberately not a grep.

    ``main()`` is a 1.7k-line sequential flow that no unit test can drive, so
    the only available proof that the migration RUNS on every install is that
    the call node is inside it. A name in a comment or a docstring cannot
    satisfy this (an ``ast.Call`` is not text), which is the bar the rule
    about source scans sets.
    """
    import ast

    root = Path(__file__).resolve().parent.parent
    tree = ast.parse((root / "install.py").read_text(encoding="utf-8"))
    main = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    called = {
        node.func.id
        for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_run_machine_migrations" in called


def test_the_cli_subcommand_runs_the_migration(tmp_path: Path):
    """End to end through ``python -m vco_lib.vscode_settings``.

    ``child_env`` pins the checkout's import roots: without it the child can
    import a different ``vco_lib`` and the test measures the wrong tree.
    """
    path = _settings(tmp_path, _pointed(model=OPUS_1M))
    state = tmp_path / "vct-root"
    env = child_env(
        VCT_STATE_DIR=str(state),
        VCT_VSCODE_SETTINGS_FILES=str(path),
    )
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.vscode_settings", "migrate-default-pin"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "migrated"
    assert payload["removed"] == [{"path": str(path), "value": OPUS_1M}]
    assert vs.MODEL_KEY not in _block(path)
    assert (state / "model-gateway" / vs.PIN_MIGRATION_BASENAME).is_file()
