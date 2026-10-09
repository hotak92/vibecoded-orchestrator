# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 lane W6-B — EVERY Preferences app_state key has a consumer.

The W6-R audit's F2: ``tests/test_v0291_pref_keys_have_consumers.py`` pins
the consumer chain for only four keys (window prefs + log level), so the
v0.2.23→v0.2.102 placebo class (keys written by the launcher's Preferences
page and read by NOTHING — the whole Code Graph Embeddings section, plus
the two KG-summary keys W5-D had to wire retroactively) sailed through
every release.

This module closes the CLASS: it enumerates every ``APP_STATE_KEY_*``
exported from ``launcher/src/lib/preferences/loaders.ts`` (the one home of
the keys the Preferences page writes) and asserts each key literal appears
in at least one NON-frontend, NON-test file — Rust (``launcher/src-tauri``),
Python (``vco_lib`` / ``claude_mcp_servers`` / ``templates`` / top level),
shell or config. A key with no such reader needs an explicit entry in
``ALLOWLIST`` below naming its reader or supersession, so the exemption is
a recorded decision, not an oversight.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOADERS_TS = REPO_ROOT / "launcher" / "src" / "lib" / "preferences" / "loaders.ts"

#: Backend trees + top-level files searched for a reader of each key.
#: Deliberately EXCLUDES the launcher frontend (``launcher/src/lib``,
#: ``launcher/src/routes``) and every test file — a consumer must be
#: non-frontend code that does something with the value.
BACKEND_ROOTS = [
    REPO_ROOT / "vco_lib",
    REPO_ROOT / "claude_mcp_servers",
    REPO_ROOT / "templates",
    REPO_ROOT / "launcher" / "src-tauri" / "src",
    REPO_ROOT / "scripts",
    REPO_ROOT / "tools",
    REPO_ROOT / "VCThelpers",
]
BACKEND_FILES = [REPO_ROOT / "install.py"]

#: File-name suffixes that never count as a consumer.
_TEST_MARKERS = ("test_", "_test", ".test.", "tests/")

#: Keys whose consumer lives OUTSIDE the backend trees, with the recorded
#: reader/supersession. An entry whose key later gains a backend reader is
#: reported as stale (second test below) so the allowlist cannot rot.
ALLOWLIST: dict[str, str] = {
    # (none today — every enumerated key has a backend reader)
}


def _iter_backend_files():
    for root in BACKEND_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in {
                ".py", ".rs", ".toml", ".sh", ".ps1", ".json",
            }:
                continue
            yield path
    for path in BACKEND_FILES:
        if path.is_file():
            yield path


def _is_test_file(path: Path) -> bool:
    name = str(path).replace(str(REPO_ROOT) + "/", "")
    return any(marker in name for marker in _TEST_MARKERS)


def _load_backend_text() -> str:
    chunks: list[str] = []
    for path in _iter_backend_files():
        if _is_test_file(path):
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(chunks)


def _pref_keys() -> dict[str, str]:
    """``{CONST_NAME: key_literal}`` from loaders.ts's APP_STATE_KEY_* exports."""
    src = LOADERS_TS.read_text(encoding="utf-8")
    found = dict(
        re.findall(r"export const (APP_STATE_KEY_\w+) = '([^']+)'", src)
    )
    assert found, "no APP_STATE_KEY_* exports parsed from loaders.ts — regex drift?"
    return found


def test_every_pref_key_has_a_backend_consumer():
    backend = _load_backend_text()
    keys = _pref_keys()
    missing = [
        f"{const} = '{key}'"
        for const, key in sorted(keys.items())
        if key not in ALLOWLIST and f'"{key}"' not in backend
    ]
    assert not missing, (
        "Preferences app_state keys with NO non-frontend consumer (the F1 "
        "placebo class — wire a reader or record the supersession in "
        "ALLOWLIST):\n  " + "\n  ".join(missing)
    )


def test_allowlist_entries_are_not_stale():
    backend = _load_backend_text()
    keys = _pref_keys()
    stale = [
        key for key in ALLOWLIST if key in keys and f'"{key}"' in backend
    ]
    assert not stale, (
        "ALLOWLIST entries whose key now HAS a backend reader (remove the "
        "exemption so it cannot mask a future regression): " + ", ".join(stale)
    )


def test_allowlist_only_names_real_keys():
    keys = _pref_keys()
    unknown = [key for key in ALLOWLIST if key not in keys]
    assert not unknown, (
        "ALLOWLIST names keys loaders.ts no longer exports: " + ", ".join(unknown)
    )
