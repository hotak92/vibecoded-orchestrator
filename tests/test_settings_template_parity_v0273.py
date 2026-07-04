# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""D-4 (v0.2.73) — settings.json.{linux,windows}.template guard parity.

The two settings templates are hand-maintained in parallel. Finding D-4
(`.claude/context/reviews/v0273-fable-review/findings/D-findings.md`) showed
the Windows template had DRIFTED: three registrations carried a **bash**
guard prefix ``[ -n "$VCT_DISABLE_HOOKS" ] || ...`` in a template whose hook
shell is cmd.exe. Under cmd, ``[`` is not a command → the ``||`` fires → the
hook runs ALWAYS, including under ``VCT_DISABLE_HOOKS=1`` (the documented
per-shell opt-out, docs/GETTING_STARTED.md names cmd.exe + PowerShell as the
Windows guarantee).

Root cause: there is NO CI gate on settings-template registration parity —
``check_hook_parity.py`` covers only ``.sh``/``.ps1`` file siblings. This
test closes that gap. It pins TWO invariants:

  1. STRUCTURAL PARITY — the two templates register the SAME set of hooks
     for the SAME (event, matcher, if, timeout, async) shape. A hook added
     to one OS but not the other fails here.
  2. WINDOWS GUARD VALIDITY — no Windows registration uses the bash test
     syntax ``[ -n "$VCT_DISABLE_HOOKS" ]``. Windows hooks either self-guard
     internally (every ``.ps1`` checks ``$env:VCT_DISABLE_HOOKS``) or use a
     cmd-valid guard — never a bash-ism that is inert under cmd.exe.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LINUX = REPO_ROOT / "templates" / "settings.json.linux.template"
WINDOWS = REPO_ROOT / "templates" / "settings.json.windows.template"

# The bash test-syntax guard that is INERT under cmd.exe.
_BASH_GUARD_RE = re.compile(r"\[\s*-n\s+\"\$VCT_DISABLE_HOOKS\"\s*\]\s*\|\|")

# Extract the script basename a command invokes (.sh or .ps1). Used to build
# an OS-agnostic identity for a registration so the two templates can be
# compared by the STEM (kg-update-nudge) not the extension.
_SCRIPT_RE = re.compile(r"\.claude/hooks/([A-Za-z0-9._-]+)\.(?:sh|ps1)")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _registration_identity(event: str, matcher, hook: dict) -> tuple:
    """Build an OS-agnostic identity for one hook registration.

    Keyed by (event, matcher, if-clause, script-stem-or-inline-marker). The
    script STEM is OS-agnostic (kg-update-nudge for both .sh and .ps1). Inline
    commands (no script reference) are keyed by a stable 'inline' marker plus
    a normalised fingerprint so an inline hook present on one side but not the
    other is still caught.
    """
    command = hook.get("command", "")
    m = _SCRIPT_RE.search(command)
    if m:
        # Script hooks share a basename stem across .sh / .ps1 — the
        # OS-agnostic identity.
        script_id = ("script", m.group(1))
    else:
        # Inline command — the body LEGITIMATELY differs bash↔powershell, so
        # it cannot be fingerprinted across OSes. Identity is the position
        # (event, matcher, if) only; the count-per-slot invariant below
        # catches an inline hook present on one OS but missing on the other.
        script_id = ("inline",)
    return (event, matcher, hook.get("if"), script_id)


def _registration_shape(hook: dict) -> tuple:
    """The behavioural shape we require to match across OSes (excludes the
    command string itself, which differs bash↔ps1, but includes timeout /
    async / if which must be identical)."""
    return (hook.get("timeout"), hook.get("async"), hook.get("if"))


def _iter_registrations(data: dict):
    """Yield (event, matcher, hook_dict) for every hook registration."""
    for event, groups in data.get("hooks", {}).items():
        for group in groups:
            matcher = group.get("matcher")
            for hook in group.get("hooks", []):
                yield event, matcher, hook


class WindowsGuardValidityTests(unittest.TestCase):
    """Windows template must not carry bash-ism guards (D-4 core fix)."""

    def test_no_bash_guard_in_windows_template(self) -> None:
        body = WINDOWS.read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in body.splitlines()
            if _BASH_GUARD_RE.search(line)
        ]
        self.assertEqual(
            offenders,
            [],
            "Windows template must not use the bash guard "
            "'[ -n \"$VCT_DISABLE_HOOKS\" ] ||' — it is inert under cmd.exe "
            "and the hook runs even when VCT_DISABLE_HOOKS=1. Windows hooks "
            "self-guard internally (each .ps1 checks $env:VCT_DISABLE_HOOKS). "
            f"Offending line(s): {offenders!r}",
        )

    def test_windows_ps1_hooks_are_file_invocations_or_cmd_valid(self) -> None:
        # Every command in the Windows template must be a powershell -File
        # invocation OR a cmd-valid inline command — never a bash construct.
        data = _load(WINDOWS)
        for event, matcher, hook in _iter_registrations(data):
            cmd = hook.get("command", "")
            if _SCRIPT_RE.search(cmd):
                self.assertIn(
                    "powershell",
                    cmd,
                    f"Windows script hook must invoke via powershell: "
                    f"{event}/{matcher}: {cmd!r}",
                )
            # No command may contain the bash test operator.
            self.assertNotRegex(
                cmd,
                _BASH_GUARD_RE,
                f"bash guard leaked into {event}/{matcher}: {cmd!r}",
            )


