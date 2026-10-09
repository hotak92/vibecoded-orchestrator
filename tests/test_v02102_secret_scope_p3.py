# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 (audit P3-2): the root-``.env`` promotion respects a project's
per-project copy of the same value.

The v0.2.97 secret-scope audit (§P3 item 2) noted that
``migrate_dotenv_openai_key`` promoted the root ``.env`` value into the
SHARED file store even when some project already
held the identical value as its own per-project secret — a second,
broader-scope copy of the same value that can drift. The v0.2.102 lane
brief (W5-C, item P299-A1.2) orders the fix: compare (constant-time, never
logged) and skip the promotion, naming the owning project in the verdict.

The audit's own disposition was "No change recommended; noted for the
record"; the v0.2.102 brief supersedes it with the fix specified above.

Every test runs with the hub pinned to the discard port and the file store
in a tmp dir; the canary value is asserted absent from every verdict and
output that is not the store itself.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vco_lib import agent_secrets, openai_key  # noqa: E402

CANARY = "sk-canary-not-a-real-key-c31d"
OTHER = "sk-other-not-a-real-key-52aa"

_LEGACY = (
    "EMBEDDING_MODEL=text-embedding-3-small\n"
    "# OpenAI (for embeddings)\n"
    "OPENAI_API_KEY={value}\n"
    "EMBEDDING_PROVIDER=openai\n"
)


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """No hub (discard port), a tmp file store, a clean resolver cache."""
    secrets = tmp_path / "secrets"
    monkeypatch.setenv("VCT_SECRETS_DIR", str(secrets))
    monkeypatch.setenv("VCT_HUB_PORT", "9")
    monkeypatch.delenv("VCT_HUB_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    openai_key._resolved.clear()
    openai_key._warned_env_var = False
    openai_key._warned_paused = False
    yield secrets
    openai_key._resolved.clear()
    openai_key._warned_env_var = False
    openai_key._warned_paused = False


def _shared(secrets: Path) -> Path:
    return secrets / "shared" / openai_key.OPENAI_SECRET_NAME


def _project_copy(secrets: Path, name: str) -> Path:
    return secrets / "projects" / name / openai_key.OPENAI_SECRET_NAME


# ─── per_project_copy_owner — the pure probe ────────────────────────────


def test_owner_names_the_project_whose_copy_equals_the_value(stores: Path) -> None:
    _project_copy(stores, "alpha").parent.mkdir(parents=True)
    _project_copy(stores, "alpha").write_text(OTHER)
    _project_copy(stores, "beta").parent.mkdir(parents=True)
    _project_copy(stores, "beta").write_text(CANARY)
    assert agent_secrets.per_project_copy_owner(
        openai_key.OPENAI_SECRET_NAME, CANARY,
    ) == "beta"


def test_owner_is_none_when_no_project_copy_equals_the_value(stores: Path) -> None:
    _project_copy(stores, "alpha").parent.mkdir(parents=True)
    _project_copy(stores, "alpha").write_text(OTHER)
    assert agent_secrets.per_project_copy_owner(
        openai_key.OPENAI_SECRET_NAME, CANARY,
    ) is None


def test_owner_is_none_with_no_projects_dir_and_ignores_the_shared_copy(
    stores: Path,
) -> None:
    """No ``projects/`` tree at all, and a SHARED copy equal to the value is
    NOT a per-project copy — the probe must not report one."""
    _shared(stores).parent.mkdir(parents=True)
    _shared(stores).write_text(CANARY)
    assert agent_secrets.per_project_copy_owner(
        openai_key.OPENAI_SECRET_NAME, CANARY,
    ) is None


def test_owner_is_deterministic_among_equal_copies(stores: Path) -> None:
    for name in ("zeta", "alpha"):
        _project_copy(stores, name).parent.mkdir(parents=True)
        _project_copy(stores, name).write_text(CANARY)
    assert agent_secrets.per_project_copy_owner(
        openai_key.OPENAI_SECRET_NAME, CANARY,
    ) == "alpha", "sorted walk: the answer does not depend on readdir order"


def test_owner_compares_a_trailing_newline_like_the_resolver(stores: Path) -> None:
    """The file store strips ONE trailing newline (``_file_store_get``
    semantics); the probe must agree or a copy written by ``vct set`` would
    read as unequal to the value it stored."""
    _project_copy(stores, "alpha").parent.mkdir(parents=True)
    _project_copy(stores, "alpha").write_text(CANARY + "\n")
    assert agent_secrets.per_project_copy_owner(
        openai_key.OPENAI_SECRET_NAME, CANARY,
    ) == "alpha"


# ─── the migration skip (audit P3-2) ────────────────────────────────────


def test_a_value_a_project_already_holds_is_not_promoted_to_shared(
    stores: Path, tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """The defect: with nothing in VCO's own stores, the migration copied
    the root ``.env`` value into ``shared/`` even though project ``alpha``
    already held the identical value per-project — two copies of one value
    that can drift. The promotion is now skipped, the line stays, and the
    verdict names the owning project (never the value)."""
    _project_copy(stores, "alpha").parent.mkdir(parents=True)
    _project_copy(stores, "alpha").write_text(CANARY)
    original = _LEGACY.format(value=CANARY)
    (tmp_path / ".env").write_text(original)

    result = openai_key.migrate_dotenv_openai_key(tmp_path)

    assert result["status"] == "left_project_copy"
    assert "alpha" in result["detail"], "the verdict names the owning project"
    assert (tmp_path / ".env").read_text() == original, "the line stays"
    assert not _shared(stores).exists(), "no second, broader-scope copy"
    err = capsys.readouterr().err
    assert "alpha" in err, "one stderr line names the project"
    assert CANARY not in err
    assert CANARY not in json.dumps(result)


def test_a_value_no_project_holds_is_still_promoted(
    stores: Path, tmp_path: Path,
) -> None:
    """Leave-alone case: the skip must not widen. With no equal per-project
    copy anywhere, a proven absence still auto-stores to shared exactly as
    before."""
    _project_copy(stores, "alpha").parent.mkdir(parents=True)
    _project_copy(stores, "alpha").write_text(OTHER)  # a DIFFERENT value
    (tmp_path / ".env").write_text(_LEGACY.format(value=CANARY))

    result = openai_key.migrate_dotenv_openai_key(tmp_path)

    assert result["status"] == "migrated", (
        "a value no project holds is promoted exactly as before"
    )
    assert _shared(stores).read_text().strip() == CANARY
    assert CANARY not in (tmp_path / ".env").read_text()


def test_a_differing_stored_shared_value_still_wins_without_the_probe_firing(
    stores: Path, tmp_path: Path,
) -> None:
    """Ordering pin: the per-project probe runs only on the nothing-stored
    path. A shared store that already holds another value classifies the
    line ``left_differs`` without consulting any project's copy."""
    _shared(stores).parent.mkdir(parents=True)
    _shared(stores).write_text(OTHER)
    _project_copy(stores, "alpha").parent.mkdir(parents=True)
    _project_copy(stores, "alpha").write_text(CANARY)
    original = _LEGACY.format(value=CANARY)
    (tmp_path / ".env").write_text(original)

    result = openai_key.migrate_dotenv_openai_key(tmp_path)

    assert result["status"] == "left_differs"
    assert (tmp_path / ".env").read_text() == original
    assert _shared(stores).read_text().strip() == OTHER
