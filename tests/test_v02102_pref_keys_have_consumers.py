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

v0.2.102 W6-B2 (review MEDIUM-4): the presence scan was satisfied by a quoted
key in a comment or docstring, or by a constant nothing reads. It now counts
only a REAL read site: the literal in executable code (Python via ``ast``,
docstrings excluded; Rust/shell/config with comments stripped), and — when
the literal only initialises a named constant — that constant referenced again
somewhere in the backend code. Still a floor, not a wiring proof: every key
ALSO needs a value-mutation reader test (the
``test_v02102_code_embed_preferences_readers.py`` /
``test_v02102_summary_preferences_readers.py`` pattern).
"""
from __future__ import annotations

import ast
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


_RUST_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_HASH_COMMENT = re.compile(r"(?m)^\s*#[^\n]*$")


def _docstring_nodes(tree: ast.AST) -> set:
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant):
            out.add(id(body[0].value))
    return out


class _Backend:
    """Backend code with comments and docstrings removed: per Python file its
    AST, per other file its comment-stripped text."""

    def __init__(self) -> None:
        self.py: list[ast.AST] = []
        self.text: list[str] = []
        for path in _iter_backend_files():
            if _is_test_file(path):
                continue
            try:
                src = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if path.suffix == ".py":
                try:
                    self.py.append(ast.parse(src))
                    continue
                except SyntaxError:
                    pass
            self.text.append(self.strip(path.suffix, src))

    @staticmethod
    def strip(suffix: str, src: str) -> str:
        """Comment-stripped text of a non-Python backend file."""
        if suffix == ".rs":
            return _RUST_COMMENT.sub("", src)
        if suffix in {".sh", ".ps1", ".toml"}:
            return _HASH_COMMENT.sub("", src)
        return src

    def _py_names(self) -> "dict[str, int]":
        if not hasattr(self, "_names"):
            counts: dict[str, int] = {}
            for tree in self.py:
                for node in ast.walk(tree):
                    name = (node.id if isinstance(node, ast.Name)
                            else node.attr if isinstance(node, ast.Attribute)
                            else node.name if isinstance(node, ast.alias) else None)
                    if name:
                        counts[name] = counts.get(name, 0) + 1
            self._names = counts
        return self._names

    def has_real_read(self, key: str) -> bool:
        for tree in self.py:
            docs = _docstring_nodes(tree)
            parents = {id(c): n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and node.value == key) or id(node) in docs:
                    continue
                parent = parents.get(id(node))
                targets = (parent.targets if isinstance(parent, ast.Assign)
                           else [parent.target] if isinstance(parent, ast.AnnAssign) else None)
                if targets is None:
                    return True  # used inline (a call argument, a comparison, …)
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                # A constant counts once somebody reads it: the assignment is
                # one Name occurrence, a reader is a second.
                if any(self._py_names().get(n, 0) >= 2 for n in names):
                    return True
        for text in self.text:
            for m in re.finditer(r'"' + re.escape(key) + r'"', text):
                const = re.search(r"const\s+(\w+)\s*:\s*&str\s*=\s*$", text[:m.start()])
                if const is None:
                    return True
                if len(re.findall(r"\b" + const.group(1) + r"\b", "\n".join(self.text))) >= 2:
                    return True
        return False


def _pref_keys() -> dict[str, str]:
    """``{CONST_NAME: key_literal}`` from loaders.ts's APP_STATE_KEY_* exports."""
    src = LOADERS_TS.read_text(encoding="utf-8")
    found = dict(
        re.findall(r"export const (APP_STATE_KEY_\w+) = '([^']+)'", src)
    )
    assert found, "no APP_STATE_KEY_* exports parsed from loaders.ts — regex drift?"
    return found


def test_every_pref_key_has_a_backend_consumer():
    backend = _Backend()
    keys = _pref_keys()
    missing = [
        f"{const} = '{key}'"
        for const, key in sorted(keys.items())
        if key not in ALLOWLIST and not backend.has_real_read(key)
    ]
    assert not missing, (
        "Preferences app_state keys with NO non-frontend consumer (the F1 "
        "placebo class — wire a reader or record the supersession in "
        "ALLOWLIST):\n  " + "\n  ".join(missing)
    )


def test_allowlist_entries_are_not_stale():
    backend = _Backend()
    keys = _pref_keys()
    stale = [
        key for key in ALLOWLIST if key in keys and backend.has_real_read(key)
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


def test_a_comment_docstring_or_unread_constant_is_not_a_reader():
    """The tightened census rejects exactly the shapes the review named."""
    fake = _Backend.__new__(_Backend)
    fake.py = [ast.parse(
        '"""Mentions "k_doc" in a docstring."""\n'
        "# reads 'k_comment' here\n"
        'UNREAD = "k_unread"\n'
        'USED = "k_used"\n'
        "def f(get):\n"
        '    """also "k_doc"."""\n'
        '    return get(USED), get("k_inline")\n'
    )]
    fake.text = [_Backend.strip(".rs", '// "k_rs_comment"\nconst DEAD: &str = "k_rs_dead";\n'
                 'const LIVE: &str = "k_rs_live";\nfn g() { read(LIVE); read("k_rs_inline"); }\n')]
    for key in ("k_doc", "k_comment", "k_unread", "k_rs_comment", "k_rs_dead"):
        assert not fake.has_real_read(key), key
    for key in ("k_used", "k_inline", "k_rs_live", "k_rs_inline"):
        assert fake.has_real_read(key), key
