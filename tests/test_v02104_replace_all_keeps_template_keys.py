# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — ``install.py --adopt-project-replace-all`` no longer drops the
settings template's own keys.

The flag's contract (``--help``): "overwrite pre-existing whitelist files …
when you want VCO's defaults to land identically to a fresh install". Its
settings.json write used ``_build_vco_settings_defaults()`` alone — sentinel,
three ``permissions.allow`` entries, ``env`` — so ``hooks``,
``skillOverrides``, ``idleCompaction`` and the shipped permission denies were
gone from the root until the next bundle update. It now writes what a fresh
install lands: the shipped template (minus the hooks the launcher parked, as
the bundle's create branch does) with the defaults filled in
(``vco_lib.self_install.replace_all_settings``).

What replace-all still resets: every user edit — extra keys, extra allow
entries, a user ``idleCompaction: true``, a user ``env`` value.

RED-PROOF: restore the Case 2 branch of ``_configure_claude_settings`` to
``settings_file.write_text(rendered_defaults, …)`` → the keep-template tests
fail (no ``idleCompaction`` / ``hooks``).
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import bundle_settings_io, parked_hooks  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "install_py_v02104_replace_all", REPO_ROOT / "install.py")
assert _spec is not None and _spec.loader is not None
install_py = importlib.util.module_from_spec(_spec)
sys.modules["install_py_v02104_replace_all"] = install_py
_spec.loader.exec_module(install_py)

_EMBED = {"text_model": "qwen3-embedding:0.6b", "active_embedding": "qwen3",
          "code_backend": "ollama"}


@pytest.fixture
def root(tmp_path, monkeypatch):
    """An orchestrator root carrying the real shipped settings templates."""
    r = tmp_path / "orch"
    (r / "templates").mkdir(parents=True)
    for name in ("settings.json.linux.template", "settings.json.windows.template"):
        shutil.copy(REPO_ROOT / "templates" / name, r / "templates" / name)
    (r / ".claude").mkdir()
    monkeypatch.setattr(install_py, "PROJECT_ROOT", r)
    return r


def _template(root: Path) -> dict:
    return json.loads(bundle_settings_io.settings_template_path(root).read_text(encoding="utf-8"))


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _commands(hooks: dict) -> list[str]:
    return [item.get("command", "") for groups in hooks.values() for g in groups
            for item in g.get("hooks", [])]


def _user_file(root: Path) -> Path:
    path = root / ".claude" / "settings.json"
    path.write_text(json.dumps({
        "_vco_managed_keys": ["env", "hooks"],
        "idleCompaction": True,
        "userExtension": "reset-by-replace-all",
        "permissions": {"allow": ["Bash(user-only *)"]},
        "env": {"EMBEDDING_MODEL": "user-model", "USER_ONLY": "1"},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "user.sh"}]}]},
    }), encoding="utf-8")
    return path


def _replace_all(root: Path) -> dict:
    install_py._configure_claude_settings(_EMBED, adopt_project_mode="replace-all")
    return json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_replace_all_keeps_idle_compaction_off_and_the_shipped_hooks(root):
    _user_file(root)
    out = _replace_all(root)
    tmpl = _template(root)
    assert out["idleCompaction"] is False
    assert out["hooks"] == tmpl["hooks"]
    assert out["skillOverrides"] == tmpl["skillOverrides"]
    assert out["permissions"]["deny"] == tmpl["permissions"]["deny"]
    # The defaults still land: sentinel, env, the default allow entries.
    assert out["_vco_managed_keys"] == ["env", "hooks"]
    assert out["env"]["EMBEDDING_MODEL"] == "qwen3-embedding:0.6b"
    for entry in ("Bash(git *)", "Bash(python *)", "Bash(.claude/scripts/*)"):
        assert entry in out["permissions"]["allow"]


def test_replace_all_still_resets_every_user_edit(root):
    _user_file(root)
    out = _replace_all(root)
    assert "userExtension" not in out
    assert "Bash(user-only *)" not in out["permissions"]["allow"]
    assert "USER_ONLY" not in out["env"]
    assert "user.sh" not in _commands(out["hooks"])


def test_replace_all_equals_what_a_fresh_install_lands(root):
    """Fresh install = bundle create (template) + step 9's fill-only adopt."""
    _user_file(root)
    out = _replace_all(root)
    created = root / "fresh.json"
    status, _ = bundle_settings_io.merge_settings_template(
        bundle_settings_io.settings_template_path(root), created, dry_run=False,
        write=_write, project_root=root)
    assert status == "created"
    from vco_lib import self_install
    expected = self_install.adopt_bundle_created_settings(
        json.loads(created.read_text(encoding="utf-8")),
        install_py._build_vco_settings_defaults(_EMBED),
        install_py._VCO_MANAGED_KEYS_SENTINEL)
    assert out == expected


def test_replace_all_keeps_a_launcher_parked_hook_out(root, monkeypatch):
    tmpl = _template(root)
    group = tmpl["hooks"]["Stop"][0]
    command = group["hooks"][0]["command"]
    state = parked_hooks.ParkedHooksState(
        readable=True, source="launcher_db",
        hooks=(parked_hooks.ParkedHook(event="Stop", matcher=group.get("matcher", ""),
                                       command=command),))
    monkeypatch.setattr(parked_hooks, "read_parked_hooks", lambda folder, **_k: state)
    _user_file(root)
    out = _replace_all(root)
    assert command in _commands(tmpl["hooks"])
    assert command not in _commands(out.get("hooks", {}))
    # Every other shipped hook is there.
    assert out["hooks"]["SessionStart"] == tmpl["hooks"]["SessionStart"]


def test_replace_all_with_no_template_leaves_the_file_alone(root, capsys):
    path = _user_file(root)
    before = path.read_bytes()
    for t in (root / "templates").iterdir():
        t.unlink()
    install_py._configure_claude_settings(_EMBED, adopt_project_mode="replace-all")
    assert path.read_bytes() == before
    assert "NOT replaced" in capsys.readouterr().err


def test_build_defaults_is_unchanged_for_the_merge_callers():
    """The managed-block merge (Case 4) and step 9's adopt still take the
    small defaults dict — adding the template there would replace the user's
    hooks on every install (``hooks`` is a managed key)."""
    d = install_py._build_vco_settings_defaults(_EMBED)
    assert set(d) == {"_vco_managed_keys", "permissions", "env"}
