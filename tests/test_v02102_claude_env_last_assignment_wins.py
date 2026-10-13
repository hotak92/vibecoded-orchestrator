# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Whole-file readers of ``.claude/env`` must agree with ``source`` (v0.2.102).

THE DEFECT. A project's ``.claude/env`` is a shell file: every hook and every
user shell ``source``s it, and a shell keeps the LAST assignment of a key. The
managed block even tells users to put their own exports OUTSIDE it (after the
END marker), so the natural override is a second assignment below the block.
The Python whole-file readers took the FIRST assignment instead
(``envfile.env_value`` is first-match), so the override was honoured by every
shell and ignored by:

* ``knowledge_residue.project_env_value`` — and through it the
  ``SHARED_KG_READ_DISABLED`` gate (``shared_read_disabled_for``),
  ``kg_sync_drift``'s ``KG_COLLECTION`` hint and the doctor's
  ``SHARED_KG_COLLECTION`` read;
* ``install.py::_read_project_name_from_envfile`` (``PROJECT_NAME``).

The venv ladder had the same bug and was fixed in v0.2.95
(``tests/test_v0295_project_env_reader_precedence.py``).

WHAT THIS PINS: with a managed-block value followed by a user override after
the block, every whole-file reader returns the LAST value — proven against
``bash -c 'source .claude/env; ...'`` itself, not against a hand-written
expectation. Managed-block-SCOPED reads keep reading the block (they ask
"what did VCO write?", not "what does the shell see?").
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import envfile, hook_retirements, kg_sync_drift, knowledge_residue  # noqa: E402
from vco_lib import service_reconcile  # noqa: E402
from vco_lib.config_projection import (  # noqa: E402
    _build_managed_block,
    _read_managed_env_canonical_value,
)

#: key -> (value VCO wrote in the managed block, the user's override after it)
BLOCK = {
    "SHARED_KG_READ_DISABLED": "false",
    "KG_COLLECTION": "Alpha_KnowledgeGraph",
    "SHARED_KG_COLLECTION": "Alpha_Shared",
    "PROJECT_NAME": "Block Project",
    "WEAVIATE_URL": "http://localhost:8081",
}
OVERRIDE_LINES = (
    "\n# my own overrides (outside the managed block, as the header asks)\n"
    "export SHARED_KG_READ_DISABLED=true\n"
    "KG_COLLECTION=Beta_KnowledgeGraph\n"
    "export SHARED_KG_COLLECTION='Beta_Shared'\n"
    'export PROJECT_NAME="User Project"\n'
    'WEAVIATE_URL="http://localhost:9999"\n'
    "# KG_COLLECTION=commented_out_is_not_an_assignment\n"
)
EXPECTED_LAST = {
    "SHARED_KG_READ_DISABLED": "true",
    "KG_COLLECTION": "Beta_KnowledgeGraph",
    "SHARED_KG_COLLECTION": "Beta_Shared",
    "PROJECT_NAME": "User Project",
    "WEAVIATE_URL": "http://localhost:9999",
}


@pytest.fixture(scope="module")
def install_mod():
    import install

    return install


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project whose ``.claude/env`` is the real managed block (the real
    writer's bytes) followed by a user override for every key. No
    ``settings.json`` — the ``.claude/env`` tier is the one under test."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "env").write_text(
        _build_managed_block(BLOCK) + OVERRIDE_LINES, encoding="utf-8")
    return tmp_path


def _sourced(env_file: Path, key: str) -> str:
    """What a shell sees for ``key`` after ``source <env_file>``."""
    out = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c",
         'unset "$2"; source "$1" >/dev/null 2>&1; printf %s "${!2-}"',
         "_", str(env_file), key],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return out.stdout


# ── the helper ──────────────────────────────────────────────────────────────


def test_env_value_last_wins_takes_the_last_assignment():
    text = "K=first\nexport K='second'\n# K=commented\nOTHER=x\n"
    assert envfile.env_value(text, "K", last_wins=True) == "second"
    # The default stays first-match: the tier-3 dotenv secret reader mirrors
    # ``dotenv_get`` / ``Get-DotenvValue``, which are first-match by contract.
    assert envfile.env_value(text, "K") == "first"
    assert envfile.env_value(text, "MISSING", last_wins=True) is None


def test_env_value_last_wins_inside_the_managed_block_only():
    text = _build_managed_block({"K": "inside"}) + "K=outside\n"
    assert envfile.env_value(
        text, "K", last_wins=True,
        begin_marker="# vco-managed-begin", end_marker="# vco-managed-end",
    ) == "inside"
    assert envfile.env_value(text, "K", last_wins=True) == "outside"


