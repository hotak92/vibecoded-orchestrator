# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97: a project's ``.env`` has ONE writer — ``vco_lib.env_template``.

The launcher's project-create path used to run a Rust append-only writer
(``ensure_project_env_template``) while ``apply_env_template``'s
block-replace contract ran against the same file. It now spawns
``python -m vco_lib.env_template apply`` (``reference`` under Safe add).
These tests drive that exact CLI, the way the bridge does, against a real
launcher-schema DB:

  * fresh project → scaffold (commented optional keys) + managed block;
  * existing project carrying the retired writers' legacy lines → ONE set
    (each key assigned once), user lines byte-identical;
  * idempotent re-run → byte-identical, no write;
  * Safe add → the live ``.env`` is never created or modified; the
    ``.env.vco.reference`` sidecar carries the intended content;
  * a failure answers with a JSON error object on stdout (what the bridge
    parses) as well as on stderr.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.common.launcher_db_fixture import make_launcher_db
from tests.common.child_env import child_env
from vco_lib.env_template import (
    ENV_TEMPLATE_BEGIN,
    ENV_TEMPLATE_END,
    effective_assignment,
    repair_stale_kg_collection,
    strip_project_env,
)

# The retired Rust writer's two shapes, as field files carry them.
_RUST_FRESH_TEMPLATE = (
    "# vibecoded-orchestrator per-project .env\n"
    "# Edit values to override defaults. Empty / commented lines are\n"
    "# treated as \"use default\". Created by vco 2026-05-06.\n"
    "\n"
    "# === Service URLs (launcher-resolved; edit only if you know what you're doing) ===\n"
    "# WEAVIATE_URL=http://localhost:8081\n"
    "# WEAVIATE_PORT=8081\n"
    "# OLLAMA_URL=http://localhost:11435\n"
    "# OLLAMA_PORT=11435\n"
    "# CODE_EMBED_URL=http://localhost:11440\n"
    "\n"
    "# === Per-project Weaviate collections ===\n"
    "# Resolved by the launcher when the project is registered. Don't\n"
    "# edit unless you know what you're doing.\n"
    "KG_COLLECTION=Acme_KnowledgeGraph\n"
    "SHARED_KG_COLLECTION=VibeCodedOrchestrator_KnowledgeGraph\n"
    "DEVELOPMENT_COLLECTION=Acme_Development\n"
    "PROJECT_NAME=Acme\n"
    "ACTIVE_EMBEDDING=qwen3\n"
    "\n"
    "# === LLM API keys (optional) ===\n"
    "# ANTHROPIC_API_KEY=\n"
    "OPENAI_API_KEY=sk-user-filled-this-in\n"
    "\n"
)
_RUST_APPEND = (
    "\n"
    "# added by vco 2026-06-01: appended missing canonical keys\n"
    "# CODE_EMBED_URL=\n"
    "PROJECT_NAME=<project>\n"
    "# GITHUB_TOKEN=\n"
    "# RL_PROJECT_ROOT=<project_root>\n"
)


def _db(tmp_path: Path, folder: Path) -> Path:
    db = tmp_path / "launcher.db"
    make_launcher_db(db, projects=[{
        "project_id": "p-1", "name": "Acme", "folder_path": str(folder), "slug": "acme",
    }])
    return db


def _cli(verb: str, db: Path, folder: Path, project_id: str = "p-1",
         *extra: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "vco_lib.env_template", verb,
           "--project-id", project_id, "--project-folder", str(folder),
           "--db-path", str(db), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, env=child_env())


def _assignments(text: str, key: str) -> list[str]:
    return [ln.split("=", 1)[1] for ln in text.splitlines() if ln.startswith(f"{key}=")]


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "acme"
    folder.mkdir()
    return folder


# ─── act: fresh project ─────────────────────────────────────────────────


