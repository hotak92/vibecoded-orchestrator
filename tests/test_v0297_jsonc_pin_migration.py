# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — the Default-pin migration reads JSONC and retries what it could not.

Before: VS Code's ``settings.json`` with a comment or a trailing comma (normal
there; VS Code writes and accepts both) was REFUSED, the once-per-machine
ledger was written anyway, and the file was never checked again — a pin in it
kept outranking the user's choice for good, with a notice that vanished after
one update.

Now:

* a JSONC file is READ and a pin in it is removed by editing the TEXT —
  every comment and byte elsewhere survives — through the one editor
  (``vco_lib.jsonc_edit``, tested on its own in ``test_v0297_jsonc_edit.py``),
  VERIFIED by re-parsing before anything is written;
* a file that still cannot be checked stays ``refused`` in the ledger, and
  every later run retries exactly those files until one is read.

Everything here runs in-process on files under ``tmp_path``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vco_lib import vscode_settings as vs

OPUS = "claude-opus-5"
OPUS_1M = "claude-opus-5[1m]"
GLM_1M = "claude-gw/glm-5.3[1m]"
ENV = vs.ENV_BLOCK_KEY
MODEL = vs.MODEL_KEY


@pytest.fixture(autouse=True)
def _offline_gateway_probe(monkeypatch):
    monkeypatch.setattr(vs, "probe_gateway", lambda **_kw: vs.GATEWAY_STOPPED)


@pytest.fixture()
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "state" / "model-gateway" / vs.PIN_MIGRATION_BASENAME


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "Code" / "User" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


JSONC_WITH_PIN = (
    "{\n"
    "    // my editor setup\n"
    '    "editor.fontSize": 13,\n'
    f'    "{ENV}": {{\n'
    '        "ANTHROPIC_BASE_URL": "http://127.0.0.1:11436", // the gateway\n'
    f'        "{MODEL}": "{OPUS_1M}",\n'
    '        "MY_KEY": "a // not a comment, /* nor this */",\n'
    "    },\n"
    "    /* trailing block comment */\n"
    "}\n"
)


# ---------------------------------------------------------------------------
# the migration on a JSONC file
# ---------------------------------------------------------------------------


def test_a_pin_in_a_jsonc_file_is_removed_with_its_comments_kept(tmp_path: Path, ledger: Path):
    path = _write(tmp_path, JSONC_WITH_PIN)

    out = vs.migrate_default_pins(ledger=ledger, targets=[path])

    (one,) = out["targets"]
    assert one["status"] == "removed" and one["value"] == OPUS_1M
    after = path.read_text(encoding="utf-8")
    assert "// my editor setup" in after and "/* trailing block comment */" in after
    assert "// the gateway" in after and MODEL not in after
    assert Path(one["backup_path"]).read_bytes() == JSONC_WITH_PIN.encode("utf-8")
    rows = json.loads(ledger.read_text(encoding="utf-8"))["targets"]
    assert [r["status"] for r in rows] == ["removed"]


def test_a_vendor_pin_in_a_jsonc_file_is_kept_byte_for_byte(tmp_path: Path, ledger: Path):
    path = _write(tmp_path, JSONC_WITH_PIN.replace(OPUS_1M, GLM_1M))
    before = path.read_bytes()
    out = vs.migrate_default_pins(ledger=ledger, targets=[path])
    assert out["targets"][0]["status"] == "kept"
    assert path.read_bytes() == before


def test_a_jsonc_file_without_a_pin_is_checked_and_untouched(tmp_path: Path, ledger: Path):
    path = _write(tmp_path, '{\n  // no pin here\n  "editor.fontSize": 13,\n}\n')
    before = path.read_bytes()
    out = vs.migrate_default_pins(ledger=ledger, targets=[path])
    assert out["targets"][0]["status"] == "absent"
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.bak-*"))


def test_an_edit_that_does_not_verify_is_refused_untouched(tmp_path: Path, ledger: Path):
    """A duplicate key: cutting one copy would leave the other governing, so
    the edit is refused, with nothing written. (Another key keeps the block
    alive — with the pin as its only key the whole block goes, correctly.)"""
    text = (
        f'{{\n  "{ENV}": {{\n    "A": "1",\n'
        f'    "{MODEL}": "{OPUS}",\n    "{MODEL}": "{OPUS_1M}",\n  }},\n}}\n'
    )
    path = _write(tmp_path, text)
    out = vs.migrate_default_pins(ledger=ledger, targets=[path])
    (one,) = out["targets"]
    assert one["status"] == "refused" and one["reason"] == "jsonc_duplicate_key"
    assert path.read_text(encoding="utf-8") == text
    assert not list(path.parent.glob("*.bak-*"))


# ---------------------------------------------------------------------------
# what still cannot be checked is retried — and only that
# ---------------------------------------------------------------------------


def test_an_uncheckable_file_is_retried_until_it_is_read(tmp_path: Path, ledger: Path):
    bad = _write(tmp_path, f'{{"{ENV}": "not an object"}}\n')
    good = tmp_path / "Other" / "User" / "settings.json"
    good.parent.mkdir(parents=True)
    good.write_text(json.dumps({ENV: {MODEL: OPUS}}), encoding="utf-8")

    first = vs.migrate_default_pins(ledger=ledger, targets=[bad, good])
    assert [t["status"] for t in first["targets"]] == ["refused", "removed"]
    assert ledger.is_file()

    # The user sets a post-ruling pin in the file that WAS checked: it is theirs.
    good.write_text(json.dumps({ENV: {MODEL: OPUS}}), encoding="utf-8")
    # And the unreadable file is fixed — with a pre-ruling pin still in it.
    bad.write_text(json.dumps({ENV: {MODEL: OPUS_1M}}), encoding="utf-8")

    second = vs.migrate_default_pins(ledger=ledger, targets=[bad, good])
    assert [t["path"] for t in second["targets"]] == [str(bad)], "only the refused file"
    assert second["targets"][0]["status"] == "removed"
    assert json.loads(good.read_text(encoding="utf-8"))[ENV][MODEL] == OPUS, "theirs, kept"
    rows = json.loads(ledger.read_text(encoding="utf-8"))["targets"]
    assert {r["path"]: r["status"] for r in rows} == {str(bad): "removed", str(good): "removed"}

    third = vs.migrate_default_pins(ledger=ledger, targets=[bad, good])
    assert third["status"] == "already-migrated" and third["targets"] == []


def test_an_unreadable_ledger_means_done_not_run_again(tmp_path: Path, ledger: Path):
    """The ledger's EXISTENCE is the guard; a garbled one retries nothing."""
    ledger.parent.mkdir(parents=True)
    ledger.write_text("{not json", encoding="utf-8")
    path = _write(tmp_path, json.dumps({ENV: {MODEL: OPUS}}))
    out = vs.migrate_default_pins(ledger=ledger, targets=[path])
    assert out["status"] == "already-migrated" and out["targets"] == []
    assert json.loads(path.read_text(encoding="utf-8"))[ENV][MODEL] == OPUS
