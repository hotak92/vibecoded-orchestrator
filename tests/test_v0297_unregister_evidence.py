# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — unregister removes a secret VALUE only on evidence VCO wrote it.

A confirmed "unregister VCO from this project" consents to removing what VCO
wrote, not a same-named key the user typed. The launcher runs
``python -m vco_lib.config_projection strip-proven-secret-values``: for every
launcher-known secret (every user-secret bucket + ``GITHUB_TOKEN``) in the
project's four env files, a value is removed only where it EQUALS the
launcher's stored value — per LINE in ``.env`` / ``.claude/env`` (review R3
F23: a key can appear twice, and the user's own line must survive), per key in
the JSON blocks — and everything else is reported with its reason (review R3
F22: a paused key is "could not check", never "not the stored value").
The resolver is faked in-process (never a live hub); the CLI child is pinned
to a discard hub port.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from vco_lib import config_projection as cp
from vco_lib import user_owned_secrets

REPO_ROOT = Path(__file__).resolve().parents[1]
STORED = "stored-value-the-launcher-holds"
TYPED = "a-value-the-user-typed"


@pytest.fixture()
def stored(monkeypatch) -> dict:
    """The launcher's stored values by env key; ``"<paused>"`` / ``"<unknown>"``
    answer those states, a missing key answers ``absent``."""
    values: dict = {}

    def fake(key: str, _root: Path):
        if values.get(key) == "<unknown>":
            return "unknown", None
        if values.get(key) == "<paused>":
            return "paused", None
        return ("ok", values[key]) if key in values else ("absent", None)

    monkeypatch.setattr(cp, "_stored_secret_value", fake)
    return values


def _files(root: Path, key: str, value: str) -> None:
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    (root / ".vscode").mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text(f"{key}={value}\n# {key}=commented\nOTHER=1\n", encoding="utf-8")
    (root / ".claude" / "env").write_text(
        f"{cp.CLAUDE_ENV_MANAGED_BEGIN}\nexport IN_BLOCK_TOKEN=\"x\"\n{cp.CLAUDE_ENV_MANAGED_END}\n"
        f"export {key}=\"{value}\"\n",
        encoding="utf-8",
    )
    (root / ".claude" / "settings.json").write_text(json.dumps({"env": {key: value, "OTHER": "1"}}))
    (root / ".vscode" / "settings.json").write_text(json.dumps({"claude-code.env": {key: value}}))


ALL = [".env", ".claude/env", ".claude/settings.json", ".vscode/settings.json"]


def _snapshot(root: Path) -> dict:
    return {rel: (root / rel).read_bytes() for rel in ALL}


# ── ACT ─────────────────────────────────────────────────────────────────────


def test_a_value_equal_to_the_stored_one_is_removed_from_every_file(tmp_path, stored):
    stored["OPENAI_API_KEY"] = STORED
    _files(tmp_path, "OPENAI_API_KEY", STORED)
    result = cp.strip_proven_secret_values(tmp_path, known_keys=["OPENAI_API_KEY"])
    assert result == {"removed": {rel: ["OPENAI_API_KEY"] for rel in ALL}, "left": {}, "errors": []}
    for rel in ALL:
        text = (tmp_path / rel).read_text()
        assert STORED not in text, rel
    env = (tmp_path / ".env").read_text()
    assert "# OPENAI_API_KEY=commented" in env and "OTHER=1" in env, "comments and other keys stay"
    assert "IN_BLOCK_TOKEN" in (tmp_path / ".claude/env").read_text(), "VCO's block is not this step's"


def test_review_r3_f23_a_user_line_above_a_vco_line_keeps_the_user_line(tmp_path, stored):
    """The reviewer's scenario: the same key twice — the user's own value
    first, the VCO-written (stored) value below. Only the proven line goes."""
    stored["OPENAI_API_KEY"] = STORED
    env = tmp_path / ".env"
    env.write_text(f"OPENAI_API_KEY={TYPED}\r\nOPENAI_API_KEY={STORED}\r\nOTHER=1\r\n", encoding="utf-8")

    result = cp.strip_proven_secret_values(tmp_path, known_keys=["OPENAI_API_KEY"])

    assert env.read_bytes() == f"OPENAI_API_KEY={TYPED}\r\nOTHER=1\r\n".encode(), "CRLF kept too"
    assert result["removed"] == {".env": ["OPENAI_API_KEY"]}
    assert result["left"] == {".env": {"OPENAI_API_KEY": "not_vco"}}


def test_a_vco_line_above_a_user_line_likewise(tmp_path, stored):
    stored["OPENAI_API_KEY"] = STORED
    env = tmp_path / ".claude" / "env"
    env.parent.mkdir()
    env.write_text(f'export OPENAI_API_KEY="{STORED}"\nexport OPENAI_API_KEY="{TYPED}"\n', encoding="utf-8")
    cp.strip_proven_secret_values(tmp_path, known_keys=["OPENAI_API_KEY"])
    assert env.read_text() == f'export OPENAI_API_KEY="{TYPED}"\n'


