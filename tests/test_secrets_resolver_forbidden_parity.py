# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.77 L3-F3 source-parity: the secrets resolver triplet classifies a
hub 403 distinctly (exit 5 for the shell pair; Forbidden for Python) rather
than mislabeling it "hub unreachable".

Ungated (pure source read) so it runs on every OS — the behavioural bash
test lives in tests/test_vct_secrets_resolve.sh (POSIX-gated) and the Python
behaviour in tests/test_agent_secrets.py.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "templates" / "scripts"


def test_sh_secrets_resolver_has_403_exit_5_arm():
    body = (SCRIPTS / "vct_secrets_resolve.sh").read_text(encoding="utf-8")
    assert "403)" in body, "sh resolver must have a `403)` case arm"
    assert "return 5" in body, "sh resolver must return exit 5 on forbidden"


def test_ps1_secrets_resolver_has_403_exit_5_arm():
    body = (SCRIPTS / "vct_secrets_resolve.ps1").read_text(encoding="utf-8")
    assert "403 {" in body, "ps1 resolver must have a `403 { ... }` switch arm"
    assert "ExitCode = 5" in body, "ps1 resolver must return exit 5 on forbidden"


def test_python_agent_secrets_raises_forbidden_on_403():
    body = (REPO_ROOT / "vco_lib" / "agent_secrets.py").read_text(encoding="utf-8")
    assert "status_code == 403" in body, "agent_secrets must handle a 403"
    assert "raise Forbidden(" in body, "agent_secrets must raise Forbidden on 403"


def test_forbidden_not_a_hub_unreachable_subclass():
    from vco_lib.agent_secrets import Forbidden, HubUnreachable

    assert not issubclass(Forbidden, HubUnreachable), (
        "Forbidden must NOT be a HubUnreachable subclass — callers that catch "
        "HubUnreachable for env-fallback must not swallow a 403"
    )


# ─── v0.2.82 WP-4b: 503 keychain_locked / keychain_error → exit 6 ───────
#
# Same ungated source-parity idiom as the 403 block above: assert the new
# distinct classification exists in ALL THREE implementations so the sh/ps1
# pair and the Python helper stay in lockstep. Behavioural coverage: the sh
# suite (tests/test_vct_secrets_resolve.sh) exercises exit 6 against a fake
# 503 hub; the Python behaviour lives in
# tests/test_agent_secrets_keychain_locked_v0282.py. The .ps1 behaviour has
# no live 503 harness (its runner uses a dead port → tier 1 always
# unreachable, like the 403 case), so this source-parity check is the .ps1's
# exit-6 gate.


def test_sh_secrets_resolver_has_503_exit_6_arm():
    body = (SCRIPTS / "vct_secrets_resolve.sh").read_text(encoding="utf-8")
    assert "503)" in body, "sh resolver must have a `503)` case arm"
    assert "return 6" in body, "sh resolver must return exit 6 on keychain-locked"
    # Both keychain states must be recognised distinctly from any other 503.
    assert "keychain_locked)" in body
    assert "keychain_error)" in body


def test_ps1_secrets_resolver_has_503_exit_6_arm():
    body = (SCRIPTS / "vct_secrets_resolve.ps1").read_text(encoding="utf-8")
    assert "503 {" in body, "ps1 resolver must have a `503 { ... }` switch arm"
    assert "ExitCode = 6" in body, "ps1 resolver must return exit 6 on keychain-locked"
    assert '"keychain_locked"' in body
    assert '"keychain_error"' in body


def test_python_agent_secrets_raises_keychain_locked_on_503():
    body = (REPO_ROOT / "vco_lib" / "agent_secrets.py").read_text(encoding="utf-8")
    assert "status_code == 503" in body, "agent_secrets must handle a 503"
    assert "raise KeychainLocked(" in body, (
        "agent_secrets must raise KeychainLocked on 503 keychain_locked/error"
    )
    assert 'code in ("keychain_locked", "keychain_error")' in body


def test_keychain_locked_not_a_hub_unreachable_subclass():
    from vco_lib.agent_secrets import HubUnreachable, KeychainLocked

    assert not issubclass(KeychainLocked, HubUnreachable), (
        "KeychainLocked must NOT be a HubUnreachable subclass — a locked "
        "keychain is unavailability, not unreachability; callers that catch "
        "HubUnreachable must not silently swallow it"
    )


def test_keychain_locked_distinct_from_access_denied():
    from vco_lib.agent_secrets import AccessDenied, KeychainLocked

    # key_not_active (authorization) and keychain_locked (unavailability)
    # are different states; neither may subclass the other.
    assert not issubclass(KeychainLocked, AccessDenied)
    assert not issubclass(AccessDenied, KeychainLocked)


def test_all_three_exit_code_tables_document_code_6():
    """The exit-code tables in BOTH shell headers must document code 6 so a
    caller reading the header knows what a `6` means (plan: 'document in the
    header')."""
    sh = (SCRIPTS / "vct_secrets_resolve.sh").read_text(encoding="utf-8")
    ps1 = (SCRIPTS / "vct_secrets_resolve.ps1").read_text(encoding="utf-8")
    for body, name in ((sh, "sh"), (ps1, "ps1")):
        lowered = body.lower()
        assert "keychain" in lowered, f"{name} header must mention keychain"
        # The digit 6 must appear as a documented exit code alongside a
        # keychain/locked reference in the header region.
        assert "6" in body and "locked" in lowered, (
            f"{name} exit-code table must document code 6 (keychain locked)"
        )
