# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""X-1 (v0.2.73): single-writer lint for KG-binding heal.

Before v0.2.73 the ~790-line ``install.py::_self_heal_kg_bindings_on_update``
carried five layers of drift-repair inline, each UPDATE-ing
``project_kg_bindings`` / ``kg_collection_access`` in the install mega-file.
X-1 extracted that repair into ONE home — ``vco_lib.kg_binding_heal`` — the
single Python writer for KG-binding heal SQL.

The single-writer contract this test enforces
---------------------------------------------
The launcher (Rust ``project_state`` / ``projects_v2`` commands) is the
authoritative *creator* of ``project_kg_bindings`` rows. On the Python side,
only ``vco_lib.kg_binding_heal`` may *heal* (UPDATE / DELETE / adopt) those
rows or the sibling ``kg_collection_access`` rows.

Why a lint, not a runtime guard
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Runtime guards can't see a NEW Python module that starts writing the tables.
A grep-style lint catches the regression at CI time: the moment a second
Python writer of these columns appears, this test fails and points the author
at the single-writer home.

Detection grammar
~~~~~~~~~~~~~~~~~
We scan ``*.py`` under the repo (excluding ``tests/`` — test fixtures build
synthetic launcher.db rows on purpose) for SQL statements that MUTATE the two
tables: ``UPDATE <table>``, ``DELETE FROM <table>``, ``INSERT ... INTO
<table>``. SELECTs are read-only (config_projection legitimately reads
bindings) and are NOT flagged.

Allowlisted writers
~~~~~~~~~~~~~~~~~~~~
* ``vco_lib/kg_binding_heal.py`` — the single legal writer.
* ``install.py`` — only via the extracted-impl DELEGATION; install.py itself
  must contain NO direct binding-mutation SQL any more. The lint asserts the
  install.py hit-count is ZERO (the extraction removed every inline write).

Run: pytest tests/test_kg_binding_heal_single_writer.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The heal-owned tables. Mutations to these outside the single writer are the
# regression we guard against.
_HEAL_TABLES = ("project_kg_bindings", "kg_collection_access")

# SQL mutation verbs targeting a heal table. Matched case-insensitively;
# whitespace-flexible so multi-line f-string SQL still hits.
_MUTATION_RE = re.compile(
    r"(?is)\b(?:UPDATE\s+{tbl}\b"
    r"|DELETE\s+FROM\s+{tbl}\b"
    r"|INSERT(?:\s+OR\s+\w+)?\s+INTO\s+{tbl}\b)"
)


def _build_patterns() -> list[re.Pattern]:
    return [
        re.compile(_MUTATION_RE.pattern.replace("{tbl}", re.escape(tbl)), re.IGNORECASE | re.DOTALL)
        for tbl in _HEAL_TABLES
    ]


# The single legal writer.
_LEGAL_WRITER = REPO_ROOT / "vco_lib" / "kg_binding_heal.py"

# Directories where any hit is acceptable (test fixtures, docs, KG prose).
_SKIP_DIRS = {
    ".git", ".venv", "node_modules", "target", "__pycache__",
    ".pytest_cache", "dist", "build", ".cargo", ".rustup",
    ".claude", ".wt",
    "tests",       # test fixtures hand-roll launcher.db rows intentionally
    "knowledge", "docs", "internal",
}


def _iter_python_files():
    import os
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if name.endswith(".py"):
                yield Path(root) / name


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def test_only_kg_binding_heal_mutates_binding_tables() -> None:
    """No Python module outside ``vco_lib.kg_binding_heal`` may UPDATE /
    DELETE / INSERT the KG-binding tables."""
    patterns = _build_patterns()
    violations: list[str] = []

    for path in _iter_python_files():
        if path.resolve() == _LEGAL_WRITER.resolve():
            continue
        content = _read(path)
        if not content:
            continue
        for pat in patterns:
            for m in pat.finditer(content):
                # Compute the line number of the hit for a useful message.
                lineno = content.count("\n", 0, m.start()) + 1
                snippet = " ".join(m.group(0).split())
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}: mutates a "
                    f"KG-binding table outside the single writer — "
                    f"`{snippet[:80]}`."
                )

    if violations:
        msg = "\n".join(violations)
        raise AssertionError(
            f"{len(violations)} KG-binding single-writer violation(s):\n{msg}\n\n"
            f"Route binding heal through vco_lib.kg_binding_heal "
            f"(self_heal_kg_bindings). The launcher (Rust) is the "
            f"authoritative row CREATOR; Python only heals via that module.\n"
        )


def test_install_py_has_no_inline_binding_mutation() -> None:
    """install.py must delegate to the extracted impl — it retains NO inline
    binding-mutation SQL after the X-1 extraction (regression canary: if a new
    inline write creeps back into the mega-file, catch it here explicitly)."""
    patterns = _build_patterns()
    content = _read(REPO_ROOT / "install.py")
    assert content, "install.py unreadable"
    hits: list[str] = []
    for pat in patterns:
        for m in pat.finditer(content):
            lineno = content.count("\n", 0, m.start()) + 1
            hits.append(f"install.py:{lineno}: `{' '.join(m.group(0).split())[:80]}`")
    assert not hits, (
        "install.py contains inline KG-binding mutation SQL after the X-1 "
        "extraction. All heal writes belong in vco_lib.kg_binding_heal:\n"
        + "\n".join(hits)
    )


def test_single_writer_module_exposes_public_entry() -> None:
    """Sanity: the single-writer home exposes ``self_heal_kg_bindings``."""
    from vco_lib import kg_binding_heal

    assert hasattr(kg_binding_heal, "self_heal_kg_bindings")
    # And the historical helper symbols install.py re-exports.
    for sym in (
        "_rebind_collection_names_to_on_disk_casing",
        "_prefix_adopt_kg_bindings_pass",
        "_count_weaviate_class_objects",
        "_KG_ACCESS_RANK",
        "_KG_BINDING_PREFIX_ADOPT_SUFFIXES",
    ):
        assert hasattr(kg_binding_heal, sym), f"missing {sym}"
