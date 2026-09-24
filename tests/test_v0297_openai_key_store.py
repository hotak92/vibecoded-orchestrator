# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97: the OpenAI key never lands in the project tree.

``install.py --openai-key`` used to write the VALUE into the orchestrator
root's ``.env`` (and its session log recorded it in argv). It now stores it
under the ONE slot name ``openai_api_key`` — the launcher keychain via the
hub when it answers, else the file store through the ``vct`` CLI — and every
Python reader resolves it from there (``vco_lib.openai_key``). A pre-v0.2.97
line VCO wrote is removed only on value evidence.

Every test runs with the hub pinned to the discard port and the file store in
a tmp dir, so nothing reaches a real keychain or ``~/.vct-secrets``. The
canary value is asserted absent from every output, log and file that is not
the store itself.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterator
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402

from vco_lib import openai_key  # noqa: E402

CANARY = "sk-canary-not-a-real-key-7d2c"
OTHER = "sk-other-not-a-real-key-a91e"


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """No hub (discard port), a tmp file store, a clean resolver cache."""
    secrets = tmp_path / "secrets"
    monkeypatch.setenv("VCT_SECRETS_DIR", str(secrets))
    monkeypatch.setenv("VCT_HUB_PORT", "9")
    monkeypatch.delenv("VCT_HUB_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    openai_key._resolved.clear()
    yield secrets
    openai_key._resolved.clear()


def _stored(secrets: Path) -> Path:
    return secrets / "shared" / openai_key.OPENAI_SECRET_NAME


# ─── store ──────────────────────────────────────────────────────────────


def test_store_falls_back_to_the_file_store_under_the_one_slot_name(stores: Path) -> None:
    where = openai_key.store_openai_api_key(CANARY)
    assert where == "file_store"
    assert _stored(stores).read_text().strip() == CANARY
    assert _stored(stores).stat().st_mode & 0o077 == 0, "the file store keeps 0600"


def test_store_prefers_the_keychain_when_the_hub_takes_it(stores: Path) -> None:
    with mock.patch.object(openai_key, "_store_in_keychain", return_value=True) as kc:
        assert openai_key.store_openai_api_key(CANARY) == "keychain"
    kc.assert_called_once_with(CANARY)
    assert not _stored(stores).exists(), "no second copy when the keychain took it"


def test_the_keychain_request_uses_the_declared_slot_name(stores: Path) -> None:
    with mock.patch(
        "vco_lib.install_env_secret_scope.post_secrets_to_hub",
        return_value=(["openai_api_key"], [], "shared"),
    ) as post:
        assert openai_key.store_openai_api_key(CANARY) == "keychain"
    (payload,), kwargs = post.call_args
    assert payload == [{"key": "openai_api_key", "value": CANARY}]
    assert kwargs == {"project_id": None}
    manifest = json.loads((Path(install.__file__).parent / "vct-module.json").read_text())
    assert {"key": "openai_api_key", "scope": "shared", "module_id": "user"}.items() <= next(
        s for s in manifest["bundled_secrets"] if s["key"] == "openai_api_key"
    ).items()


# ─── resolve ────────────────────────────────────────────────────────────


def test_readers_resolve_the_stored_key_and_env_still_wins(stores: Path, monkeypatch) -> None:
    assert openai_key.resolve_openai_api_key() == ""
    openai_key.store_openai_api_key(CANARY)
    assert openai_key.resolve_openai_api_key() == CANARY
    monkeypatch.setenv("OPENAI_API_KEY", OTHER)
    assert openai_key.resolve_openai_api_key() == OTHER


def test_the_embedding_service_reads_through_the_resolver(stores: Path) -> None:
    from vco_lib import embedding_service

    openai_key.store_openai_api_key(CANARY)
    snapshot = embedding_service._redacted_env_snapshot()
    assert snapshot["OPENAI_API_KEY"] == f"<redacted len={len(CANARY)}>"
    assert CANARY[:4] not in json.dumps(snapshot)


# ─── migrate a pre-v0.2.97 .env line ────────────────────────────────────

_LEGACY = (
    "EMBEDDING_MODEL=text-embedding-3-small\n"
    "# OpenAI (for embeddings)\n"
    "OPENAI_API_KEY={value}\n"
    "EMBEDDING_PROVIDER=openai\n"
)


def test_migrate_moves_an_unstored_vco_line_into_the_file_store(stores, tmp_path, capsys) -> None:
    (tmp_path / ".env").write_text(_LEGACY.format(value=CANARY))
    result = openai_key.migrate_dotenv_openai_key(tmp_path)
    assert result["status"] == "migrated"
    text = (tmp_path / ".env").read_text()
    assert CANARY not in text
    assert text == "EMBEDDING_MODEL=text-embedding-3-small\n# OpenAI (for embeddings)\nEMBEDDING_PROVIDER=openai\n"
    assert _stored(stores).read_text().strip() == CANARY
    assert CANARY not in json.dumps(result) + capsys.readouterr().out


def test_migrate_removes_a_line_equal_to_the_stored_value_without_rewriting_it(stores, tmp_path) -> None:
    openai_key.store_openai_api_key(CANARY)
    before = _stored(stores).stat().st_mtime_ns
    (tmp_path / ".env").write_text(_LEGACY.format(value=CANARY))
    assert openai_key.migrate_dotenv_openai_key(tmp_path)["status"] == "migrated"
    assert CANARY not in (tmp_path / ".env").read_text()
    assert _stored(stores).stat().st_mtime_ns == before


def test_migrate_leaves_a_line_that_differs_from_the_stored_value(stores, tmp_path) -> None:
    openai_key.store_openai_api_key(OTHER)
    original = _LEGACY.format(value=CANARY)
    (tmp_path / ".env").write_text(original)
    result = openai_key.migrate_dotenv_openai_key(tmp_path)
    assert result["status"] == "left_differs"
    assert (tmp_path / ".env").read_text() == original
    assert _stored(stores).read_text().strip() == OTHER, "the store is never overwritten"
    assert CANARY not in json.dumps(result) and OTHER not in json.dumps(result)


def test_migrate_never_touches_a_line_the_user_wrote(stores, tmp_path) -> None:
    original = f"OPENAI_API_KEY={CANARY}\n"
    (tmp_path / ".env").write_text(original)
    assert openai_key.migrate_dotenv_openai_key(tmp_path)["status"] == "absent"
    assert (tmp_path / ".env").read_text() == original
    assert not _stored(stores).exists()


def test_migrate_leaves_the_line_when_it_cannot_be_stored(stores, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(openai_key, "_vct_cli", lambda: None)
    original = _LEGACY.format(value=CANARY)
    (tmp_path / ".env").write_text(original)
    result = openai_key.migrate_dotenv_openai_key(tmp_path)
    assert result["status"] == "left_unverified"
    assert (tmp_path / ".env").read_text() == original
    assert CANARY not in json.dumps(result)


# ─── install.py end to end ──────────────────────────────────────────────


def _run_write_env(root: Path, **embed) -> None:
    (root / "state" / "logs").mkdir(parents=True, exist_ok=True)
    orig = install.PROJECT_ROOT
    install.PROJECT_ROOT = root
    install._PENDING_EVENTS.clear()
    try:
        cfg = dict(install.EMBEDDING_CONFIGS["openai"])
        cfg.update(embed)
        args = mock.Mock(telemetry="off", yes=True)
        install._write_env_config(cfg, args)
    finally:
        install.PROJECT_ROOT = orig


def test_install_openai_key_goes_to_the_store_never_the_tree(stores, tmp_path, capsys) -> None:
    root = tmp_path / "orch"
    root.mkdir()
    _run_write_env(root, openai_key=CANARY)

    env_text = (root / ".env").read_text()
    assert CANARY not in env_text
    assert "OPENAI_API_KEY=" not in env_text
    assert "EMBEDDING_PROVIDER=openai" in env_text
    assert _stored(stores).read_text().strip() == CANARY
    out = capsys.readouterr().out
    assert CANARY not in out and "file store" in out
    for path in root.rglob("*"):
        if path.is_file():
            assert CANARY not in path.read_text(errors="replace"), path


def test_install_migrates_a_legacy_root_env_line_on_rerun(stores, tmp_path) -> None:
    root = tmp_path / "orch"
    root.mkdir()
    (root / ".env").write_text("KG_COLLECTION=Gamma_KG\n" + _LEGACY.format(value=CANARY))
    _run_write_env(root)
    text = (root / ".env").read_text()
    assert CANARY not in text and text.startswith("KG_COLLECTION=Gamma_KG\n")
    assert _stored(stores).read_text().strip() == CANARY


def test_update_reconcile_moves_a_legacy_line_out_of_the_reconciled_env(stores, tmp_path) -> None:
    """``install.py --update`` (``_reconcile_env_keys``) runs the same
    migration on the folder it reconciles — never on another tree."""
    (tmp_path / ".env").write_text("KG_COLLECTION=Gamma_KG\n" + _LEGACY.format(value=CANARY))
    install._reconcile_env_keys(tmp_path / ".env")
    assert CANARY not in (tmp_path / ".env").read_text()
    assert _stored(stores).read_text().strip() == CANARY


# ─── review R5 F38: every run that accepts the flag stores it ───────────


def test_update_stores_the_openai_key(stores, tmp_path, capsys) -> None:
    """``install.py --update --openai-key``: the key lands in the store (it
    used to be accepted and dropped — step 8 skips the fresh write)."""
    root = tmp_path / "orch"
    (root / "state" / "logs").mkdir(parents=True)
    (root / ".env").write_text("KG_COLLECTION=Gamma_KG\n")
    orig = install.PROJECT_ROOT
    install.PROJECT_ROOT = root
    try:
        install._update_env_config(mock.Mock(openai_key=CANARY))
    finally:
        install.PROJECT_ROOT = orig
    assert _stored(stores).read_text().strip() == CANARY
    assert CANARY not in (root / ".env").read_text()
    assert CANARY not in capsys.readouterr().out


def test_lightweight_stores_the_openai_key(stores, tmp_path, monkeypatch) -> None:
    root = tmp_path / "orch"
    (root / "state" / "logs").mkdir(parents=True)
    monkeypatch.setattr(install, "PROJECT_ROOT", root)
    # Stop right after the store: the rest of the lightweight path is not
    # under test here.
    monkeypatch.setattr(install, "_lightweight_venv_triage",
                        mock.Mock(side_effect=SystemExit(0)), raising=False)
    monkeypatch.setattr(install, "_lightweight_rewrite_paths",
                        mock.Mock(side_effect=SystemExit(0)))
    args = mock.Mock(openai_key=CANARY, lightweight_old_path="/old")
    with pytest.raises(SystemExit):
        install._run_lightweight(args)
    assert _stored(stores).read_text().strip() == CANARY


@pytest.mark.parametrize("flag", [
    "--uninstall", "--desktop-icon-only", "--adopt-project-dry-run", "--no-adopt-project",
])
def test_a_run_that_stores_nothing_refuses_the_flag_loudly(stores, monkeypatch, capsys, flag) -> None:
    monkeypatch.setattr(install.sys, "argv", ["install.py", flag, "--openai-key", CANARY])
    with pytest.raises(SystemExit) as done:
        install.main()
    assert done.value.code == 2
    err = capsys.readouterr().err
    assert f"--openai-key cannot be used with {flag}" in err
    assert CANARY not in err
    assert not _stored(stores).exists()


# ─── review R5 F34: the line is read with the ONE grammar ───────────────


@pytest.mark.parametrize("line", [
    f'OPENAI_API_KEY="{CANARY}"\n',          # the reviewer's probe B
    f"OPENAI_API_KEY='{CANARY}'\n",
    f"export OPENAI_API_KEY={CANARY}\n",
    f'export OPENAI_API_KEY="{CANARY}"\n',
    f"OPENAI_API_KEY={CANARY}\r\n",          # CRLF file
    f"  OPENAI_API_KEY = {CANARY}  \n",
])
def test_migrate_stores_the_parsed_value_not_the_raw_text(stores, tmp_path, line) -> None:
    """Act: whatever shape the VCO line was edited into, the store holds the
    value a READER of the line gets (quotes/export stripped), the line goes,
    and resolving afterwards returns that same value."""
    crlf = line.endswith("\r\n")
    nl = "\r\n" if crlf else "\n"
    (tmp_path / ".env").write_bytes(
        f"A=1{nl}# OpenAI (for embeddings){nl}{line}B=2{nl}".encode()
    )
    result = openai_key.migrate_dotenv_openai_key(tmp_path)
    assert result["status"] == "migrated", result
    assert _stored(stores).read_text().strip() == CANARY
    assert openai_key.resolve_openai_api_key() == CANARY
    assert (tmp_path / ".env").read_bytes() == f"A=1{nl}# OpenAI (for embeddings){nl}B=2{nl}".encode()


def test_migrate_compares_the_parsed_value_against_the_store(stores, tmp_path) -> None:
    """Act + leave-alone: a quoted line EQUAL to the stored key goes; a quoted
    line whose parsed value differs stays (``left_differs``)."""
    openai_key.store_openai_api_key(CANARY)
    (tmp_path / ".env").write_text(f'# OpenAI (for embeddings)\nOPENAI_API_KEY="{CANARY}"\n')
    assert openai_key.migrate_dotenv_openai_key(tmp_path)["status"] == "migrated"

    other = tmp_path / "other"
    other.mkdir()
    original = f'# OpenAI (for embeddings)\nOPENAI_API_KEY="{OTHER}"\n'
    (other / ".env").write_text(original)
    assert openai_key.migrate_dotenv_openai_key(other)["status"] == "left_differs"
    assert (other / ".env").read_text() == original


@pytest.mark.parametrize("line", [
    f"OPENAI_API_KEY={CANARY} # my key\n",   # trailing comment
    f"OPENAI_API_KEY=\"{CANARY}\n",           # unmatched quote
    f"OPENAI_API_KEY={CANARY} extra\n",
    "OPENAI_API_KEY=\n",
])
def test_a_value_that_does_not_parse_cleanly_is_left_and_reported(stores, tmp_path, line) -> None:
    """Leave-alone: a value that is not one clean token is never stored (it
    would plant a broken key) and the line stays, byte-identical."""
    original = f"# OpenAI (for embeddings)\n{line}"
    (tmp_path / ".env").write_text(original)
    result = openai_key.migrate_dotenv_openai_key(tmp_path)
    assert result["status"] == "left_unparsed", result
    assert (tmp_path / ".env").read_text() == original
    assert not _stored(stores).exists()
    assert CANARY not in json.dumps(result)


def test_the_session_log_argv_never_carries_the_key() -> None:
    red = install._install_companions.redact_secret_argv(
        ["--yes", "--openai-key", CANARY, f"--openai-key={CANARY}"]
    )
    assert red == ["--yes", "--openai-key", "<redacted>", "--openai-key=<redacted>"]