def test_fresh_project_gets_scaffold_and_block(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    out = _cli("apply", _db(tmp_path, folder), folder, "p-1", "--weaviate-port", "18081")
    assert out.returncode == 0, out.stderr
    reply = json.loads(out.stdout)
    assert reply["ok"] is True and reply["report"]["action"] == ["created"]

    text = (folder / ".env").read_text(encoding="utf-8")
    assert text.startswith("# vibecoded-orchestrator per-project .env\n")
    assert "# OPENAI_API_KEY=\n" in text and "# GITHUB_TOKEN=\n" in text
    assert f"# RL_PROJECT_ROOT={folder}\n" in text
    assert "VIBECODED_TELEMETRY" not in text and "# VCT_TELEMETRY=off\n" in text
    block = text[text.index(ENV_TEMPLATE_BEGIN):]
    assert block.rstrip().endswith(ENV_TEMPLATE_END)
    assert _assignments(text, "KG_COLLECTION") == ["Acme_KnowledgeGraph"]
    assert _assignments(text, "PROJECT_NAME") == ["Acme"]
    assert _assignments(text, "WEAVIATE_URL") == ["http://localhost:18081"]
    assert _assignments(text, "OPENAI_API_KEY") == []


# ─── act: existing project with legacy lines ────────────────────────────


def test_existing_project_with_legacy_lines_ends_with_one_set(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    user_head = "# my notes\nMY_TOKEN=keep-me\n\n"
    (folder / ".env").write_text(user_head + _RUST_FRESH_TEMPLATE + _RUST_APPEND,
                                 encoding="utf-8")

    out = _cli("apply", _db(tmp_path, folder), folder)
    assert out.returncode == 0, out.stderr
    text = (folder / ".env").read_text(encoding="utf-8")

    # User lines untouched, including a value typed into a template line.
    assert text.startswith(user_head + "# vibecoded-orchestrator per-project .env\n")
    assert _assignments(text, "MY_TOKEN") == ["keep-me"]
    assert _assignments(text, "OPENAI_API_KEY") == ["sk-user-filled-this-in"]
    # ONE assignment per managed key; the bogus placeholder is gone.
    for key in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION", "SHARED_KG_COLLECTION",
                "PROJECT_NAME", "ACTIVE_EMBEDDING", "WEAVIATE_URL", "OLLAMA_URL",
                "CODE_EMBED_URL", "CODE_GRAPH_PROJECT"):
        assert len(_assignments(text, key)) == 1, (key, text)
    assert "<project>\n" not in text
    # Commented legacy duplicates of managed keys are gone too.
    assert "# WEAVIATE_URL=" not in text and "# CODE_EMBED_URL=" not in text
    # Placeholders for keys the block never carries stay.
    assert "# GITHUB_TOKEN=\n" in text and "# ANTHROPIC_API_KEY=\n" in text
    assert text.count(ENV_TEMPLATE_BEGIN) == 1


def test_rerun_is_byte_identical_and_writes_nothing(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    db = _db(tmp_path, folder)
    (folder / ".env").write_text("A=1\n" + _RUST_APPEND, encoding="utf-8")
    assert _cli("apply", db, folder).returncode == 0
    first = (folder / ".env").read_bytes()
    mtime = (folder / ".env").stat().st_mtime_ns

    out = _cli("apply", db, folder)

    assert out.returncode == 0, out.stderr
    assert (folder / ".env").read_bytes() == first
    assert (folder / ".env").stat().st_mtime_ns == mtime
    assert json.loads(out.stdout)["report"]["action"] == ["unchanged"]


def test_user_assigned_key_is_never_rendered(tmp_path: Path) -> None:
    """Leave-alone: the user's KG_COLLECTION stays the one assignment."""
    folder = _folder(tmp_path)
    (folder / ".env").write_text("KG_COLLECTION=MyCustom_KG\n", encoding="utf-8")
    assert _cli("apply", _db(tmp_path, folder), folder).returncode == 0
    text = (folder / ".env").read_text(encoding="utf-8")
    assert _assignments(text, "KG_COLLECTION") == ["MyCustom_KG"]
    assert text.startswith("KG_COLLECTION=MyCustom_KG\n")


# ─── leave-alone: Safe add ──────────────────────────────────────────────


def test_safe_add_reference_leaves_the_live_env_byte_identical(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    live = "KG_COLLECTION=LegacyBare\n" + _RUST_APPEND
    (folder / ".env").write_text(live, encoding="utf-8")
    mtime = (folder / ".env").stat().st_mtime_ns

    out = _cli("reference", _db(tmp_path, folder), folder)

    assert out.returncode == 0, out.stderr
    assert (folder / ".env").read_text(encoding="utf-8") == live
    assert (folder / ".env").stat().st_mtime_ns == mtime
    sidecar = (folder / ".env.vco.reference").read_text(encoding="utf-8")
    assert "safe_add_skipped_env_merge" in sidecar
    assert _assignments(sidecar, "KG_COLLECTION") == ["Acme_KnowledgeGraph"]
    assert json.loads(out.stdout)["path"] == str(folder / ".env.vco.reference")


def test_safe_add_reference_never_creates_a_live_env(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    assert _cli("reference", _db(tmp_path, folder), folder).returncode == 0
    assert (folder / ".env.vco.reference").exists()
    assert not (folder / ".env").exists()


# ─── failures reach the bridge ──────────────────────────────────────────


def test_unknown_project_answers_with_a_json_error_on_stdout(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    db = _db(tmp_path, folder)
    for verb in ("apply", "reference"):
        out = _cli(verb, db, folder, "ghost")
        assert out.returncode == 2
        reply = json.loads(out.stdout)
        assert reply == {"ok": False, "error": "project_not_found",
                         "message": reply["message"]}
        assert json.loads(out.stderr)["error"] == "project_not_found"
    assert not (folder / ".env").exists()
    assert not (folder / ".env.vco.reference").exists()


# ─── the read-only drift probe rename uses ──────────────────────────────


def test_effective_assignment_is_the_last_active_line_and_its_location() -> None:
    block = f"{ENV_TEMPLATE_BEGIN}\nKG_COLLECTION=Block_KG\n{ENV_TEMPLATE_END}\n"
    assert effective_assignment("", "KG_COLLECTION") == (None, False)
    assert effective_assignment("KG_COLLECTION=User\n" + block, "KG_COLLECTION") == ("Block_KG", True)
    assert effective_assignment(block + "export KG_COLLECTION=Late\n", "KG_COLLECTION") == ("Late", False)
    assert effective_assignment("# KG_COLLECTION=x\n", "KG_COLLECTION") == (None, False)


def test_effective_verb_answers_managed_keys_only_and_never_prints_a_secret(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    canary = "sk-canary-not-real-5f1e"
    (folder / ".env").write_text(f"OPENAI_API_KEY={canary}\nKG_COLLECTION=Mine\n", encoding="utf-8")

    def run(key: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "vco_lib.env_template", "effective",
             "--project-folder", str(folder), "--key", key],
            capture_output=True, text=True, env=child_env(),
        )

    ok = run("KG_COLLECTION")
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout) == {"ok": True, "key": "KG_COLLECTION", "value": "Mine",
                                     "in_block": False}
    refused = run("OPENAI_API_KEY")
    assert refused.returncode == 2
    assert json.loads(refused.stdout)["error"] == "key_not_managed"
    assert canary not in refused.stdout + refused.stderr


# ─── the unregister's .env strip (review R5 F40: moved from Rust) ───────

_BY_NAME = {"KG_COLLECTION", "PROJECT_NAME", "DEVELOPMENT_COLLECTION",
            "ACTIVE_EMBEDDING", "OLLAMA_URL", "CODE_GRAPH_PROJECT"}


def test_strip_removes_the_block_whole_and_by_name_keys_keeps_user_lines(tmp_path: Path) -> None:
    """Act: VCO's managed block goes WHOLE (markers and forensic comments —
    the old by-name strip left them), plus canonical keys outside it, active
    or commented; user lines survive byte-for-byte."""
    folder = _folder(tmp_path)
    db = _db(tmp_path, folder)
    (folder / ".env").write_text(
        "# my header\nUSER_API_KEY=secret123\nKG_COLLECTION=Old_KG\n"
        "# OLLAMA_URL=http://localhost:11435\nexport PROJECT_NAME=Mine\n",
        encoding="utf-8",
    )
    assert _cli("apply", db, folder).returncode == 0

    removed = strip_project_env(folder, _BY_NAME)
    text = (folder / ".env").read_text(encoding="utf-8")

    assert text == "# my header\nUSER_API_KEY=secret123\n"
    assert ENV_TEMPLATE_BEGIN not in text and ENV_TEMPLATE_END not in text
    assert "added by vco" not in text
    assert {"KG_COLLECTION", "OLLAMA_URL", "PROJECT_NAME", "WEAVIATE_URL"} <= set(removed)


def test_strip_leaves_a_file_without_vco_lines_untouched(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    (folder / ".env").write_text("USER_KEY=value\r\n", encoding="utf-8", newline="")
    mtime = (folder / ".env").stat().st_mtime_ns
    assert strip_project_env(folder, _BY_NAME) == []
    assert (folder / ".env").read_bytes() == b"USER_KEY=value\r\n"
    assert (folder / ".env").stat().st_mtime_ns == mtime
    assert strip_project_env(tmp_path / "nowhere", _BY_NAME) == []


def test_strip_cli_takes_keys_on_stdin(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    (folder / ".env").write_text("KG_COLLECTION=X\nKEEP=1\n", encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-m", "vco_lib.env_template", "strip", "--project-folder", str(folder)],
        input=json.dumps({"keys": ["KG_COLLECTION"]}), capture_output=True, text=True,
        env=child_env(),
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"ok": True, "removed": ["KG_COLLECTION"]}
    assert (folder / ".env").read_text() == "KEEP=1\n"


# ─── infrastructure/.env has one writer too (review R5 F40) ─────────────


def test_the_launcher_sets_its_infra_key_through_compose_env(tmp_path: Path) -> None:
    from vco_lib.compose_env import set_infrastructure_env_key

    infra = tmp_path / "infrastructure"
    infra.mkdir()
    (infra / ".env").write_text("CODE_EMBED_BACKEND=gpu\nVCT_VOLUMES_PATH=/old\n")
    assert set_infrastructure_env_key(infra, "VCT_VOLUMES_PATH", "/new") == "set"
    assert (infra / ".env").read_text() == "CODE_EMBED_BACKEND=gpu\nVCT_VOLUMES_PATH=/new\n"
    assert set_infrastructure_env_key(infra, "VCT_VOLUMES_PATH", "/new") == "unchanged"
    fresh = tmp_path / "fresh"
    assert set_infrastructure_env_key(fresh, "VCT_VOLUMES_PATH", "/v") == "set"
    assert (fresh / ".env").read_text() == "VCT_VOLUMES_PATH=/v\n"


def test_the_infra_setter_refuses_other_keys_and_line_breaks(tmp_path: Path) -> None:
    import pytest

    from vco_lib.compose_env import set_infrastructure_env_key

    with pytest.raises(ValueError):
        set_infrastructure_env_key(tmp_path, "CODE_EMBED_BACKEND", "cpu")
    with pytest.raises(ValueError):
        set_infrastructure_env_key(tmp_path, "VCT_VOLUMES_PATH", "/a\nINJECTED=1")
    assert not (tmp_path / ".env").exists()


# ─── B12 and the "Migrate from .env" sentinel: the one writer too ───────


def test_b12_repair_rewrites_the_first_stale_line_once(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    (folder / ".env").write_bytes(b"# h\r\nKG_COLLECTION=KnowledgeGraph\r\nKG_COLLECTION=Acme\r\nX=1\r\n")
    assert repair_stale_kg_collection(folder, "Acme_KnowledgeGraph", ["KnowledgeGraph", "Acme"])
    assert (folder / ".env").read_bytes() == (
        b"# h\r\nKG_COLLECTION=Acme_KnowledgeGraph # B12 auto-repaired 0.2.11: was "
        b"\"KG_COLLECTION=KnowledgeGraph\"\r\nKG_COLLECTION=Acme\r\nX=1\r\n"
    )
    # Idempotent: the canonical line is present now.
    assert not repair_stale_kg_collection(folder, "Acme_KnowledgeGraph", ["KnowledgeGraph", "Acme"])


def test_b12_repair_leaves_a_file_without_a_stale_line_alone(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    (folder / ".env").write_text("KG_COLLECTION=Mine_KG\n")
    mtime = (folder / ".env").stat().st_mtime_ns
    assert not repair_stale_kg_collection(folder, "Acme_KnowledgeGraph", ["KnowledgeGraph", "Acme"])
    assert (folder / ".env").stat().st_mtime_ns == mtime
    assert not repair_stale_kg_collection(tmp_path / "nowhere", "A_KG", ["KnowledgeGraph"])


def test_sentinel_cli_rewrites_only_named_keys_and_never_prints_a_value(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    canary = "sk-canary-not-real-44aa"
    (folder / ".env").write_text(f"export OPENAI_API_KEY={canary}  # team\nB_SECRET=two\n")
    done = subprocess.run(
        [sys.executable, "-m", "vco_lib.env_template", "sentinel", "--project-folder", str(folder)],
        input=json.dumps({"keys": ["OPENAI_API_KEY"]}), capture_output=True, text=True,
        env=child_env(),
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"ok": True, "replaced": 1, "missed": []}
    assert (folder / ".env").read_text() == "export OPENAI_API_KEY=__vco_keychain__  # team\nB_SECRET=two\n"
    assert canary not in done.stdout + done.stderr
