# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.84 review F1 — Windows path-separator pins for the bundle install flow.

The v0.2.81 incident root-caused a Windows-only silent MASS-DELETE to a
separator mismatch: the orchestrator's manifest / ``_BundleFileOp.dest_rel``
values are built via ``str(Path(...))`` whose separator is ``\\`` on Windows,
but the comparison code matched a raw ``"knowledge/"`` prefix — so on Windows the
guard never fired. The fix routes every such comparison / join through the ONE
shared helper ``vco_lib.paths.to_posix_rel`` (A4 one-concern-one-home).

These pins lock the separator-normalization contract at the three sites the
review flagged, exercising WINDOWS-SHAPED (backslash) ``dest_rel`` values on a
POSIX test host (the values are plain strings; the code under test must treat
``\\`` and ``/`` uniformly regardless of the host OS):

  (a) ``to_posix_rel`` itself — backslash / mixed / already-posix / empty.
  (b) ``_file_action`` — a ``knowledge\\concepts\\x.md`` dest_rel classifies as
      ``preserve`` (the user-owned-KG guard holds on Windows-shaped keys, so a
      divergent KG node is NEVER adopted/overwritten — "never destroy user data").
  (c) ``_backup_bytes_for_adoption`` — a ``.claude\\hooks\\foo.sh`` dest_rel backs
      the current bytes up at the POSIX-mirrored path (component-wise join, no raw
      backslash leaking into the on-disk mirror tree).
"""
from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import paths as vco_paths  # noqa: E402
from vco_lib import project_init  # noqa: E402
from vco_lib.paths import to_posix_rel  # noqa: E402


# ---------------------------------------------------------------------------
# (a) to_posix_rel — the shared normalization primitive
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        # Windows-shaped backslash separators → POSIX.
        ("knowledge\\concepts\\x.md", "knowledge/concepts/x.md"),
        (".claude\\hooks\\foo.sh", ".claude/hooks/foo.sh"),
        # Mixed separators (a path assembled from both flavours).
        ("a\\b/c\\d.py", "a/b/c/d.py"),
        # Already-POSIX → returned unchanged.
        ("knowledge/concepts/x.md", "knowledge/concepts/x.md"),
        (".claude/scripts/kg-search", ".claude/scripts/kg-search"),
        # Empty string → empty string (no crash, no separator invented).
        ("", ""),
        # Single component, no separator at all.
        ("file.md", "file.md"),
        # Trailing / leading backslash preserved as a slash (pure swap, no strip).
        ("\\leading", "/leading"),
        ("trailing\\", "trailing/"),
    ],
)
def test_to_posix_rel_normalizes_separator(value: str, expected: str) -> None:
    assert to_posix_rel(value) == expected


def test_to_posix_rel_accepts_pathlike() -> None:
    """`to_posix_rel` accepts a Path (via ``str(rel)``). On a POSIX host a
    ``PurePosixPath`` round-trips unchanged; the point is it never raises on a
    non-str input."""
    assert to_posix_rel(PurePosixPath("a/b/c.md")) == "a/b/c.md"


def test_to_posix_rel_is_pure_no_filesystem() -> None:
    """The helper must not resolve/absolutize — it only swaps the separator. A
    non-existent relative path is returned verbatim (modulo separator)."""
    assert to_posix_rel("does\\not\\exist.xyz") == "does/not/exist.xyz"


def test_to_posix_rel_is_the_single_home() -> None:
    """One-home pin (A4): the helper lives in ``vco_lib.paths`` and is exactly the
    ``str(rel).replace('\\\\', '/')`` idiom the incident sites duplicated."""
    assert vco_paths.to_posix_rel("x\\y") == str("x\\y").replace("\\", "/")


# ---------------------------------------------------------------------------
# (b) _file_action — the knowledge\ user-owned guard on Windows-shaped keys
# ---------------------------------------------------------------------------
def test_file_action_knowledge_windows_dest_rel_preserved(tmp_path: Path) -> None:
    """A divergent ``knowledge\\...`` node (Windows-shaped dest_rel) must classify
    as ``preserve`` — the user-owned-KG guard is separator-normalized, so it holds
    for the Windows key shape. Without the ``to_posix_rel`` normalization the guard
    would MISS (raw ``startswith('knowledge/')`` is False for a ``\\`` key) and the
    node would be ADOPTED — destroying the user's KG content."""
    # Shipped source bytes (what the bundle would install).
    source = tmp_path / "shipped_node.md"
    source.write_bytes(b"# shipped KG node\nshipped body\n")

    # Installed target: EXISTS and DIFFERS from source → reaches the terminal
    # classification (not create/noop). A user-edited KG node.
    target = tmp_path / "installed_node.md"
    target.write_bytes(b"# user-edited KG node\nUSER content the user wrote\n")

    op = project_init._BundleFileOp(
        dest_rel="knowledge\\concepts\\x.md",  # WINDOWS-shaped key
        source_abs=source,
    )
    # A non-existent manifest entry for this path → prior_hash == "" → the file
    # would fall through to the terminal adopt/preserve decision. With
    # `orchestrator_root=None` the git-history heal is skipped, so we land on the
    # knowledge guard directly.
    action, _bytes = project_init._file_action(
        op, target,
        update_mode=True,
        manifest={"files": {}},
        orchestrator_root=None,
        project_root=tmp_path,
    )
    assert action == "preserve", (
        "a divergent Windows-shaped knowledge\\ node must be PRESERVED (never "
        f"adopted/overwritten — user data), got {action!r}"
    )