class SettingsTemplateStructuralParityTests(unittest.TestCase):
    """The two templates must register the same hooks with the same shape."""

    def setUp(self) -> None:
        self.linux = _load(LINUX)
        self.windows = _load(WINDOWS)

    def test_same_events_registered(self) -> None:
        self.assertEqual(
            set(self.linux.get("hooks", {})),
            set(self.windows.get("hooks", {})),
            "linux and windows templates register different hook events",
        )

    def test_registration_identities_match(self) -> None:
        lin = {
            _registration_identity(e, m, h)
            for e, m, h in _iter_registrations(self.linux)
        }
        win = {
            _registration_identity(e, m, h)
            for e, m, h in _iter_registrations(self.windows)
        }
        only_linux = lin - win
        only_windows = win - lin
        self.assertEqual(
            (only_linux, only_windows),
            (set(), set()),
            "settings-template registration drift:\n"
            f"  only on linux : {sorted(only_linux)}\n"
            f"  only on windows: {sorted(only_windows)}",
        )

    def test_inline_hook_count_per_slot_matches(self) -> None:
        # Inline hooks can't be fingerprint-compared across OS, so assert the
        # NUMBER of inline hooks per (event, matcher, if) slot is identical —
        # an inline hook added to one OS but not the other still fails here.
        def inline_counts(data):
            counts: dict = {}
            for e, m, h in _iter_registrations(data):
                if _SCRIPT_RE.search(h.get("command", "")):
                    continue
                key = (e, m, h.get("if"))
                counts[key] = counts.get(key, 0) + 1
            return counts

        self.assertEqual(
            inline_counts(self.linux),
            inline_counts(self.windows),
            "inline-hook count drift between templates",
        )

    def test_registration_shapes_match_per_identity(self) -> None:
        # For each (event, matcher, if, script-stem) present on BOTH sides,
        # timeout + async + if must be identical.
        def by_identity(data):
            out = {}
            for e, m, h in _iter_registrations(data):
                out[_registration_identity(e, m, h)] = _registration_shape(h)
            return out

        lin = by_identity(self.linux)
        win = by_identity(self.windows)
        for ident in set(lin) & set(win):
            self.assertEqual(
                lin[ident],
                win[ident],
                f"shape drift for {ident}: linux={lin[ident]} "
                f"windows={win[ident]}",
            )

    def test_in3_kg3_hooks_present_both_os(self) -> None:
        # The IN-3 / KG-3 SessionStart additions must land on BOTH OSes.
        for stem in (
            "session-start-deferral-surface",
            "session-start-retrieval-health",
        ):
            self.assertIn(
                stem,
                LINUX.read_text(encoding="utf-8"),
                f"{stem} missing from linux template",
            )
            self.assertIn(
                stem,
                WINDOWS.read_text(encoding="utf-8"),
                f"{stem} missing from windows template",
            )

    def test_inline_kg_sync_deleted_both_os(self) -> None:
        # HK-3: the redundant inline knowledge-sync registration is deleted
        # from BOTH templates (post-file-edit owns that sync).
        for path in (LINUX, WINDOWS):
            body = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "sync_knowledge_graph.py",
                body,
                f"redundant inline KG-sync still registered in {path.name}",
            )


if __name__ == "__main__":
    unittest.main()
