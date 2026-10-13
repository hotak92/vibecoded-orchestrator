# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 (W6-AB): one secret-read timeout, held in one place per language.

The hub answers a secret read from the OS keychain, whose own per-operation
wait is ``KEYCHAIN_OP_TIMEOUT`` (10 s). Every client of the hub's secret
route must therefore wait longer than that, or a slow-but-working hub is cut
off and its miss reported as "not found" (2026-10-13). The value lives once
per language — Python (``project_config``), shell (the resolver and the
``vct`` CLI's miss probe, two separately-installed programs) and PowerShell —
and this test pins them equal, plus the derived bounds that depend on them.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_config  # noqa: E402

_SH = REPO_ROOT / "templates" / "scripts" / "vct_secrets_resolve.sh"
_PS1 = REPO_ROOT / "templates" / "scripts" / "vct_secrets_resolve.ps1"
_VCT = REPO_ROOT / "tools" / "vct-secrets" / "vct"
_SUMMARY = REPO_ROOT / "templates" / "scripts" / "summary_backends.py"
_WORKER = (
    REPO_ROOT / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "secrets_worker.rs"
)


def _int(pattern: str, path: Path) -> int:
    m = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    assert m, f"{pattern!r} not found in {path}"
    return int(m.group(1))


def test_every_language_holds_the_same_secret_read_timeout():
    py = project_config.SECRET_READ_TIMEOUT_SECONDS
    assert _int(r"^_SECRET_READ_TIMEOUT_S=(\d+)$", _SH) == py
    assert _int(r"^\$script:SecretReadTimeoutMs = (\d+)$", _PS1) == py * 1000
    assert _int(r"^_VCT_HUB_PROBE_READ_TIMEOUT_S=(\d+)$", _VCT) == py


def test_secret_read_timeout_outlasts_the_hubs_own_keychain_wait():
    op_timeout = _int(
        r"KEYCHAIN_OP_TIMEOUT: std::time::Duration =\s*std::time::Duration::from_secs\((\d+)\)",
        _WORKER,
    )
    # The lock probe plus at least one read must fit, with margin.
    assert project_config.SECRET_READ_TIMEOUT_SECONDS >= 2 * op_timeout


def test_config_reads_keep_the_short_timeout():
    assert project_config._READ_TIMEOUT_SECONDS < project_config.SECRET_READ_TIMEOUT_SECONDS


def test_summary_backends_outlasts_the_shell_resolvers_worst_case():
    read = project_config.SECRET_READ_TIMEOUT_SECONDS
    retry_wait = _int(r"^_HUB_RETRY_WAIT_S=(\d+)$", _SH)
    worst = 4 * read + retry_wait  # (by-path + /env) x (attempt + one retry)
    assert _int(r"^_SHIPPED_RESOLVER_TIMEOUT_S = (\d+)$", _SUMMARY) > worst