# ── every whole-file reader returns the LAST assignment ─────────────────────


def test_project_env_value_returns_the_override(project: Path):
    for key, want in EXPECTED_LAST.items():
        assert knowledge_residue.project_env_value(project, key) == want, key


def test_shared_read_gate_honours_the_override(project: Path):
    # Block says "false", the user's override says "true": the shell sees
    # true, so the gate must be ON.
    assert knowledge_residue.shared_read_disabled_for(project) is True


def test_kg_collection_hint_returns_the_override(project: Path):
    assert kg_sync_drift._local_kg_collection_hint(project) == "Beta_KnowledgeGraph"


def test_project_name_reader_returns_the_override(project: Path, install_mod):
    env = project / ".claude" / "env"
    assert install_mod._read_project_name_from_envfile(env) == "User Project"


def test_service_continuity_reader_returns_the_override(project: Path):
    env = service_reconcile._read_env_file(project / ".claude" / "env")
    assert env["WEAVIATE_URL"] == "http://localhost:9999"
    assert env["SHARED_KG_COLLECTION"] == "Beta_Shared"


def test_service_continuity_reader_follows_the_one_quote_rule(tmp_path: Path):
    """``_read_env_file`` now parses with the ONE line grammar: exactly one
    MATCHING quote pair is stripped. Its old private copy stripped every
    leading/trailing quote of either kind (``.strip("'\"")``), so a
    mismatched pair lost both quotes."""
    path = tmp_path / "env"
    path.write_text(
        "A=\"mismatched'\n"
        "B='\"nested\"'\n"
        "C=\"\"\"\n"
        "export D=\"ok\"\n",
        encoding="utf-8")
    env = service_reconcile._read_env_file(path)
    assert env["A"] == "\"mismatched'"
    assert env["B"] == '"nested"'
    assert env["C"] == '"'
    assert env["D"] == "ok"


def test_service_continuity_reader_soft_fails_on_non_utf8(tmp_path: Path):
    """A non-UTF-8 file is unreadable as text: an empty mapping (the caller
    then reports no continuity statements), never a raised
    ``UnicodeDecodeError`` out of the reconcile."""
    path = tmp_path / "env"
    path.write_bytes(b"WEAVIATE_URL=http://localhost:8081\n\xff\xfe\xfa\n")
    assert service_reconcile._read_env_file(path) == {}
    assert service_reconcile._read_env_file(tmp_path / "missing") == {}


def test_disabled_stems_reader_returns_the_override(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    key = hook_retirements.DISABLED_STEMS_ENV_KEY
    (tmp_path / ".claude" / "env").write_text(
        f"{key}=a,b\n{key}='c'\n", encoding="utf-8")
    assert hook_retirements._read_disabled_stems(tmp_path) == ["c"]


def test_an_empty_override_clears_the_value_as_the_shell_does(tmp_path: Path):
    """``KEY=`` after the block leaves the shell with an EMPTY value; the
    reader must not resurrect the block's value."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "env").write_text(
        _build_managed_block({"SHARED_KG_READ_DISABLED": "true"})
        + "SHARED_KG_READ_DISABLED=\n", encoding="utf-8")
    assert knowledge_residue.project_env_value(tmp_path, "SHARED_KG_READ_DISABLED") is None
    assert knowledge_residue.shared_read_disabled_for(tmp_path) is False


def test_managed_block_scoped_read_still_reads_the_block(project: Path):
    """The projection's read-back asks what VCO wrote, not what the shell
    sees — it keeps reading the block (by design, unchanged)."""
    path = project / ".claude" / "env"
    assert _read_managed_env_canonical_value(path, "KG_COLLECTION") == "Alpha_KnowledgeGraph"


# ── parity with the shell itself ────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
@pytest.mark.parametrize("key", sorted(EXPECTED_LAST))
def test_every_whole_file_reader_matches_bash_source(project: Path, install_mod, key: str):
    env_file = project / ".claude" / "env"
    shell = _sourced(env_file, key)
    assert shell == EXPECTED_LAST[key]  # the fixture really overrides
    assert knowledge_residue.project_env_value(project, key) == shell
    assert envfile.env_value(env_file.read_text(encoding="utf-8"), key, last_wins=True) == shell
    assert service_reconcile._read_env_file(env_file).get(key) == shell
    if key == "PROJECT_NAME":
        assert install_mod._read_project_name_from_envfile(env_file) == shell
