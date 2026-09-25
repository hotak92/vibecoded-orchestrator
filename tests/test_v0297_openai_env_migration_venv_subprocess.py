# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The step-9 OpenAI-key migration runs as a VENV SUBPROCESS, not in-process.

install.py never re-execs into the venv it creates, so for its whole run the
process is the interpreter that launched it. On a fresh clone that is the
system Python with none of the venv's packages — and v0.2.97's first cut of
``_migrate_install_dotenv_openai_key`` migrated in-process, importing
``vco_lib.agent_secrets`` → ``vco_lib.project_config`` → ``requests``, which
crashed EVERY fresh install at step 9 (all install-smoke platforms red).

These tests manufacture the fresh-clone interpreter in-process — the same
``_FreshClonePython`` blocker the v0296 gate uses, with the migration's chain
evicted so the guard actually fires — and prove the step now COMPLETES by
sending the work to the install venv's python (tier A, ``python -m
vco_lib.openai_key migrate-dotenv``), with the failure modes reported rather
than crashed or silently skipped.

The value never crosses the process boundary: argv carries only the root
path, the child reads ``<root>/.env`` itself, and its stdout verdict is
status+detail by ``migrate_dotenv_openai_key``'s no-value contract — pinned
here by asserting the canary absent from every captured output.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402
from vco_lib import openai_key  # noqa: E402
from tests.common.child_env import child_env  # noqa: E402
from tests.common.fake_venv import install_fake_venv_python  # noqa: E402
from tests.test_v0296_install_pre_venv_is_stdlib_only import (  # noqa: E402
    POST_VENV_PACKAGES,
    _FreshClonePython,
)

CANARY = "sk-canary-not-a-real-key-7d2c"

_LEGACY = (
    "EMBEDDING_MODEL=text-embedding-3-small\n"
    "# OpenAI (for embeddings)\n"
    f"OPENAI_API_KEY={CANARY}\n"
    "EMBEDDING_PROVIDER=openai\n"
)

#: The migration's own chain, evicted so the blocker's guard re-enters
#: ``__import__`` for it instead of hitting a warm ``sys.modules`` cache.
_EVICT = ("vco_lib.openai_key", "vco_lib.agent_secrets", "vco_lib.project_config")

posix_only = pytest.mark.skipif(os.name != "posix", reason="fake venv is a #! stub")


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


def _prepared_root(tmp_path: Path) -> Path:
    """A tmp install root with the legacy ``.env`` and the log dir."""
    root = tmp_path / "orch"
    (root / "state" / "logs").mkdir(parents=True)
    (root / ".env").write_text("KG_COLLECTION=Gamma_KG\n" + _LEGACY, encoding="utf-8")
    return root


@posix_only
def test_the_migration_step_survives_a_fresh_clone(stores: Path, tmp_path: Path, capsys) -> None:
    """The exact CI failure, replayed: ``requests`` unimportable in-process.

    On HEAD's in-process implementation this raised
    ``ModuleNotFoundError: No module named 'requests'`` inside
    ``_migrate_install_dotenv_openai_key``. With the venv subprocess the step
    completes: the child (which HAS the packages — here the fake venv
    delegating to this interpreter) migrates the line, and the parent's
    process never imports the chain at all.
    """
    root = _prepared_root(tmp_path)
    install_fake_venv_python(root)
    orig_root = install.PROJECT_ROOT
    install.PROJECT_ROOT = root
    try:
        with _FreshClonePython(POST_VENV_PACKAGES, _EVICT):
            install._migrate_install_dotenv_openai_key()  # must not raise
    finally:
        install.PROJECT_ROOT = orig_root

    text = (root / ".env").read_text(encoding="utf-8")
    assert CANARY not in text
    assert "OPENAI_API_KEY=" not in text
    assert "# OpenAI (for embeddings)\nEMBEDDING_PROVIDER=openai\n" in text
    stored = stores / "shared" / openai_key.OPENAI_SECRET_NAME
    assert stored.read_text().strip() == CANARY, "the child stored the value"
    captured = capsys.readouterr()
    assert "moved the OpenAI key" in captured.out
    assert CANARY not in captured.out + captured.err


@posix_only
def test_a_failing_venv_python_leaves_the_line_and_reports(stores: Path, tmp_path: Path, capsys) -> None:
    """A subprocess that FAILS must reach the report (a left-in-place warning),
    never crash the install and never print the value."""
    root = _prepared_root(tmp_path)
    stub = install_fake_venv_python(root)
    stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")  # keep the exec bit
    orig_root = install.PROJECT_ROOT
    install.PROJECT_ROOT = root
    try:
        install._migrate_install_dotenv_openai_key()  # must not raise
    finally:
        install.PROJECT_ROOT = orig_root

    text = (root / ".env").read_text(encoding="utf-8")
    assert CANARY in text, "the line is left in place"
    out = capsys.readouterr().out
    assert "LEFT in place" in out and "the venv migration failed" in out
    assert CANARY not in out
    # The failure was REPORTED through the install-event channel too.
    log = root / "state" / "logs" / "install.jsonl"
    assert log.is_file()
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    assert any(r.get("phase") == "warn" and "left" in r.get("detail", "") for r in rows)
    assert CANARY not in log.read_text(encoding="utf-8")


def test_no_venv_python_leaves_the_line_and_reports(stores: Path, tmp_path: Path, capsys) -> None:
    """``python_exe is None`` (no install venv found) is the same honest
    soft-fail — not a crash, not a silent skip."""
    root = _prepared_root(tmp_path)
    orig_root = install.PROJECT_ROOT
    install.PROJECT_ROOT = root
    try:
        install._migrate_install_dotenv_openai_key()  # must not raise
    finally:
        install.PROJECT_ROOT = orig_root

    assert CANARY in (root / ".env").read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "LEFT in place" in out and "re-run install.py" in out
    assert CANARY not in out


@posix_only
def test_the_cli_prints_one_json_verdict_and_no_value(stores: Path, tmp_path: Path) -> None:
    """``python -m vco_lib.openai_key migrate-dotenv --root R`` — the child
    side of the tier-A boundary, run here through the REAL interpreter with
    the checkout pinned first on its path (``child_env``)."""
    root = tmp_path / "orch"
    root.mkdir()
    (root / ".env").write_text(_LEGACY, encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-m", "vco_lib.openai_key", "migrate-dotenv", "--root", str(root)],
        env=child_env(), capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    verdict = json.loads(done.stdout.strip().splitlines()[-1])
    assert verdict["status"] == "migrated"
    assert verdict["detail"]
    assert CANARY not in done.stdout + done.stderr
    assert CANARY not in (root / ".env").read_text(encoding="utf-8")
    assert (stores / "shared" / openai_key.OPENAI_SECRET_NAME).read_text().strip() == CANARY
