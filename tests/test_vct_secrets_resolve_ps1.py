# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""PS1 sibling of the resolver-chain tests (v0.2.73, multi-OS rule).

``templates/scripts/vct_secrets_resolve.ps1`` must implement the SAME
three-tier resolution chain as the .sh and Python siblings:

    tier 1  vct-hub (keychain, permission-matrix gated)
    tier 2  file store ($Env:VCT_SECRETS_DIR, projects/<NAME> → shared)
    tier 3  the project's own .env (read-only, lowest priority)

These tests exercise tiers 2 + 3 with tier 1 unreachable (dead port +
empty state dir), using the SAME synthetic fixture as
``tests/test_vct_secrets_resolve.sh`` and
``tests/test_agent_secrets.py`` — same key names, same values, same
parse cases. Auto-skipped when no PowerShell runtime (``pwsh`` /
``powershell.exe``) is on PATH, mirroring the other ps1-test wrappers.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVER = REPO_ROOT / "templates" / "scripts" / "vct_secrets_resolve.ps1"

_PWSH = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(
    _PWSH is None,
    reason=(
        "no PowerShell runtime on PATH (pwsh / powershell.exe). "
        "PS1 resolver-chain tests skipped — install PowerShell Core 7+ "
        "to exercise this matrix on non-Windows hosts."
    ),
)

# Same fixture as the sh + py siblings — keep in lockstep.
_DOTENV_FIXTURE = (
    "# comment line — skipped\n"
    "export EXPORTED_KEY=plain-exported\n"
    'QUOTED_KEY="double quoted value"\n'
    "SINGLE_KEY='single quoted value'\n"
    "FIRST_MATCH=first-wins\n"
    "FIRST_MATCH=second-loses\n"
    "NO_EXPANSION=$HOME/literal\n"
    "MISMATCHED='half\"\n"
)

_PARSE_CASES = [
    ("EXPORTED_KEY", "plain-exported"),
    ("QUOTED_KEY", "double quoted value"),
    ("SINGLE_KEY", "single quoted value"),
    ("FIRST_MATCH", "first-wins"),
    ("NO_EXPANSION", "$HOME/literal"),
    ("MISMATCHED", "'half\""),
]


def _dead_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run_resolver(
    tmp_path: Path,
    arg1: str,
    key: str,
    secrets_dir: Path,
) -> subprocess.CompletedProcess:
    """Invoke the ps1 resolver with tier 1 guaranteed unreachable."""
    env = {
        # Minimal env: dead hub port, isolated state dir (no hub.token
        # — the resolver treats that as hub-unreachable), isolated
        # file store. PATH/HOME so pwsh itself works.
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "VCT_HUB_PORT": str(_dead_port()),
        "VCT_STATE_DIR": str(tmp_path / "empty-state"),
        "VCT_SECRETS_DIR": str(secrets_dir),
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(
        [_PWSH, "-NoProfile", "-NonInteractive", "-File", str(RESOLVER), arg1, key],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


@pytest.fixture()
def secrets_dir(tmp_path: Path) -> Path:
    root = tmp_path / "secrets-store"
    (root / "shared").mkdir(parents=True)
    (root / "projects").mkdir()
    return root


@pytest.fixture()
def dotenv_proj(tmp_path: Path) -> Path:
    proj = tmp_path / "proj-with-dotenv"
    proj.mkdir()
    (proj / ".env").write_text(_DOTENV_FIXTURE, encoding="utf-8")
    return proj


def test_tier2_file_store_when_hub_unreachable(tmp_path, secrets_dir):
    (secrets_dir / "shared" / "EXAMPLE_API_TOKEN").write_text(
        "synthetic-file-store-value", encoding="utf-8"
    )
    cp = _run_resolver(tmp_path, "some-project-id", "EXAMPLE_API_TOKEN", secrets_dir)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == "synthetic-file-store-value"


def test_tier2_strips_one_trailing_newline(tmp_path, secrets_dir):
    (secrets_dir / "shared" / "NEWLINE_KEY").write_text(
        "value-with-newline\n", encoding="utf-8"
    )
    cp = _run_resolver(tmp_path, "some-project-id", "NEWLINE_KEY", secrets_dir)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == "value-with-newline"


@pytest.mark.parametrize(("key", "want"), _PARSE_CASES)
def test_tier3_dotenv_parse_cases(tmp_path, secrets_dir, dotenv_proj, key, want):
    cp = _run_resolver(tmp_path, str(dotenv_proj), key, secrets_dir)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == want


def test_tier3_skipped_for_bare_project_id(tmp_path, secrets_dir):
    cp = _run_resolver(tmp_path, "bare-project-id", "EXPORTED_KEY", secrets_dir)
    assert cp.returncode == 1  # tier-1 code preserved (hub unreachable)
    assert "tier 3 (.env) skipped" in cp.stderr


def test_never_prints_values_on_miss(tmp_path, secrets_dir, dotenv_proj):
    (secrets_dir / "shared" / "EXAMPLE_API_TOKEN").write_text(
        "synthetic-file-store-value", encoding="utf-8"
    )
    cp = _run_resolver(tmp_path, str(dotenv_proj), "TOTALLY_MISSING_KEY", secrets_dir)
    assert cp.returncode != 0
    combined = cp.stdout + cp.stderr
    for leaked in (
        "synthetic-file-store-value",
        "plain-exported",
        "double quoted value",
        "single quoted value",
    ):
        assert leaked not in combined
    # Miss diagnostic names the tiers consulted.
    assert "tier 1" in cp.stderr and "tier 3" in cp.stderr
