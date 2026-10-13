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


def test_every_language_honours_the_same_retry_after_cap_and_default():
    """v0.2.102 fix round: the ONE retry after a ``503
    keychain_unavailable`` waits the hub's numeric ``Retry-After`` — clamped
    to the same cap in every client — and falls back to the same hub default
    when the header is absent or unparseable. Python holds the rule
    (:func:`agent_secrets._retry_wait_seconds`); the shell/PowerShell/vct
    resolvers reimplement it per language, so the cap and the default are
    pinned here exactly like the read timeout above."""
    from vco_lib import agent_secrets

    cap = agent_secrets.HUB_RETRY_AFTER_CAP_SECONDS
    default = agent_secrets._HUB_RETRY_AFTER_DEFAULT_SECONDS
    assert _int(r"^_HUB_RETRY_AFTER_CAP_S=(\d+)$", _SH) == cap
    assert _int(r"^\$script:HubRetryAfterCapMs = (\d+)$", _PS1) == cap * 1000
    assert _int(r"^_VCT_HUB_RETRY_AFTER_CAP_S=(\d+)$", _VCT) == cap
    assert _int(r"^_HUB_RETRY_WAIT_S=(\d+)$", _SH) == default
    assert _int(r"^\$script:HubRetryWaitMs = (\d+)$", _PS1) == default * 1000
    assert _int(r"^_VCT_HUB_RETRY_WAIT_S=(\d+)$", _VCT) == default


def test_python_retry_wait_semantics_are_the_parity_anchor():
    """What every other language's wait computation must reproduce, pinned on
    the ONE implementation all the others are held against (the shell/ps1/vct
    behavioural tests assert the same numbers against their fake hubs)."""
    from vco_lib.agent_secrets import _retry_wait_seconds

    assert _retry_wait_seconds("4") == 4.0  # honoured
    assert _retry_wait_seconds("2.5") == 2.5  # fractional seconds honoured
    assert _retry_wait_seconds("9") == 5.0  # capped
    assert _retry_wait_seconds("3600") == 5.0  # capped, however large
    assert _retry_wait_seconds("0") == 0.0  # retry immediately
    assert _retry_wait_seconds("-3") == 0.0  # negative clamps to zero
    assert _retry_wait_seconds(None) == 2.0  # absent → hub default
    assert _retry_wait_seconds("soon") == 2.0  # unparseable → hub default
    assert _retry_wait_seconds("") == 2.0


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
    # The retry wait is the Retry-After-derived CAP in the worst case, not
    # the 2 s default (v0.2.102 fix round: the resolvers honour the header).
    retry_wait = _int(r"^_HUB_RETRY_AFTER_CAP_S=(\d+)$", _SH)
    worst = 4 * read + retry_wait  # (by-path + /env) x (attempt + one retry)
    assert _int(r"^_SHIPPED_RESOLVER_TIMEOUT_S = (\d+)$", _SUMMARY) > worst
