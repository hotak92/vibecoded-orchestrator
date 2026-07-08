# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""B12 (audit v0.2.75): the rename-time stale-`.env` warning must not point the
user at a tool that never shipped.

The pre-v0.2.75 warning in ``rename_project_v2`` told the user to "run
repair-env (PR 5) to fix" the stale ``KG_COLLECTION`` line in their
project-root ``.env``. That ``repair-env`` command/tool was never built, so the
remediation was un-actionable. The fix rewrites the warning to name the REAL
remediations (``install-bundle --update`` or a manual one-line edit) and folds
the same finding into the durable rename deferral.

This is a source-scan guard: it asserts the un-actionable ``repair-env (PR 5)``
string only survives as an explanatory ``//`` comment (documenting what the old
message said), never inside a user-facing string literal. The argparse-validity
of the new ``install-bundle --update`` remediation is covered separately by
``tests/test_deferral_command_argparse_sweep.py``; the deferral-surfacing
behaviour is covered by the Rust ``#[test]``s in ``projects_v2.rs``.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECTS_V2 = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "projects_v2.rs"
)


def _non_comment_lines(text: str) -> list[str]:
    """Return source lines that are NOT pure ``//`` comment lines.

    The one legitimate remaining mention of ``repair-env (PR 5)`` is a ``//``
    comment explaining what the old (removed) message said — that's fine. A
    mention in a real string literal would be the regression this guards.
    """
    out: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("//"):
            continue
        out.append(line)
    return out


def test_rename_env_warning_does_not_reference_unshipped_repair_env() -> None:
    text = PROJECTS_V2.read_text(encoding="utf-8")
    offenders = [
        ln for ln in _non_comment_lines(text) if "repair-env" in ln
    ]
    assert not offenders, (
        "rename_project_v2's user-facing stale-.env warning must not tell the "
        "user to run the never-shipped `repair-env` tool. Offending "
        "non-comment line(s):\n" + "\n".join(offenders)
    )


def test_rename_env_warning_names_real_remediations() -> None:
    """Positive assertion: the rewritten warning + deferral note must name at
    least one runnable remediation so the user can actually fix the stale line."""
    text = PROJECTS_V2.read_text(encoding="utf-8")
    non_comment = "\n".join(_non_comment_lines(text))
    assert "install-bundle --update" in non_comment, (
        "the rewritten stale-.env warning must name `install-bundle --update` "
        "(the real remediation) in a user-facing string."
    )
