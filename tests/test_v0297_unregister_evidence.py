# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — unregister removes a secret VALUE only on evidence VCO wrote it.

A confirmed "unregister VCO from this project" consents to removing what VCO
wrote, not a same-named key the user typed. The launcher asks
``python -m vco_lib.config_projection classify-secret-values`` which of the
launcher-known secret values (every user-secret bucket + ``GITHUB_TOKEN``) in
the project's four env files EQUAL the launcher's stored value, removes those
(``projects_v2::apply_secret_verdicts``, Rust-tested) and reports the rest.
This file tests the evidence side; the resolver is faked in-process (never a
live hub), and the CLI child is pinned to a discard hub port.
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

REPO_ROOT = Path(__file__).resolve().parents[1]
STORED = "stored-value-the-launcher-holds"
TYPED = "a-value-the-user-typed"


@pytest.fixture()
def stored(monkeypatch) -> dict:
    values: dict = {}

    def fake(key: str, _root: Path):
        if values.get(key) == "<unknown>":
            return "unknown", None
        return ("ok", values[key]) if key in values else ("absent", None)

    monkeypatch.setattr(cp, "_stored_secret_value", fake)
    return values


def _files(root: Path, key: str, value: str) -> None:
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    (root / ".vscode").mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text(f"{key}={value}\n# {key}=commented\n", encoding="utf-8")
    (root / ".claude" / "env").write_text(
        f"{cp.CLAUDE_ENV_MANAGED_BEGIN}\nexport IN_BLOCK_TOKEN=\"x\"\n{cp.CLAUDE_ENV_MANAGED_END}\n"
        f"export {key}=\"{value}\"\n",
        encoding="utf-8",
    )
    (root / ".claude" / "settings.json").write_text(json.dumps({"env": {key: value}}))
    (root / ".vscode" / "settings.json").write_text(json.dumps({"claude-code.env": {key: value}}))


ALL = [".env", ".claude/env", ".claude/settings.json", ".vscode/settings.json"]


def test_a_value_equal_to_the_stored_one_is_proven_in_every_file(tmp_path, stored):
    stored["OPENAI_API_KEY"] = STORED
    _files(tmp_path, "OPENAI_API_KEY", STORED)
    verdicts = cp.classify_vco_secret_values(tmp_path, known_keys=["OPENAI_API_KEY"])
    assert verdicts == {rel: {"OPENAI_API_KEY": "proven"} for rel in ALL}


def test_a_same_named_typed_value_is_not_vcos(tmp_path, stored):
    stored["OPENAI_API_KEY"] = STORED
    _files(tmp_path, "OPENAI_API_KEY", TYPED)
    verdicts = cp.classify_vco_secret_values(tmp_path, known_keys=["OPENAI_API_KEY"])
    assert verdicts == {rel: {"OPENAI_API_KEY": "not_vco"} for rel in ALL}


@pytest.mark.parametrize("answer, verdict", [("absent", "not_vco"), ("<unknown>", "unknown")])
def test_a_paused_or_unresolvable_key_is_never_proven(tmp_path, stored, answer, verdict):
    if answer == "<unknown>":
        stored["OPENAI_API_KEY"] = "<unknown>"
    _files(tmp_path, "OPENAI_API_KEY", STORED)
    verdicts = cp.classify_vco_secret_values(tmp_path, known_keys=["OPENAI_API_KEY"])
    assert {v["OPENAI_API_KEY"] for v in verdicts.values()} == {verdict}


def test_github_token_is_checked_against_the_stored_pat(tmp_path, stored):
    stored["GITHUB_TOKEN"] = STORED   # the fake maps env keys; the real one reads github_pat
    _files(tmp_path, "GITHUB_TOKEN", TYPED)
    verdicts = cp.classify_vco_secret_values(tmp_path, known_keys=[])
    assert {v["GITHUB_TOKEN"] for v in verdicts.values()} == {"not_vco"}
    assert cp._STORED_SLOT_FOR_ENV_KEY["GITHUB_TOKEN"] == "github_pat"


def test_names_the_launcher_never_stored_are_not_listed(tmp_path, stored):
    """Only a launcher-known name (or GITHUB_TOKEN) can be VCO's; the managed
    block's own content is VCO's region and is not classified."""
    _files(tmp_path, "MY_OWN_TOKEN", TYPED)
    assert cp.classify_vco_secret_values(tmp_path, known_keys=[]) == {}


def test_the_cli_prints_names_and_verdicts_only(tmp_path):
    _files(tmp_path, "GITHUB_TOKEN", TYPED)
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.config_projection", "classify-secret-values",
         "--project-folder", str(tmp_path)],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False, timeout=60,
        env=child_env(VCT_HUB_PORT="9", VCT_LAUNCHER_DB_PATH=str(tmp_path / "absent.db")),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    assert {v["GITHUB_TOKEN"] for v in out["verdicts"].values()} == {"unknown"}, "no hub ⇒ no evidence"
    assert TYPED not in proc.stdout and TYPED not in proc.stderr


def test_the_rust_evidence_gated_list_matches_the_python_one():
    """`projects_v2.rs::EVIDENCE_GATED_ENV_KEYS` — the canonical keys unregister
    never removes by name — MUST MATCH `_LEGACY_SECRET_ENV_KEYS`."""
    src = (REPO_ROOT / "launcher/src-tauri/src/commands/projects_v2.rs").read_text(encoding="utf-8")
    m = re.search(r"pub\(crate\) const EVIDENCE_GATED_ENV_KEYS: &\[&str\] = &\[([^\]]*)\];", src)
    assert m, "EVIDENCE_GATED_ENV_KEYS not found"
    rust = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert rust == set(cp._LEGACY_SECRET_ENV_KEYS)