# ── LEAVE ALONE ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("answer, verdict", [
    (STORED, "not_vco"), ("<paused>", "paused"), ("<unknown>", "unknown"), (None, "not_vco"),
])
def test_an_unproven_value_is_left_byte_for_byte_and_reported(tmp_path, stored, answer, verdict):
    if answer is not None:
        stored["OPENAI_API_KEY"] = answer
    _files(tmp_path, "OPENAI_API_KEY", STORED if answer in ("<paused>", "<unknown>") else TYPED)
    before = _snapshot(tmp_path)

    result = cp.strip_proven_secret_values(tmp_path, known_keys=["OPENAI_API_KEY"])

    assert _snapshot(tmp_path) == before
    assert result["removed"] == {}
    assert result["left"] == {rel: {"OPENAI_API_KEY": verdict} for rel in ALL}


def test_names_the_launcher_never_stored_are_not_touched_or_listed(tmp_path, stored):
    _files(tmp_path, "MY_OWN_TOKEN", TYPED)
    before = _snapshot(tmp_path)
    assert cp.strip_proven_secret_values(tmp_path, known_keys=[]) == {"removed": {}, "left": {}, "errors": []}
    assert _snapshot(tmp_path) == before


def test_github_token_is_checked_against_the_stored_pat(tmp_path, stored):
    stored["GITHUB_TOKEN"] = STORED   # the fake maps env keys; the real one reads github_pat
    _files(tmp_path, "GITHUB_TOKEN", TYPED)
    result = cp.strip_proven_secret_values(tmp_path, known_keys=[])
    assert {v["GITHUB_TOKEN"] for v in result["left"].values()} == {"not_vco"}
    assert cp._STORED_SLOT_FOR_ENV_KEY["GITHUB_TOKEN"] == "github_pat"


# ── F22: the paused wording, one home, both reports ─────────────────────────


def test_a_paused_key_is_never_described_as_not_the_stored_value():
    paused = cp.EVIDENCE_REASONS[cp.EVIDENCE_PAUSED]
    assert "could not check" in paused and "paused" in paused
    assert "not the value" not in paused


def test_the_user_owned_entry_gives_the_paused_reason(tmp_path, stored, monkeypatch):
    stored["OPENAI_API_KEY"] = "<paused>"
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"env": {"OPENAI_API_KEY": TYPED}}))
    monkeypatch.setattr(cp, "known_user_secret_keys_for_folder", lambda _f: ["OPENAI_API_KEY"])
    assert user_owned_secrets.found_with_reasons(tmp_path) == {
        ".claude/settings.json": {"OPENAI_API_KEY": "paused"},
    }
    user_owned_secrets.emit_deferral(tmp_path)
    from vco_lib.deferral_report import DeferralReport

    entry = DeferralReport.read(tmp_path).entry_for(user_owned_secrets.CID)
    assert entry is not None
    assert "OPENAI_API_KEY (VCO could not check it — the launcher's copy is paused" in entry.detected
    assert TYPED not in json.dumps(entry.__dict__, default=str)


# ── the CLI the launcher calls ─────────────────────────────────────────────


def test_the_cli_reports_names_verdicts_and_wording_only(tmp_path):
    _files(tmp_path, "GITHUB_TOKEN", TYPED)
    before = _snapshot(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.config_projection", "strip-proven-secret-values",
         "--project-folder", str(tmp_path)],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False, timeout=60,
        env=child_env(VCT_HUB_PORT="9", VCT_STATE_DIR=str(tmp_path / "state"),
                      VCT_LAUNCHER_DB_PATH=str(tmp_path / "absent.db")),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["ok"] is True and out["removed"] == {}
    assert {v["GITHUB_TOKEN"] for v in out["left"].values()} == {"unknown"}, "no hub ⇒ no evidence"
    assert out["reasons"] == cp.EVIDENCE_REASONS
    assert TYPED not in proc.stdout and TYPED not in proc.stderr
    assert _snapshot(tmp_path) == before


def test_the_rust_evidence_gated_list_matches_the_python_one():
    """`projects_v2.rs::EVIDENCE_GATED_ENV_KEYS` — the canonical keys unregister
    never removes by name — MUST MATCH `_LEGACY_SECRET_ENV_KEYS`."""
    src = (REPO_ROOT / "launcher/src-tauri/src/commands/projects_v2.rs").read_text(encoding="utf-8")
    m = re.search(r"pub\(crate\) const EVIDENCE_GATED_ENV_KEYS: &\[&str\] = &\[([^\]]*)\];", src)
    assert m, "EVIDENCE_GATED_ENV_KEYS not found"
    rust = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert rust == set(cp._LEGACY_SECRET_ENV_KEYS)


def test_no_rust_copy_of_the_env_line_grammar_remains():
    """Review R3 F24: the per-line strip is the Python SSOT's
    (`envfile.parse_env_line`); the Rust mirror is retired."""
    src = (REPO_ROOT / "launcher/src-tauri/src/commands/projects_v2.rs").read_text(encoding="utf-8")
    assert "fn strip_active_env_lines" not in src
