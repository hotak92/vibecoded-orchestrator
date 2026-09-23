# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — the env projection no longer destroys a JSONC settings file.

``config_projection._write_json_env_block`` (the ``.claude/settings.json``
``env`` and ``.vscode/settings.json`` ``claude-code.env`` surfaces) read the
file with :func:`json.loads` and treated ANY failure as ``{}`` — so a
workspace ``.vscode/settings.json`` with one comment in it (VS Code's own
format) was rewritten as just the env block: every other setting gone. The
user-secret STRIP writer did the same.

Both now read JSONC and edit the original text (``vco_lib.jsonc_edit``);
an edit that cannot be verified writes nothing and raises
``SettingsWriteRefused`` saying why.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vco_lib import config_projection as cp
from vco_lib import jsonc_edit

ENV_KEY = "claude-code.env"

HEAD = (
    "{\n"
    "    // the team's workspace settings — hand-maintained\n"
    '    "editor.formatOnSave": true,\n'
    '    "files.exclude": {"**/.git": true}, /* keep */\n'
    f'    "{ENV_KEY}": {{\n'
    '        "USER_KEY": "mine", // do not touch\n'
)
TAIL = "    },\n}\n"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".vscode" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_the_canonical_env_is_written_into_a_jsonc_file_in_place(tmp_path: Path):
    path = _write(tmp_path, HEAD + '        "KG_COLLECTION": "Old_KG",\n' + TAIL)

    written = cp._write_json_env_block(
        path, {"KG_COLLECTION": "New_KG", "PROJECT_NAME": "Proj"},
        {"KG_COLLECTION", "PROJECT_NAME"}, env_key=ENV_KEY,
    )

    assert written == ["KG_COLLECTION", "PROJECT_NAME"]
    text = path.read_text(encoding="utf-8")
    assert text.startswith(HEAD), "every byte before the env members is untouched"
    data = jsonc_edit.loads(text)
    assert data["editor.formatOnSave"] is True and data["files.exclude"] == {"**/.git": True}
    assert data[ENV_KEY] == {"USER_KEY": "mine", "KG_COLLECTION": "New_KG", "PROJECT_NAME": "Proj"}
    assert "/* keep */" in text and "// do not touch" in text


def test_a_canonical_key_is_removed_from_a_jsonc_file_in_place(tmp_path: Path):
    path = _write(tmp_path, HEAD + '        "KG_COLLECTION": "Old_KG",\n' + TAIL)

    cp._write_json_env_block(path, {}, {"KG_COLLECTION"}, env_key=ENV_KEY)

    assert path.read_text(encoding="utf-8") == HEAD + TAIL


def test_a_key_removal_edits_a_jsonc_file_in_place(tmp_path: Path):
    """The removal-only editor (``strip_env_keys`` — which superseded the
    retired strip-by-name verb) edits JSONC in place."""
    path = _write(tmp_path, HEAD + '        "MY_TOKEN": "x",\n' + TAIL)

    stripped = cp.strip_env_keys(tmp_path, "vscode_settings_json", ["MY_TOKEN"])

    assert stripped == ["MY_TOKEN"]
    assert path.read_text(encoding="utf-8") == HEAD + TAIL


def test_an_unverifiable_jsonc_edit_writes_nothing_and_says_why(tmp_path: Path):
    """A duplicate key: setting one copy would leave the other governing.

    v0.2.97: the refusal is RAISED (``SettingsWriteRefused``) rather than
    printed on stderr and reported as "no keys" — through the launcher's CLI
    spawn an exit-0 stderr line was invisible, so the file silently stayed
    stale. The raise reaches the caller's warning surface and the deferral
    ledger (``vco_lib.settings_refusal``)."""
    raw =HEAD + '        "KG_COLLECTION": "a",\n        "KG_COLLECTION": "b",\n' + TAIL
    path = _write(tmp_path, raw)

    with pytest.raises(cp.SettingsWriteRefused) as info:
        cp._write_json_env_block(
            path, {"KG_COLLECTION": "New_KG"}, {"KG_COLLECTION"}, env_key=ENV_KEY,
        )

    assert path.read_text(encoding="utf-8") == raw
    msg = str(info.value)
    assert str(path) in msg and "NOT updated" in msg and "JSONC" in msg
    assert info.value.refusals[0].kind == "jsonc_edit_refused"


def test_a_strict_json_file_keeps_the_rust_parity_layout(tmp_path: Path):
    """Unchanged for strict JSON: 2-space indent, no trailing newline."""
    path = _write(tmp_path, json.dumps({"a": 1}))
    cp._write_json_env_block(path, {"K": "v"}, {"K"}, env_key=ENV_KEY)
    assert path.read_text(encoding="utf-8") == json.dumps(
        {"a": 1, ENV_KEY: {"K": "v"}}, indent=2, ensure_ascii=False,
    )
