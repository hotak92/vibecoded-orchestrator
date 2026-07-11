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