def test_file_action_knowledge_posix_dest_rel_also_preserved(tmp_path: Path) -> None:
    """Companion: the SAME guard holds for the POSIX key shape (parity — the
    normalization must not change the already-correct POSIX behavior)."""
    source = tmp_path / "shipped.md"
    source.write_bytes(b"shipped\n")
    target = tmp_path / "installed.md"
    target.write_bytes(b"user edited\n")
    op = project_init._BundleFileOp(
        dest_rel="knowledge/concepts/x.md",  # POSIX-shaped key
        source_abs=source,
    )
    action, _ = project_init._file_action(
        op, target,
        update_mode=True,
        manifest={"files": {}},
        orchestrator_root=None,
        project_root=tmp_path,
    )
    assert action == "preserve"


def test_file_action_non_knowledge_windows_dest_rel_adopts(tmp_path: Path) -> None:
    """Contrast pin: a NON-knowledge Windows-shaped codefile (e.g.
    ``.claude\\hooks\\foo.sh``) that diverges is ADOPTED (R2) — the guard is
    specific to ``knowledge\\`` and must not over-preserve other surfaces. This
    proves the separator normalization didn't accidentally widen the guard."""
    source = tmp_path / "shipped_hook.sh"
    source.write_bytes(b"#!/bin/sh\necho shipped\n")
    target = tmp_path / "installed_hook.sh"
    target.write_bytes(b"#!/bin/sh\necho stale-shipped-version\n")
    op = project_init._BundleFileOp(
        dest_rel=".claude\\hooks\\foo.sh",  # WINDOWS-shaped, NOT knowledge
        source_abs=source,
    )
    action, _ = project_init._file_action(
        op, target,
        update_mode=True,
        manifest={"files": {}},
        orchestrator_root=None,
        project_root=tmp_path,
    )
    assert action == "adopt", (
        f"a divergent non-knowledge codefile must be adopted (R2), got {action!r}"
    )


# ---------------------------------------------------------------------------
# (c) _backup_bytes_for_adoption — POSIX mirror path from a Windows dest_rel
# ---------------------------------------------------------------------------
def test_backup_bytes_for_adoption_windows_dest_rel_mirrors_posix(tmp_path: Path) -> None:
    """A ``.claude\\hooks\\foo.sh`` dest_rel must back the bytes up at the
    POSIX-MIRRORED path under the timestamp dir — the on-disk backup tree must
    carry ``/`` components, never a raw ``\\`` filename. Component-wise join keeps
    it path-length-aware and OS-uniform."""
    folder = tmp_path
    ts = "20260717T000000Z"
    current = b"#!/bin/sh\nthe user's current bytes\n"

    backup_rel = project_init._backup_bytes_for_adoption(
        folder, ".claude\\hooks\\foo.sh", ts, current,
    )

    # The returned relative path is POSIX-normalized.
    assert "\\" not in backup_rel, f"backup rel leaked a backslash: {backup_rel!r}"
    expected_rel = f".claude/backups/bundle-adoptions/{ts}/.claude/hooks/foo.sh"
    assert backup_rel == expected_rel, backup_rel

    # The file physically landed at the POSIX-mirrored absolute path with the
    # ORIGINAL bytes, and NO literal-backslash path component was created.
    backup_abs = folder / PurePosixPath(backup_rel)
    assert backup_abs.is_file(), f"backup file missing at {backup_abs}"
    assert backup_abs.read_bytes() == current
    # There must be NO file whose single name contains the raw backslash blob.
    blob = folder / ".claude" / "backups" / "bundle-adoptions" / ts / ".claude\\hooks\\foo.sh"
    assert not blob.exists(), (
        "a monolithic backslash filename was created — the join did not split "
        "the Windows-shaped dest_rel into POSIX components"
    )


def test_backup_bytes_for_adoption_posix_dest_rel(tmp_path: Path) -> None:
    """Parity: a POSIX-shaped dest_rel mirrors identically (normalization is a
    no-op for already-POSIX keys)."""
    ts = "20260717T010000Z"
    current = b"data\n"
    backup_rel = project_init._backup_bytes_for_adoption(
        tmp_path, ".claude/scripts/kg-search", ts, current,
    )
    assert backup_rel == f".claude/backups/bundle-adoptions/{ts}/.claude/scripts/kg-search"
    assert (tmp_path / PurePosixPath(backup_rel)).read_bytes() == current
